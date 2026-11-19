"""ScreenCaptureKit audio output, independent of the desktop video stream."""
from array import array
import threading
import time

from desktop_audio import CHANNELS, SAMPLE_RATE
from macos_capture import CM, NSObject, SC, MacCapture, _DELEGATE, _OUTPUT, dispatch, objc


LINEAR_PCM = int.from_bytes(b'lpcm', 'big')
FLOAT = 1
BIG_ENDIAN = 1 << 1
NON_INTERLEAVED = 1 << 5


class _AudioOutput(NSObject, protocols=[_OUTPUT, _DELEGATE]):
    def stream_didOutputSampleBuffer_ofType_(self, stream, sample, kind):
        owner = self.owner
        if owner is None or owner.channel.stopped.is_set() or kind != SC.SCStreamOutputTypeAudio:
            return
        try:
            with objc.autorelease_pool():
                if CM.CMSampleBufferIsValid(sample):
                    owner.submit(sample)
        except Exception as exc:
            owner.channel.stop(f'ScreenCaptureKit 音频读取失败：{exc}')

    def stream_didStopWithError_(self, stream, error):
        owner = self.owner
        if owner is not None:
            try:
                with objc.autorelease_pool():
                    owner.channel.stop(f'ScreenCaptureKit 音频已停止：{error}')
            except Exception:
                pass  # Native callbacks must not propagate Python exceptions.


class MacAudio:
    def __init__(self, channel):
        self.channel = channel
        self.stream = self.output = self.queue = None
        self.closed = threading.Event()

    def __enter__(self):
        try:
            with objc.autorelease_pool():
                config = SC.SCStreamConfiguration.alloc().init()
                config.setCapturesAudio_(True)
                config.setExcludesCurrentProcessAudio_(True)
                config.setSampleRate_(SAMPLE_RATE)
                config.setChannelCount_(CHANNELS)
                # The stream registers only an audio output. Keep any internal
                # display updates small and infrequent.
                config.setWidth_(64)
                config.setHeight_(64)
                config.setMinimumFrameInterval_(CM.CMTimeMake(1, 1))
                displays = MacCapture._content().displays()
                if not displays or self.channel.stopped.is_set():
                    raise OSError('无法启动 Mac 系统音频采集')
                content = SC.SCContentFilter.alloc().initWithDisplay_excludingWindows_(displays[0], [])
                self.output = _AudioOutput.alloc().init()
                self.output.owner = self
                self.queue = dispatch.dispatch_queue_create(b'org.shareinput.audio', None)
                self.stream = SC.SCStream.alloc().initWithFilter_configuration_delegate_(
                    content, config, self.output)
                success, error = self.stream.addStreamOutput_type_sampleHandlerQueue_error_(
                    self.output, SC.SCStreamOutputTypeAudio, self.queue, None)
                if not success:
                    raise OSError(f'ScreenCaptureKit 添加音频输出失败：{error}')
                ready = threading.Event()
                result = [None]
                stream = self.stream

                def started(error):
                    try:
                        with objc.autorelease_pool():
                            result[0] = str(error) if error is not None else None
                            if self.closed.is_set() and error is None:
                                stream.stopCaptureWithCompletionHandler_(lambda unused: None)
                    except Exception as exc:
                        result[0] = str(exc)
                    finally:
                        ready.set()

                stream.startCaptureWithCompletionHandler_(started)
                if not ready.wait(3):
                    raise OSError('ScreenCaptureKit 启动音频采集超时')
                if result[0] is not None:
                    raise OSError(f'ScreenCaptureKit 启动音频采集失败：{result[0]}')
        except Exception:
            self.close()
            raise
        return self

    def submit(self, sample):
        stamp = time.monotonic_ns()
        frames = CM.CMSampleBufferGetNumSamples(sample)
        if not frames:
            return
        description = CM.CMSampleBufferGetFormatDescription(sample)
        fmt = CM.CMAudioFormatDescriptionGetStreamBasicDescription(description)
        # Native-endian Float32 LPCM, usually stored as two contiguous planes.
        if fmt is None:
            raise ValueError('系统采集没有返回 PCM 格式')
        rate, format_id, flags, _, _, _, channels, bits, _ = fmt
        if (format_id != LINEAR_PCM or rate != SAMPLE_RATE or channels != CHANNELS
                or bits != 32 or flags & (FLOAT | BIG_ENDIAN) != FLOAT):
            raise ValueError('系统采集返回了不支持的音频格式')
        data = CM.CMSampleBufferGetDataBuffer(sample)
        size = frames * CHANNELS * 4
        if data is None or CM.CMBlockBufferGetDataLength(data) != size:
            raise ValueError('系统采集返回了无效的 PCM 缓冲')
        status, raw = CM.CMBlockBufferCopyDataBytes(data, 0, size, None)
        if status:
            raise OSError(f'复制系统音频缓冲失败（{status}）')
        floats = memoryview(raw).cast('f')
        if flags & NON_INTERLEAVED:
            samples = (value for pair in zip(floats[:frames], floats[frames:]) for value in pair)
        else:
            samples = floats
        pcm = array('h', (int(max(-32768, min(32767, value * 32768))) for value in samples))
        self.channel.submit(pcm.tobytes(), stamp)

    def close(self):
        self.closed.set()
        with objc.autorelease_pool():
            if self.output is not None:
                self.output.owner = None
            if self.stream is not None:
                try:
                    self.stream.stopCaptureWithCompletionHandler_(lambda unused: None)
                except Exception:
                    pass
            self.stream = self.output = self.queue = None

    def __exit__(self, *args):
        self.close()
