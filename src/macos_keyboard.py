"""macOS 修饰键原生事件：保留 pynput 的布局解析，补充 flagsChanged 和锁定状态。"""
from pynput.keyboard import Controller, Key
from Quartz import (CGEventPost, CGEventGetFlags, CGEventSetFlags, CGEventSetType,
                    CGEventCreateKeyboardEvent,
                    kCGHIDEventTap, kCGEventFlagsChanged, kCGEventFlagMaskAlphaShift,
                    kCGEventFlagMaskNumericPad, kCGEventFlagMaskShift,
                    kCGEventFlagMaskCommand, kCGEventFlagMaskControl,
                    kCGEventFlagMaskAlternate)
from key_mapping import ANSI_MAC_LETTERS
from macos_input_source import CapsLockInputSource
from key_timing import caps_long_press
import threading

# 数字行的翻页键必须发送硬件事件，中文布局可能无法解析这些字符。
NUMBER_ROW_KEYS = {'-': (0x1B, False), '_': (0x1B, True),
                   '=': (0x18, False), '+': (0x18, True)}


class MacKeyboardController(Controller):
    def __init__(self):
        super().__init__()
        self._letter_keycodes = {}
        self._insert_paste = None
        self._caps_input_source = CapsLockInputSource()
        self._caps_press = None
        self._caps_press_lock = threading.RLock()

    def press(self, key, caps_time_ms=None):
        if self._resolve(key) != Key.caps_lock.value:
            return super().press(key)
        with self._caps_press_lock:
            if self._caps_press is not None:
                return  # 锁定键的自动重复不能反复切输入法或锁定状态。
            pending = self._caps_press = (caps_time_ms, None)
        # 不能持锁等待主队列：主线程关闭会话时也需要取消 pending Caps。
        mode = self._caps_input_source.enabled()
        with self._caps_press_lock:
            if self._caps_press is not pending:
                return
            self._caps_press = (pending[0], mode)
            if mode is False:
                super().press(key)

    def release(self, key, caps_time_ms=None):
        if self._resolve(key) != Key.caps_lock.value:
            return super().release(key)
        with self._caps_press_lock:
            pending, self._caps_press = self._caps_press, None
        if pending is None:
            return
        source_started, mode = pending
        if mode is False:
            super().release(key)
        elif mode is True:
            if self._caps_lock or caps_long_press(source_started, caps_time_ms):
                # 长按释放时切换大写锁定；已锁定时短按只解锁。
                super().press(key)
                super().release(key)
            else:
                self._caps_input_source.toggle()

    def cancel_caps_lock(self):
        with self._caps_press_lock:
            pending, self._caps_press = self._caps_press, None
            if pending is not None and pending[1] is False:
                super().release(Key.caps_lock)

    def merge_mouse_modifiers(self, event_type, event):
        """将远端按住的修饰键合并到本地鼠标事件，保留原有本地标志。"""
        flags = 0
        with self.modifiers as modifiers:
            for key, mask in ((Key.shift, kCGEventFlagMaskShift),
                              (Key.cmd, kCGEventFlagMaskCommand),
                              (Key.ctrl, kCGEventFlagMaskControl),
                              (Key.alt, kCGEventFlagMaskAlternate)):
                if key in modifiers:
                    flags |= mask
        if flags:
            CGEventSetFlags(event, CGEventGetFlags(event) | flags)
        return event

    def reset_remote_modifiers(self):
        self.cancel_caps_lock()
        with self._modifiers_lock:
            held = list(self._modifiers)
        for key in held:
            self.release(key)
        self._insert_paste = None

    def _handle(self, key, is_press):
        # 与 pynput Darwin Controller 相同的布局和字符解析路径。
        with self.modifiers as modifiers:
            event = key._event(modifiers, self._mapping, is_press)
            if key.vk == 0x72:
                # 在按下时确定映射，重复和释放沿用，允许先松开 Shift。
                if is_press and self._insert_paste is None:
                    self._insert_paste = modifiers == {Key.shift}
                if self._insert_paste:
                    event = CGEventCreateKeyboardEvent(None, 9, is_press)
                    CGEventSetFlags(event, kCGEventFlagMaskCommand)
                if not is_press:
                    self._insert_paste = None
            # 源端 CapsLock 会把 char 变成大写，布局表却只包含小写字母。
            # 普通字母也必须走硬件事件，否则 Unicode 回退会绕过拼音组词。
            # 大小写由 Mac 的 Shift/Caps 状态决定；释放沿用按下时键码。
            char = key.char.lower() if key.char else None
            code = self._letter_keycodes.get(char)
            if is_press and key.vk is None and char in ANSI_MAC_LETTERS:
                code = self._mapping.get(char, ANSI_MAC_LETTERS[char])
                self._letter_keycodes[char] = code
            number_row = NUMBER_ROW_KEYS.get(char) if key.vk is None else None
            if number_row is not None:
                code = number_row[0]
            if code is not None:
                flags = CGEventGetFlags(event)
                if number_row is not None and number_row[1]:
                    flags |= kCGEventFlagMaskShift
                event = CGEventCreateKeyboardEvent(None, code, is_press)
                CGEventSetFlags(event, flags)
                if not is_press:
                    self._letter_keycodes.pop(char, None)
        modifier_keys = (Key.shift, Key.shift_l, Key.shift_r,
                         Key.ctrl, Key.ctrl_l, Key.ctrl_r,
                         Key.alt, Key.alt_l, Key.alt_r,
                         Key.cmd, Key.cmd_l, Key.cmd_r, Key.caps_lock)
        if any(key == item.value for item in modifier_keys):
            CGEventSetType(event, kCGEventFlagsChanged)
        flags = CGEventGetFlags(event)
        if key.vk in (0x45, 0x4E):
            flags |= kCGEventFlagMaskNumericPad
        if self._caps_lock:
            flags |= kCGEventFlagMaskAlphaShift
        else:
            flags &= ~kCGEventFlagMaskAlphaShift
        CGEventSetFlags(event, flags)
        CGEventPost(kCGHIDEventTap, event)
