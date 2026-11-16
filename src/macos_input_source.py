"""Caps Lock 中英切换：尊重 macOS 偏好，仅在主线程使用 Text Input Sources。"""
import ctypes
import ctypes.util
import logging
from concurrent.futures import Future, TimeoutError

from Foundation import NSOperationQueue, NSThread, NSUserDefaults


logger = logging.getLogger(__name__)


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


class _TextInputSources:
    """只持有函数与常量；所有查询、选择和 CF 对象释放均在主线程完成。"""
    def __init__(self):
        self.carbon = ctypes.CDLL(ctypes.util.find_library('Carbon'))
        self.cf = ctypes.CDLL(ctypes.util.find_library('CoreFoundation'))
        signatures = {
            'TISCopyCurrentKeyboardInputSource': ([], ctypes.c_void_p),
            'TISCopyCurrentASCIICapableKeyboardInputSource': ([], ctypes.c_void_p),
            'TISCreateInputSourceList': ([ctypes.c_void_p, ctypes.c_bool], ctypes.c_void_p),
            'TISGetInputSourceProperty': ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p),
            'TISSelectInputSource': ([ctypes.c_void_p], ctypes.c_int32),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.carbon, name)
            function.argtypes, function.restype = args, result
        for name, args, result in (
            ('CFRelease', [ctypes.c_void_p], None),
            ('CFArrayGetCount', [ctypes.c_void_p], ctypes.c_long),
            ('CFArrayGetValueAtIndex', [ctypes.c_void_p, ctypes.c_long], ctypes.c_void_p),
            ('CFStringGetCString', [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, ctypes.c_uint32], ctypes.c_bool),
            ('CFBooleanGetValue', [ctypes.c_void_p], ctypes.c_bool),
        ):
            function = getattr(self.cf, name)
            function.argtypes, function.restype = args, result
        names = ('InputSourceID', 'LocalizedName', 'InputSourceIsASCIICapable',
                 'InputSourceIsEnabled', 'InputSourceIsSelectCapable')
        self.properties = {name: ctypes.c_void_p.in_dll(self.carbon, 'kTISProperty' + name)
                           for name in names}

    def _property(self, source, name):
        return self.carbon.TISGetInputSourceProperty(source, self.properties[name])

    def _string(self, source, name):
        value = self._property(source, name)
        if not value:
            return ''
        text = ctypes.create_string_buffer(4096)
        if not self.cf.CFStringGetCString(value, text, len(text), 0x08000100):
            return ''
        return text.value.decode('utf-8')

    def _boolean(self, source, name):
        value = self._property(source, name)
        return bool(value and self.cf.CFBooleanGetValue(value))

    def describe(self, source):
        if not source:
            return None
        return {'ref': source, 'id': self._string(source, 'InputSourceID'),
                'name': self._string(source, 'LocalizedName'),
                'ascii': self._boolean(source, 'InputSourceIsASCIICapable'),
                'enabled': self._boolean(source, 'InputSourceIsEnabled'),
                'selectable': self._boolean(source, 'InputSourceIsSelectCapable')}

    def toggle(self, previous_non_latin, history):
        if not NSThread.isMainThread():
            raise RuntimeError('Text Input Sources 必须在主线程使用')
        current = self.carbon.TISCopyCurrentKeyboardInputSource()
        latin = self.carbon.TISCopyCurrentASCIICapableKeyboardInputSource()
        sources = self.carbon.TISCreateInputSourceList(None, False)
        try:
            active = self.describe(current)
            if not active or not sources:
                raise RuntimeError('未获取到当前已启用的键盘输入源')
            available = [self.describe(self.cf.CFArrayGetValueAtIndex(sources, index))
                         for index in range(self.cf.CFArrayGetCount(sources))]
            available = [source for source in available
                         if source and source['enabled'] and source['selectable']]
            target, remembered = choose_source(active, self.describe(latin), available,
                                                previous_non_latin, history)
            if target is None:
                raise RuntimeError('未找到可切换的最近拉丁/非拉丁输入源')
            status = self.carbon.TISSelectInputSource(target['ref'])
            if status:
                raise RuntimeError(f'TISSelectInputSource 返回 {status}')
            return remembered
        finally:
            for owned in (current, latin, sources):
                if owned:
                    self.cf.CFRelease(owned)


def choose_source(current, latin, available, previous_non_latin, history):
    """优先最近使用来源；不会启用输入法、硬编码 ABC，或从多个候选任意选一项。"""
    if not current['ascii']:
        target = next((source for source in available
                       if latin and source['id'] == latin['id'] and source['ascii']), None)
        return target, current['id']
    candidates = {source['id']: source for source in available if not source['ascii']}
    if previous_non_latin in candidates:
        return candidates[previous_non_latin], previous_non_latin
    for item in history:
        identifier = item.get('Input Mode') or item.get('Bundle ID')
        if identifier in candidates:
            return candidates[identifier], identifier
        name = item.get('KeyboardLayout Name')
        if name:
            matches = [source for source in candidates.values() if source['name'] == name]
            if len(matches) == 1:
                return matches[0], matches[0]['id']
    if len(candidates) == 1:
        target = next(iter(candidates.values()))
        return target, target['id']
    return None, previous_non_latin


class CapsLockInputSource:
    def __init__(self):
        self._native = None
        self._previous_non_latin = None

    def enabled(self):
        try:
            return _on_main(lambda: bool(NSUserDefaults.standardUserDefaults().objectForKey_(
                'TISRomanSwitchState')))
        except Exception:
            logger.exception('读取 macOS Caps Lock 输入源设置失败')
            return None  # 无法读取时不把本来用于切输入法的键变成大小写锁定。

    def toggle(self):
        def perform():
            if self._native is None:
                self._native = _TextInputSources()
            preferences = NSUserDefaults.standardUserDefaults().persistentDomainForName_('com.apple.HIToolbox') or {}
            self._previous_non_latin = self._native.toggle(
                self._previous_non_latin, preferences.get('AppleInputSourceHistory', ()))
        try:
            _on_main(perform)
            return True
        except Exception:
            logger.exception('Caps Lock 输入源切换失败，未更改大小写锁定')
            return False
