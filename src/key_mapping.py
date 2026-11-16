"""跨平台键名归一化；禁止将其他系统的原始键码当成本机键码。"""
ALIASES = {
    'win': 'cmd', 'win_l': 'cmd_l', 'win_r': 'cmd_r',
    'super': 'cmd', 'super_l': 'cmd_l', 'super_r': 'cmd_r',
    'command': 'cmd', 'command_l': 'cmd_l', 'command_r': 'cmd_r',
    'option': 'alt', 'option_l': 'alt_l', 'option_r': 'alt_r',
    'control': 'ctrl', 'control_l': 'ctrl_l', 'control_r': 'ctrl_r',
    'capslk': 'caps_lock', 'capslock': 'caps_lock',
    'shift_l': 'shift_l', 'shift_r': 'shift_r',
}
# pynput 的 Xorg 后端使用 X keysym，既不是 evdev code 也不是 macOS vk。
X11_MODIFIERS = {
    0xFFE1: 'shift_l', 0xFFE2: 'shift_r',
    0xFFE3: 'ctrl_l', 0xFFE4: 'ctrl_r', 0xFFE5: 'caps_lock',
    0xFFE7: 'cmd_l', 0xFFE8: 'cmd_r',
    0xFFE9: 'alt_l', 0xFFEA: 'alt_r',
    0xFFEB: 'cmd_l', 0xFFEC: 'cmd_r',
    0xFE03: 'alt_gr', 0xFF7E: 'alt_gr',
}

# Mac ANSI 键盘上拉丁字母的虚拟键码，用于布局无法给出快捷键字母时回退。
ANSI_MAC_LETTERS = dict(zip('abcdefghijklmnopqrstuvwxyz',
    (0, 11, 8, 2, 14, 3, 5, 4, 34, 38, 40, 37, 46,
     45, 31, 35, 12, 15, 1, 17, 32, 9, 13, 7, 16, 6)))


def normalize_key_data(data, target):
    data = dict(data)
    if target == 'Darwin' and (
            (data.get('type') == 'special' and data.get('name', '').lower() == 'insert')
            or (data.get('type') == 'vk' and data.get('platform') in (None, 'Linux')
                and data.get('vk') in (0xFF63, 0xFF9E))):
        # PC Insert 对应 Mac 扩展键盘的 Help 位置；Darwin Key 没有 insert。
        return {'type': 'vk', 'vk': 0x72, 'platform': 'Darwin'}
    # 保留小键盘身份，避免字符注入绕过输入法的按键处理。
    if (target == 'Darwin' and data.get('type') in ('char', 'vk')
            and data.get('platform') in (None, 'Linux')
            and data.get('vk') in (0xFFAB, 0xFFAD)):
        return {'type': 'vk', 'vk': {0xFFAB: 0x45, 0xFFAD: 0x4E}[data['vk']],
                'platform': 'Darwin'}
    if data.get('type') == 'vk':
        source = data.get('platform')
        code = data.get('vk')
        # 兼容旧 Linux 发送端：这些 X keysym 不在 Mac 硬件键码范围内。
        if source in (None, 'Linux') and code in X11_MODIFIERS:
            data = {'type': 'special', 'name': X11_MODIFIERS[code]}
        elif source and source != target:
            return {'type': 'unknown'}
    if data.get('type') == 'special':
        name = str(data.get('name', '')).lower()
        name = ALIASES.get(name, name)
        if target == 'Darwin' and name == 'alt_gr':
            name = 'alt_r'
        data['name'] = name
    return data
