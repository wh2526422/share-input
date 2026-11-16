"""
GUI 服务器端
带有图形界面的输入设备共享服务器
"""
from src import qt_runtime as tk

import threading
import queue
import sys
import os
import platform
from datetime import datetime
from pynput.mouse import Button

# 添加src目录到路径，使用 insert(1) 避免覆盖 sys.path[0] 且优先级高于 site-packages
src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src')
if src_path not in sys.path:
    sys.path.insert(1, src_path)

from protocol import Protocol, MessageType
from desktop import DesktopController
from device_info import local_ipv4_addresses
from ui import MainView, choose_devices, GREEN, MUTED
from input_handler import InputHandler
from server_connections import ServerConnections
from file_transfer import FileTransfer, FileTransferController
from desktop_stream import RemoteInput


class ServerGUI:
    """服务器GUI类"""
    
    def __init__(self, parent=None):
        self._ui_thread_id = threading.get_ident()
        self._log_queue = queue.Queue(maxsize=1000)
        self._log_poll_id = None
        self.is_standalone = False
        if parent is None:
            self.root = tk.Tk()
            self.root.title(f"输入设备共享 - 服务端 ({platform.system()})")
            self.root.geometry("750x550")
            self.is_standalone = True
            self.root.protocol("WM_DELETE_WINDOW", self.stop)
        else:
            self.root = parent
        
        self.running = False
        self._stopped = False
        self._input_started = False
        self._ui_events = queue.SimpleQueue()
        self._listed_clients = ()
        self._displayed_clipboard = None
        self._input_lock = threading.RLock()
        self._keyboard_targets = ()
        self._mouse_targets = ()
        self._held_keys = {}
        self._held_buttons = set()
        self._remote_inputs = {}

        # 输入处理器
        self.input_handler = InputHandler()

        # 获取屏幕尺寸
        self.screen_width = self.root.winfo_screenwidth()
        self.screen_height = self.root.winfo_screenheight()
        self.input_handler.set_screen_size(self.screen_width, self.screen_height)
        
        # 主显示器尺寸（用于多显示器映射）
        self.primary_screen_width = 1920  # 每次开始共享时询问，沿用上次值作为默认值
        self.primary_screen_height = 1080
        
        self.desktop = DesktopController(
            self.root, self.input_handler, self.log, server=True,
            on_active=self._stop_input_sharing, on_changed=self._update_controls,
        )
        self.files = FileTransferController(self.root, self.log, self._ui_events.put, server=True)
        self.connections = ServerConnections(
            self.screen_width, self.screen_height, self._handle_message, self.log,
            desktop_channel=self.desktop.accept_channel,
            file_channel=lambda *args: self.files.accept_channel(*args),
            input_sharing=True, on_disconnect=self._release_client_input,
        )

        # 创建GUI
        self._create_gui()
        self._drain_logs()
        
        # 自动启动服务器
        if self.is_standalone:
            self.root.after(100, self.start_server)
    
    def _create_gui(self):
        self.view = MainView(self, 'server')
        self._update_controls()

    def log(self, message, level="INFO"):
        """添加日志"""
        if threading.get_ident() != self._ui_thread_id:
            try:
                self._log_queue.put_nowait((message, level))
            except queue.Full:
                pass
            return
        try:
            timestamp = datetime.now().strftime("%H:%M:%S")
            log_msg = f"[{timestamp}] {level}: {message}\n"
            
            self.log_text.insert(tk.END, log_msg)
            self.log_text.see(tk.END)
            
            # 同时更新底部状态
            if level != 'PERF':
                self.bottom_status.config(text=message)
        except Exception:
            pass
    
    def _drain_logs(self):
        for _ in range(100):
            try:
                message, level = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self.log(message, level)
        for _ in range(100):
            try:
                callback = self._ui_events.get_nowait()
            except queue.Empty:
                break
            callback()
        if self.running:
            self._refresh_connections()
            content = self.connections.clipboard_content
            if content is not None and content != self._displayed_clipboard:
                self._displayed_clipboard = content
                self.update_clipboard_display(content)
        self._log_poll_id = self.root.after(100, self._drain_logs)

    def update_clipboard_display(self, content):
        self.clipboard_text.delete(1.0, tk.END)
        self.clipboard_text.insert(1.0, content)

    @property
    def server_socket(self):
        return self.connections.listener

    def _refresh_connections(self):
        clients = self.connections.clients()
        if clients == self._listed_clients:
            return
        self._listed_clients = clients
        session = self.desktop.session
        if session is not None and not any(peer.socket is session.control for peer in clients):
            self.desktop.end("投屏设备已断开", notify_peer=False)
        if clients and not self._input_started:
            try:
                self._start_input_listeners()
                self._input_started = True
            except Exception as exc:
                self.log(f"无法启动键鼠监听：{exc}", "ERROR")
        with self._input_lock:
            self._keyboard_targets = tuple(peer for peer in self._keyboard_targets if peer in clients)
            self._mouse_targets = tuple(peer for peer in self._mouse_targets if peer in clients)
            if not self._keyboard_targets:
                self._held_keys.clear()
            if not self._mouse_targets:
                self._held_buttons.clear()
        for row in self.client_list.get_children():
            self.client_list.delete(row)
        for peer in clients:
            address = peer.address[0]
            self.client_list.insert('', tk.END, text=peer.name or peer.address[0],
                                    values=(address, peer.system or '—', f'{peer.width}×{peer.height}'))
        self._update_controls()

    def _update_controls(self):
        count = len(self._listed_clients)
        state = tk.NORMAL if count else tk.DISABLED
        self.status_label.config(
            text="● 服务端运行中 · 端口 9999" if self.running else "● 服务端已停止",
            foreground=GREEN if self.running else MUTED)
        self.service_btn.config(text="停止服务" if self.running else "启动服务")
        self.device_count_label.config(text=f"在线设备  {count}")
        if count:
            self.empty_label.place_forget()
        else:
            self.empty_label.place(relx=.5, rely=.5, anchor=tk.CENTER)
        keyboard_count = len(self._keyboard_targets)
        mouse_count = len(self._mouse_targets)
        sharing_state = tk.DISABLED if self.desktop.active else state
        receiving_keyboard = 'keyboard' in self._remote_inputs
        receiving_mouse = 'mouse' in self._remote_inputs
        self.keyboard_share_btn.config(
            text="被共享中" if receiving_keyboard else "停止共享" if keyboard_count else "开始共享",
            state=tk.DISABLED if receiving_keyboard else sharing_state)
        self.mouse_share_btn.config(
            text="被共享中" if receiving_mouse else "停止共享" if mouse_count else "开始共享",
            state=tk.DISABLED if receiving_mouse else sharing_state)
        self.keyboard_target_label.config(text="来自客户端" if receiving_keyboard else f"共享到 {keyboard_count} 台设备" if keyboard_count else "未共享")
        self.mouse_target_label.config(text="来自客户端" if receiving_mouse else f"共享到 {mouse_count} 台设备" if mouse_count else "未共享")
        self.sharing_label.config(text="投屏期间暂停本机输入共享" if self.desktop.active else "")
        busy = self.desktop.session is not None
        self.desktop_btn.config(
            text="结束投屏" if busy else "查看并控制",
            state=tk.NORMAL if busy or any(peer.desktop for peer in self._listed_clients) else tk.DISABLED,
        )
        self.send_file_btn.config(state=state)
        self.send_msg_btn.config(state=state)
        self.info_text.delete('1.0', tk.END)
        info = f"服务端屏幕 {self.screen_width}×{self.screen_height} · TCP 9999\n\n"
        info += '\n'.join(f"{peer.label}\n{peer.system} · {peer.width}×{peer.height}\n"
                          for peer in self._listed_clients)
        self.info_text.insert('1.0', info)

    def toggle_server(self):
        if not self.running:
            self.start_server()
            return
        self.running = False
        self._stop_input_sharing()
        self.desktop.end()
        self.connections.stop()
        self.files.close()
        self.files = FileTransferController(self.root, self.log, self._ui_events.put, server=True)
        self._refresh_connections()
        self._update_controls()
        self.log("服务端已停止")

    def _choose_clients(self, title, always_prompt=False, desktop_only=False):
        clients = self.connections.clients()
        if desktop_only:
            clients = tuple(peer for peer in clients if peer.desktop)
        if not clients:
            self.log("请先连接客户端", "WARNING")
            return ()
        if len(clients) == 1 and not always_prompt:
            return clients

        selected = choose_devices(self.root, title, clients, multiple=not desktop_only)
        return self._connected_targets(selected)

    def _connected_targets(self, selected):
        clients = self.connections.clients()
        available = tuple(peer for peer in selected if peer in clients)
        if len(available) != len(selected):
            self.log("部分所选设备已断开" if available else "所选设备已断开", "WARNING")
        return available

    def _ask_primary_resolution(self):
        """询问主显示器分辨率"""
        from qt_runtime import W
        width, ok = W.QInputDialog.getInt(self.root, '主显示器分辨率', '宽度', self.primary_screen_width, 1, 65535)
        if not ok: return False
        height, ok = W.QInputDialog.getInt(self.root, '主显示器分辨率', '高度', self.primary_screen_height, 1, 65535)
        if not ok: return False
        self.primary_screen_width, self.primary_screen_height = width, height
        return True

    def toggle_keyboard_sharing(self):
        if 'keyboard' in self._remote_inputs:
            return
        if self._keyboard_targets:
            self._stop_keyboard_sharing()
        else:
            targets = self._choose_clients("选择键盘共享设备")
            if not targets:
                return
            if self.desktop.active:
                return
            with self._input_lock:
                self._stop_remote_sharing('keyboard')
                self._keyboard_targets = targets
                self._notify_receiving(targets, 'keyboard', True)
        self._update_controls()

    def toggle_mouse_sharing(self):
        if 'mouse' in self._remote_inputs:
            return
        if self._mouse_targets:
            self._stop_mouse_sharing()
        else:
            targets = self._choose_clients("选择鼠标共享设备")
            if not targets:
                return
            if not self._ask_primary_resolution():
                return
            targets = self._connected_targets(targets)
            if self.desktop.active:
                return
            with self._input_lock:
                self._stop_remote_sharing('mouse')
                self._mouse_targets = targets
                self._notify_receiving(targets, 'mouse', True)
        self._update_controls()

    def _stop_keyboard_sharing(self):
        with self._input_lock:
            for data in self._held_keys.values():
                self._send_to_clients(self._keyboard_targets, MessageType.KEY_RELEASE,
                                      dict(data, cancelled=True) if data.get('name') == 'caps_lock' else data)
            self._held_keys.clear()
            self._notify_receiving(self._keyboard_targets, 'keyboard', False)
            self._keyboard_targets = ()

    def _stop_mouse_sharing(self):
        with self._input_lock:
            for button in self._held_buttons:
                self._send_to_clients(self._mouse_targets, MessageType.MOUSE_CLICK,
                                      {'button': button, 'pressed': False})
            self._held_buttons.clear()
            self._notify_receiving(self._mouse_targets, 'mouse', False)
            self._mouse_targets = ()

    def _notify_receiving(self, targets, device, active):
        targets = tuple(peer for peer in targets if getattr(peer, 'input_receive_state', False))
        if targets:
            self._send_to_clients(targets, MessageType.INPUT_SHARE,
                                  {'kind': 'state', 'device': device, 'active': active, 'receiving': True})

    def _stop_input_sharing(self):
        self._stop_keyboard_sharing()
        self._stop_mouse_sharing()
        with self._input_lock:
            for device in tuple(self._remote_inputs):
                self._stop_remote_sharing(device)

    def _stop_remote_sharing(self, device, notify=True):
        owner = self._remote_inputs.pop(device, None)
        if owner:
            peer, remote = owner
            remote.release()
            if notify:
                self._send_to_clients((peer,), MessageType.INPUT_SHARE,
                                      {'kind': 'state', 'device': device, 'active': False})
            self._ui_events.put(self._update_controls)

    def _release_client_input(self, peer):
        with self._input_lock:
            for device, (owner, remote) in tuple(self._remote_inputs.items()):
                if owner is peer:
                    self._stop_remote_sharing(device, notify=False)

    def _receive_shared_input(self, peer, data):
        if not isinstance(data, dict):
            return
        device = data.get('device')
        if device not in ('keyboard', 'mouse'):
            return
        with self._input_lock:
            kind = data.get('kind')
            if kind == 'start':
                if self.desktop.active:
                    self._send_to_clients((peer,), MessageType.INPUT_SHARE,
                                          {'kind': 'state', 'device': device, 'active': False})
                    return
                self._stop_remote_sharing(device)
                if device == 'keyboard':
                    self._stop_keyboard_sharing()
                else:
                    self._stop_mouse_sharing()
                remote = RemoteInput(self.input_handler, {
                    'left': 0, 'top': 0, 'width': self.screen_width, 'height': self.screen_height,
                })
                self._remote_inputs[device] = peer, remote
                self._send_to_clients((peer,), MessageType.INPUT_SHARE,
                                      {'kind': 'state', 'device': device, 'active': True})
                self._ui_events.put(self._update_controls)
            elif device in self._remote_inputs and self._remote_inputs[device][0] is peer:
                if kind == 'stop':
                    self._stop_remote_sharing(device)
                elif ((device == 'keyboard' and kind == 'key')
                      or (device == 'mouse' and kind in ('move', 'click', 'scroll'))):
                    self._remote_inputs[device][1].apply(data)

    def toggle_desktop(self):
        if self.desktop.session is not None:
            self.desktop.end()
            return
        targets = self._choose_clients("选择要查看的客户端", desktop_only=True)
        if targets:
            peer = targets[0]
            self.desktop.request(peer.socket, peer.label)

    def send_file(self):
        targets = self._choose_clients("选择文件接收设备", always_prompt=True)
        if targets:
            self.files.send_files(tuple((peer.socket, peer.label, peer.file_token) for peer in targets))

    def send_text_message(self):
        message = self.message_entry.get().strip()
        if not message:
            return
        targets = self._choose_clients("选择消息接收设备")
        if self._send_to_clients(targets, MessageType.TEXT_MESSAGE, {'content': message, 'sender': 'server'}):
            self.add_message_to_display(f"我: {message}", "server")
            self.message_entry.delete(0, tk.END)

    def add_message_to_display(self, message, sender):
        """添加消息到显示区域"""
        self.message_text.config(state='normal')
        timestamp = datetime.now().strftime("%H:%M:%S")
        
        if sender == 'server':
            self.message_text.insert(tk.END, f"[{timestamp}] {message}\n", 'server')
            self.message_text.tag_config('server', foreground='blue')
        else:
            self.message_text.insert(tk.END, f"[{timestamp}] {message}\n", 'client')
            self.message_text.tag_config('client', foreground='green')
        
        self.message_text.see(tk.END)
        self.message_text.config(state='disabled')
    
    def start_server(self):
        if self.running or self._stopped:
            return
        try:
            self.connections.start('0.0.0.0', 9999)
        except OSError as exc:
            self.log(f"服务器错误（监听 0.0.0.0:9999）：{exc}", "ERROR")
            return
        self.running = True
        self.log("服务器启动成功，等待客户端连接", "SUCCESS")
        self._update_controls()
        threading.Thread(target=self._show_local_ips, daemon=True).start()

    def _show_local_ips(self):
        """显示本机IP地址"""
        for address in local_ipv4_addresses():
            self.log(f"本机IP: {address}")
    


    def _start_input_listeners(self):
        """启动输入监听"""
        self.input_handler.start_mouse_listener(
            on_move=self._on_mouse_move,
            on_click=self._on_mouse_click,
            on_scroll=self._on_mouse_scroll
        )
        
        self.input_handler.start_keyboard_listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release
        )
        
        self.log("输入监听已启动")
        self.log("💡 点击按钮切换控制模式")
    
    def _send_to_clients(self, targets, msg_type, data):
        if not targets:
            return False
        packet = Protocol.encode(msg_type, data)
        sent = False
        for peer in targets:
            try:
                Protocol.send_message(peer.socket, packet)
            except OSError as exc:
                self.log(f"发送到 {peer.label} 失败：{exc}", "WARNING")
            else:
                sent = True
        return sent

    def _on_mouse_move(self, x, y):
        with self._input_lock:
            for peer in self._mouse_targets:
                mapped_x = int(x * peer.width / self.primary_screen_width)
                mapped_y = int(y * peer.height / self.primary_screen_height)
                self._send_to_clients((peer,), MessageType.MOUSE_MOVE, {
                    'x': max(0, min(mapped_x, peer.width - 1)),
                    'y': max(0, min(mapped_y, peer.height - 1)),
                    'force': False,
                })

    def _on_mouse_click(self, x, y, button, pressed):
        with self._input_lock:
            if not self._mouse_targets:
                return
            name = 'left' if button == Button.left else 'right' if button == Button.right else 'middle'
            if pressed:
                self._held_buttons.add(name)
            elif name not in self._held_buttons:
                return
            else:
                self._held_buttons.discard(name)
            self._send_to_clients(self._mouse_targets, MessageType.MOUSE_CLICK, {'button': name, 'pressed': pressed})

    def _on_mouse_scroll(self, x, y, dx, dy):
        with self._input_lock:
            if self._mouse_targets:
                self._send_to_clients(self._mouse_targets, MessageType.MOUSE_SCROLL, {'dx': dx, 'dy': dy})

    def _on_key_press(self, key):
        if self.desktop.forward_caps(key, True):
            return
        with self._input_lock:
            if not self._keyboard_targets:
                return
            data = InputHandler.serialize_key(key)
            if self._send_to_clients(self._keyboard_targets, MessageType.KEY_PRESS, data):
                self._held_keys.setdefault(str(key), data)

    def _on_key_release(self, key):
        if self.desktop.forward_caps(key, False):
            return
        with self._input_lock:
            if not self._keyboard_targets:
                return
            data = InputHandler.serialize_key(key)
            self._held_keys.pop(str(key), None)
            self._send_to_clients(self._keyboard_targets, MessageType.KEY_RELEASE, data)

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self.running = False
        self.desktop.close()
        self.files.close()
        self._stop_input_sharing()
        if self._log_poll_id is not None:
            self.root.after_cancel(self._log_poll_id)
            self._log_poll_id = None
        self.connections.stop()
        self.input_handler.stop_listeners()
        if self.is_standalone:
            self.root.destroy()

    def _handle_message(self, peer, msg_type, data):
        if msg_type == MessageType.INPUT_SHARE:
            self._receive_shared_input(peer, data)
            return
        if msg_type in (MessageType.DESKTOP_REQUEST, MessageType.DESKTOP_RESPONSE, MessageType.DESKTOP_STOP):
            if peer.desktop:
                self.desktop.receive(peer.socket, peer.label, msg_type, data)
            return
        if msg_type == MessageType.CLIPBOARD:
            content = data.get('content')
            if isinstance(content, str) and content.startswith("FILE:") and data.get('text') is not True:
                self._ui_events.put(lambda: FileTransfer.receive_file(content[5:], self.log, self.root))
            return
        if msg_type == MessageType.TEXT_MESSAGE:
            content = data.get('content')
            if isinstance(content, str):
                self._ui_events.put(lambda: self.add_message_to_display(f"{peer.label}: {content}", "client"))
            return

    def run(self):
        """运行GUI"""
        self.root.mainloop()


if __name__ == '__main__':
    app = ServerGUI()
    app.run()
