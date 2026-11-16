"""ScreenCaptureKit 连续读屏；原生缩放，消费时才转换最新图像。"""
import logging
import platform
import sys
import threading
import time

if sys.platform != 'darwin':
    raise ImportError('ScreenCaptureKit 仅用于 macOS')
version = platform.mac_ver()[0].split('.')
if not version[0].isdigit() or tuple(int(part) for part in version[:2]) < (12, 3):
    raise ImportError('ScreenCaptureKit 需要 macOS 12.3 或更新版本')

try:
    import objc
    try:
        import dispatch
    except ImportError:
        import libdispatch as dispatch
    import CoreMedia as CM
    import Quartz as Q
    import ScreenCaptureKit as SC
    from Foundation import NSObject, NSThread
    from PIL import Image
    _OUTPUT = objc.protocolNamed('SCStreamOutput')
    _DELEGATE = objc.protocolNamed('SCStreamDelegate')
except Exception as exc:
    raise ImportError('ScreenCaptureKit 框架不可用；请使用启动程序的 Python 执行 '
                      f'python -m pip install -r requirements.txt。原始错误：{exc}') from exc


logger = logging.getLogger(__name__)


class _ScreenOutput(NSObject, protocols=[_OUTPUT, _DELEGATE]):
    # Selector 签名由 PyObjC 的 SCStreamOutput 协议元数据提供；sample 是 CF 指针。
    def stream_didOutputSampleBuffer_ofType_(self, stream, sample, kind):
        owner = self.owner
        if owner is None or kind != SC.SCStreamOutputTypeScreen:
            return
        try:
            with objc.autorelease_pool():
                if owner._closed or not CM.CMSampleBufferIsValid(sample):
                    return
                attachments = CM.CMSampleBufferGetSampleAttachmentsArray(sample, False)
                if (not attachments or attachments[0].get(SC.SCStreamFrameInfoStatus)
                        != SC.SCFrameStatusComplete):
                    return
                with owner._condition:
                    if not owner._closed:
                        # PyObjC 按 CF 元数据维护强引用，持有 sample 直到消费，
                        # 让 SCK 的原生缓冲租约与画面消费周期保持一致。
                        # 回调不转换即将被覆盖的帧，发送线程取走后才锁定、转色。
                        owner._latest = sample
                        owner._condition.notify_all()
        except Exception as exc:
            # 不能让 Python 异常跨越原生回调；由 grab() 通知发送线程回退。
            owner._fail(exc)

    def stream_didStopWithError_(self, stream, error):
        owner = self.owner
        if owner is not None:
            try:
                with objc.autorelease_pool():
                    owner._fail(error or 'ScreenCaptureKit 已停止')
            except Exception:
                pass  # 原生回调不能传播 Python 异常。


