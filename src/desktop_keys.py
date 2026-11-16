"""投屏窗口的键名转换与按键跟踪。"""
from key_mapping import ANSI_MAC_LETTERS
from key_timing import add_caps_time, valid_event_time


SPECIAL_KEYS = {
    'backspace': 'backspace', 'tab': 'tab', 'iso_left_tab': 'tab', 'return': 'enter',
    'kp_enter': 'enter', 'escape': 'esc', 'space': 'space', 'delete': 'delete',
    'insert': 'insert', 'home': 'home', 'end': 'end', 'prior': 'page_up',
    'next': 'page_down', 'page_up': 'page_up', 'page_down': 'page_down',
    'left': 'left', 'right': 'right', 'up': 'up', 'down': 'down',
    'caps_lock': 'caps_lock', 'num_lock': 'num_lock',
    'shift_l': 'shift_l', 'shift_r': 'shift_r', 'control_l': 'ctrl_l', 'control_r': 'ctrl_r',
    'alt_l': 'alt_l', 'alt_r': 'alt_r', 'option': 'alt_l', 'option_l': 'alt_l',
    'option_r': 'alt_r', 'alt_graph': 'alt_gr', 'iso_level3_shift': 'alt_gr',
    'meta_l': 'cmd_l', 'meta_r': 'cmd_r', 'super_l': 'cmd_l', 'super_r': 'cmd_r',
    'win_l': 'cmd_l', 'win_r': 'cmd_r', 'command': 'cmd_l',
    'command_l': 'cmd_l', 'command_r': 'cmd_r',
}
SPECIAL_KEYS.update({f'f{number}': f'f{number}' for number in range(1, 21)})

# Tk Aqua 的 state 分别用 ShiftMask、ControlMask、Mod1Mask 和 Mod2Mask；
# Qt 事件也转换为这些位，供各平台补齐进入窗口前已按住的修饰键。
MAC_MODIFIER_MASKS = {'shift': 1, 'ctrl': 4, 'cmd': 8, 'alt': 16}
MAC_SHORTCUT_KEYS = {code: char for char, code in ANSI_MAC_LETTERS.items()}
MAC_SHORTCUT_KEYS.update(zip(
    (29, 18, 19, 20, 21, 23, 22, 26, 28, 25, 50, 27, 24, 33, 30, 42, 41, 43, 39, 47, 44),
    "0123456789`-=[]\\;,'./",
))


def key_payload(event):
    name = SPECIAL_KEYS.get(event.keysym.lower())
    if name:
        return {'type': 'special', 'name': name}
    if len(event.char) == 1 and event.char.isprintable():
        return {'type': 'char', 'char': event.char}
    if len(event.keysym) == 1 and event.keysym.isprintable():
        return {'type': 'char', 'char': event.keysym}
    return None


def _modifier_group(payload):
    if payload and payload['type'] == 'special':
        group = payload['name'].split('_', 1)[0]
        if group in MAC_MODIFIER_MASKS:
            return group
    return None


def _mac_shortcut(payload, code):
    if payload and payload['type'] != 'char':
        return payload
    char = payload['char'] if payload else None
    if not char or not char.isascii():
        char = MAC_SHORTCUT_KEYS.get(code)
    if not char:
        return payload
    char = char.lower()
    shortcut = {**(payload or {'type': 'char', 'char': char}), 'platform': 'Darwin', 'shortcut': True}
    if shortcut['char'] != char:
        shortcut['shortcut_char'] = char
    return shortcut


