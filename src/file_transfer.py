"""
文件传输处理模块（带进度条）
"""
import json
import os
import secrets
import socket
import tempfile
import qt_runtime as tk
from qt_runtime import connect_signal, W, C

class filedialog:
    @staticmethod
    def askopenfilename(title="选择文件", parent=None):
        return W.QFileDialog.getOpenFileName(parent, title)[0]
    @staticmethod
    def asksaveasfilename(title="保存文件", initialfile="", defaultextension="", parent=None):
        return W.QFileDialog.getSaveFileName(parent, title, initialfile)[0]
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from protocol import MessageType, Protocol


CHUNK_SIZE = 256 * 1024
LEGACY_LIMIT = 256 * 1024
OFFER_TIMEOUT = 120
TRANSFER_TIMEOUT = 30
ACCEPT = b'\x01'
DECLINE = b'\x00'


def _legacy_message(filename):
    with open(filename, 'rb') as source:
        data = source.read(LEGACY_LIMIT + 1)
    if len(data) > LEGACY_LIMIT:
        raise ValueError("文件超过旧版传输上限（256 KiB），请更新对端")
    info = {'name': os.path.basename(filename), 'size': len(data), 'data': data.hex()}
    return Protocol.encode(MessageType.CLIPBOARD, {'content': f"FILE:{json.dumps(info)}"}), len(data)


def _file_metadata(data):
    name = data.get('name')
    size = data.get('size')
    if (not isinstance(name, str) or '\x00' in name
            or type(size) is not int or not 0 <= size < 1 << 63):
        raise ValueError("文件信息无效")
    name = name.replace('\\', '/').rsplit('/', 1)[-1]
    if name in ('', '.', '..'):
        raise ValueError("文件名无效")
    return name, size


def _send_bytes(connection, filename, size, progress):
    with open(filename, 'rb') as source:
        if os.fstat(source.fileno()).st_size != size:
            raise OSError("源文件大小已变化，请重新发送")
        sent = 0
        while sent < size:
            block = source.read(min(CHUNK_SIZE, size - sent))
            if not block:
                raise OSError("源文件读取不完整")
            remaining = memoryview(block)
            while remaining:
                count = connection.send(remaining)
                if not count:
                    raise OSError("文件通道已断开")
                remaining = remaining[count:]
                sent += count
                progress(sent)
    if connection.recv(1) != ACCEPT:
        raise OSError("未收到对端的保存确认")


def _receive_bytes(connection, filename, size, progress):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=os.path.dirname(filename) or '.',
                                         prefix='.share-input-', suffix='.part',
                                         delete=False) as target:
            temporary = target.name
            received = 0
            while received < size:
                block = connection.recv(min(CHUNK_SIZE, size - received))
                if not block:
                    raise OSError("文件未接收完整")
                target.write(block)
                received += len(block)
                progress(received)
        os.replace(temporary, filename)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


class _FileProgress:
    def __init__(self, root, title, name, size, count=1):
        self._bytes = [0] * count
        self._results = [None] * count
        self._lock = threading.Lock()
        if root is None:
            return
        self.window = window = W.QDialog(root)
        window.setWindowTitle(title)
        layout = W.QVBoxLayout(window)
        layout.addWidget(W.QLabel(name))
        bar = W.QProgressBar(); bar.setRange(0,1000); layout.addWidget(bar)
        status = W.QLabel(); layout.addWidget(status)
        timer = C.QTimer(window)
        def refresh():
            with self._lock:
                done = all(result is not None for result in self._results)
                success = self._results.count(True)
                transferred = sum(self._bytes)
            total = size * count
            bar.setValue(int(1000 * transferred / total) if total else 1000 if done else 0)
            status.setText(f"{transferred:,} / {total:,} 字节" + (f" · 完成 {success}/{count}" if done else ""))
            if done:
                timer.stop()
                C.QTimer.singleShot(1500, window.close)
        connect_signal(timer.timeout, refresh); timer.start(100)
        window.setAttribute(C.Qt.WA_DeleteOnClose)
        window.show()

    def update(self, index, transferred):
        with self._lock:
            self._bytes[index] = transferred

    def finish(self, index, success):
        with self._lock:
            self._results[index] = success


@dataclass
class _FileOffer:
    filename: str
    size: int
    progress: object
    claimed: threading.Event = field(default_factory=threading.Event)
    result: Future = field(default_factory=Future)


