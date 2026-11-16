"""
服务器端主程序
"""
import threading
from pynput.mouse import Button
from pynput.keyboard import Key
from protocol import Protocol, MessageType
from input_handler import InputHandler
from server_connections import ServerConnections


class Server:
    """服务器类"""
    
    def __init__(self, host: str = '0.0.0.0', port: int = 9999, client=None):
        """
        初始化服务器
        
        Args:
            host: 监听地址
            port: 监听端口
        """
        self.host = host
        self.port = port
        self.client = client
        self.input_handler = InputHandler()
        self._input_lock = threading.RLock()
        self._target = None
        self._held_keys = {}
        self._held_buttons = set()

        # 获取屏幕尺寸
        try:
            import tkinter as tk
            root = tk.Tk()
            self.screen_width = root.winfo_screenwidth()
            self.screen_height = root.winfo_screenheight()
            root.destroy()
        except:
            self.screen_width = 1920
            self.screen_height = 1080
        
        self.input_handler.set_screen_size(self.screen_width, self.screen_height)
        self.connections = ServerConnections(
            self.screen_width, self.screen_height, self._handle_message, self._log)

    @staticmethod
    def _log(message, level="INFO"):
        print(message)

    def start(self):
        try:
            self.connections.start(self.host, self.port)
        except OSError as exc:
            print(f"绑定端口失败：{exc}")
            return

        print(f"监听地址：{self.host}:{self.port}，等待客户端连接")
        self._print_local_ips()
        try:
            self._start_input_listeners()
            self.connections.wait()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _print_local_ips(self):
        """打印本机IP地址"""
        try:
            import subprocess
            result = subprocess.run(['ip', 'addr', 'show'], capture_output=True, text=True)
            lines = result.stdout.split('\n')
            
            ips = []
            for line in lines:
                if 'inet ' in line and '127.0.0.1' not in line:
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        ip = parts[1].split('/')[0]
                        ips.append(ip)
            
            if ips:
                print(f"🌐 本机IP地址:")
                for ip in ips:
                    print(f"   - {ip}")
            else:
                print(f"⚠️  未找到网络IP地址")
        except Exception as e:
            print(f"⚠️  获取IP地址失败: {e}")
    
    def stop(self):
        with self._input_lock:
            self._release_target()
        self.connections.stop()
        self.input_handler.stop_listeners()
        print("服务器已关闭")

    def _start_input_listeners(self):
        """启动输入监听器"""
        # 鼠标监听（用于鼠标移动和点击转发）
        self.input_handler.start_mouse_listener(
            on_move=self._on_mouse_move,
            on_click=self._on_mouse_click,
            on_scroll=self._on_mouse_scroll
        )
        
        # 键盘监听（用于热键和输入转发）
        self.input_handler.start_keyboard_listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release
        )
        
        print("右方向键切换到客户端，客户端左方向键切回服务端")

    def _active_target(self):
        if self._target not in self.connections.clients():
            self._target = None
            self._held_keys.clear()
            self._held_buttons.clear()
        return self._target

    def _send(self, peer, msg_type, data):
        try:
            Protocol.send_message(peer.socket, Protocol.encode(msg_type, data))
            return True
        except OSError:
            return False

    def _release_target(self):
        target = self._active_target()
        if target:
            for data in self._held_keys.values():
                self._send(target, MessageType.KEY_RELEASE,
                           dict(data, cancelled=True) if data.get('name') == 'caps_lock' else data)
            for button in self._held_buttons:
                self._send(target, MessageType.MOUSE_CLICK, {'button': button, 'pressed': False})
        self._target = None
        self._held_keys.clear()
        self._held_buttons.clear()

    def _on_mouse_move(self, x, y):
        with self._input_lock:
            target = self._active_target()
            if target:
                self._send(target, MessageType.MOUSE_MOVE, {'x': x, 'y': y, 'force': False})

    def _on_mouse_click(self, x, y, button, pressed):
        with self._input_lock:
            target = self._active_target()
            if not target:
                return
            name = 'left' if button == Button.left else 'right' if button == Button.right else 'middle'
            if pressed:
                self._held_buttons.add(name)
            elif name in self._held_buttons:
                self._held_buttons.remove(name)
            else:
                return
            self._send(target, MessageType.MOUSE_CLICK, {'button': name, 'pressed': pressed})

    def _on_mouse_scroll(self, x, y, dx, dy):
        with self._input_lock:
            target = self._active_target()
            if target:
                self._send(target, MessageType.MOUSE_SCROLL, {'dx': dx, 'dy': dy})

    def _on_key_press(self, key):
        with self._input_lock:
            target = self._active_target()
            if target:
                data = InputHandler.serialize_key(key)
                if self._send(target, MessageType.KEY_PRESS, data):
                    self._held_keys.setdefault(str(key), data)
            elif key == Key.right:
                clients = self.connections.clients()
                if self.client:
                    clients = tuple(peer for peer in clients if self.client in (
                        peer.name, peer.address[0], f"{peer.address[0]}:{peer.address[1]}"))
                if len(clients) != 1:
                    print("无法唯一确定切换目标，请在 GUI 中选择，或用 --client 指定设备")
                    return
                target = clients[0]
                if self._send(target, MessageType.MOUSE_MOVE, {
                    'x': target.width // 2, 'y': target.height // 2, 'force': True,
                }):
                    self._target = target
                    print(f"已切换到 {target.label}")

    def _on_key_release(self, key):
        with self._input_lock:
            target = self._active_target()
            if target:
                data = InputHandler.serialize_key(key)
                self._held_keys.pop(str(key), None)
                self._send(target, MessageType.KEY_RELEASE, data)

    def _handle_message(self, peer, msg_type, data):
        if msg_type == MessageType.CLIPBOARD:
            print(f"{peer.label} 发来了文件。请使用图形界面接收文件")
            return
        with self._input_lock:
            target = self._active_target()
            if msg_type == MessageType.MOUSE_MOVE and data.get('force'):
                if target is peer:
                    self._release_target()
                    self.input_handler.simulate_mouse_move(data['x'], data['y'])
                return
            if target or self.connections.clients() != (peer,):
                return
            if msg_type == MessageType.MOUSE_MOVE:
                self.input_handler.simulate_mouse_move(data['x'], data['y'])
            elif msg_type == MessageType.MOUSE_CLICK:
                self.input_handler.simulate_mouse_click(data['button'], data['pressed'])
            elif msg_type == MessageType.MOUSE_SCROLL:
                self.input_handler.simulate_mouse_scroll(data['dx'], data['dy'])
            elif msg_type == MessageType.KEY_PRESS:
                self.input_handler.simulate_key_press(data)
            elif msg_type == MessageType.KEY_RELEASE:
                self.input_handler.simulate_key_release(data)


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='输入设备共享服务器')
    parser.add_argument('--host', default='0.0.0.0', help='监听地址')
    parser.add_argument('--port', type=int, default=9999, help='监听端口')
    
    parser.add_argument('--client', help='键鼠切换目标的设备名、IP 或 IP:端口')
    args = parser.parse_args()
    
    server = Server(host=args.host, port=args.port, client=args.client)
    server.start()


if __name__ == '__main__':
    main()
