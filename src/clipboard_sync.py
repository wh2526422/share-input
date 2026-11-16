"""
剪贴板同步模块
"""
import sys
import threading
import time
from contextlib import nullcontext
from typing import Callable

import pyperclip


_clipboard_pool = nullcontext
if sys.platform == 'darwin':
    try:
        from objc import autorelease_pool as _clipboard_pool
    except ImportError:
        # pyperclip 也支持通过 pbcopy/pbpaste 访问 macOS 剪贴板。
        pass


class ClipboardSync:
    """剪贴板同步器"""
    
    def __init__(self, on_change: Callable[[str], None], publish_initial=True):
        """
        初始化剪贴板同步器
        
        Args:
            on_change: 剪贴板变化回调函数
        """
        self.on_change = on_change
        self.last_content = None
        self._publish_initial = publish_initial
        self._lock = threading.Lock()
        self.running = False
        self.thread = None
        self.enabled = True
    
    def start(self):
        """启动剪贴板监控"""
        self.running = True
        self.thread = threading.Thread(target=self._monitor, daemon=True)
        self.thread.start()
    
    def stop(self):
        """停止剪贴板监控"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=1)
    
    def _monitor(self):
        """监控剪贴板变化"""
        clipboard_available = True
        
        while self.running:
            try:
                if self.enabled and clipboard_available:
                    with self._lock:
                        with _clipboard_pool():
                            current_content = pyperclip.paste()
                        # 图片、文件或无文本内容时，macOS 后端可能返回 None。
                        previous = self.last_content
                        if isinstance(current_content, str):
                            self.last_content = current_content
                        elif previous is None:
                            self.last_content = ""
                    if (isinstance(current_content, str) and current_content != previous
                            and (previous is not None or self._publish_initial)):
                        self.on_change(current_content)
            except pyperclip.PyperclipException as e:
                if clipboard_available:
                    print(f"⚠️  剪贴板不可用，已禁用剪贴板同步功能")
                    print(f"💡 提示: 安装 xclip 以启用剪贴板同步")
                    print(f"   命令: sudo apt-get install xclip")
                    clipboard_available = False
            except Exception as e:
                if clipboard_available:
                    print(f"⚠️  剪贴板监控错误: {e}")
                    clipboard_available = False
            
            time.sleep(0.5)  # 每0.5秒检查一次
    
    def set_clipboard(self, content: str):
        """
        设置剪贴板内容
        
        Args:
            content: 要设置的内容
        """
        if not isinstance(content, str):
            return
        try:
            with self._lock:
                with _clipboard_pool():
                    pyperclip.copy(content)
                self.last_content = content
        except pyperclip.PyperclipException:
            # 剪贴板不可用，静默忽略
            pass
        except Exception as e:
            print(f"⚠️  设置剪贴板错误: {e}")