class FileTransferController:
    def __init__(self, root, log, post, server=False):
        self.root = root
        self.log = log
        self.post = post
        self.server = server
        self._lock = threading.Lock()
        self._offers = {}
        self._channels = set()
        self._closed = False

    def send_files(self, recipients):
        if not recipients:
            return
        filename = filedialog.askopenfilename(title="选择要发送的文件", parent=self.root)
        if not filename:
            return
        try:
            size = os.path.getsize(filename)
        except OSError as exc:
            self.log(f"无法读取文件：{exc}", "ERROR")
            return
        progress = _FileProgress(self.root, "发送文件", os.path.basename(filename), size, len(recipients))

        def send(index, control, label, token):
            def update(transferred):
                progress.update(index, transferred)

            success = False
            try:
                if not token:
                    message, transferred = _legacy_message(filename)
                    Protocol.send_message(control, message)
                    update(transferred)
                    self.log(f"已发送给 {label}：{os.path.basename(filename)}")
                elif self.server:
                    self._offer_file(control, filename, size, update)
                else:
                    self._upload_file(control, token, filename, size, update)
                success = True
                if token:
                    self.log(f"{label} 已保存文件：{os.path.basename(filename)}")
            except (OSError, ValueError) as exc:
                self.log(f"发送文件到 {label} 失败：{exc}", "ERROR")
            finally:
                progress.finish(index, success)

        def run():
            with ThreadPoolExecutor() as pool:
                for index, (control, label, token) in enumerate(recipients):
                    pool.submit(send, index, control, label, token)

        threading.Thread(target=run, daemon=True).start()

    def _offer_file(self, control, filename, size, progress):
        offer_id = secrets.token_hex(16)
        key = control, offer_id
        offer = _FileOffer(filename, size, progress)
        with self._lock:
            if self._closed:
                raise OSError("文件传输已关闭")
            self._offers[key] = offer
        try:
            Protocol.send_message(control, Protocol.encode(MessageType.FILE_OFFER, {
                'id': offer_id, 'name': os.path.basename(filename), 'size': size,
            }))
            if not offer.claimed.wait(OFFER_TIMEOUT):
                with self._lock:
                    if self._offers.pop(key, None) is not None:
                        raise TimeoutError("等待对端选择保存位置超时")
            offer.result.result()
        finally:
            with self._lock:
                self._offers.pop(key, None)

    def _open_channel(self, control, token, **data):
        connection = socket.create_connection(control.getpeername(), timeout=6)
        try:
            greeting = Protocol.receive_message(connection, max_size=65536)
            if not greeting or greeting[0] != MessageType.SCREEN_INFO:
                raise OSError("文件通道握手失败")
            Protocol.send_message(connection, Protocol.encode(MessageType.FILE_CHANNEL, {
                'token': token, **data,
            }))
            return connection
        except (OSError, ValueError):
            connection.close()
            raise

    def _upload_file(self, control, token, filename, size, progress):
        with self._open_channel(control, token, direction='upload',
                                name=os.path.basename(filename), size=size) as connection:
            self._track(connection)
            try:
                connection.settimeout(OFFER_TIMEOUT + 6)
                if connection.recv(1) != ACCEPT:
                    raise OSError("对端未接受文件")
                connection.settimeout(TRANSFER_TIMEOUT)
                _send_bytes(connection, filename, size, progress)
            finally:
                self._untrack(connection)

    def _choose_destination(self, name, size, label):
        chosen = Future()

        def choose():
            if chosen.cancelled():
                return
            try:
                filename = filedialog.asksaveasfilename(
                    title=f"保存来自 {label} 的文件（{size:,} 字节）",
                    initialfile=name, defaultextension=os.path.splitext(name)[1], parent=self.root,
                )
            except tk.TclError:
                filename = ''
            if chosen.set_running_or_notify_cancel():
                try:
                    progress = _FileProgress(self.root, "接收文件", name, size) if filename else None
                except tk.TclError:
                    chosen.set_result(('', None))
                else:
                    chosen.set_result((filename, progress))

        self.post(choose)
        try:
            return chosen.result(timeout=OFFER_TIMEOUT)
        except TimeoutError:
            if chosen.cancel():
                raise
            return chosen.result()

    def accept_channel(self, peer, connection, data):
        direction = data.get('direction')
        if direction == 'download':
            self._serve_download(peer.socket, connection, data)
        elif direction == 'upload':
            self._receive_upload(peer.label, connection, data)

    def _serve_download(self, control, connection, data):
        offer_id = data.get('id')
        if not isinstance(offer_id, str):
            return
        with self._lock:
            offer = self._offers.pop((control, offer_id), None)
            if offer is None:
                return
            offer.claimed.set()
        try:
            if data.get('accept') is not True:
                raise OSError("对端取消保存")
            connection.settimeout(TRANSFER_TIMEOUT)
            connection.sendall(ACCEPT)
            _send_bytes(connection, offer.filename, offer.size, offer.progress)
        except (OSError, ValueError) as exc:
            offer.result.set_exception(exc)
        else:
            offer.result.set_result(None)

    def _receive_upload(self, label, connection, data):
        progress = None
        success = False
        try:
            name, size = _file_metadata(data)
            filename, progress = self._choose_destination(name, size, label)
            if not filename:
                connection.sendall(DECLINE)
                return
            connection.settimeout(TRANSFER_TIMEOUT)
            connection.sendall(ACCEPT)
            self._save(connection, filename, size, progress)
            success = True
        except (OSError, ValueError) as exc:
            self.log(f"接收来自 {label} 的文件失败：{exc}", "ERROR")
        finally:
            if progress is not None:
                progress.finish(0, success)

    def receive_offer(self, control, token, label, data):
        if not isinstance(data, dict) or not isinstance(data.get('id'), str):
            return

        def run():
            progress = None
            success = False
            try:
                name, size = _file_metadata(data)
                filename, progress = self._choose_destination(name, size, label)
                with self._open_channel(control, token, direction='download',
                                        id=data['id'], accept=bool(filename)) as connection:
                    if filename:
                        self._track(connection)
                        try:
                            if connection.recv(1) != ACCEPT:
                                raise OSError("文件发送请求已失效")
                            connection.settimeout(TRANSFER_TIMEOUT)
                            self._save(connection, filename, size, progress)
                            success = True
                        finally:
                            self._untrack(connection)
            except (OSError, ValueError) as exc:
                self.log(f"接收来自 {label} 的文件失败：{exc}", "ERROR")
            finally:
                if progress is not None:
                    progress.finish(0, success)

        threading.Thread(target=run, daemon=True).start()

    def _save(self, connection, filename, size, progress):
        _receive_bytes(connection, filename, size, lambda count: progress.update(0, count))
        self.log(f"文件已保存：{os.path.basename(filename)}（{size:,} 字节）")
        try:
            connection.sendall(ACCEPT)
        except OSError:
            self.log("文件已保存，但发送保存确认失败", "WARNING")

    def _track(self, connection):
        with self._lock:
            if self._closed:
                raise OSError("文件传输已关闭")
            self._channels.add(connection)

    def _untrack(self, connection):
        with self._lock:
            self._channels.discard(connection)

    def close(self):
        with self._lock:
            self._closed = True
            offers = tuple(self._offers.values())
            self._offers.clear()
            channels = tuple(self._channels)
        for offer in offers:
            offer.result.set_exception(OSError("文件传输已关闭"))
            offer.claimed.set()
        for connection in channels:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class FileTransfer:
    """文件传输处理器"""
    
    @staticmethod
    def send_file(socket, log_callback, parent_window=None):
        FileTransfer.send_files(((socket, '对方'),), log_callback, parent_window)

    @staticmethod
    def send_files(recipients, log_callback, parent_window=None):
        """选择一次文件，将同一帧分别发送到选中的连接。"""
        log = lambda message, level='INFO': log_callback(message, level)
        FileTransferController(parent_window, log, lambda fn: parent_window.after(0, fn) if parent_window else fn()).send_files(
            tuple((connection, label, '') for connection, label in recipients))

    @staticmethod
    def receive_file(file_info_json, log_callback, parent_window=None, on_complete=None):
        def choose():
            try:
                info = json.loads(file_info_json)
                name, size = _file_metadata(info)
                data = bytes.fromhex(info['data'])
                if len(data) != size or size > LEGACY_LIMIT:
                    raise ValueError('旧版文件大小无效')
                filename = filedialog.asksaveasfilename(title='保存文件', initialfile=name, parent=parent_window)
                if not filename: return
            except (ValueError, KeyError) as exc:
                log_callback(f'接收文件失败：{exc}', 'ERROR'); return
            def save():
                try:
                    with open(filename, 'wb') as target: target.write(data)
                    log_callback(f'文件已保存：{name}')
                    if on_complete: parent_window.after(0, on_complete)
                except OSError as exc: log_callback(f'保存文件失败：{exc}', 'ERROR')
            threading.Thread(target=save, daemon=True).start()
        if parent_window is not None: parent_window.after(0, choose)
        else: log_callback('接收文件需要图形主窗口', 'ERROR')
