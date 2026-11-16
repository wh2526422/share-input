"""
客户端主程序
"""
import socket
import platform
import threading
from pynput.mouse import Button
from protocol import Protocol, MessageType
from input_handler import InputHandler
from clipboard_sync import ClipboardSync


class Client:
    """客户端类"""
    
    def __init__(self, server_host: str, server_port: int = 9999):
        """
        初始化客户端
        
        Args:
            server_host: 服务器地址
            server_port: 服务器端口
        """
        self.server_host = server_host
        self.server_port = server_port
        self.socket = None
        self.running = False
        self.input_handler = InputHandler()
        self.clipboard_sync = None
        self.control_mode = False  # False=本地控制, True=远程控制
        
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
        self.remote_screen_width = 1920
        self.remote_screen_height = 1080
        
        # 单向模式：如果为True，客户端只接受控制，不主动控制服务器
        self.passive_mode = True  # 默认启用单向模式
    
    def start(self):
        """启动客户端"""
        print(f"🔌 正在连接到服务器...")
        print(f"📍 服务器地址: {self.server_host}:{self.server_port}")
        
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(10)  # 10秒超时
            
            # 开启 TCP Keep-Alive
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            try:
                if platform.system() == "Linux":
                    self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 5)
                    self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
                    self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
                elif platform.system() == "Windows":
                    self.socket.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 5000, 3000))
                elif platform.system() == "Darwin":
                    self.socket.setsockopt(socket.IPPROTO_TCP, 0x10, 5)
            except AttributeError:
                pass
            
            print(f"⏳ 连接中...")
            self.socket.connect((self.server_host, self.server_port))
            self.socket.settimeout(None)  # 连接后取消超时
            self.running = True
            
            print(f"\n✅ 已连接到服务器: {self.server_host}:{self.server_port}")
            print(f"🖥️  本地屏幕: {self.screen_width}x{self.screen_height}")
            
            # 接收屏幕信息
            result = Protocol.receive_message(self.socket)
            if result:
                msg_type, data = result
                if msg_type == MessageType.SCREEN_INFO:
                    self.remote_screen_width = data['width']
                    self.remote_screen_height = data['height']
                    print(f"🖥️  远程屏幕: {self.remote_screen_width}x{self.remote_screen_height}")
            
            # 发送本地屏幕信息给服务器
            screen_data = {
                'width': self.screen_width,
                'height': self.screen_height,
                'name': socket.gethostname(),
                'system': f"{platform.system()} {platform.release()}"
            }
            msg = Protocol.encode(MessageType.SCREEN_INFO, screen_data)
            Protocol.send_message(self.socket, msg)
            
            # 显示工作模式
            if self.passive_mode:
                print(f"🎮 工作模式: 单向控制（只接受服务器控制）")
                print(f"💡 服务器可以控制此客户端")
            else:
                print(f"🎮 工作模式: 双向控制")
                print(f"💡 鼠标移到左边缘可切换到控制服务器")
            
            # 启动剪贴板同步
            self.clipboard_sync = ClipboardSync(self._on_clipboard_change, publish_initial=False)
            self.clipboard_sync.start()
            
            # 启动输入监听
            self._start_input_listeners()
            
            # 开始心跳发送线程
            import threading
            threading.Thread(target=self._heartbeat_loop, daemon=True).start()
            
            # 接收消息
            self._receive_loop()
            
        except KeyboardInterrupt:
            print("\n⚠️  客户端关闭中...")
        except socket.timeout:
            print(f"\n❌ 连接超时！")
            print(f"💡 请检查:")
            print(f"   1. 服务器是否正在运行")
            print(f"   2. IP地址是否正确: {self.server_host}")
            print(f"   3. 防火墙是否阻止了端口 {self.server_port}")
            print(f"   4. 两台设备是否在同一局域网")
        except ConnectionRefusedError:
            print(f"\n❌ 连接被拒绝！")
            print(f"💡 请检查:")
            print(f"   1. 服务器是否正在运行")
            print(f"   2. 端口号是否正确: {self.server_port}")
            print(f"   3. 服务器防火墙设置")
        except socket.gaierror:
            print(f"\n❌ 无法解析服务器地址: {self.server_host}")
            print(f"💡 请检查IP地址是否正确")
        except Exception as e:
            print(f"\n❌ 连接失败: {e}")
            print(f"💡 错误类型: {type(e).__name__}")
        finally:
            self.stop()
    
    def stop(self):
        """停止客户端"""
        self.running = False
        
        if self.clipboard_sync:
            self.clipboard_sync.stop()
        
        self.input_handler.stop_listeners()
        
        if self.socket:
            self.socket.close()
        
        print("👋 客户端已关闭")
    
    def _start_input_listeners(self):
        """启动输入监听器"""
        # 鼠标监听
        self.input_handler.start_mouse_listener(
            on_move=self._on_mouse_move,
            on_click=self._on_mouse_click,
            on_scroll=self._on_mouse_scroll
        )
        
        # 键盘监听
        self.input_handler.start_keyboard_listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release
        )
    
    def _on_mouse_move(self, x, y):
        """鼠标移动事件"""
        # 检查是否在右边缘（Windows右边缘切换回Ubuntu）
        edge_threshold = 10
        at_right_edge = x >= self.screen_width - edge_threshold
        
        # 如果在右边缘且当前是本地控制（被服务器控制），发送位置让服务器检测
        if at_right_edge and not self.control_mode:
            # 发送当前位置，服务器会检测并切换回去
            data = {'x': x, 'y': y, 'force': False}
            msg = Protocol.encode(MessageType.MOUSE_MOVE, data)
            try:
                Protocol.send_message(self.socket, msg)
            except:
                pass
            return
        
        # 单向模式：客户端不主动控制服务器（其他边缘不处理）
        if self.passive_mode:
            return
    
    def _on_mouse_click(self, x, y, button, pressed):
        """鼠标点击事件"""
        if self.passive_mode:
            return
        
        if self.control_mode:
            button_name = 'left' if button == Button.left else 'right' if button == Button.right else 'middle'
            data = {'button': button_name, 'pressed': pressed}
            msg = Protocol.encode(MessageType.MOUSE_CLICK, data)
            try:
                Protocol.send_message(self.socket, msg)
            except:
                pass
    
    def _on_mouse_scroll(self, x, y, dx, dy):
        """鼠标滚轮事件"""
        if self.passive_mode:
            return
        
        if self.control_mode:
            data = {'dx': dx, 'dy': dy}
            msg = Protocol.encode(MessageType.MOUSE_SCROLL, data)
            try:
                Protocol.send_message(self.socket, msg)
            except:
                pass
    
    
    def _on_key_press(self, key):
        """按键按下事件"""
        from pynput.keyboard import Key
        
        # 检测左箭头键切换回 Ubuntu
        if key == Key.left and not self.control_mode:
            # 发送切换回 Ubuntu 的信号
            print(f"\n🔄 切换回 Ubuntu")
            print(f"   热键触发: Left Arrow")
            
            # 将鼠标移到 Ubuntu 中心
            remote_x = self.remote_screen_width // 2
            remote_y = self.remote_screen_height // 2
            
            print(f"   发送到 Ubuntu: x={remote_x}, y={remote_y}")
            
            data = {'x': remote_x, 'y': remote_y, 'force': True}
            msg = Protocol.encode(MessageType.MOUSE_MOVE, data)
            try:
                Protocol.send_message(self.socket, msg)
                print(f"   ✅ 已发送切换命令\n")
            except Exception as e:
                print(f"   ❌ 发送失败: {e}")
            return  # 不转发这个按键
        
        # 单向模式下不转发其他按键
        if self.passive_mode:
            return
    
    def _on_key_release(self, key):
        """按键释放事件"""
        # 单向模式下不转发
        if self.passive_mode:
            return
    
    def _on_clipboard_change(self, content: str):
        """剪贴板变化事件"""
        data = {'content': content, 'text': True}
        msg = Protocol.encode(MessageType.CLIPBOARD, data)
        try:
            Protocol.send_message(self.socket, msg)
            print(f"📋 剪贴板已同步: {content[:50]}...")
        except:
            pass
    
    def _heartbeat_loop(self):
        """发送心跳循环"""
        import time
        while self.running and self.socket:
            try:
                msg = Protocol.encode(MessageType.HEARTBEAT, {})
                Protocol.send_message(self.socket, msg)
                time.sleep(2)
            except:
                break

    def _receive_loop(self):
        """接收消息循环"""
        import select
        import time
        last_receive_time = time.time()
        
        while self.running:
            try:
                # 使用 select 检查有没有数据来，1秒超时
                ready, _, _ = select.select([self.socket], [], [], 1.0)
                
                if not ready:
                    # 如果超过 6 秒什么都没收到（包括心跳），认为是断网
                    if time.time() - last_receive_time > 6.0:
                        print("⚠️  服务器无响应，连接已断开")
                        break
                    continue
                
                result = Protocol.receive_message(self.socket)
                if not result:
                    print("❌ 服务器断开连接")
                    break
                
                last_receive_time = time.time()
                msg_type, data = result
                self._handle_message(msg_type, data)
                
            except Exception as e:
                print(f"接收消息错误: {e}")
                break
    
    def _handle_message(self, msg_type: MessageType, data: dict):
        """处理接收到的消息"""
        if msg_type == MessageType.MOUSE_MOVE:
            # 检查是否强制移动（从服务器切换过来）
            if data.get('force', False):
                self.control_mode = False
                print(f"🔄 切换到本地控制模式 (Windows)")
                print(f"   目标位置: x={data['x']}, y={data['y']}")
                if data.get('move_cursor', True):
                    self.input_handler.simulate_mouse_move(data['x'], data['y'])
                print(f"   ✅ 鼠标已移动")
            elif not self.control_mode:
                # 在本地控制模式下，模拟鼠标移动
                self.input_handler.simulate_mouse_move(data['x'], data['y'])
        
        elif msg_type == MessageType.MOUSE_CLICK:
            if not self.control_mode:
                self.input_handler.simulate_mouse_click(data['button'], data['pressed'])
        
        elif msg_type == MessageType.MOUSE_SCROLL:
            if not self.control_mode:
                self.input_handler.simulate_mouse_scroll(data['dx'], data['dy'])
        
        elif msg_type == MessageType.KEY_PRESS:
            if not self.control_mode:
                self.input_handler.simulate_key_press(data)
        
        elif msg_type == MessageType.KEY_RELEASE:
            if not self.control_mode:
                self.input_handler.simulate_key_release(data)
        
        elif msg_type == MessageType.CLIPBOARD:
            content = data.get('content')
            if not isinstance(content, str):
                return
            if content.startswith('FILE:') and data.get('text') is not True:
                print("命令行客户端请使用图形界面接收文件")
                return
            self.clipboard_sync.set_clipboard(content)
            print(f"📋 接收剪贴板: {content[:50]}...")


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='输入设备共享客户端')
    parser.add_argument('server', help='服务器地址')
    parser.add_argument('--port', type=int, default=9999, help='服务器端口')
    
    args = parser.parse_args()
    
    client = Client(server_host=args.server, server_port=args.port)
    client.start()


if __name__ == '__main__':
    main()
