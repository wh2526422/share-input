"""
GUI 客户端 V2
带有服务器地址输入、连接历史和系统信息的客户端
"""
from src import qt_runtime as tk

import socket
import secrets
import queue
import threading
import sys
import os
import platform
from datetime import datetime
from pynput.mouse import Button
from pynput.keyboard import Key

# 添加src目录到路径
# 添加src目录到路径，使用 insert(1) 避免覆盖 sys.path[0] 且优先级高于 site-packages
import os
import sys
src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src')
if src_path not in sys.path:
    sys.path.insert(1, src_path)

from protocol import Protocol, MessageType
from desktop import DesktopController
from ui import MainView, GREEN, MUTED
from input_handler import InputHandler
from clipboard_sync import ClipboardSync
from config import Config
from file_transfer import FileTransfer, FileTransferController
from desktop_stream import COORD_MAX


class ClientGUI:
    """客户端GUI类"""
    def __init__(self, parent=None):
        # 记录 Tk 主线程，用于确保所有 UI 更新在主线程执行
        self._ui_thread_id = threading.get_ident()
        self._ui_events = queue.SimpleQueue()
        self._ui_poll_id = None
        self._disconnect_pending = None

        self.is_standalone = False
        if parent is None:
            self.root = tk.Tk()
            self.root.title(f"输入设备共享 - 客户端 ({platform.system()})")
            self.root.geometry("750x550")
            self.is_standalone = True
            self.root.protocol("WM_DELETE_WINDOW", self.stop)
        else:
            self.root = parent
        
        # 客户端状态
        self.socket = None
        self.running = False
        self.server_ip = ''
        self.connected = False
        self._desktop_supported = False
        self._file_token = ''
        self._sharing_supported = False
        self._receive_state_supported = False
        self._input_lock = threading.RLock()
        self._sharing = {'keyboard': False, 'mouse': False}
        self._receiving = {'keyboard': False, 'mouse': False}
        self._held_keys = {}
        self._held_buttons = set()
        self._next_key_code = 0
        self._input_messages = queue.Queue(maxsize=256)
        self._input_sender_closed = threading.Event()
        threading.Thread(target=self._send_shared_inputs, daemon=True).start()
        
        # 输入处理器
        self.input_handler = InputHandler()
        self.clipboard_sync = None
        self.config = Config()  # 配置管理器
        
        # 获取屏幕尺寸
        self.screen_width = self.root.winfo_screenwidth()
        self.screen_height = self.root.winfo_screenheight()
        self.input_handler.set_screen_size(self.screen_width, self.screen_height)
        
        self.remote_screen_width = 3840
        self.remote_screen_height = 1080
        self._mouse_source_width = self.screen_width
        self._mouse_source_height = self.screen_height
        
        self.desktop = DesktopController(
            self.root, self.input_handler, self.log, on_active=self._stop_input_sharing,
            on_changed=self._update_desktop_button,
        )
        self.files = FileTransferController(self.root, self.log, self._ui)

        # 创建GUI
        self._create_gui()
        self._drain_ui()
    
    def _create_gui(self):
        self.view = MainView(self, 'client')

    def _update_history_list(self):
        """更新历史记录列表"""
        history = self.config.get_server_history()
        if history:
            values = [f"{h['name']} ({h['ip']})" for h in history]
            self.history_combo['values'] = values
    
    def _on_history_selected(self, event):
        """历史记录被选中"""
        selection = self.history_combo.get()
        if selection:
            # 提取IP地址
            ip = selection.split('(')[1].split(')')[0]
            self.server_entry.delete(0, tk.END)
            self.server_entry.insert(0, ip)
    
    def _ui(self, fn, connection=None):
        """确保在 Tk 主线程执行 UI 操作"""
        def apply():
            if connection is None or self.socket is connection:
                fn()
        if threading.get_ident() == self._ui_thread_id:
            apply()
        else:
            self._ui_events.put(apply)

    def _drain_ui(self):
        for _ in range(100):
            try:
                callback = self._ui_events.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except tk.TclError:
                pass
        if not self._input_sender_closed.is_set():
            self._ui_poll_id = self.root.after(20, self._drain_ui)

    def _request_disconnect(self, connection):
        if self.socket is not connection or self._disconnect_pending is connection:
            return
        self._disconnect_pending = connection
        def disconnect_current():
            if self.socket is connection:
                self.disconnect()
        self._ui_events.put(disconnect_current)

    def log(self, message, level="INFO"):
        """添加日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_msg = f"[{timestamp}] {level}: {message}\n"

        def _do():
            try:
                self.log_text.insert(tk.END, log_msg)
                self.log_text.see(tk.END)
                if level != 'PERF':
                    self.bottom_status.config(text=message)
            except Exception:
                pass

        self._ui(_do)
    
    def update_clipboard_display(self, content):
        """更新剪贴板显示"""
        def _do():
            try:
                self.clipboard_text.delete(1.0, tk.END)
                self.clipboard_text.insert(1.0, content)
            except Exception:
                pass
        self._ui(_do)
    
    def update_info(self):
        """更新连接信息"""
        info = f"""
