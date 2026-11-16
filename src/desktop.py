"""GUI 两端共用的远程桌面会话协商和授权。"""
import hmac
import platform
import queue
import secrets
import socket
import threading
import time
from types import SimpleNamespace
import qt_runtime as tk
from ui import choose_value

from desktop_stream import (
    DEFAULT_RESOLUTION, RESOLUTION_PROFILES, DesktopSession, check_screen_permission,
    close_socket, list_displays,
)
from desktop_viewer import DesktopViewer
from protocol import MessageType, Protocol


def normalize_resolution(value):
    return value if isinstance(value, str) and value in RESOLUTION_PROFILES else DEFAULT_RESOLUTION


def choose_resolution(root, current):
    profiles = tuple(RESOLUTION_PROFILES)
    labels = [f"{name} · {width}×{height}" for name, (width, height) in RESOLUTION_PROFILES.items()]

    index = choose_value(root, '投屏分辨率', labels, profiles.index(current))
    return profiles[index] if index is not None else None


def display_names(displays):
    return [f"显示器 {index + 1} · {screen['width']}×{screen['height']}"
            for index, screen in enumerate(displays)]


def remote_displays(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        return []
    screens = []
    for screen in value:
        if not isinstance(screen, dict):
            return []
        if not all(type(screen.get(key)) is int for key in ('left', 'top', 'width', 'height')):
            return []
        if not (0 < screen['width'] <= 65535 and 0 < screen['height'] <= 65535):
            return []
        screens.append({key: screen[key] for key in ('left', 'top', 'width', 'height')})
    return screens


def choose_display(root, displays, current):
    index = choose_value(root, '选择远程显示器', display_names(displays), current)
    return index


class DesktopController:
    def __init__(self, root, handler, log, server=False, on_active=None, on_changed=None):
        self.root = root
        self.handler = handler
        self.log = log
        self.server = server
        self.on_active = on_active
        self.on_changed = on_changed
        self.session = None
        self.viewer = None
        self._reported_ready = False
        self._reported_first_frame = False
        self._reported_network_rtt = False
        self._preferred_resolution = '1080p'
        self.performance_logging = False
        self._events = queue.Queue(maxsize=32)
        self._caps_events = queue.Queue(maxsize=32)
        self._signals = queue.SimpleQueue()
        threading.Thread(target=self._send_signals, daemon=True).start()
        self._after_id = root.after(30, self._tick)

    @property
    def active(self):
        return self.session is not None and self.session.approved.is_set()

    def _changed(self):
        if self.on_changed:
            self.on_changed()

    def request(self, connection, label):
        if self.session is not None:
            self.log("请先结束当前投屏", "WARNING")
            return
        try:
            import PIL
        except ImportError:
            self.log("投屏需要 Pillow，请先安装 requirements.txt 中的依赖", "ERROR")
            return
        profile = choose_resolution(self.root, self._preferred_resolution)
        if profile is None or connection.fileno() < 0:
            return
        if self.session is not None:
            self.log("请先结束当前投屏", "WARNING")
            return
        self._preferred_resolution = profile
        session = DesktopSession(connection, label, secrets.token_hex(16), secrets.token_hex(32),
                                 viewer=True, resolution=RESOLUTION_PROFILES[profile])
        self.session = session
        self._changed()
        self.log(f"正在请求 {label} 的屏幕（最高 {profile}）")
        self._signal(session, MessageType.DESKTOP_REQUEST, {
            'id': session.id, 'token': session.token, 'resolution': profile,
            'live_resolution': True, 'live_fps': True, 'low_latency': True,
            'live_display': True, 'network_latency': True,
        })

    def receive(self, connection, label, msg_type, data):
        # 网络接收线程只排队，会话协商和 Tk 窗口操作都在主线程进行。
        try:
            self._events.put_nowait((connection, label, msg_type, data))
        except queue.Full:
            pass

    def forward_caps(self, key, pressed):
        viewer = self.viewer
        if viewer is None or not viewer.native_caps:
            return False
        payload = self.handler.serialize_key(key)
        if payload.get('name') != 'caps_lock':
            return False
        if not viewer._active:
            return True
        stamp = payload.get('caps_time_ms') or time.monotonic_ns() // 1_000_000
        try:
            self._caps_events.put_nowait((viewer, viewer._input_epoch, pressed, stamp))
        except queue.Full:
            viewer.session.close('Caps Lock 输入队列已满，已释放远端键鼠')
        return True

    def _tick(self):
        # 轮询比视频帧周期短，避免 Tk 固定刷新间隔限制高帧率。
        interval = 8 if self.viewer is not None else 30
        self._after_id = self.root.after(interval, self._tick)
        for _ in range(32):
            try:
                viewer, epoch, pressed, stamp = self._caps_events.get_nowait()
            except queue.Empty:
                break
            if (viewer is not self.viewer or viewer._destroyed or epoch != viewer._input_epoch
                    or viewer.session.closed.is_set() or not viewer._active
                    or viewer._image is None or not viewer.window.isActiveWindow()):
                continue
            event = SimpleNamespace(keysym='Caps_Lock', char='', keycode=0xFFFFFFFD,
                                    caps_time_ms=stamp)
            if pressed:
                if 0xFFFFFFFD not in viewer._keys._pressed:
                    viewer._keys.press(event)
            else:
                viewer._keys.release(event)
        for _ in range(16):
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            self._handle(*event)
        session = self.session
        if session is not None:
            for message, level in session.take_notices():
                self.log(message, level)
            if session.closed.is_set():
                self.end(session.error, notify_peer=True)
                return
            if not session.first_frame.is_set() and time.monotonic() > session.deadline:
                if not session.approved.is_set():
                    self.end("投屏请求超时")
                elif not session.ready.is_set():
                    self.end("投屏媒体通道连接超时")
                else:
                    self.end("投屏首帧超时")
                return
            if session.ready.is_set() and not self._reported_ready:
                self._reported_ready = True
                self.log("投屏媒体通道已连接")
                if session.viewer:
                    self.log("网络延迟探测已启用" if session.network_latency else "对端未支持独立网络延迟探测，请更新对端")
            if session.viewer and session.network_rtt_ms is not None and not self._reported_network_rtt:
                self._reported_network_rtt = True
                self.log(f"网络往返延迟 {session.network_rtt_ms:.0f} ms")
            stats = session.take_performance_stats()
            if stats is not None and self.performance_logging:
                names = {'capture_encode':'采集/编码', 'send':'写入发送缓冲', 'receive':'收帧等待',
                         'decode':'解码', 'convert_ui':'转为界面图像', 'paint':'绘制',
                         'grab':'读屏', 'color':'转色', 'resize':'缩放', 'jpeg':'JPEG编码',
                         'grab_scaled':'原生读屏/缩放/转色', 'ack_wait':'等候画面确认',
                         'pace_wait':'帧率限速等候', 'grab_gpu':'DXGI采集/GPU缩放/读回',
                         'desktop_wait':'等候桌面更新', 'grab_sck':'ScreenCaptureKit取帧/转色',
                         'queue_wait':'编码后待发'}
                timings = '，'.join(f'{names[stage]} {mean:.1f}/{maximum:.1f} ms'
                                    for stage, (mean, maximum) in stats['stages'].items())
                direction = '接收' if session.viewer else '发送'
                replaced = (f"，替换未发送旧帧 {stats['replaced_frames']} 张"
                            if not session.viewer else '')
                self.log(f"投屏性能（{direction}）：平均画面更新 {stats['fps']:.1f} fps（设定上限 {session.fps} fps），"
                         f"{stats['mbps']:.1f} Mbps，每帧 {stats['frame_kib']:.0f} KiB；"
                         f"{timings}（平均/最大）{replaced}", "PERF")
            if session.first_frame.is_set() and not self._reported_first_frame:
                self._reported_first_frame = True
                self.log("已接收并解码首帧" if session.viewer else "已发送投屏首帧")
                if not session.viewer and session.display:
                    from PIL import __version__ as pillow_version, features
                    self.log(f"采集环境：{platform.system()} {platform.machine()}，"
                             f"原始 {session.display['width']}×{session.display['height']}，"
                             f"目标上限 {session.resolution[0]}×{session.resolution[1]}，"
                             f"Pillow {pillow_version}，libjpeg-turbo={features.check_feature('libjpeg_turbo')}")
            if self.viewer is not None:
                # 保留处理显示器选择框时的轮询。绘制帧前撤销它，避免慢帧让下一次定时器
                # 在画布有机会重绘之前就到期。
                self.root.after_cancel(self._after_id)
                started = time.monotonic()
                try:
                    first_size = self.viewer.update()
                    if first_size is not None:
                        width, height = first_size
                        self.log(f"已在投屏窗口显示首帧（{width}×{height}）")
                except (ImportError, OSError, RuntimeError, ValueError, tk.TclError) as exc:
                    self.end(f"投屏窗口绘制失败：{exc}")
                finally:
                    if self._after_id is not None:
                        elapsed_ms = int((time.monotonic() - started) * 1000)
                        self._after_id = self.root.after(max(2, interval - elapsed_ms), self._tick)

    @staticmethod
    def _valid_hex(value, length):
        return isinstance(value, str) and len(value) == length and all(c in '0123456789abcdef' for c in value)

    def _handle(self, connection, label, msg_type, data):
        if connection.fileno() < 0 or not isinstance(data, dict):
            return
        session_id = data.get('id')
        session = self.session
        if msg_type == MessageType.DESKTOP_REQUEST:
            if self._valid_hex(session_id, 32) and self._valid_hex(data.get('token'), 64):
                self._incoming(connection, label, session_id, data['token'],
                               normalize_resolution(data.get('resolution')),
                               data.get('live_resolution') is True,
                               data.get('live_fps') is True,
                               data.get('low_latency') is True,
                               data.get('live_display') is True,
                               data.get('network_latency') is True)
        elif session is not None and connection is session.control and session_id == session.id:
            if msg_type == MessageType.DESKTOP_STOP:
                self.end("对端已结束投屏", notify_peer=False)
            elif msg_type == MessageType.DESKTOP_RESPONSE and session.viewer and not session.approved.is_set():
                if data.get('accepted') is True:
                    profile = normalize_resolution(data.get('resolution'))
                    size = RESOLUTION_PROFILES[profile]
                    if size < session.resolution:
                        self.log(f"对端将投屏分辨率上限设为 {profile}")
                    session.resolution = min(session.resolution, size)
                    session.live_resolution = data.get('live_resolution') is True
                    session.live_fps = data.get('live_fps') is True
                    session.set_low_latency(data.get('low_latency') is True)
                    session.network_latency = data.get('network_latency') is True
                    session.displays = remote_displays(data.get('displays'))
                    session.live_display = data.get('live_display') is True and bool(session.displays)
                    index = data.get('display_index', 0)
                    if type(index) is int and 0 <= index < len(session.displays):
                        session.display_index = index
                    session.host_platform = data.get('host_platform')
                    self._approve(session)
                    if not self.server:
                        threading.Thread(target=self._dial, args=(session,), daemon=True).start()
                    if session.live_display and len(session.displays) > 1:
                        choice = choose_display(self.root, session.displays, session.display_index)
                        if self.session is session:
                            if choice is None:
                                self.end()
                            else:
                                self.viewer.select_display(choice)
                else:
                    self.end("对端未接受投屏请求", notify_peer=False)

    def _incoming(self, connection, label, session_id, token, profile, live_resolution, live_fps,
                  low_latency=False, live_display=False, network_latency=False):
        session = DesktopSession(connection, label, session_id, token, viewer=False,
                                 resolution=RESOLUTION_PROFILES[profile], live_resolution=live_resolution,
                                 live_fps=live_fps, low_latency=low_latency,
                                 live_display=live_display, network_latency=network_latency)
        if self.session is not None:
            self._reply(session, accepted=False)
            return
        self.session = session
        self._changed()
        try:
            import PIL
            check_screen_permission()
            displays = list_displays()
            if not displays:
                raise RuntimeError("未发现可共享的显示器")
            if self.session is not session:
                return
            session.displays = displays
            session.display_index = 0
            session.display = displays[0]
            self.log(f"已接受来自 {label} 的远程桌面连接")
            self._approve(session)
            self._reply(session, accepted=True, profile=profile)
            return
        except ImportError:
            self.log("投屏需要 mss 和 Pillow，请先安装 requirements.txt 中的依赖", "ERROR")
        except Exception as exc:
            self.log(f"无法共享屏幕：{exc}", "ERROR")
        self.end(notify_peer=False)
        self._reply(session, accepted=False)

    def _approve(self, session):
        if self.on_active:
            self.on_active()
        # Allow cold native framework startup and a bounded fallback before
        # declaring the first frame lost. Steady-state frame waits are separate.
        session.deadline = time.monotonic() + 20
        session.approved.set()
        if session.viewer:
            self.viewer = DesktopViewer(self.root, session, self.end)
            listener = getattr(self.handler, 'keyboard_listener', None)
            self.viewer.native_caps = (platform.system() in ('Windows', 'Linux')
                                       and session.host_platform == 'Darwin'
                                       and listener is not None and listener.is_alive())
            self.log("投屏窗口已打开，等待画面")
        else:
            self.log(f"正在向 {session.label} 共享屏幕")
        self._changed()

    def _signal(self, session, msg_type, data, then=None):
        self._signals.put((session, msg_type, data, then))

    def _send_signals(self):
        while True:
            signal = self._signals.get()
            if signal is None:
                return
            session, msg_type, data, then = signal
            try:
                Protocol.send_message(session.control, Protocol.encode(msg_type, data))
            except OSError as exc:
                session.close(f"投屏信令失败：{exc}")
                continue
            if then:
                threading.Thread(target=then, args=(session,), daemon=True).start()

    def _reply(self, session, accepted, profile=None):
        data = {'id': session.id, 'accepted': accepted}
        if accepted:
            data['host_platform'] = platform.system()
        if profile is not None:
            data['resolution'] = profile
        if accepted and session.live_resolution:
            data['live_resolution'] = True
        if accepted and session.live_fps:
            data['live_fps'] = True
        if accepted and session.low_latency:
            data['low_latency'] = True
        if accepted and session.live_display:
            data.update(live_display=True, displays=session.displays, display_index=session.display_index)
        if accepted and session.network_latency:
            data['network_latency'] = True
        self._signal(session, MessageType.DESKTOP_RESPONSE, data,
                     self._dial if accepted and not self.server else None)

    def _dial(self, session):
        if session.closed.is_set():
            return
        connection = None
        try:
            connection = socket.create_connection(session.control.getpeername(), timeout=6)
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            greeting = Protocol.receive_message(connection)
            if not greeting or greeting[0] != MessageType.SCREEN_INFO:
                raise OSError("未收到投屏通道握手")
            Protocol.send_message(connection, Protocol.encode(MessageType.DESKTOP_CHANNEL, {
                'id': session.id, 'token': session.token,
            }))
            ack = Protocol.receive_message(connection)
            if ack != (MessageType.DESKTOP_CHANNEL, {'id': session.id}):
                raise OSError("对端未接受投屏通道")
            if session.attach(connection):
                if session.network_latency:
                    threading.Thread(target=self._dial_probe, args=(session,), daemon=True).start()
                session.run(self.handler)
        except OSError as exc:
            session.close(f"无法连接投屏通道：{exc}")
        finally:
            if connection:
                close_socket(connection)

    def accept_channel(self, connection, address, data):
        session = self.session
        if session is None or not isinstance(data, dict) or data.get('id') != session.id:
            return
        token = data.get('token')
        if not self._valid_hex(token, 64) or not hmac.compare_digest(token, session.token):
            return
        if address[0] != session.control.getpeername()[0]:
            return
        if data.get('kind') == 'latency':
            if not session.network_latency or not session.approved.wait(timeout=5):
                return
            if session.attach_probe(connection):
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                Protocol.send_message(connection, Protocol.encode(
                    MessageType.DESKTOP_CHANNEL, {'id': session.id, 'kind': 'latency'}))
                session.run_probe()
            return
        if not session.approved.wait(timeout=5) or not session.attach(connection):
            return
        try:
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            Protocol.send_message(connection, Protocol.encode(MessageType.DESKTOP_CHANNEL, {'id': session.id}))
            session.run(self.handler)
        finally:
            session.close()

    def _dial_probe(self, session):
        while not session.closed.is_set():
            self._probe_connection(session)
            if session.closed.wait(2):
                return

    def _probe_connection(self, session):
        connection = None
        try:
            connection = socket.create_connection(session.control.getpeername(), timeout=3)
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            greeting = Protocol.receive_message(connection, max_size=65536)
            if not greeting or greeting[0] != MessageType.SCREEN_INFO:
                return
            Protocol.send_message(connection, Protocol.encode(MessageType.DESKTOP_CHANNEL, {
                'id': session.id, 'token': session.token, 'kind': 'latency',
            }))
            ack = Protocol.receive_message(connection, max_size=65536)
            if ack != (MessageType.DESKTOP_CHANNEL, {'id': session.id, 'kind': 'latency'}):
                return
            if session.attach_probe(connection):
                session.run_probe()
        except OSError:
            pass
        finally:
            if connection is not None:
                close_socket(connection)

    def end(self, reason=None, notify_peer=True):
        session = self.session
        if session is None:
            return
        self.session = None
        self._reported_ready = False
        self._reported_first_frame = False
        self._reported_network_rtt = False
        session.close()
        if self.viewer is not None:
            self.viewer.destroy()
            self.viewer = None
        if notify_peer:
            self._signal(session, MessageType.DESKTOP_STOP, {'id': session.id})
        if reason:
            self.log(reason, "INFO")
        self._changed()

    def close(self):
        self.end()
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None
            self._signals.put(None)