class DesktopKeys:
    def __init__(self, send_input, aqua=False, sync_modifiers=False):
        self._send = send_input
        self._aqua = aqua
        self._sync_modifier_state = sync_modifiers
        self._pressed = {}

    def clear(self):
        self._pressed.clear()

    def mac_clipboard_shortcut(self, char):
        # 与实际设备的物理键编号分开，仍使用现有按下/松键协议。
        command, letter = 0xFFFFFFFE, 0xFFFFFFFF
        self._press(command, {'type': 'special', 'name': 'cmd_l'})
        self._press(letter, {'type': 'char', 'char': char})
        self._release(letter)
        self._release(command)

    def _code(self, event):
        if event.keysym.lower() in ('caps_lock', 'capslock', 'capslk'):
            # Lock keys can have different native codes between Qt/XKB press
            # and release. Pair them by their logical identity on every OS.
            return 0xFFFFFFFD
        if not isinstance(event.keycode, int):
            return None
        if self._aqua:
            # Tk Aqua 的低 24 位包括字符和 Shift/Option 状态，不可用来配对松键。
            virtual = (event.keycode & 0xffffffff) >> 24
            if virtual < 128:
                return virtual
        return event.keycode & 0xffffffff

    def _press(self, code, payload, caps_time_ms=None):
        payload = add_caps_time(payload, caps_time_ms)
        key = self._pressed.setdefault(code, payload)
        self._send({'kind': 'key', 'key': key, 'code': code, 'pressed': True})

    def _release(self, code, caps_time_ms=None, cancelled=False):
        key = self._pressed.pop(code, None)
        if key is not None:
            data = {'kind': 'key', 'code': code, 'pressed': False}
            if cancelled:
                data['cancelled'] = True
            if key.get('name') == 'caps_lock' and valid_event_time(caps_time_ms):
                data['caps_time_ms'] = caps_time_ms
            self._send(data)

    def _sync_modifiers(self, state, press_missing=True):
        for group, mask in MAC_MODIFIER_MASKS.items():
            codes = [code for code, key in self._pressed.items() if _modifier_group(key) == group]
            if state & mask and not codes and press_missing:
                # 窗口获得焦点前已经按住的修饰键，可能没有收到对应的按下事件。
                self._press(0x100 + mask, {'type': 'special', 'name': group + '_l'})
            elif not state & mask:
                for code in codes:
                    # X11 的独立 Super/Hyper 不一定包含在 Qt.MetaModifier 中。
                    # Qt 状态只用于清理补发的键；实际收到的物理键沿用其松键事件。
                    if not self._sync_modifier_state or code == 0x100 + mask:
                        self._release(code)

    def sync_modifiers(self, state):
        """鼠标事件也携带修饰键状态，补齐在进入画面前按住的 Win/Shift 等键。"""
        if self._sync_modifier_state:
            self._sync_modifiers(state)

    def press(self, event):
        payload = key_payload(event)
        code = self._code(event)
        if code is None:
            return
        if self._aqua or self._sync_modifier_state:
            group = _modifier_group(payload)
            state = getattr(event, 'state', None)
            if group:
                # Tk Aqua 将 Fn 报为 Super_L，Command 使用 Meta_L/Meta_R。
                if self._aqua and code == 63 and event.keysym.lower() == 'super_l':
                    return
                self._release(0x100 + MAC_MODIFIER_MASKS[group])
            elif isinstance(state, int):
                self._sync_modifiers(state)
                if self._aqua and state & (MAC_MODIFIER_MASKS['ctrl'] | MAC_MODIFIER_MASKS['cmd'] | MAC_MODIFIER_MASKS['alt']):
                    payload = _mac_shortcut(payload, code)
        if payload:
            if payload.get('name') == 'caps_lock' and code in self._pressed:
                # A new physical down must not reuse an unmatched earlier
                # press timestamp and turn a short tap into a long hold.
                self._release(code, cancelled=True)
            self._press(code, payload, getattr(event, 'caps_time_ms', None))

    def release(self, event):
        self._release(self._code(event), getattr(event, 'caps_time_ms', None))
        if self._sync_modifier_state:
            state = getattr(event, 'state', None)
            if isinstance(state, int):
                self._sync_modifiers(state, press_missing=False)
        elif self._aqua and _modifier_group(key_payload(event)):
            state = getattr(event, 'state', None)
            if isinstance(state, int):
                self._sync_modifiers(state)
