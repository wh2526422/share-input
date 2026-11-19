"""Play remote PCM using a Qt event loop dedicated to audio."""
import threading
import time

from PySide6.QtCore import QCoreApplication, QEventLoop, QThread, QTimer, Qt, Slot
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

try:
    # Recent PySide6 versions return QtAudio enums, distinct from QAudio aliases.
    from PySide6.QtMultimedia import QtAudio
except ImportError:
    from PySide6.QtMultimedia import QAudio as QtAudio

from desktop_audio import CHANNELS, MAX_AGE_NS, PACKET_BYTES, PREBUFFER_PACKETS, SAMPLE_RATE
from qt_runtime import connect_signal


class AudioPlayer(QThread):
    def __init__(self, channel, volume):
        app = QCoreApplication.instance()
        super().__init__(app)
        self.channel = channel
        self.volume = volume
        self.playing = threading.Event()
        self.finished.connect(self.deleteLater)
        app.aboutToQuit.connect(self._shutdown)

    @Slot()
    def _shutdown(self):
        self.channel.stop()
        self.wait()

    def run(self):
        try:
            self._play()
        except Exception as exc:
            self.channel.stop(f'远端声音播放失败：{exc}')

    def _play(self):
        if self.channel.stopped.is_set():
            return
        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(CHANNELS)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull() or not device.isFormatSupported(fmt):
            raise OSError('本地音频输出不可用或不支持 48 kHz 双声道')
        sink = QAudioSink(device, fmt)
        sink.setBufferSize(2 * PACKET_BYTES)
        volume = self.volume
        sink.setVolume(volume)
        output = sink.start()
        error = sink.error()
        if output is None or error != QtAudio.Error.NoError:
            raise OSError(f'无法打开本地音频输出（{device.description()}，{error.name}）')
        buffering = True
        loop = QEventLoop()
        timer = QTimer()
        timer.setTimerType(Qt.TimerType.PreciseTimer)

        def write_audio():
            nonlocal buffering, volume
            if self.channel.stopped.is_set():
                loop.quit()
                return
            try:
                error = sink.error()
                if error not in (QtAudio.Error.NoError, QtAudio.Error.UnderrunError):
                    raise OSError(f'本地音频输出中断（{device.description()}，{error.name}）')
                if volume != self.volume:
                    volume = self.volume
                    sink.setVolume(volume)
                for _ in range(2):
                    if sink.bytesFree() < PACKET_BYTES:
                        return
                    packet = self.channel.take(PREBUFFER_PACKETS if buffering else 1)
                    if packet is None:
                        buffering = sink.state() == QtAudio.State.IdleState
                        return
                    arrived, pcm = packet
                    if time.monotonic_ns() - arrived > MAX_AGE_NS:
                        continue
                    if output.write(pcm) != len(pcm):
                        raise OSError('写入本地音频缓冲失败')
                    buffering = False
                    self.playing.set()
            except Exception as exc:
                self.channel.stop(f'远端声音播放失败：{exc}')
                loop.quit()

        connect_signal(timer.timeout, write_audio)
        timer.start(10)
        try:
            loop.exec()
        finally:
            timer.stop()
            sink.reset()
