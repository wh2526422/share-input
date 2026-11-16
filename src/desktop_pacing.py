"""Thread-owned frame pacing without changing the system timer resolution.

Windows 10 1803+ can wait on a private high-resolution, one-shot timer. Older
systems or failed native calls fall back to Event.wait. Create, wait and close
the timer on the capture worker, never close its handle from another thread.

API contracts:
https://learn.microsoft.com/windows/win32/api/synchapi/nf-synchapi-createwaitabletimerexw
https://learn.microsoft.com/windows/win32/api/synchapi/nf-synchapi-setwaitabletimer
https://learn.microsoft.com/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject
"""
import ctypes
import math
import sys
import time


MAX_WAIT = 1 / 15
CREATE_WAITABLE_TIMER_HIGH_RESOLUTION = 0x00000002
TIMER_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0


class FramePacer:
    def __init__(self):
        self._kernel = None
        self._timer = None

    @property
    def high_resolution(self):
        return self._timer is not None

    def __enter__(self):
        if sys.platform != 'win32':
            return self
        try:
            self._kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True)
            pointer, dword, boolean = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32
            self._kernel.CreateWaitableTimerExW.argtypes = [pointer, ctypes.c_wchar_p, dword, dword]
            self._kernel.CreateWaitableTimerExW.restype = pointer
            self._kernel.SetWaitableTimer.argtypes = [
                pointer, ctypes.POINTER(ctypes.c_int64), ctypes.c_int32, pointer, pointer, boolean]
            self._kernel.SetWaitableTimer.restype = boolean
            self._kernel.WaitForSingleObject.argtypes = [pointer, dword]
            self._kernel.WaitForSingleObject.restype = dword
            self._kernel.CancelWaitableTimer.argtypes = [pointer]
            self._kernel.CancelWaitableTimer.restype = boolean
            self._kernel.CloseHandle.argtypes = [pointer]
            self._kernel.CloseHandle.restype = boolean
            self._timer = self._kernel.CreateWaitableTimerExW(
                None, None, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_MODIFY_STATE | SYNCHRONIZE)
        except (AttributeError, OSError):
            self.close()
        return self

    def wait(self, stopped, seconds):
        """Return True when stopped; each wait is bounded to one 15-fps period."""
        if stopped.is_set():
            return True
        delay = min(MAX_WAIT, max(0.0, seconds))
        if not delay:
            return stopped.is_set()
        if self._timer is None:
            return stopped.wait(delay)
        deadline = time.monotonic() + delay
        # Negative due times are relative; units are 100 ns. Round up so a
        # short interval never becomes an immediate/absolute zero-time wait.
        due = ctypes.c_int64(-max(1, math.ceil(delay * 10_000_000)))
        try:
            if self._kernel.SetWaitableTimer(self._timer, ctypes.byref(due), 0, None, None, False):
                # Finite watchdog: cancellation cannot strand this worker if a
                # timer/driver fails. Stop is checked on both sides of the wait.
                timeout = max(1, math.ceil(delay * 1000) + 1)
                if self._kernel.WaitForSingleObject(self._timer, timeout) == WAIT_OBJECT_0:
                    return stopped.is_set()
        except (AttributeError, OSError):
            pass
        self.close()
        return stopped.wait(max(0.0, deadline - time.monotonic()))

    def close(self):
        timer, self._timer = self._timer, None
        if timer is not None:
            try:
                self._kernel.CancelWaitableTimer(timer)
            except (AttributeError, OSError):
                pass
            finally:
                try:
                    self._kernel.CloseHandle(timer)
                except (AttributeError, OSError):
                    pass

    def __exit__(self, *_):
        self.close()
