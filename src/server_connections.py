"""GUI 和命令行服务端共用的客户端连接和剪贴板同步。"""
import select
import socket
import threading
import time
from dataclasses import dataclass
from hmac import compare_digest

from clipboard_sync import ClipboardSync
from protocol import MessageType, Protocol


@dataclass(frozen=True, eq=False)
class ClientConnection:
    socket: socket.socket
    address: tuple[str, int]
    width: int
    height: int
    system: str
    name: str
    desktop: bool = False
    file_token: str = ''
    input_receive_state: bool = False

    @property
    def label(self):
        address = f'{self.address[0]}:{self.address[1]}'
        return f'{self.name} ({address})' if self.name else address


class ServerConnections:
    def __init__(self, width, height, on_message, log, desktop_channel=None, file_channel=None,
                 input_sharing=False, on_disconnect=None):
        screen_info = {'width': width, 'height': height}
        if desktop_channel is not None:
            screen_info['desktop'] = True
        if file_channel is not None:
            screen_info['files'] = True
        if input_sharing:
            screen_info['input_sharing'] = True
            screen_info['input_receive_state'] = True
        self._on_disconnect = on_disconnect
        self._screen_info = Protocol.encode(MessageType.SCREEN_INFO, screen_info)
        self._desktop_channel = desktop_channel
        self._file_channel = file_channel
        self._heartbeat = Protocol.encode(MessageType.HEARTBEAT, {})
        self._on_message = on_message
        self._log = log
        self._lock = threading.Lock()
        self._clipboard_lock = threading.Lock()
        self._sockets = set()
        self._clients = {}
        self._pending_clipboard = {}
        self._clipboard_content = None
        self._clipboard_packet = None
        self._clipboard = ClipboardSync(self._publish_clipboard)
        self.listener = None
        self._accept_thread = None

    def start(self, host, port):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
            listener.listen()
            listener.settimeout(0.5)
        except OSError:
            listener.close()
            raise
        self.listener = listener
        self._clipboard.start()
        self._accept_thread = threading.Thread(target=self._accept, args=(listener,), daemon=True)
        self._accept_thread.start()

    def clients(self):
        with self._lock:
            return tuple(self._clients.values())

    @property
    def clipboard_content(self):
        with self._lock:
            return self._clipboard_content

    def wait(self):
        self._accept_thread.join()

    def stop(self):
        with self._clipboard_lock, self._lock:
            listener, self.listener = self.listener, None
            sockets = tuple(self._sockets)
            self._sockets.clear()
            self._clients.clear()
            self._pending_clipboard.clear()
        if listener:
            listener.close()
        for connection in sockets:
            self._close(connection)
        self._clipboard.stop()
        if self._accept_thread:
            self._accept_thread.join(timeout=1)

    @staticmethod
    def _close(connection):
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()

    def _accept(self, listener):
        while self.listener is listener:
            try:
                connection, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                if self.listener is not listener:
                    connection.close()
                    break
                self._sockets.add(connection)
            threading.Thread(target=self._serve, args=(connection, address), daemon=True).start()

    def _serve(self, connection, address):
        peer = None
        try:
            connection.settimeout(6)
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            Protocol.send_message(connection, self._screen_info)
            response = Protocol.receive_message(connection, max_size=65536)
            if response and response[0] == MessageType.DESKTOP_CHANNEL and self._desktop_channel:
                self._desktop_channel(connection, address, response[1])
                return
            if response and response[0] == MessageType.FILE_CHANNEL and self._file_channel:
                self._serve_file_channel(connection, address, response[1])
                return
            if not response or response[0] != MessageType.SCREEN_INFO:
                return
            _, data = response
            width, height = data['width'], data['height']
            if type(width) is not int or type(height) is not int or min(width, height) < 1:
                return
            token = data.get('file_token')
            if not (self._file_channel and isinstance(token, str) and len(token) == 64
                    and all(char in '0123456789abcdef' for char in token)):
                token = ''
            peer = ClientConnection(connection, address, width, height,
                                    str(data.get('system', '')), str(data.get('name', '')),
                                    data.get('desktop') is True, token,
                                    data.get('input_receive_state') is True)
            with self._lock:
                if connection not in self._sockets:
                    return
                self._clients[connection] = peer
                if self._clipboard_packet is not None:
                    self._pending_clipboard[connection] = self._clipboard_packet
            self._log(f"客户端已连接：{peer.label} · {width}×{height}", "SUCCESS")
            if peer.file_token:
                Protocol.send_message(connection, Protocol.encode(MessageType.FILE_CHANNEL, {'ready': True}))
            self._receive(peer)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            if self.listener is not None:
                self._log(f"客户端连接错误（{address[0]}:{address[1]}）：{exc}", "WARNING")
        finally:
            with self._lock:
                registered = self._clients.pop(connection, None)
                self._pending_clipboard.pop(connection, None)
                self._sockets.discard(connection)
            self._close(connection)
            if registered:
                if self._on_disconnect:
                    self._on_disconnect(peer)
                self._log(f"客户端已断开：{peer.label}", "INFO")

    def _serve_file_channel(self, connection, address, data):
        token = data.get('token') if isinstance(data, dict) else None
        if not isinstance(token, str) or len(token) != 64 or not token.isascii():
            return
        with self._lock:
            peer = next((peer for peer in self._clients.values()
                         if peer.file_token and peer.address[0] == address[0]
                         and compare_digest(peer.file_token, token)), None)
        if peer is not None:
            self._file_channel(peer, connection, data)

    def _receive(self, peer):
        connection = peer.socket
        last_receive = time.monotonic()
        next_heartbeat = last_receive
        while self.listener is not None:
            with self._lock:
                clipboard = self._pending_clipboard.pop(connection, None)
            if clipboard is not None:
                Protocol.send_message(connection, clipboard)

            now = time.monotonic()
            if now >= next_heartbeat:
                Protocol.send_message(connection, self._heartbeat)
                next_heartbeat = now + 2
            ready, _, _ = select.select([connection], [], [], 0.1)
            if not ready:
                if time.monotonic() - last_receive > 6:
                    return
                continue
            result = Protocol.receive_message(connection)
            if not result:
                return
            last_receive = time.monotonic()
            msg_type, data = result
            if msg_type == MessageType.HEARTBEAT or not isinstance(data, dict):
                continue
            if msg_type == MessageType.CLIPBOARD:
                content = data.get('content')
                if not isinstance(content, str):
                    continue
                if data.get('text') is True or not content.startswith('FILE:'):
                    self._publish_clipboard(content, peer)
                    continue
            self._on_message(peer, msg_type, data)

    def _publish_clipboard(self, content, source=None):
        # 多端同时复制时，服务端按接收顺序决定最终内容。每条连接只保留最新一份。
        with self._clipboard_lock:
            if self.listener is None or content == self._clipboard_content:
                return
            if source is None:
                if content != self._clipboard.last_content:
                    return
            else:
                self._clipboard.set_clipboard(content)
            packet = Protocol.encode(MessageType.CLIPBOARD, {'content': content, 'text': True})
            with self._lock:
                self._clipboard_content = content
                self._clipboard_packet = packet
                for connection in self._clients:
                    self._pending_clipboard[connection] = packet
