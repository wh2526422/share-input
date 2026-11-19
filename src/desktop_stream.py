"""远程桌面的独立 TCP 通道，画面和输入分别在两个方向传输。"""
from contextlib import closing, ExitStack
import io
import json
import logging
import os
import queue
import socket
import struct
import sys
import threading
import time

from protocol import Protocol
from desktop_audio import DesktopAudio
from desktop_frames import LatestFrames


RESOLUTION_PROFILES = {
    '720p': (1280, 720),
    '1080p': (1920, 1080),
    '1440p': (2560, 1440),
    '4K': (3840, 2160),
}
DEFAULT_RESOLUTION = '720p'
FRAME_SIZE = RESOLUTION_PROFILES[DEFAULT_RESOLUTION]
FPS_PROFILES = (15, 30, 45, 60)
DEFAULT_FPS = FPS_PROFILES[0]
COORD_MAX = 65535
HEADER = struct.Struct('!I')
RESOLUTION_ACK = b'R:'
FPS_ACK = b'F:'
DISPLAY_ACK = b'D:'
PONG = b'P:'
VIDEO_FRAME = b'V:'
SEQUENCE = struct.Struct('!I')
NETWORK_PROBE = struct.Struct('!Q')
MAX_INFLIGHT_FRAMES = 2


def close_socket(connection):
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    connection.close()


def send_packet(connection, data, prefix=b''):
    # One write also for JPEGs: avoid a separate four-byte TCP segment and
    # a scheduling gap between the length header and its image payload.
    connection.sendall(b''.join((HEADER.pack(len(prefix) + len(data)), prefix, data)))


def receive_packet(connection, limit):
    header = Protocol._receive_exact(connection, HEADER.size)
    if header is None:
        raise EOFError
    size, = HEADER.unpack(header)
    if not 0 < size <= limit:
        raise ValueError("投屏数据长度无效")
    data = Protocol._receive_exact(connection, size)
    if data is None:
        raise EOFError
    return data


def list_displays():
    if sys.platform.startswith('linux') and (
            os.environ.get('XDG_SESSION_TYPE') == 'wayland' or os.environ.get('WAYLAND_DISPLAY')):
        raise RuntimeError("此版本的 Linux 屏幕共享需要 X11 会话")
    from mss import MSS
    with MSS() as capture:
        return [
            {key: monitor[key] if isinstance(monitor, dict) else getattr(monitor, key)
             for key in ('left', 'top', 'width', 'height')}
            for monitor in capture.monitors[1:]
        ]


def check_screen_permission():
    if sys.platform == 'darwin':
        import Quartz
        import platform
        from ApplicationServices import AXIsProcessTrusted
        permission_app = ('ShareInput.app' if '__compiled__' in globals() or getattr(sys, 'frozen', False)
                          else '启动 share-input 的应用')
        preflight = getattr(Quartz, 'CGPreflightScreenCaptureAccess', None)
        if preflight and not preflight() and not Quartz.CGRequestScreenCaptureAccess():
            raise RuntimeError(f'请在 Mac“隐私与安全性 → 屏幕录制”为 {permission_app} 授权，'
                               '然后完全退出并重开 Mac 端软件')
        if not AXIsProcessTrusted():
            major = platform.mac_ver()[0].split('.')[0]
            permission = '设备控制和数据访问' if major.isdigit() and int(major) >= 27 else '辅助功能'
            raise RuntimeError(f'请在 Mac“隐私与安全性 → {permission}”为 {permission_app} 授权，'
                               '然后完全退出并重开 Mac 端软件')


def frame_byte_limit(resolution):
    width, height = resolution
    return max(4 * 1024 * 1024, 2 * width * height)