客户端信息
{'='*50}
系统: {platform.system()} {platform.release()}
本地屏幕: {self.screen_width}x{self.screen_height}
状态: {'已连接' if self.connected else '未连接'}

服务器信息
{'='*50}
"""
        if self.connected:
            info += f"地址: {self.server_ip}:9999\n"
            info += f"屏幕尺寸: {self.remote_screen_width}x{self.remote_screen_height}\n"
        else:
            info += "未连接\n"
        
        def _do():
            try:
                self.info_text.delete(1.0, tk.END)
                self.info_text.insert(1.0, info)
                self.peer_label.config(
                    text=f"服务端 {self.server_ip}\n{self.remote_screen_width} × {self.remote_screen_height} · 已连接"
                    if self.connected else "尚未连接服务端")
            except Exception:
                pass
        self._ui(_do)
    
    def send_file(self):
        """发送文件到服务端"""
        if not self.socket:
            self.log("未连接到服务器！", "WARNING")
            return
        
        self.files.send_files(((self.socket, f"服务端 {self.server_ip}", self._file_token),))

    def _update_desktop_button(self):
        busy = self.desktop.session is not None
        self.desktop_btn.config(
            text="结束投屏" if busy else "查看并控制",
            state=tk.NORMAL if busy or (self.connected and self._desktop_supported) else tk.DISABLED,
        )
        self._update_sharing_buttons()

    def _update_sharing_buttons(self):
        enabled = self.connected and self._sharing_supported and not self.desktop.active
        for device, button in (
                ('keyboard', self.keyboard_share_btn), ('mouse', self.mouse_share_btn)):
            receiving = self._receiving[device]
            button.config(text="被共享中" if receiving else "停止共享" if self._sharing[device] else "开始共享",
                          state=tk.NORMAL if enabled and not receiving else tk.DISABLED)
            getattr(self, device + '_target_label').config(
                text="来自服务端" if receiving else "共享到服务端" if self._sharing[device] else "未共享")
        self.sharing_label.config(text="投屏期间暂停本机输入共享" if self.desktop.active else "")

    def _queue_shared_input(self, data):
        connection = self.socket
        if connection is None:
            return
        try:
            self._input_messages.put_nowait((connection, data))
        except queue.Full:
            self._request_disconnect(connection)

    def _send_shared_inputs(self):
        while not self._input_sender_closed.is_set():
            try:
                connection, data = self._input_messages.get(timeout=0.2)
            except queue.Empty:
                continue
            if connection is not self.socket:
                continue
            try:
                Protocol.send_message(connection, Protocol.encode(MessageType.INPUT_SHARE, data))
            except OSError:
                self._request_disconnect(connection)

    def _toggle_input_sharing(self, device):
        if not self.connected or not self._sharing_supported or self.desktop.active or self._receiving[device]:
            return
        if device == 'mouse' and not self._sharing[device]:
            from qt_runtime import W
            width, ok = W.QInputDialog.getInt(self.root, '共享鼠标屏幕尺寸', '宽度', self._mouse_source_width, 1, 65535)
            if not ok:
                return
            height, ok = W.QInputDialog.getInt(self.root, '共享鼠标屏幕尺寸', '高度', self._mouse_source_height, 1, 65535)
            if not ok:
                return
            if (not self.connected or not self._sharing_supported or self.desktop.active
                    or self._receiving[device] or self._sharing[device]):
                return
            self._mouse_source_width, self._mouse_source_height = width, height
        with self._input_lock:
            if self._sharing[device]:
                self._sharing[device] = False
                self._queue_shared_input({'kind': 'stop', 'device': device})
                if device == 'keyboard':
                    self._held_keys.clear()
                else:
                    self._held_buttons.clear()
            else:
                self._queue_shared_input({'kind': 'start', 'device': device})
        self._update_sharing_buttons()

    def _stop_input_sharing(self):
        with self._input_lock:
            for device in self._sharing:
                if self._sharing[device]:
                    self._queue_shared_input({'kind': 'stop', 'device': device})
                self._sharing[device] = False
            self._held_keys.clear()
            self._held_buttons.clear()
        self._ui(self._update_sharing_buttons)

    def _receive_sharing_state(self, data, connection=None):
        if not isinstance(data, dict) or data.get('kind') != 'state':
            return
        device = data.get('device')
        if device not in self._sharing or type(data.get('active')) is not bool:
            return
        with self._input_lock:
            if connection is not None and connection is not self.socket:
                return
            if data.get('receiving') is True:
                if not self._receive_state_supported:
                    return
                self._receiving[device] = data['active']
                if data['active']:
                    if self._sharing[device]:
                        self._queue_shared_input({'kind': 'stop', 'device': device})
                    self._sharing[device] = False
                    if device == 'keyboard':
                        self._held_keys.clear()
                    else:
                        self._held_buttons.clear()
                self._ui(self._update_sharing_buttons)
                return
            active = data['active'] and not self.desktop.active
            self._sharing[device] = active
            if data['active'] and not active:
                self._queue_shared_input({'kind': 'stop', 'device': device})
            if not active:
                if device == 'keyboard':
                    self._held_keys.clear()
                else:
                    self._held_buttons.clear()
        self._ui(self._update_sharing_buttons)

    def toggle_desktop(self):
        if self.desktop.session is not None:
            self.desktop.end()
        elif self.connected and self._desktop_supported and self.socket is not None:
            self.desktop.request(self.socket, f"服务端 {self.server_ip}")
    

    def send_text_message(self):
        """发送文本消息"""
        if not self.socket:
            return
        
        message = self.message_entry.get().strip()
        if not message:
            return
        
        try:
            # 发送消息
            msg = Protocol.encode(MessageType.TEXT_MESSAGE, {'content': message, 'sender': 'client'})
            Protocol.send_message(self.socket, msg)
            
            # 显示在消息区域
            self.add_message_to_display(f"我: {message}", "client")
            
            # 清空输入框
            self.message_entry.delete(0, tk.END)
        except Exception as e:
            self.log(f"发送消息失败: {e}", "ERROR")
    
    def add_message_to_display(self, message, sender):
        """添加消息到显示区域"""
        timestamp = datetime.now().strftime("%H:%M:%S")

        def _do():
            try:
                self.message_text.config(state='normal')

                if sender == 'client':
                    self.message_text.insert(tk.END, f"[{timestamp}] {message}\n", 'client')
                    self.message_text.tag_config('client', foreground='blue')
                else:
                    self.message_text.insert(tk.END, f"[{timestamp}] {message}\n", 'server')
                    self.message_text.tag_config('server', foreground='green')

                self.message_text.see(tk.END)
                self.message_text.config(state='disabled')
            except Exception:
                pass

        self._ui(_do)
    
    def connect_to_server(self):
        """连接到服务器"""
        if self.connected:
            self.log("已经连接到服务器", "INFO")
            return
        
        self.server_ip = self.server_entry.get().strip()
        if not self.server_ip:
            self.log("请输入服务器地址！", "WARNING")
            return
        
        # 禁用连接按钮，并更新文本为连接中
        self.connect_btn.config(state=tk.DISABLED, text="连接中...")
        self.status_label.config(text="● 连接中", foreground="orange")
        
        def connect_thread():
            try:
                self.log(f"正在连接到服务器: {self.server_ip}:9999")
                
                self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.socket.settimeout(10)  # 10秒超时
                self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                
                # 开启 TCP Keep-Alive 以在断网时快速检测到断开
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                try:
                    if platform.system() == "Linux":
                        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 5)
                        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
                        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
                    elif platform.system() == "Windows":
                        self.socket.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 5000, 3000))
                    elif platform.system() == "Darwin":
                        self.socket.setsockopt(socket.IPPROTO_TCP, 0x10, 5) # TCP_KEEPALIVE = 0x10
                except AttributeError:
                    pass # 不支持该系统的特定参数时忽略
                
                self.socket.connect((self.server_ip, 9999))
                self.socket.settimeout(None)  # 取消超时
                self.running = True
                self.connected = True
                
                self.log("已连接到服务器", "SUCCESS")
                
                # 保存到历史
                self.config.add_server_history(self.server_ip, self.server_ip)
                self.root.after(0, self._update_history_list)
                
                # 更新GUI状态
                self.root.after(0, lambda: self.status_label.config(
                    text="● 已连接", foreground=GREEN))
                self.root.after(0, lambda: self.send_msg_btn.config(state=tk.NORMAL))
                self.root.after(0, lambda: self.connect_btn.config(state=tk.DISABLED, text="连接"))
                self.root.after(0, lambda: self.disconnect_btn.config(state=tk.NORMAL))
                
                # 接收服务器屏幕信息
                result = Protocol.receive_message(self.socket)
                if result:
                    msg_type, data = result
                    if msg_type == MessageType.SCREEN_INFO:
                        self.remote_screen_width = data['width']
                        self.remote_screen_height = data['height']
                        self._desktop_supported = data.get('desktop') is True
                        self._sharing_supported = data.get('input_sharing') is True
                        self._receive_state_supported = data.get('input_receive_state') is True
                        self._file_token = secrets.token_hex(32) if data.get('files') is True else ''
                        self.root.after(0, self._update_desktop_button)
                        self.log(f"服务器屏幕: {self.remote_screen_width}x{self.remote_screen_height}")
                
                # 发送本地屏幕信息和系统信息
                screen_data = {
                    'width': self.screen_width, 
                    'height': self.screen_height,
                    'desktop': True,
                    'name': socket.gethostname(),
                    'system': f"{platform.system()} {platform.release()}"
                }
                if self._receive_state_supported:
                    screen_data['input_receive_state'] = True
                if self._file_token:
                    screen_data['file_token'] = self._file_token
                msg = Protocol.encode(MessageType.SCREEN_INFO, screen_data)
                Protocol.send_message(self.socket, msg)
                if not self._file_token:
                    self.root.after(0, lambda: self.send_file_btn.config(state=tk.NORMAL))
                
                self.log(f"本地屏幕: {self.screen_width}x{self.screen_height}")
                self.log(f"系统: {platform.system()} {platform.release()}")
                
                # 启动剪贴板同步
                self.clipboard_sync = ClipboardSync(self._on_clipboard_change, publish_initial=False)
                self.clipboard_sync.start()
                
                # Mac 本地鼠标需要合并远端修饰键，也必须启动鼠标监听。
                self.root.after(0, self._start_input_listeners)
                
                # 更新信息显示
                self.root.after(0, self.update_info)
                
                # 开始心跳发送线程
                threading.Thread(target=self._heartbeat_loop, daemon=True).start()
                
                # 接收消息循环
                self._receive_loop()
                
            except socket.timeout:
                self.log("连接超时", "ERROR")
                self.log(f"连接超时，请检查服务器地址和网络", "ERROR")
                self.root.after(0, lambda: self.connect_btn.config(state=tk.NORMAL, text="连接"))
                self.root.after(0, lambda: self.status_label.config(text="● 未连接", foreground=MUTED))
                self.root.after(0, self.disconnect)
            except Exception as e:
                self.log(f"连接失败: {e}", "ERROR")
                self.log(f"无法连接到服务器:\n{e}", "ERROR")
                self.root.after(0, lambda: self.connect_btn.config(state=tk.NORMAL, text="连接"))
                self.root.after(0, lambda: self.status_label.config(text="● 未连接", foreground=MUTED))
                self.root.after(0, self.disconnect)
        
        threading.Thread(target=connect_thread, daemon=True).start()
    
    
    def stop(self):
        """完全停止客户端"""
        self._input_sender_closed.set()
        if self._ui_poll_id is not None:
            self.root.after_cancel(self._ui_poll_id)
            self._ui_poll_id = None
        self.files.close()
        self.disconnect()
        self.desktop.close()
        if hasattr(self, 'input_handler'):
            self.input_handler.stop_listeners()
            
        import sys
        if getattr(self, 'is_standalone', False):
            try:
                self.root.destroy()
            except:
                pass
            sys.exit(0)
            
    def disconnect(self):
        """断开连接"""
        connection, self.socket = self.socket, None
        self._disconnect_pending = None
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        self._stop_input_sharing()
        self._sharing_supported = False
        self._receive_state_supported = False
        self._receiving = {'keyboard': False, 'mouse': False}
        self.running = False
        self.connected = False
        self._desktop_supported = False
        self._file_token = ''
        self.desktop.end(notify_peer=False)
        self.input_handler.reset_remote_modifiers()
        
        if self.clipboard_sync:
            self.clipboard_sync.stop()
            self.clipboard_sync = None
        
        self.log("已断开连接", "INFO")

        # 更新GUI（必须在主线程）
        def _do():
            try:
                self.status_label.config(text="● 未连接", foreground=MUTED)
                self.send_file_btn.config(state=tk.DISABLED)
                self.send_msg_btn.config(state=tk.DISABLED)
                self.disconnect_btn.config(state=tk.DISABLED)
                self.connect_btn.config(state=tk.NORMAL, text="连接")
                self._update_desktop_button()
            except Exception:
                pass

        self._ui(_do)
        self._ui(self.update_info)
    
    def _heartbeat_loop(self, connection=None):
        """发送心跳循环"""
        import time
        connection = self.socket if connection is None else connection
        while self.running and self.socket is connection:
            try:
                msg = Protocol.encode(MessageType.HEARTBEAT, {})
                Protocol.send_message(connection, msg)
                time.sleep(2)
            except:
                break
    
    def _start_input_listeners(self):
        """启动输入监听"""
        if not self.connected:
            return
        self.input_handler.start_mouse_listener(
            on_move=self._on_mouse_move,
            on_click=self._on_mouse_click,
            on_scroll=self._on_mouse_scroll
        )
        self.input_handler.start_keyboard_listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release
        )
    
    def _on_mouse_move(self, x, y):
        with self._input_lock:
            if self._sharing['mouse'] and not self.desktop.active:
                self._queue_shared_input({'device': 'mouse', 'kind': 'move', **self._shared_point(x, y)})

    def _shared_point(self, x, y):
        return {
            'x': max(0, min(COORD_MAX, int(x * COORD_MAX / max(1, self._mouse_source_width - 1)))),
            'y': max(0, min(COORD_MAX, int(y * COORD_MAX / max(1, self._mouse_source_height - 1)))),
        }
    
    def _on_mouse_click(self, x, y, button, pressed):
        with self._input_lock:
            if not self._sharing['mouse'] or self.desktop.active:
                return
            name = 'left' if button == Button.left else 'right' if button == Button.right else 'middle'
            if pressed:
                self._held_buttons.add(name)
            elif name not in self._held_buttons:
                return
            else:
                self._held_buttons.remove(name)
            self._queue_shared_input({'device': 'mouse', 'kind': 'click', 'button': name,
                                      'pressed': pressed, **self._shared_point(x, y)})
    
    def _on_mouse_scroll(self, x, y, dx, dy):
        with self._input_lock:
            if self._sharing['mouse'] and not self.desktop.active:
                self._queue_shared_input({'device': 'mouse', 'kind': 'scroll',
                    'dx': max(-10, min(10, int(dx))), 'dy': max(-10, min(10, int(dy))),
                    **self._shared_point(x, y)})
    
    def _on_key_press(self, key):
        if self.desktop.forward_caps(key, True):
            return
        with self._input_lock:
            if not self._sharing['keyboard'] or self.desktop.active:
                return
            identity = str(key)
            if identity not in self._held_keys:
                self._next_key_code = (self._next_key_code + 1) & 0xffffffff
                self._held_keys[identity] = self._next_key_code, InputHandler.serialize_key(key)
            code, payload = self._held_keys[identity]
            self._queue_shared_input({'device': 'keyboard', 'kind': 'key',
                                      'code': code, 'key': payload, 'pressed': True})
    
    def _on_key_release(self, key):
        if self.desktop.forward_caps(key, False):
            return
        with self._input_lock:
            held = self._held_keys.pop(str(key), None)
            if held and self._sharing['keyboard']:
                data = {'device': 'keyboard', 'kind': 'key', 'code': held[0], 'pressed': False}
                payload = InputHandler.serialize_key(key)
                if 'caps_time_ms' in payload:
                    data['caps_time_ms'] = payload['caps_time_ms']
                self._queue_shared_input(data)
    
    def _on_clipboard_change(self, content, connection=None):
        """剪贴板变化事件"""
        connection = self.socket if connection is None else connection
        if connection is None or self.socket is not connection:
            return
        data = {'content': content, 'text': True}
        msg = Protocol.encode(MessageType.CLIPBOARD, data)
        try:
            Protocol.send_message(connection, msg)
            self.log(f"剪贴板已同步: {content[:30]}...")
            self._ui(lambda: self.update_clipboard_display(content), connection)
        except:
            pass
    
    def _receive_loop(self, connection=None):
        """接收消息循环"""
        import select
        import time
        connection = self.socket if connection is None else connection
        last_receive_time = time.time()

        while self.running and self.socket is connection:
            try:
                # 使用 select 检查有没有数据来，1秒超时
                ready, _, _ = select.select([connection], [], [], 1.0)
                
                if not ready:
                    # 如果超过 6 秒什么都没收到（包括心跳），认为是断网
                    if time.time() - last_receive_time > 6.0:
                        self.log("服务器无响应，连接已断开", "WARNING")
                        self._request_disconnect(connection)
                        break
                    continue
                
                result = Protocol.receive_message(connection)
                if self.socket is not connection:
                    return
                if not result:
                    self.log("服务器断开连接", "WARNING")
                    self._request_disconnect(connection)
                    break
                
                last_receive_time = time.time()
                msg_type, data = result
                self._handle_message(msg_type, data, connection)
                
            except Exception as e:
                if self.socket is not connection or not self.running:
                    return
                self.log(f"接收消息错误: {e}", "ERROR")
                self._request_disconnect(connection)
                break
    
    def _handle_message(self, msg_type, data, connection=None):
        """处理接收到的消息"""
        if msg_type == MessageType.INPUT_SHARE:
            if connection is self.socket and self._sharing_supported:
                self._receive_sharing_state(data, connection)
        elif msg_type == MessageType.FILE_CHANNEL:
            if connection is not None and connection is self.socket and self._file_token:
                if isinstance(data, dict) and data.get('ready') is True:
                    self._ui(lambda: self.send_file_btn.config(state=tk.NORMAL))
        elif msg_type == MessageType.FILE_OFFER:
            if connection is not None and connection is self.socket and self._file_token:
                self.files.receive_offer(connection, self._file_token, f"服务端 {self.server_ip}", data)
        elif msg_type in (MessageType.DESKTOP_REQUEST, MessageType.DESKTOP_RESPONSE, MessageType.DESKTOP_STOP):
            if connection is not None and connection is self.socket and self._desktop_supported:
                self.desktop.receive(connection, f"服务端 {self.server_ip}", msg_type, data)

        elif msg_type == MessageType.MOUSE_MOVE:
            if not data.get('force') or data.get('move_cursor', True):
                try:
                    self.input_handler.simulate_mouse_move(data['x'], data['y'])
                except Exception as e:
                    self.log(f"鼠标移动错误: {e}", "ERROR")

        elif msg_type == MessageType.MOUSE_CLICK:
            try:
                self.input_handler.simulate_mouse_click(data['button'], data['pressed'])
            except Exception as e:
                self.log(f"点击模拟失败: {e}", "ERROR")

        elif msg_type == MessageType.MOUSE_SCROLL:
            self.input_handler.simulate_mouse_scroll(data['dx'], data['dy'])

        elif msg_type == MessageType.KEY_PRESS:
            try:
                char = data.get('char', '').lower()
                if char in ('c', 'v'):
                    with self.input_handler.keyboard_controller.modifiers as modifiers:
                        if Key.cmd in modifiers:
                            self.log(f"收到组合键 Command+{char.upper()}")
                self.input_handler.simulate_key_press(data)
            except Exception as e:
                self.log(f"按键模拟失败: {e}", "ERROR")

        elif msg_type == MessageType.KEY_RELEASE:
            self.input_handler.simulate_key_release(data)

        elif msg_type == MessageType.CLIPBOARD:
            content = data.get('content')
            if not isinstance(content, str):
                return  # 无文本的剪贴板消息不能中断输入共享连接。
            if content.startswith("FILE:") and data.get('text') is not True:
                # 文件传输
                file_info_json = content[5:]
                FileTransfer.receive_file(file_info_json, self.log, self.root)
            else:
                # 普通剪贴板
                self.clipboard_sync.set_clipboard(content)
                self.log(f"接收剪贴板: {content[:30]}...")
                self._ui(lambda: self.update_clipboard_display(content), connection)
    
        
        elif msg_type == MessageType.TEXT_MESSAGE:
            content = data['content']
            self.add_message_to_display(f"服务端: {content}", "server")
            self.log(f"收到消息: {content[:30]}...")
    
    def run(self):
        """运行GUI"""
        self.root.mainloop()


if __name__ == '__main__':
    app = ClientGUI()
    app.run()
