"""
输入事件处理模块
"""
from pynput import mouse, keyboard
from pynput.mouse import Button, Controller as MouseController
from pynput.keyboard import Key, Controller as KeyboardController
import threading
import platform
import logging
from key_mapping import normalize_key_data
from key_timing import add_caps_time, source_event_time
from typing import Callable, Dict, Any


class InputHandler:
    """输入事件处理器"""
    
    def __init__(self):
        if platform.system() == 'Darwin':
            from macos_keyboard import MacKeyboardController
            from macos_mouse import MacMouseController
            self.keyboard_controller = MacKeyboardController()
            self.mouse_controller = MacMouseController(self.keyboard_controller)
        else:
            self.keyboard_controller = KeyboardController()
            self.mouse_controller = MouseController()
        self.mouse_listener = None
        self.keyboard_listener = None
        self.enabled = True
        self.screen_width = 1920
        self.screen_height = 1080
        
    def set_screen_size(self, width: int, height: int):
        """设置屏幕尺寸"""
        self.screen_width = width
        self.screen_height = height
    
    def start_mouse_listener(self, on_move: Callable, on_click: Callable, on_scroll: Callable):
        """
        启动鼠标监听器
        
        Args:
            on_move: 鼠标移动回调
            on_click: 鼠标点击回调
            on_scroll: 鼠标滚轮回调
        """
        # GUI 重连会再次调用；复用仍在运行的 tap，避免叠加拦截器。
        if self.mouse_listener is not None and self.mouse_listener.is_alive():
            return

        def on_move_wrapper(x, y):
            if self.enabled:
                on_move(x, y)
        
        def on_click_wrapper(x, y, button, pressed):
            if self.enabled:
                on_click(x, y, button, pressed)
        
        def on_scroll_wrapper(x, y, dx, dy):
            if self.enabled:
                on_scroll(x, y, dx, dy)
        
        options = {}
        if platform.system() == 'Darwin':
            options['darwin_intercept'] = self.keyboard_controller.merge_mouse_modifiers
        self.mouse_listener = mouse.Listener(
            on_move=on_move_wrapper,
            on_click=on_click_wrapper,
            on_scroll=on_scroll_wrapper,
            **options
        )
        self.mouse_listener.start()
    
    def start_keyboard_listener(self, on_press: Callable, on_release: Callable):
        """
        启动键盘监听器
        
        Args:
            on_press: 按键按下回调
            on_release: 按键释放回调
        """
        if getattr(self, 'keyboard_listener', None) is not None and self.keyboard_listener.is_alive():
            return

        def on_press_wrapper(key):
            if self.enabled:
                on_press(key)
        
        def on_release_wrapper(key):
            if self.enabled:
                on_release(key)
        
        if platform.system() == 'Darwin':
            from macos_listener import MacKeyboardListener
            listener_class = MacKeyboardListener
        else:
            listener_class = keyboard.Listener
            if platform.system() == 'Windows':
                import ctypes
                get_message_time = ctypes.WinDLL('user32', use_last_error=True).GetMessageTime
                get_message_time.argtypes = []
                get_message_time.restype = ctypes.c_int32

                class TimedWindowsListener(listener_class):
                    def _process(self, wparam, lparam):
                        # Use the queued native message timestamp, not callback
                        # execution time after Python/GUI scheduling delays.
                        with source_event_time(get_message_time() & 0xffffffff):
                            return super()._process(wparam, lparam)

                listener_class = TimedWindowsListener
            if platform.system() == 'Linux':
                class TimedXorgListener(listener_class):
                    # Timing must not depend on this optional pynput mapping.
                    if hasattr(listener_class, '_KEYPAD_KEYS'):
                        _KEYPAD_KEYS = dict(listener_class._KEYPAD_KEYS)
                        _KEYPAD_KEYS.update({
                            0xFFAB: keyboard.KeyCode.from_char('+', vk=0xFFAB),
                            0xFFAD: keyboard.KeyCode.from_char('-', vk=0xFFAD),
                        })

                    def _handle_message(self, display, event, *args):
                        # 使用 X server 记录的发生时间，回调排队或发送阻塞不会拉长短按。
                        with source_event_time(getattr(event, 'time', None)):
                            return super()._handle_message(display, event, *args)
                listener_class = TimedXorgListener
        self.keyboard_listener = listener_class(
            on_press=on_press_wrapper,
            on_release=on_release_wrapper
        )
        self.keyboard_listener.start()
    
    def stop_listeners(self):
        """停止所有监听器"""
        self.reset_remote_modifiers()
        if self.mouse_listener:
            self.mouse_listener.stop()
        if self.keyboard_listener:
            self.keyboard_listener.stop()

    def reset_remote_modifiers(self):
        if platform.system() == 'Darwin':
            self.keyboard_controller.reset_remote_modifiers()

    def cancel_pending_caps_lock(self):
        if platform.system() == 'Darwin':
            self.keyboard_controller.cancel_caps_lock()
    
    def simulate_mouse_move(self, x: int, y: int):
        """模拟鼠标移动"""
        self.enabled = False
        self.mouse_controller.position = (x, y)
        self.enabled = True
    
    def simulate_mouse_click(self, button_name: str, pressed: bool, click_count=None):
        """模拟鼠标点击"""
        self.enabled = False
        try:
            button_map = {
                'left': Button.left,
                'right': Button.right,
                'middle': Button.middle
            }
            button = button_map.get(button_name, Button.left)
            
            if pressed:
                if platform.system() == 'Darwin' and click_count is not None:
                    self.mouse_controller.press(button, click_count=click_count)
                else:
                    self.mouse_controller.press(button)
            else:
                self.mouse_controller.release(button)
        except Exception as e:
            pass  # print(f"鼠标点击错误: {e}")
        finally:
            self.enabled = True
    
    def simulate_mouse_scroll(self, dx: int, dy: int):
        """模拟鼠标滚轮"""
        self.enabled = False
        self.mouse_controller.scroll(dx, dy)
        self.enabled = True
    
    def simulate_key_press(self, key_data: Dict[str, Any]):
        """模拟按键按下"""
        self.enabled = False
        try:
            key = self._parse_key(key_data)
            if key:
                if platform.system() == 'Darwin' and key == Key.caps_lock:
                    self.keyboard_controller.press(key, caps_time_ms=key_data.get('caps_time_ms'))
                else:
                    self.keyboard_controller.press(key)
        except Exception as e:
            logging.getLogger(__name__).exception("模拟键盘事件失败: %r", key_data)
        finally:
            self.enabled = True
    
    def simulate_key_release(self, key_data: Dict[str, Any]):
        """模拟按键释放"""
        self.enabled = False
        try:
            if key_data.get('cancelled') is True:
                self.cancel_pending_caps_lock()
            key = self._parse_key(key_data)
            if key:
                if platform.system() == 'Darwin' and key == Key.caps_lock:
                    self.keyboard_controller.release(key, caps_time_ms=key_data.get('caps_time_ms'))
                else:
                    self.keyboard_controller.release(key)
        except Exception as e:
            logging.getLogger(__name__).exception("模拟键盘事件失败: %r", key_data)
        finally:
            self.enabled = True
    
    def _parse_key(self, key_data: Dict[str, Any]):
        """解析按键数据"""
        try:
            system = platform.system()
            key_data = normalize_key_data(key_data, system)
            key_type = key_data.get('type', 'unknown')
            
            # 普通字符键
            if key_type == 'char':
                char = key_data['char']
                shortcut_char = key_data.get('shortcut_char', char)
                if (system == 'Linux' and key_data.get('platform') == 'Darwin'
                        and key_data.get('shortcut') is True and isinstance(shortcut_char, str)
                        and len(shortcut_char) == 1 and 0x20 <= ord(shortcut_char) <= 0x7e):
                    # Xorg pynput 用 XSendEvent 发送字符，且其状态掩码不包含 Super。
                    # 投屏快捷键走 XTEST，交给 X server 使用实际按住的修饰键。
                    keysym = ord(shortcut_char)
                    if keysym in getattr(self.keyboard_controller, 'keyboard_mapping', {}):
                        return keyboard.KeyCode.from_vk(keysym)
                return char
            
            # 特殊键（Ctrl, Alt, Shift, Tab, CapsLock等）
            elif key_type == 'special':
                key_name = key_data['name']
                # 尝试从Key枚举中获取
                try:
                    return getattr(Key, key_name)
                except AttributeError:
                    pass  # 未知按键
                    return None
            
            # 虚拟键码
            elif key_type == 'vk':
                return keyboard.KeyCode.from_vk(key_data['vk'])
            
            else:
                return None
        except Exception as e:
            pass  # 错误已忽略
            return None
    
    @staticmethod
    def serialize_key(key):
        """序列化按键对象"""
        try:
            # 处理普通字符键
            if hasattr(key, 'char') and key.char:
                data = {'type': 'char', 'char': key.char}
                if platform.system() == 'Linux' and getattr(key, 'vk', None) in (0xFFAB, 0xFFAD):
                    data.update(vk=key.vk, platform='Linux')
                return data
            
            # 处理特殊键（Ctrl, Alt, Shift等）
            elif hasattr(key, 'name'):
                return add_caps_time({'type': 'special', 'name': key.name})
            
            # 处理虚拟键码
            elif hasattr(key, 'vk'):
                return {'type': 'vk', 'vk': key.vk, 'platform': platform.system()}
            
            else:
                return {'type': 'unknown'}
        except Exception as e:
            pass  # 错误已忽略
            return {'type': 'unknown'}
    
    def is_at_edge(self, x: int, y: int, edge_threshold: int = 5) -> str | None:
        """
        检查鼠标是否在屏幕边缘
        
        Args:
            x: 鼠标X坐标
            y: 鼠标Y坐标
            edge_threshold: 边缘阈值（像素）
            
        Returns:
            边缘位置 ('left', 'right', 'top', 'bottom') 或 None
        """
        if x <= edge_threshold:
            return 'left'
        elif x >= self.screen_width - edge_threshold:
            return 'right'
        elif y <= edge_threshold:
            return 'top'
        elif y >= self.screen_height - edge_threshold:
            return 'bottom'
        return None