def _grab_mac_retina(display, resolution=None):
    import Quartz
    from objc import autorelease_pool
    from PIL import Image

    grab = getattr(Quartz, 'CGWindowListCreateImage', None)
    if grab is None:
        return None
    with autorelease_pool():
        rect = Quartz.CGRectMake(display['left'], display['top'], display['width'], display['height'])
        options = (Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageShouldBeOpaque
                   | Quartz.kCGWindowImageBestResolution)
        image = grab(rect, Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID, options)
        if image is None or Quartz.CGImageGetBitsPerPixel(image) != 32:
            return None
        info = Quartz.CGImageGetBitmapInfo(image)
        if (info & Quartz.kCGBitmapByteOrderMask) != Quartz.kCGBitmapByteOrder32Little:
            return None
        if (info & Quartz.kCGBitmapAlphaInfoMask) not in (
                Quartz.kCGImageAlphaNoneSkipFirst, Quartz.kCGImageAlphaPremultipliedFirst,
                Quartz.kCGImageAlphaFirst):
            return None
        size = Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)
        if resolution is not None and (size[0] > resolution[0] or size[1] > resolution[1]):
            scale = min(resolution[0] / size[0], resolution[1] / size[1])
            target = tuple(max(1, round(dimension * scale)) for dimension in size)
            stride = target[0] * 4
            context = Quartz.CGBitmapContextCreate(
                None, *target, 8, stride, Quartz.CGImageGetColorSpace(image),
                Quartz.kCGImageAlphaNoneSkipFirst | Quartz.kCGBitmapByteOrder32Little,
            )
            if context is not None:
                # 保留原屏幕色彩空间，在原生缓冲区高质量缩放后再复制到 Python。
                Quartz.CGContextSetInterpolationQuality(context, Quartz.kCGInterpolationHigh)
                Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, *target), image)
                data = Quartz.CGBitmapContextGetData(context)
                if data is not None:
                    return Image.frombytes('RGB', target, data.as_buffer(stride * target[1]),
                                           'raw', 'BGRX', stride)
        stride = Quartz.CGImageGetBytesPerRow(image)
        data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image))
        if data is None:
            return None
        return Image.frombytes('RGB', size, bytes(data), 'raw', 'BGRX', stride)