class MacCapture:
    def __init__(self, display, resolution, fps):
        self.display = display
        self.resolution = tuple(resolution)
        self.size = None
        self._fps = fps
        self._condition = threading.Condition()
        self._latest = None
        self._error = None
        self._closed = False
        self._stream = self._output = self._configuration = None
        self._queue = None
        self._output_added = False
        self._configuring = False
        self._configuration_started = 0
        self._started_at = None
        self._has_frame = False

    def _fail(self, error):
        try:
            text = str(error)
        except Exception:
            text = '原生采集错误'
        with self._condition:
            if not self._closed:
                self._error = OSError(f'ScreenCaptureKit：{text}')
                self._condition.notify_all()

    @staticmethod
    def _content():
        ready = threading.Event()
        result = [None, None]
        def completed(content, error):
            try:
                with objc.autorelease_pool():
                    result[:] = [content, error]
            except Exception as exc:
                result[:] = [None, OSError(str(exc))]
            finally:
                ready.set()
        SC.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            False, True, completed)
        if not ready.wait(5):
            raise OSError('ScreenCaptureKit 获取显示器超时')
        if result[1] is not None or result[0] is None:
            raise OSError(f'ScreenCaptureKit 获取显示器失败：{result[1]}')
        return result[0]

    @staticmethod
    def _bounds(identifier):
        bounds = Q.CGDisplayBounds(identifier)
        width, height = bounds.size.width, bounds.size.height
        # 与 MSS Darwin 的显示器列表保持一致，旋转屏幕还会交换逻辑宽高。
        if Q.CGDisplayRotation(identifier) in (90.0, -90.0):
            width, height = height, width
        return round(bounds.origin.x), round(bounds.origin.y), round(width), round(height)

    def _select_display(self, content):
        identifier = self.display.get('display_id', self.display.get('id'))
        expected = (tuple(self.display[key] for key in ('left', 'top', 'width', 'height'))
                    if identifier is None else None)
        for display in content.displays():
            if identifier is not None:
                if int(display.displayID()) == identifier:
                    return display
                continue
            # CGDisplayBounds 与 MSS 使用相同的全局逻辑坐标，包含负坐标显示器。
            actual = self._bounds(display.displayID())
            if actual == expected:
                return display
        raise OSError('ScreenCaptureKit 未找到所选显示器')

    def _make_configuration(self, fps):
        config = SC.SCStreamConfiguration.alloc().init()
        config.setWidth_(self.size[0])
        config.setHeight_(self.size[1])
        config.setPixelFormat_(Q.kCVPixelFormatType_32BGRA)
        config.setMinimumFrameInterval_(CM.CMTimeMake(1, fps))
        # 使用三个原生缓冲；Python 侧只有一个可覆盖的最新帧。
        config.setQueueDepth_(3)
        config.setShowsCursor_(False)
        config.setScalesToFit_(True)
        if hasattr(config, 'setCapturesAudio_'):
            config.setCapturesAudio_(False)
        return config

    def __enter__(self):
        if NSThread.isMainThread():
            raise OSError('ScreenCaptureKit 初始化必须在采集线程执行')
        try:
            with objc.autorelease_pool():
                screen = self._select_display(self._content())
                mode = Q.CGDisplayCopyDisplayMode(screen.displayID())
                if mode is None:
                    raise OSError('无法读取 Mac 显示器的物理像素尺寸')
                width, height = Q.CGDisplayModeGetPixelWidth(mode), Q.CGDisplayModeGetPixelHeight(mode)
                if width <= 0 or height <= 0:
                    raise OSError('Mac 显示器的物理像素尺寸无效')
                _, _, logical_width, logical_height = self._bounds(screen.displayID())
                if (width > height) != (logical_width > logical_height):
                    width, height = height, width
                scale = min(1, self.resolution[0] / width, self.resolution[1] / height)
                self.size = (max(1, round(width * scale)), max(1, round(height * scale)))
                self._configuration = self._make_configuration(self._fps)
                content_filter = SC.SCContentFilter.alloc().initWithDisplay_excludingWindows_(screen, [])
                self._output = _ScreenOutput.alloc().init()
                self._output.owner = self
                make_qos = getattr(dispatch, 'dispatch_queue_attr_make_with_qos_class', None)
                attributes = (make_qos(None, 0x21, 0) if make_qos is not None else None)
                # QOS_CLASS_USER_INTERACTIVE：优先及时交付画面，队列仍为串行。
                self._queue = dispatch.dispatch_queue_create(b'org.shareinput.capture', attributes)
                self._stream = SC.SCStream.alloc().initWithFilter_configuration_delegate_(
                    content_filter, self._configuration, self._output)
                success, error = self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
                    self._output, SC.SCStreamOutputTypeScreen, self._queue, None)
                if not success:
                    raise OSError(f'ScreenCaptureKit 添加画面输出失败：{error}')
                self._output_added = True
                ready = threading.Event()
                result = [None]
                stream = self._stream
                def started(error):
                    try:
                        with objc.autorelease_pool():
                            result[0] = error
                            # 初始化超时后的迟到成功，也要停止，不能留下后台采集流。
                            if self._closed and error is None:
                                stream.stopCaptureWithCompletionHandler_(lambda unused: None)
                    except Exception as exc:
                        result[0] = OSError(str(exc))
                    finally:
                        ready.set()
                stream.startCaptureWithCompletionHandler_(started)
                if not ready.wait(3):
                    raise OSError('ScreenCaptureKit 启动采集超时')
                if result[0] is not None:
                    raise OSError(f'ScreenCaptureKit 启动采集失败：{result[0]}')
                self._started_at = time.monotonic()
            return self
        except Exception as exc:
            self.close()
            if isinstance(exc, OSError):
                raise
            raise OSError(f'ScreenCaptureKit 初始化失败：{exc}') from exc

    def update_fps(self, fps):
        if type(fps) is not int or not 0 < fps <= 240:
            raise OSError('ScreenCaptureKit 帧率无效')
        with self._condition:
            if self._closed:
                raise OSError('ScreenCaptureKit 已关闭')
            if self._fps == fps:
                return
            self._fps = fps
            if self._configuring:
                return  # 只覆盖最新配置，不排队保存中间切换。
            self._configuring = True
        self._apply_fps(fps)

    def _apply_fps(self, fps):
        try:
            with objc.autorelease_pool():
                configuration = self._make_configuration(fps)
                self._configuration_started = time.monotonic()
                def completed(error):
                    try:
                        with objc.autorelease_pool():
                            if error is not None:
                                self._fail(error)
                            with self._condition:
                                self._configuring = False
                                again = not self._closed and self._error is None and self._fps != fps
                                next_fps = self._fps
                                if again:
                                    self._configuring = True
                            if again:
                                self._apply_fps(next_fps)
                    except Exception as exc:
                        self._fail(exc)
                self._configuration = configuration
                self._stream.updateConfiguration_completionHandler_(configuration, completed)
        except Exception as exc:
            with self._condition:
                self._configuring = False
            self._fail(exc)

    def grab(self, raw=False):
        with self._condition:
            if self._latest is None and self._error is None and not self._closed:
                self._condition.wait(0.01)
            if self._error is not None:
                raise self._error
            now = time.monotonic()
            if self._configuring and now - self._configuration_started > 2:
                raise OSError('ScreenCaptureKit 更新帧率超时')
            if (not self._has_frame and self._latest is None and self._started_at is not None
                    and now - self._started_at > 2):
                raise OSError('ScreenCaptureKit 首帧超时')
            sample, self._latest = self._latest, None
        if sample is None:
            return None
        with objc.autorelease_pool():
            pixel = None
            try:
                if not CM.CMSampleBufferIsValid(sample):
                    return None
                pixel = CM.CMSampleBufferGetImageBuffer(sample)
                if pixel is None:
                    return None
                if Q.CVPixelBufferGetPixelFormatType(pixel) != Q.kCVPixelFormatType_32BGRA:
                    raise OSError('ScreenCaptureKit 返回了非 BGRA 图像')
                width, height = Q.CVPixelBufferGetWidth(pixel), Q.CVPixelBufferGetHeight(pixel)
                if not (0 < width <= self.resolution[0] and 0 < height <= self.resolution[1]):
                    raise OSError('ScreenCaptureKit 图像尺寸超过请求上限')
                flags = Q.kCVPixelBufferLock_ReadOnly
                status = Q.CVPixelBufferLockBaseAddress(pixel, flags)
                if status:
                    raise OSError(f'锁定 ScreenCaptureKit 图像失败：{status}')
                try:
                    stride = Q.CVPixelBufferGetBytesPerRow(pixel)
                    address = Q.CVPixelBufferGetBaseAddress(pixel)
                    if address is None or stride < width * 4:
                        raise OSError('ScreenCaptureKit 图像缓冲无效')
                    pixels = address.as_buffer(stride * height)
                    if raw:
                        from jpeg_codec import RawFrame
                        image = RawFrame(bytes(pixels), (width, height), stride, 'BGRX')
                    else:
                        # PIL 拥有独立 RGB 像素，解锁后不引用原生缓冲。
                        image = Image.frombytes('RGB', (width, height), pixels, 'raw', 'BGRX', stride)
                finally:
                    pixels = address = None
                    Q.CVPixelBufferUnlockBaseAddress(pixel, flags)
                self.size = image.size
                self._has_frame = True
                return image
            except Exception as exc:
                if isinstance(exc, OSError):
                    raise
                raise OSError(f'ScreenCaptureKit 转换画面失败：{exc}') from exc
            finally:
                sample = pixel = None  # 在池退出前归还 sample 及其原生像素缓冲。

    def close(self):
        with objc.autorelease_pool():
            with self._condition:
                if self._closed:
                    return
                self._closed = True
                self._latest = None
                self._condition.notify_all()
            stream, output = self._stream, self._output
            if output is not None:
                output.owner = None
            try:
                if stream is not None:
                    stopped = threading.Event()
                    stream.stopCaptureWithCompletionHandler_(lambda error: stopped.set())
                    if not NSThread.isMainThread():
                        stopped.wait(2)
                    if self._output_added:
                        stream.removeStreamOutput_type_error_(output, SC.SCStreamOutputTypeScreen, None)
            except Exception as exc:
                logger.debug('停止 ScreenCaptureKit：%s', exc)
            finally:
                self._stream = self._output = self._configuration = None
                self._queue = None
                stream = output = None

    def __exit__(self, *args):
        self.close()
