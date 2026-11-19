"""Independent desktop audio channel with bounded PCM buffers."""
from collections import deque
from contextlib import ExitStack
import importlib.util
import platform
import select
import socket
import struct
import sys
import threading
import time

from protocol import Protocol


AUDIO_PROFILE = 'pcm-s16le-48000-stereo-v1'
SAMPLE_RATE = 48000
CHANNELS = 2
PACKET_FRAMES = SAMPLE_RATE // 50
PACKET_NS = 1_000_000_000 // 50
PACKET_BYTES = PACKET_FRAMES * CHANNELS * 2
PREBUFFER_PACKETS = 3
MAX_BUFFER_PACKETS = 5
MAX_AGE_NS = 100_000_000
OFFSET_WINDOW_NS = 500_000_000
STAMP = struct.Struct('!Q')
SILENT_PACKET = 1 << 63
SILENCE = bytes(PACKET_BYTES)


def can_play_audio():
    return importlib.util.find_spec('PySide6.QtMultimedia') is not None


def can_capture_audio():
    if sys.platform == 'darwin':
        version = platform.mac_ver()[0].split('.')[0]
        return (version.isdigit() and int(version) >= 13
                and importlib.util.find_spec('ScreenCaptureKit') is not None)
    if sys.platform == 'win32':
        return True
    if sys.platform.startswith('linux'):
        from linux_audio import can_capture
        return can_capture()
    return False


class DesktopAudio:
    def __init__(self, generation):
        self.generation = generation
        self.started = time.monotonic()
        self.receiving = threading.Event()
        self.stopped = threading.Event()
        self.error = None
        self.retryable = False
        self._connection = None
        self._buffer = deque(maxlen=MAX_BUFFER_PACKETS)
        self._condition = threading.Condition()
        self._pending = bytearray()
        self._pending_stamp = 0

    def attach(self, connection):
        with self._condition:
            if self.stopped.is_set() or self._connection is not None:
                return False
            self._connection = connection
            return True

    def stop(self, error=None, *, retryable=False):
        with self._condition:
            if self.stopped.is_set():
                return
            self.error = error
            self.retryable = retryable
            self.stopped.set()
            self._buffer.clear()
            connection, self._connection = self._connection, None
            self._condition.notify_all()
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # Socket ownership remains with the dial/accept worker.

    def put(self, stamp, pcm):
        with self._condition:
            if not self.stopped.is_set():
                self._buffer.append((stamp, pcm))
                self._condition.notify()

    def submit(self, pcm, stamp=None):
        # The capture backend has a single producer. Keep partial packets
        # outside the queue lock and never mix samples across a long gap.
        if stamp is None:
            stamp = time.monotonic_ns()
        if not self._pending or stamp - self._pending_stamp > MAX_AGE_NS:
            self._pending.clear()
            self._pending_stamp = stamp
        self._pending.extend(pcm)
        while len(self._pending) >= PACKET_BYTES:
            self.put(self._pending_stamp, bytes(self._pending[:PACKET_BYTES]))
            del self._pending[:PACKET_BYTES]
            self._pending_stamp += PACKET_NS

    def take(self, minimum=1, wait=0):
        with self._condition:
            if wait and not self._buffer and not self.stopped.is_set():
                self._condition.wait(wait)
            if len(self._buffer) >= minimum:
                return self._buffer.popleft()
            return None

    def receive(self, connection):
        connection.settimeout(10)  # Also allows for native capture startup.
        window_start = None
        previous_min = None
        current_min = None
        while not self.stopped.is_set():
            header = Protocol._receive_exact(connection, STAMP.size)
            if header is None:
                break
            self.receiving.set()
            stamp, = STAMP.unpack(header)
            if not stamp:  # Keep the connection alive when the system is silent.
                continue
            if stamp & SILENT_PACKET:
                stamp &= ~SILENT_PACKET
                pcm = SILENCE
            else:
                pcm = Protocol._receive_exact(connection, PACKET_BYTES)
                if pcm is None:
                    break
            arrival = time.monotonic_ns()
            offset = arrival - stamp
            # Compare against recent transit times. An early fast packet must
            # not mute a stream that settles at a higher one-way delay.
            if window_start is None or arrival - window_start >= OFFSET_WINDOW_NS:
                previous_min = current_min
                current_min = offset
                window_start = arrival
            else:
                current_min = min(current_min, offset)
            best_offset = current_min if previous_min is None else min(previous_min, current_min)
            if offset - best_offset <= MAX_AGE_NS:
                self.put(arrival, pcm)
        self.stop('远端音频连接已断开', retryable=True)

    def capture(self, connection):
        if sys.platform == 'darwin':
            from macos_audio import MacAudio
            backend = MacAudio(self)
        elif sys.platform == 'win32':
            from windows_audio import WindowsAudio
            backend = WindowsAudio(self.stopped)
        elif sys.platform.startswith('linux'):
            from linux_audio import LinuxAudio
            backend = LinuxAudio()
        else:
            raise OSError('系统音频采集不可用')

        connection.settimeout(PACKET_NS / 1_000_000_000)
        # Leave room for in-flight frames while TCP waits for acknowledgments.
        # The capture queue and the playout freshness limit stay independent.
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16 * PACKET_BYTES)
        with ExitStack() as resources:
            if hasattr(backend, 'read'):
                threading.Thread(target=self._read_capture, args=(backend,),
                                 name='desktop-audio-capture', daemon=True).start()
            else:
                resources.enter_context(backend)
            last_sent = time.monotonic()
            while not self.stopped.is_set():
                if select.select([connection], [], [], 0)[0]:
                    return  # The viewer closed the one-way audio channel.
                packet = self.take(wait=0.04)
                if self.stopped.is_set():
                    return
                if packet is not None:
                    stamp, pcm = packet
                    if time.monotonic_ns() - stamp > MAX_AGE_NS:
                        continue
                    if pcm == SILENCE:
                        data = STAMP.pack(stamp | SILENT_PACKET)
                    else:
                        data = STAMP.pack(stamp) + pcm
                elif time.monotonic() - last_sent >= 1:
                    data = STAMP.pack(0)
                else:
                    continue
                if self._send_packet(connection, data):
                    last_sent = time.monotonic()

    def _send_packet(self, connection, data):
        pending = memoryview(data)
        while not self.stopped.is_set():
            try:
                sent = connection.send(pending)
            except socket.timeout:
                # An untouched packet can be dropped. Once any byte is on
                # the wire, finish this packet before starting the next one.
                if len(pending) == len(data):
                    return False
                continue
            if not sent:
                raise ConnectionError('音频连接已关闭')
            pending = pending[sent:]
            if not pending:
                return True
        return False

    def _read_capture(self, backend):
        if self.stopped.is_set():
            return
        try:
            with backend:
                while not self.stopped.is_set():
                    pcm = backend.read()
                    if pcm:
                        self.submit(pcm)
        except Exception as exc:
            self.stop(f'系统声音采集失败：{exc}')