def capture_frames(display, stopped, resolution=FRAME_SIZE, record=None, get_fps=None, report=None):
    from mss import MSS
    from PIL import Image
    from jpeg_codec import JpegEncoder, RawFrame

    def announce(message, level='INFO'):
        if report is not None:
            report(message, level)
        elif level == 'WARNING':
            logging.getLogger(__name__).warning(message)

    # macOS 的 MSS 默认按逻辑点截图。只有目标尺寸需要更多像素时才读取 Retina 背板。
    retina = (sys.platform == 'darwin' and resolution[0] > display['width']
              and resolution[1] > display['height'])
    limit = frame_byte_limit(resolution)
    with ExitStack() as resources:
        encoder = resources.enter_context(JpegEncoder())
        codec_name = None
        gpu = None
        mac = None
        native = None
        capture = None
        mac_fps = get_fps() if get_fps is not None else DEFAULT_FPS
        gdi_pending = (sys.platform == 'win32' and (display['width'] > resolution[0]
                                                 or display['height'] > resolution[1]))
        if sys.platform == 'win32':
            try:
                from windows_dxgi import DXGICapture
                gpu = resources.enter_context(DXGICapture(display, resolution))
                announce(f'采集后端：DXGI / GPU，输出 {gpu.size[0]}×{gpu.size[1]}')
            except (ImportError, OSError) as exc:
                announce(f'Windows DXGI 采集不可用，回退 GDI/MSS：{exc}', 'WARNING')
        elif sys.platform == 'darwin':
            try:
                from macos_capture import MacCapture
                mac = resources.enter_context(MacCapture(display, resolution, mac_fps))
                announce(f'采集后端：ScreenCaptureKit，输出 {mac.size[0]}×{mac.size[1]}')
            except (ImportError, OSError) as exc:
                announce(f'ScreenCaptureKit 不可用，回退 CoreGraphics/MSS：{exc}', 'WARNING')
        accelerated_started = time.monotonic()
        accelerated_has_frame = False
        while not stopped.is_set():
            started = time.perf_counter()
            image = None
            accelerated = gpu or mac
            if accelerated is not None:
                try:
                    if mac is not None and get_fps is not None:
                        desired_fps = get_fps()
                        if desired_fps != mac_fps:
                            mac.update_fps(desired_fps)
                            mac_fps = desired_fps
                    image = accelerated.grab(raw=encoder.name == 'TurboJPEG')
                    if (image is None and not accelerated_has_frame
                            and time.monotonic() - accelerated_started > 2):
                        raise OSError('原生采集未能取得首帧')
                except OSError as exc:
                    accelerated.close()
                    gpu = mac = None
                    announce(f'原生采集失败，使用兼容采集：{exc}', 'WARNING')
                else:
                    if image is None:
                        # 桌面没有更新，不重编码旧画面；返回发送循环以处理实时设置和关闭。
                        if record is not None:
                            record('desktop_wait', time.perf_counter() - started)
                        yield b''
                        continue
                    accelerated_has_frame = True
                    if record is not None:
                        record('grab_gpu' if gpu is not None else 'grab_sck', time.perf_counter() - started)
            if gpu is None and gdi_pending:
                gdi_pending = False
                try:
                    from windows_capture import WindowsCapture
                    native = resources.enter_context(WindowsCapture(display, resolution))
                    announce(f'采集后端：GDI 原生缩放，输出 {native.size[0]}×{native.size[1]}')
                except (ImportError, OSError) as exc:
                    announce(f'Windows 原生缩放采集不可用，使用 MSS：{exc}', 'WARNING')
            if native is not None:
                try:
                    image = native.grab(raw=encoder.name == 'TurboJPEG')
                except OSError as exc:
                    native.close()
                    native = None
                    announce(f'Windows 原生缩放采集失败，使用 MSS：{exc}', 'WARNING')
                else:
                    if record is not None:
                        record('grab_scaled', time.perf_counter() - started)
            if image is None and retina:
                image = _grab_mac_retina(display, resolution)
            if image is None:
                retina = False
                if capture is None:
                    capture = resources.enter_context(MSS())
                    announce('采集后端：MSS（兼容模式）')
                shot = capture.grab(display)
                grabbed = time.perf_counter()
                # MSS 10.x 的 bgra 会复制整帧，新版可直接返回只读缓冲。
                pixels = getattr(shot, 'raw', None)
                if pixels is None:
                    pixels = shot.bgra
                if (encoder.name == 'TurboJPEG' and shot.size[0] <= resolution[0]
                        and shot.size[1] <= resolution[1]):
                    image = RawFrame(bytes(pixels), shot.size, shot.size[0] * 4)
                else:
                    image = Image.frombytes('RGB', shot.size, pixels, 'raw', 'BGRX')
                if record is not None:
                    record('grab', grabbed - started)
                    record('color', time.perf_counter() - grabbed)
            elif native is None and gpu is None and mac is None and record is not None:
                record('grab', time.perf_counter() - started)
            started = time.perf_counter()
            if image.width > resolution[0] or image.height > resolution[1]:
                if isinstance(image, RawFrame):
                    image = image.to_image()
                image.thumbnail(resolution, Image.Resampling.LANCZOS)
            if record is not None:
                record('resize', time.perf_counter() - started)
            started = time.perf_counter()
            encoded = encoder.encode(image, quality=85, subsampling=0)
            if len(encoded) > limit:
                encoded = encoder.encode(image, quality=80, subsampling=2)
                if len(encoded) > limit:
                    raise ValueError("投屏画面数据超过分辨率上限")
            if record is not None:
                record('jpeg', time.perf_counter() - started)
            if codec_name != encoder.name:
                codec_name = encoder.name
                announce(f'JPEG 编码器：{codec_name}（默认 quality 85，4:4:4）')
                if encoder.fallback_reason:
                    announce(f'TurboJPEG 加速不可用，已使用 Pillow：{encoder.fallback_reason}', 'WARNING')
            yield encoded


def decode_frame(data, resolution=FRAME_SIZE):
    from PIL import Image

    with Image.open(io.BytesIO(data), formats=('JPEG',)) as image:
        if image.width > resolution[0] or image.height > resolution[1]:
            raise ValueError("投屏画面尺寸无效")
        return image.convert('RGB')


