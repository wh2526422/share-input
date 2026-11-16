"""Capture and encode independently of socket writes, retaining one latest JPEG."""
from contextlib import closing
import threading
import time

from desktop_pacing import FramePacer


class LatestFrames:
    def __init__(self, factory, stopped, get_fps, record, replaced):
        self._factory = factory
        self._parent_stopped = stopped
        self._get_fps = get_fps
        self._record = record
        self._replaced = replaced
        self._stopped = threading.Event()
        self._condition = threading.Condition()
        self._latest = None
        self._error = None
        self._finished = False
        threading.Thread(target=self._run, daemon=True, name='desktop-capture').start()

    def _run(self):
        try:
            if self._stopped.is_set() or self._parent_stopped.is_set():
                return
            # Native capture objects are constructed and destroyed on this thread.
            with FramePacer() as pacer, closing(self._factory(self._stopped)) as frames:
                next_frame_at = time.monotonic()
                while not self._stopped.is_set() and not self._parent_stopped.is_set():
                    pace_started = time.perf_counter()
                    if pacer.wait(self._stopped, next_frame_at - time.monotonic()):
                        return
                    self._record('pace_wait', time.perf_counter() - pace_started)
                    started = time.perf_counter()
                    frame_started = time.monotonic()
                    frame = next(frames, None)
                    if frame is None:
                        return
                    if not frame:
                        # Native capture waits for updates. Do not add another
                        # frame-period sleep after an idle acquisition.
                        continue
                    encoded_at = time.perf_counter()
                    self._record('capture_encode', encoded_at - started)
                    next_frame_at = frame_started + 1 / self._get_fps()
                    with self._condition:
                        if self._stopped.is_set() or self._parent_stopped.is_set():
                            return
                        replaced = self._latest is not None
                        self._latest = frame, encoded_at
                        self._condition.notify_all()
                    if replaced:
                        self._replaced()
        except Exception as exc:
            with self._condition:
                if not self._stopped.is_set() and not self._parent_stopped.is_set():
                    self._error = exc
        finally:
            with self._condition:
                self._finished = True
                self._condition.notify_all()

    def take(self):
        with self._condition:
            if (self._latest is None and self._error is None and not self._finished
                    and not self._stopped.is_set() and not self._parent_stopped.is_set()):
                self._condition.wait(0.02)
            if self._stopped.is_set() or self._parent_stopped.is_set():
                return None, None
            if self._error is not None:
                raise self._error
            if self._latest is not None:
                frame, self._latest = self._latest, None
                return frame
            return (None, None) if self._finished else (b'', None)

    def close(self):
        self._stopped.set()
        with self._condition:
            self._latest = None
            self._condition.notify_all()
        # The owner thread closes its generator, including native resources.
        # Never join a driver/ScreenCaptureKit operation on the GUI thread.
