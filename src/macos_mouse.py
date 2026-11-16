"""Mac 合成鼠标事件在发布前合并远端修饰键，避免依赖异步 event tap。"""
import time
from AppKit import NSEvent
from config import Config
from pynput.mouse import Controller
from Quartz import (CGEventCreateMouseEvent, CGEventCreateScrollWheelEvent,
                    CGEventCreate, CGEventGetLocation, CGEventGetType,
                    CGEventSetIntegerValueField, CGEventPost,
                    kCGHIDEventTap, kCGEventMouseMoved, kCGMouseEventClickState,
                    kCGScrollEventUnitLine)


class MacMouseController(Controller):
    def __init__(self, keyboard_controller):
        super().__init__()
        self._keyboard = keyboard_controller
        self._position = None
        self._position_at = 0
        self._pressed_clicks = {}
        self._last_click = None
        self._double_click_interval = NSEvent.doubleClickInterval()
        lines = Config().config.get('mac_scroll_lines_per_notch', 3)
        self._scroll_lines = lines if type(lines) is int and 1 <= lines <= 20 else 3

    def _post(self, event):
        self._keyboard.merge_mouse_modifiers(CGEventGetType(event), event)
        CGEventPost(kCGHIDEventTap, event)

    def _position_set(self, pos):
        pos = tuple(pos)
        if self._drag_button is not None and pos == self._position:
            # 松键包也携带坐标；零位移不能变成拖动，否则 Swing 会取消点击。
            return
        if self._drag_button is None:
            kind, button = kCGEventMouseMoved, 0
        else:
            (_, _, kind), button = self._drag_button.value
            self._last_click = None
        event = CGEventCreateMouseEvent(None, kind, pos, button)
        if self._drag_button is not None:
            CGEventSetIntegerValueField(event, kCGMouseEventClickState,
                                       self._pressed_clicks.get(self._drag_button, 1))
        self._post(event)
        self._position = pos
        self._position_at = time.monotonic()

    def _position_get(self):
        # Quartz 与截图共用左上原点，不在输入线程查询 AppKit 的鼠标位置。
        return tuple(CGEventGetLocation(CGEventCreate(None)))

    def _button_event(self, button, pressed, click_count=None):
        (down, up, _), native_button = button.value
        # CGEventPost 异步更新系统指针，紧接着回读位置可能拿到上一次坐标。
        now = time.monotonic()
        # 只短暂保留尚未被系统消费的移动，旧共享包不能永远复用过期位置。
        recent_move = self._position is not None and 0 <= now - self._position_at <= 0.1
        pos = self._position if self._position is not None and (recent_move or self._pressed_clicks) else self.position
        event = CGEventCreateMouseEvent(None, down if pressed else up, pos, native_button)
        if pressed:
            if self._click is not None:
                self._click += 1
                click_count = self._click
            elif click_count is None:
                click_count = 1
                if self._last_click is not None:
                    previous_button, previous_time, previous_pos, previous_count = self._last_click
                    if (previous_button == button and now - previous_time <= self._double_click_interval
                            and sum((a-b)**2 for a,b in zip(pos, previous_pos)) <= 16):
                        click_count = previous_count + 1
            self._pressed_clicks[button] = click_count
            self._last_click = (button, now, pos, click_count)
            self._position = pos
            self._position_at = now
        else:
            click_count = self._pressed_clicks.pop(button, 1)
        CGEventSetIntegerValueField(event, kCGMouseEventClickState, click_count)
        self._post(event)

    def press(self, button, click_count=None):
        self._press(button, click_count)

    def _press(self, button, click_count=None):
        self._button_event(button, True, click_count)
        self._drag_button = button

    def _release(self, button):
        self._button_event(button, False)
        if button == self._drag_button:
            self._drag_button = None
        if not self._pressed_clicks:
            self._position = None

    def _scroll(self, dx, dy):
        # 协议传的是滚轮格数；Quartz 的 line 单位是行数，两者不能直接等同。
        self._post(CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 2,
                   int(dy) * self._scroll_lines, int(dx) * self._scroll_lines))