class RemoteInput:
    """每个远程会话独立记录按住的键和按钮，结束时补发释放。"""

    def __init__(self, handler, display):
        self.handler = handler
        self.display = display
        self.keys = {}
        self.buttons = set()

    def release(self):
        cancel_caps = getattr(self.handler, 'cancel_pending_caps_lock', None)
        if cancel_caps is not None:
            cancel_caps()
        for key in self.keys.values():
            self.handler.simulate_key_release(key)
        for button in self.buttons:
            self.handler.simulate_mouse_click(button, False)
        self.keys.clear()
        self.buttons.clear()

    def _move(self, data):
        x, y = data.get('x'), data.get('y')
        if not all(type(value) is int and 0 <= value <= COORD_MAX for value in (x, y)):
            return False
        display = self.display
        self.handler.simulate_mouse_move(
            display['left'] + x * (display['width'] - 1) // COORD_MAX,
            display['top'] + y * (display['height'] - 1) // COORD_MAX,
        )
        return True

    def apply(self, data):
        if not isinstance(data, dict):
            return
        kind = data.get('kind')
        if kind == 'release':
            self.release()
        elif kind == 'move':
            self._move(data)
        elif kind == 'click':
            button, pressed = data.get('button'), data.get('pressed')
            if button not in ('left', 'middle', 'right') or type(pressed) is not bool:
                return
            moved = self._move(data)
            if moved and pressed:
                count = data.get('click_count')
                if type(count) is int and 1 <= count <= 10:
                    self.handler.simulate_mouse_click(button, True, click_count=count)
                else:
                    self.handler.simulate_mouse_click(button, True)
                self.buttons.add(button)
            elif not pressed and button in self.buttons:
                self.handler.simulate_mouse_click(button, False)
                self.buttons.remove(button)
        elif kind == 'scroll':
            dx, dy = data.get('dx'), data.get('dy')
            if all(type(value) is int and -10 <= value <= 10 for value in (dx, dy)) and self._move(data):
                self.handler.simulate_mouse_scroll(dx, dy)
        elif kind == 'key':
            code, key, pressed = data.get('code'), data.get('key'), data.get('pressed')
            if type(code) is not int or not 0 <= code <= 0xFFFFFFFF or type(pressed) is not bool:
                return
            if not pressed:
                held = self.keys.pop(code, None)
                if held:
                    if held.get('name') == 'caps_lock':
                        # 释放时必须带释放事件的源端时间，不能重用按下的时间。
                        held = dict(held, caps_time_ms=data.get('caps_time_ms'))
                        if data.get('cancelled') is True:
                            held['cancelled'] = True
                    self.handler.simulate_key_release(held)
            elif isinstance(key, dict) and len(self.keys) < 128:
                key_type = key.get('type')
                value = key.get('char' if key_type == 'char' else 'name')
                if (key_type == 'char' and isinstance(value, str) and len(value) == 1
                        or key_type == 'special' and isinstance(value, str) and len(value) <= 32):
                    self.handler.simulate_key_press(self.keys.setdefault(code, key))


