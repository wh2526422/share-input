"""避免 pynput 在监听线程查询 TIS 输入源，引发新 macOS 的队列断言。"""
from pynput.keyboard._darwin import Listener as DarwinKeyboardListener, kSystemDefinedEventMediaKeysSubtype
from pynput._util.darwin import ListenerMixin
from concurrent.futures import ThreadPoolExecutor

from AppKit import NSEvent, NSSystemDefined
from Foundation import NSOperationQueue
from Quartz import CGEventCreateCopy, CGEventGetFlags


class MacKeyboardListener(DarwinKeyboardListener):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._system_keys = ThreadPoolExecutor(max_workers=1, thread_name_prefix='system-keys')

    def _run(self):
        # pynput 1.8.1 的 Darwin Listener._run() 建立 keycode_context，
        # 但 _event_to_key() 已直接读取 CGEvent Unicode 和键码，不使用它。
        # 保留原生事件 tap、权限检查、事件解析和 stop 的运行循环，
        # 只跳过后台线程中的 TISGetInputSourceProperty 调用。
        try:
            ListenerMixin._run(self)
        finally:
            self._system_keys.shutdown(wait=False, cancel_futures=True)

    def _handle_message(self, proxy, event_type, event, refcon, *event_args):
        if event_type != NSSystemDefined:
            return super()._handle_message(proxy, event_type, event, refcon, *event_args)
        # Caps Lock 的系统事件会进入输入法状态机，只能在主线程转换 NSEvent。
        copied = CGEventCreateCopy(event)
        self._flags = CGEventGetFlags(event)

        def convert():
            if not self.running:
                return
            native = NSEvent.eventWithCGEvent_(copied)
            if native is None or native.subtype() != kSystemDefinedEventMediaKeysSubtype:
                return
            data = native.data1()
            key = self._SPECIAL_KEYS.get(((data >> 16) & 0xffff, True))
            if key is not None:
                try:
                    self._system_keys.submit(self._deliver_system_key, key,
                                             ((data & 0xff00) >> 8) == 0x0a, event_args)
                except RuntimeError:
                    # 主队列中的转换可能晚于监听器关闭。
                    pass

        NSOperationQueue.mainQueue().addOperationWithBlock_(convert)

    @DarwinKeyboardListener._emitter
    def _deliver_system_key(self, key, pressed, event_args):
        if self.running:
            callback = self.on_press if pressed else self.on_release
            # pynput 1.8 多传入 injected 标记，1.7 的回调只有 key。
            callback(key, *event_args)
