"""Caps Lock 中英切换：优先触发系统输入源快捷键，让输入法处理模式切换。"""
import ctypes
import ctypes.util
import logging
from concurrent.futures import Future, TimeoutError

from Foundation import NSOperationQueue, NSThread, NSUserDefaults
from Quartz import (CGEventSourceFlagsState, kCGEventSourceStateHIDSystemState,
                    CGEventCreateKeyboardEvent, CGEventSetFlags, CGEventSetType,
                    CGEventPost, kCGHIDEventTap, kCGEventFlagsChanged)


logger = logging.getLogger(__name__)

# 通用修饰位及设备左右位；快捷键结束后恢复本地和远端按住的修饰键。
_MODIFIERS = (
    (0x20000, ((56, 0x2), (60, 0x4))),
    (0x40000, ((59, 0x1), (62, 0x2000))),
    (0x80000, ((58, 0x20), (61, 0x40))),
    (0x100000, ((55, 0x8), (54, 0x10))),
    (0x800000, ((63, 0x800000),)),
)
_MODIFIER_BITS = {code: mask | side for mask, sides in _MODIFIERS for code, side in sides}
_MODIFIER_MASK = 0
for _bits in _MODIFIER_BITS.values():
    _MODIFIER_MASK |= _bits


def _input_source_shortcut():
    settings = NSUserDefaults.standardUserDefaults().persistentDomainForName_(
        'com.apple.symbolichotkeys') or {}
    hotkeys = settings.get('AppleSymbolicHotKeys', {})
    # 60：上一个输入源，61：下一个输入源。缺省项使用 macOS 默认值。
    for identity, default_flags in (('60', 0x40000), ('61', 0xc0000)):
        entry = hotkeys.get(identity, {})
        if not entry.get('enabled', True):
            continue
        value = entry.get('value', {})
        if value.get('type', 'standard') != 'standard':
            continue
        _, code, flags = value.get('parameters', (32, 49, default_flags))
        if not isinstance(code, int) or not 0 <= code < 128 or not isinstance(flags, int):
            raise ValueError('macOS 输入源快捷键配置无效')
        return code, flags
    return None


def _modifier_keys(flags):
    keys = set()
    for mask, sides in _MODIFIERS:
        if flags & mask:
            keys.update(code for code, side in sides if flags & side)
            if not any(code in keys for code, _ in sides):
                keys.add(sides[0][0])
    return keys


def _post_shortcut(code, flags, remote_flags):
    original = CGEventSourceFlagsState(kCGEventSourceStateHIDSystemState) | remote_flags
    held = _modifier_keys(original)
    restore = held.copy()
    base = original & ~_MODIFIER_MASK

    def post(key, pressed, modifier=False):
        event = CGEventCreateKeyboardEvent(None, key, pressed)
        if event is None:
            raise OSError('无法创建输入源快捷键事件')
        event_flags = base
        for item in held:
            event_flags |= _MODIFIER_BITS[item]
        CGEventSetFlags(event, event_flags)
        if modifier:
            CGEventSetType(event, kCGEventFlagsChanged)
        CGEventPost(kCGHIDEventTap, event)

    def transition(target):
        for key in sorted(held - target):
            held.remove(key)
            post(key, False, True)
        for key in sorted(target - held):
            held.add(key)
            post(key, True, True)

    try:
        transition(_modifier_keys(flags))
        try:
            post(code, True)
        finally:
            post(code, False)
    finally:
        transition(restore)


def _on_main(callback):
    if NSThread.isMainThread():
        return callback()
    result = Future()
    def perform():
        if result.set_running_or_notify_cancel():
            try:
                result.set_result(callback())
            except Exception as exc:
                result.set_exception(exc)
    NSOperationQueue.mainQueue().addOperationWithBlock_(perform)
    try:
        return result.result(timeout=1)
    except TimeoutError:
        result.cancel()  # 未开始的操作不能在断线/超时后才切换输入法。
        raise


class _Point(ctypes.Structure):
    _fields_ = [('x', ctypes.c_int16), ('y', ctypes.c_int16)]