class DesktopSession:
    def __init__(self, connection, label, session_id, token, viewer, resolution=FRAME_SIZE,
                 live_resolution=False, live_fps=False, low_latency=False, live_display=False,
                 network_latency=False):
        self.control = connection
        self.label = label
        self.id = session_id
        self.token = token
        self.viewer = viewer
        self.resolution = resolution
        self.live_resolution = live_resolution
        self.fps = DEFAULT_FPS
        self.live_fps = live_fps
        self.host_platform = None
        self.network_latency = network_latency
        self.network_rtt_ms = None
        self.network_rtt_updated_at = None
        self._probe = None
        self.display = None
        self.displays = []
        self.display_index = 0
        self.live_display = live_display
        self.approved = threading.Event()
        self.closed = threading.Event()
        self.ready = threading.Event()
        self.first_frame = threading.Event()
        self.deadline = time.monotonic() + 90
        self.error = None
        self._media = None
        self._lock = threading.Lock()
        self.audio_supported = False
        self.audio = None
        self._events = queue.Queue(maxsize=128)
        self._frame = None
        self._pending_resolution = None
        self._pending_fps = None
        self._pending_display = None
        self._display_acknowledged = False
        self._send_lock = threading.Lock()
        self._replies = queue.Queue(maxsize=4)
        self._frame_ack = threading.Condition(self._lock)
        self._frame_sequence = 0
        self._inflight_sequences = set()
        self._ping_sequence = 0
        self._pending_pings = {}
        self._performance_lock = threading.Lock()
        self._performance_since = time.monotonic()
        self._performance = {}
        self._performance_frames = self._performance_bytes = 0
        self._performance_replaced = 0
        self._notices = queue.Queue(maxsize=16)
        self.rtt_ms = None
        self.rtt_updated_at = None
        self.low_latency = False
        self.set_low_latency(low_latency)

    def set_low_latency(self, enabled):
        self.low_latency = enabled is True
        self.fps = 30 if self.low_latency else DEFAULT_FPS
        self.rtt_ms = None
        self.rtt_updated_at = None
        self._pending_pings.clear()

    def record_performance(self, stage, seconds, frame_bytes=None):
        # 只累计总量、最大值和次数，不保留逐帧历史或图像。
        with self._performance_lock:
            total, maximum, count = self._performance.get(stage, (0, 0, 0))
            self._performance[stage] = (total + seconds, max(maximum, seconds), count + 1)
            if frame_bytes is not None:
                self._performance_frames += 1
                self._performance_bytes += frame_bytes

    def record_replaced_frame(self):
        with self._performance_lock:
            self._performance_replaced += 1

    def report(self, message, level='INFO'):
        try:
            self._notices.put_nowait((message, level))
        except queue.Full:
            pass

    def take_notices(self):
        notices = []
        while True:
            try:
                notices.append(self._notices.get_nowait())
            except queue.Empty:
                return notices

    def take_performance_stats(self, interval=5):
        with self._performance_lock:
            now = time.monotonic()
            elapsed = now - self._performance_since
            if elapsed < interval or not self._performance_frames:
                return None
            frames, size = self._performance_frames, self._performance_bytes
            stats = {'fps': frames / elapsed, 'mbps': size * 8 / elapsed / 1_000_000,
                     'frame_kib': size / frames / 1024,
                     'replaced_frames': self._performance_replaced,
                     'stages': {stage: (total * 1000 / count, maximum * 1000)
                                for stage, (total, maximum, count) in self._performance.items()}}
            self._performance.clear()
            self._performance_frames = self._performance_bytes = 0
            self._performance_replaced = 0
            self._performance_since = now
            return stats

    def attach(self, connection):
        with self._lock:
            if self.closed.is_set() or self._media is not None:
                return False
            self._media = connection
            return True

    def attach_probe(self, connection):
        with self._lock:
            if not self.network_latency or self.closed.is_set() or self._probe is not None:
                return False
            self._probe = connection
            return True

    def begin_audio(self, generation=None):
        with self._lock:
            if not self.audio_supported or self.closed.is_set():
                return None
            previous = self.audio
            last_generation = previous.generation if previous is not None else 0
            if generation is None:
                generation = last_generation + 1
            if generation <= last_generation:
                return None
            audio = self.audio = DesktopAudio(generation)
        if previous is not None:
            previous.stop()
        return audio

    def run_probe(self):
        connection = self._probe
        if connection is None:
            return
        try:
            connection.settimeout(3)
            sequence = 0
            while not self.closed.is_set():
                if self.viewer:
                    sequence = (sequence + 1) & 0xFFFFFFFFFFFFFFFF
                    packet = NETWORK_PROBE.pack(sequence)
                    started = time.monotonic()
                    connection.sendall(packet)
                    if Protocol._receive_exact(connection, NETWORK_PROBE.size) != packet:
                        raise OSError("网络延迟测量应答无效")
                    now = time.monotonic()
                    with self._lock:
                        self.network_rtt_ms = max(0.0, (now - started) * 1000)
                        self.network_rtt_updated_at = now
                    self.closed.wait(max(0.0, 1 - (now - started)))
                else:
                    packet = Protocol._receive_exact(connection, NETWORK_PROBE.size)
                    if packet is None:
                        return
                    connection.sendall(packet)
        except OSError:
            with self._lock:
                self.network_rtt_ms = None
                self.network_rtt_updated_at = None
        finally:
            with self._lock:
                if self._probe is connection:
                    self._probe = None
            close_socket(connection)

    def close(self, error=None):
        with self._lock:
            if self.closed.is_set():
                return
            self.error = error
            self.closed.set()
            self._frame_ack.notify_all()
            if self._media is not None:
                close_socket(self._media)
            if self._probe is not None:
                close_socket(self._probe)
            if self.audio is not None:
                self.audio.stop()

    def _worker(self, operation):
        def run():
            try:
                operation()
            except (OSError, EOFError) as exc:
                self.close(f"投屏连接结束：{exc}" if str(exc) else None)
            except Exception as exc:
                self.close(f"投屏失败：{exc}")
            finally:
                self.close()
        threading.Thread(target=run, daemon=True).start()

    def run(self, handler):
        if self.closed.is_set():
            return
        self.ready.set()
        if self.viewer:
            self._worker(self._read_frames)
            self._worker(self._send_inputs)
        else:
            self._worker(self._capture)
            self._worker(lambda: self._receive_inputs(handler))
            if self.low_latency:
                self._worker(self._send_replies)
        self.closed.wait()

    def send_input(self, data):
        if self.closed.is_set():
            return
        full = False
        with self._lock:
            if (self.live_display and self._pending_display is not None and isinstance(data, dict)
                    and data.get('kind') in ('move', 'click', 'scroll', 'key')):
                return
            try:
                self._events.put_nowait(data)
            except queue.Full:
                full = True
        if full:
            self.close("投屏网络拥塞，已释放远端键鼠")

    @property
    def pending_display(self):
        with self._lock:
            return self._pending_display

    def request_display(self, index):
        if not self.viewer or not self.live_display or type(index) is not int:
            return False
        with self._lock:
            if (not 0 <= index < len(self.displays) or index == self.display_index
                    or self.closed.is_set() or self._pending_display is not None):
                return False
            try:
                self._events.put_nowait({'kind': 'display', 'index': index})
            except queue.Full:
                return False
            self._pending_display = index
            self._display_acknowledged = False
            self._frame = None
            return True

    @property
    def pending_resolution(self):
        with self._lock:
            return self._pending_resolution

    def request_resolution(self, profile):
        if not self.viewer or not self.live_resolution or not isinstance(profile, str):
            return False
        resolution = RESOLUTION_PROFILES.get(profile)
        with self._lock:
            if (resolution is None or resolution == self.resolution
                    or not self.ready.is_set() or self.closed.is_set()
                    or self._pending_resolution is not None):
                return False
            try:
                self._events.put_nowait({'kind': 'resolution', 'profile': profile})
            except queue.Full:
                return False
            self._pending_resolution = profile
            return True

    @property
    def pending_fps(self):
        with self._lock:
            return self._pending_fps

    def request_fps(self, fps):
        if not self.viewer or not self.live_fps or type(fps) is not int or fps not in FPS_PROFILES:
            return False
        with self._lock:
            if (fps == self.fps or not self.ready.is_set() or self.closed.is_set()
                    or self._pending_fps is not None):
                return False
            try:
                self._events.put_nowait({'kind': 'fps', 'fps': fps})
            except queue.Full:
                return False
            self._pending_fps = fps
            return True

    def latest_frame(self):
        full = False
        with self._lock:
            frame, self._frame = self._frame, None
            if frame is not None and self._pending_display is not None and self._display_acknowledged:
                try:
                    self._events.put_nowait({'kind': 'display_ready', 'index': self._pending_display})
                except queue.Full:
                    full = True
                self._pending_display = None
                self._display_acknowledged = False
        if full:
            self.close("投屏网络拥塞，已释放远端键鼠")
        return frame

    def _read_frames(self):
        while not self.closed.is_set():
            extra = len(VIDEO_FRAME) + SEQUENCE.size if self.low_latency else 0
            receive_started = time.perf_counter()
            packet = receive_packet(self._media, frame_byte_limit(self.resolution) + extra)
            received_at = time.perf_counter()
            if self.low_latency and packet.startswith(PONG):
                if len(packet) != len(PONG) + SEQUENCE.size:
                    raise ValueError("投屏延迟应答无效")
                sequence, = SEQUENCE.unpack(packet[len(PONG):])
                with self._lock:
                    ping_sent = self._pending_pings.pop(sequence, None)
                    if ping_sent is not None:
                        now = time.monotonic()
                        self.rtt_ms = max(0.0, (now - ping_sent) * 1000)
                        self.rtt_updated_at = now
                continue
            if self.live_resolution and packet.startswith(RESOLUTION_ACK):
                with self._lock:
                    profile = self._pending_resolution
                    if profile is None or packet != RESOLUTION_ACK + profile.encode('ascii'):
                        raise ValueError("投屏分辨率确认无效")
                    self.resolution = RESOLUTION_PROFILES[profile]
                    self._pending_resolution = None
                    self._frame = None
                continue
            if self.live_fps and packet.startswith(FPS_ACK):
                with self._lock:
                    fps = self._pending_fps
                    if fps is None or packet != FPS_ACK + str(fps).encode('ascii'):
                        raise ValueError("投屏帧率确认无效")
                    self.fps = fps
                    self._pending_fps = None
                continue
            if self.live_display and packet.startswith(DISPLAY_ACK):
                if len(packet) != len(DISPLAY_ACK) + SEQUENCE.size:
                    raise ValueError("投屏显示器确认无效")
                index, = SEQUENCE.unpack(packet[len(DISPLAY_ACK):])
                with self._lock:
                    if index != self._pending_display or not 0 <= index < len(self.displays):
                        raise ValueError("投屏显示器确认无效")
                    self.display_index = index
                    self.display = self.displays[index]
                    self._display_acknowledged = True
                    self._frame = None
                continue
            sequence = None
            if self.low_latency:
                if not packet.startswith(VIDEO_FRAME) or len(packet) <= extra:
                    raise ValueError("投屏画面帧无效")
                sequence, = SEQUENCE.unpack(packet[len(VIDEO_FRAME):extra])
                packet = packet[extra:]
            frame = decode_frame(packet, self.resolution)
            self.record_performance('decode', time.perf_counter() - received_at, len(packet))
            # 包含等下一帧的时间，并非纯网络延迟；独立 probe 才是网络 RTT。
            self.record_performance('receive', received_at - receive_started)
            with self._lock:
                if self._pending_display is None or self._display_acknowledged:
                    self._frame = frame
            if not self.first_frame.is_set():
                self.first_frame.set()
            if self.low_latency:
                self.send_input({'kind': 'frame_ack', 'sequence': sequence})

    def _send_media(self, data, prefix=b''):
        with self._send_lock:
            send_packet(self._media, data, prefix)

    def _send_replies(self):
        while not self.closed.is_set():
            try:
                reply = self._replies.get(timeout=0.2)
            except queue.Empty:
                continue
            self._send_media(reply)

    def _capture(self):
        profile_to_ack = None
        fps_to_ack = None
        display_to_ack = None
        legacy_frame = None
        legacy_sent_at = 0
        while not self.closed.is_set():
            factory = lambda stopped, display=self.display, resolution=self.resolution: capture_frames(
                display, stopped, resolution, record=self.record_performance,
                get_fps=lambda: self.fps, report=self.report)
            with closing(LatestFrames(factory, self.closed, lambda: self.fps,
                                      self.record_performance, self.record_replaced_frame)) as frames:
                while not self.closed.is_set():
                    with self._lock:
                        if self.low_latency:
                            def can_capture():
                                changing = (self._pending_display is not None
                                            or self._pending_resolution is not None
                                            or self._pending_fps is not None)
                                return (self.closed.is_set() or not self._inflight_sequences
                                        or (len(self._inflight_sequences) < MAX_INFLIGHT_FRAMES
                                            and not changing))

                            ack_started = time.perf_counter()
                            confirmed = self._frame_ack.wait_for(can_capture, timeout=6)
                            self.record_performance('ack_wait', time.perf_counter() - ack_started)
                            if not confirmed:
                                raise TimeoutError("投屏画面确认超时")
                            if self.closed.is_set():
                                return
                        profile, self._pending_resolution = self._pending_resolution, None
                        if profile is not None:
                            self.resolution = RESOLUTION_PROFILES[profile]
                        fps, self._pending_fps = self._pending_fps, None
                        if fps is not None:
                            self.fps = fps
                        display, self._pending_display = self._pending_display, None
                        if display is not None:
                            self.display_index = display
                            self.display = self.displays[display]
                    if fps is not None:
                        fps_to_ack = fps
                    if profile is not None or display is not None:
                        legacy_frame = None
                        if profile is not None:
                            profile_to_ack = profile
                        if display is not None:
                            display_to_ack = display
                        break
                    frame, encoded_at = frames.take()
                    if frame is None or self.closed.is_set():
                        return
                    if not frame:
                        if fps_to_ack is not None:
                            self._send_media(FPS_ACK + str(fps_to_ack).encode('ascii'))
                            fps_to_ack = None
                        if (self.low_latency or legacy_frame is None
                                or time.monotonic() - legacy_sent_at < 1):
                            continue
                        # 旧协议没有 PONG；静止时每秒发缓存 JPEG，避免对端六秒读超时。
                        frame = legacy_frame
                    elif encoded_at is not None:
                        self.record_performance('queue_wait', time.perf_counter() - encoded_at)
                    if profile_to_ack is not None:
                        # 确认与新画面在同一方向发送，旧画面始终按旧上限解码。
                        self._send_media(RESOLUTION_ACK + profile_to_ack.encode('ascii'))
                        profile_to_ack = None
                    if fps_to_ack is not None:
                        self._send_media(FPS_ACK + str(fps_to_ack).encode('ascii'))
                        fps_to_ack = None
                    if display_to_ack is not None:
                        self._send_media(DISPLAY_ACK + SEQUENCE.pack(display_to_ack))
                        display_to_ack = None
                    prefix = b''
                    if self.low_latency:
                        with self._lock:
                            self._frame_sequence = (self._frame_sequence + 1) & 0xFFFFFFFF
                            self._inflight_sequences.add(self._frame_sequence)
                        prefix = VIDEO_FRAME + SEQUENCE.pack(self._frame_sequence)
                    send_started = time.perf_counter()
                    self._send_media(frame, prefix)
                    self.record_performance('send', time.perf_counter() - send_started, len(prefix) + len(frame))
                    if not self.low_latency:
                        legacy_frame = frame
                        legacy_sent_at = time.monotonic()
                    if not self.first_frame.is_set():
                        self.first_frame.set()

    def _send_inputs(self):
        next_ping = time.monotonic()
        while not self.closed.is_set():
            if self.low_latency and time.monotonic() >= next_ping:
                with self._lock:
                    self._ping_sequence = (self._ping_sequence + 1) & 0xFFFFFFFF
                    sequence = self._ping_sequence
                    ping_sent = time.monotonic()
                    self._pending_pings = {
                        number: sent_at for number, sent_at in self._pending_pings.items()
                        if ping_sent - sent_at < 10
                    }
                    if len(self._pending_pings) >= 8:
                        self._pending_pings.pop(next(iter(self._pending_pings)))
                    self._pending_pings[sequence] = ping_sent
                send_packet(self._media, json.dumps({
                    'kind': 'ping', 'sequence': sequence,
                }).encode('utf-8'))
                next_ping = ping_sent + 1
            try:
                timeout = max(0, next_ping - time.monotonic()) if self.low_latency else 1
                data = self._events.get(timeout=timeout)
            except queue.Empty:
                if self.low_latency:
                    continue
                data = {'kind': 'ping'}
            send_packet(self._media, json.dumps(data).encode('utf-8'))

    def _receive_inputs(self, handler):
        remote = RemoteInput(handler, self.display)
        awaiting_display = None
        try:
            while not self.closed.is_set():
                data = json.loads(receive_packet(self._media, 8192))
                kind = data.get('kind') if isinstance(data, dict) else None
                if self.low_latency and kind == 'ping':
                    sequence = data.get('sequence')
                    if type(sequence) is int and 0 <= sequence <= 0xFFFFFFFF:
                        try:
                            self._replies.put_nowait(PONG + SEQUENCE.pack(sequence))
                        except queue.Full:
                            pass
                elif self.low_latency and kind == 'frame_ack':
                    sequence = data.get('sequence')
                    with self._lock:
                        if type(sequence) is int and sequence in self._inflight_sequences:
                            self._inflight_sequences.remove(sequence)
                            self._frame_ack.notify_all()
                elif self.live_display and kind == 'display':
                    index = data.get('index')
                    if type(index) is int and 0 <= index < len(self.displays):
                        remote.release()
                        awaiting_display = index
                        with self._lock:
                            self._pending_display = index
                elif self.live_display and kind == 'display_ready':
                    index = data.get('index')
                    with self._lock:
                        if type(index) is int and index == awaiting_display == self.display_index:
                            remote.display = self.display
                            awaiting_display = None
                elif self.live_resolution and kind == 'resolution':
                    profile = data.get('profile')
                    if isinstance(profile, str) and profile in RESOLUTION_PROFILES:
                        with self._lock:
                            if RESOLUTION_PROFILES[profile] != self.resolution:
                                self._pending_resolution = profile
                elif self.live_fps and kind == 'fps':
                    fps = data.get('fps')
                    if type(fps) is int and fps in FPS_PROFILES:
                        with self._lock:
                            if fps != self.fps:
                                self._pending_fps = fps
                elif awaiting_display is None or kind == 'release':
                    remote.apply(data)
        finally:
            remote.release()
