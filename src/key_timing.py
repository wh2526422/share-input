"""Caps 长按只使用源端原始事件时间，避免网络或 UI 队列等待被算入按压。"""
from contextlib import contextmanager
import threading


_event = threading.local()


def valid_event_time(value):
    # X11/Qt 的原始事件时间以毫秒计；0 也用于没有时间的合成 Qt 事件。
    return type(value) is int and 0 < value <= 0xffffffffffffffff


@contextmanager
def source_event_time(value):
    previous = getattr(_event, 'time_ms', None)
    _event.time_ms = value if valid_event_time(value) else None
    try:
        yield
    finally:
        _event.time_ms = previous


def add_caps_time(payload, value=None):
    if payload.get('type') == 'special' and payload.get('name') == 'caps_lock':
        value = getattr(_event, 'time_ms', None) if value is None else value
        if valid_event_time(value):
            return dict(payload, caps_time_ms=value)
    return payload


def caps_long_press(start, end):
    if not valid_event_time(start) or not valid_event_time(end):
        return False  # 旧端没有源端时间，不能用收包间隔猜测长按。
    elapsed = end - start
    if elapsed < 0 and start <= 0xffffffff and end <= 0xffffffff:
        elapsed &= 0xffffffff  # X11 的 32 位毫秒计时器会回绕。
    # 32 位时钟只能可靠区分半个计时周期内的正向间隔。
    return 500 <= elapsed <= 0x7fffffff