class _AuxEvent(ctypes.Structure):
    # NXEventData.compound: 48 bytes, 4-byte alignment (IOLLEvent.h).
    _fields_ = [('reserved', ctypes.c_int16), ('subtype', ctypes.c_int16),
                ('data', ctypes.c_int32 * 11)]


class _NativeCapsLock:
    """通过 IOHIDSystem 发送 Caps 系统键；按下、松开均成功才报告完成。"""
    def __init__(self):
        self.io = ctypes.CDLL(ctypes.util.find_library('IOKit'))
        self.task = ctypes.c_uint32.in_dll(ctypes.CDLL(None), 'mach_task_self_').value
        signatures = {
            'IOHIDCheckAccess': ([ctypes.c_int], ctypes.c_int),
            'IOServiceMatching': ([ctypes.c_char_p], ctypes.c_void_p),
            'IOServiceGetMatchingService': ([ctypes.c_uint32, ctypes.c_void_p], ctypes.c_uint32),
            'IOServiceOpen': ([ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                               ctypes.POINTER(ctypes.c_uint32)], ctypes.c_int32),
            'IOObjectRelease': ([ctypes.c_uint32], ctypes.c_int32),
            'IOServiceClose': ([ctypes.c_uint32], ctypes.c_int32),
            'IOHIDPostEvent': ([ctypes.c_uint32, ctypes.c_uint32, _Point,
                                ctypes.POINTER(_AuxEvent), ctypes.c_uint32,
                                ctypes.c_uint32, ctypes.c_uint32], ctypes.c_int32),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.io, name)
            function.argtypes, function.restype = args, result

    @staticmethod
    def _check(status, operation):
        if status:
            raise OSError(f'{operation} 返回 0x{status & 0xffffffff:08x}')

    def tap(self, remote_flags=0):
        # 不在后台自动弹权限请求；沿用 ShareInput 现有的设备控制授权。
        if self.io.IOHIDCheckAccess(0) != 0:
            raise PermissionError('CapsLock 系统键需要为 ShareInput 授予设备控制/辅助功能权限')
        service = self.io.IOServiceGetMatchingService(0, self.io.IOServiceMatching(b'IOHIDSystem'))
        if not service:
            raise OSError('未找到 IOHIDSystem')
        handle = ctypes.c_uint32()
        try:
            self._check(self.io.IOServiceOpen(service, self.task, 1, ctypes.byref(handle)), 'IOServiceOpen')
        finally:
            self.io.IOObjectRelease(service)
        try:
            flags = CGEventSourceFlagsState(kCGEventSourceStateHIDSystemState) | remote_flags
            event = _AuxEvent()
            event.subtype = 8  # NX_SUBTYPE_AUX_CONTROL_BUTTONS
            for phase in (10, 11):  # NX_KEYDOWN, NX_KEYUP
                event.data[0] = (4 << 16) | (phase << 8)  # NX_KEYTYPE_CAPS_LOCK
                self._check(self.io.IOHIDPostEvent(handle.value, 14, _Point(),
                            ctypes.byref(event), 2, flags, 1), 'IOHIDPostEvent')
        finally:
            self.io.IOServiceClose(handle.value)


class CapsLockInputSource:
    def __init__(self):
        self._native = None

    def enabled(self):
        try:
            return _on_main(lambda: bool(NSUserDefaults.standardUserDefaults().objectForKey_(
                'TISRomanSwitchState')))
        except Exception:
            logger.exception('读取 macOS Caps Lock 输入源设置失败')
            return None

    def toggle(self, remote_flags=0):
        def perform():
            shortcut = _input_source_shortcut()
            if shortcut is not None:
                _post_shortcut(*shortcut, remote_flags)
                return
            # 用户禁用了两个输入源快捷键时，保留原生 Caps 系统键路径。
            if self._native is None:
                self._native = _NativeCapsLock()
            self._native.tap(remote_flags)
        try:
            _on_main(perform)
            return True
        except Exception:
            logger.exception('Caps Lock 输入法切换失败')
            return False
