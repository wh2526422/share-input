"""Capture the Windows default playback endpoint through WASAPI loopback."""
import ctypes

from desktop_audio import CHANNELS, SAMPLE_RATE
from windows_dxgi import GUID, HRESULT, PPTR, PTR, UINT


CLSID_ENUMERATOR = GUID.parse('bcde0395-e52f-467c-8e3d-c4579291692e')
IID_ENUMERATOR = GUID.parse('a95664d2-9614-4f35-a746-de8db63617e6')
IID_AUDIO_CLIENT = GUID.parse('1cb9ad4c-dbfa-4c32-b178-c2f568a703b2')
IID_CAPTURE_CLIENT = GUID.parse('c8adbd64-e71e-48a0-a4de-185c395cd317')
GUID_PTR = ctypes.POINTER(GUID)
UINT_PTR = ctypes.POINTER(UINT)
REFERENCE_TIME = ctypes.c_int64
CLSCTX_INPROC_SERVER = 1
SHAREMODE_SHARED = 0
FLOW_RENDER = 0
ROLE_MULTIMEDIA = 1
LOOPBACK = 0x00020000
AUTO_CONVERT_PCM = 0x80000000
SRC_DEFAULT_QUALITY = 0x08000000
BUFFER_EMPTY = 0x08890001
BUFFER_SILENT = 0x2
FRAME_BYTES = CHANNELS * 2

# Slots from mmdeviceapi.h and audioclient.h, including IUnknown.
RELEASE = 2
GET_DEFAULT_ENDPOINT = 4
ACTIVATE = 3
INITIALIZE = 3
START = 10
STOP = 11
GET_SERVICE = 14
GET_BUFFER = 3
RELEASE_BUFFER = 4


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ('tag', ctypes.c_uint16), ('channels', ctypes.c_uint16), ('rate', UINT),
        ('bytes_per_second', UINT), ('block_align', ctypes.c_uint16),
        ('bits', ctypes.c_uint16), ('extra_size', ctypes.c_uint16),
    ]


def _check(result, operation):
    if result < 0:
        raise OSError(f'WASAPI {operation} 失败（0x{result & 0xFFFFFFFF:08X}）')


def _method(interface, slot, result, *args):
    table = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(PTR))).contents
    return ctypes.WINFUNCTYPE(result, PTR, *args)(table[slot])


class WindowsAudio:
    def __init__(self, stopped):
        self.stopped = stopped
        self.refs = []
        self.ole = None
        self.started = False

    def __enter__(self):
        ole = ctypes.WinDLL('ole32')
        ole.CoInitializeEx.argtypes = [PTR, UINT]
        ole.CoInitializeEx.restype = HRESULT
        ole.CoUninitialize.argtypes = []
        ole.CoUninitialize.restype = None
        ole.CoCreateInstance.argtypes = [GUID_PTR, PTR, UINT, GUID_PTR, PPTR]
        ole.CoCreateInstance.restype = HRESULT
        _check(ole.CoInitializeEx(None, 0), 'CoInitializeEx')
        self.ole = ole
        try:
            enumerator, endpoint, client, capture = PTR(), PTR(), PTR(), PTR()
            _check(ole.CoCreateInstance(ctypes.byref(CLSID_ENUMERATOR), None, CLSCTX_INPROC_SERVER,
                                        ctypes.byref(IID_ENUMERATOR), ctypes.byref(enumerator)),
                   'CoCreateInstance')
            self.refs.append(enumerator)
            _check(_method(enumerator, GET_DEFAULT_ENDPOINT, HRESULT, UINT, UINT, PPTR)(
                enumerator, FLOW_RENDER, ROLE_MULTIMEDIA, ctypes.byref(endpoint)), 'GetDefaultAudioEndpoint')
            self.refs.append(endpoint)
            _check(_method(endpoint, ACTIVATE, HRESULT, GUID_PTR, UINT, PTR, PPTR)(
                endpoint, ctypes.byref(IID_AUDIO_CLIENT), CLSCTX_INPROC_SERVER, None, ctypes.byref(client)),
                'Activate')
            self.refs.append(client)
            self.client = client
            fmt = WAVEFORMATEX(1, CHANNELS, SAMPLE_RATE, SAMPLE_RATE * FRAME_BYTES, FRAME_BYTES, 16, 0)
            flags = LOOPBACK | AUTO_CONVERT_PCM | SRC_DEFAULT_QUALITY
            _check(_method(client, INITIALIZE, HRESULT, UINT, UINT, REFERENCE_TIME,
                           REFERENCE_TIME, ctypes.POINTER(WAVEFORMATEX), GUID_PTR)(
                client, SHAREMODE_SHARED, flags, 500_000, 0, ctypes.byref(fmt), None), 'Initialize loopback')
            _check(_method(client, GET_SERVICE, HRESULT, GUID_PTR, PPTR)(
                client, ctypes.byref(IID_CAPTURE_CLIENT), ctypes.byref(capture)), 'GetService')
            self.refs.append(capture)
            self.capture = capture
            self.get_buffer = _method(capture, GET_BUFFER, HRESULT, PPTR, UINT_PTR, UINT_PTR, PTR, PTR)
            self.release_buffer = _method(capture, RELEASE_BUFFER, HRESULT, UINT)
            _check(_method(client, START, HRESULT)(client), 'Start')
            self.started = True
        except Exception:
            self.close()
            raise
        return self

    def read(self):
        address, frames, flags = PTR(), UINT(), UINT()
        status = self.get_buffer(
            self.capture, ctypes.byref(address), ctypes.byref(frames), ctypes.byref(flags), None, None)
        if status == BUFFER_EMPTY:
            self.stopped.wait(0.01)
            return b''
        _check(status, 'GetBuffer')
        try:
            size = frames.value * FRAME_BYTES
            if flags.value & BUFFER_SILENT:
                return bytes(size)
            if size and not address:
                raise OSError('WASAPI 返回了空的 PCM 缓冲')
            return ctypes.string_at(address, size) if size else b''
        finally:
            _check(self.release_buffer(self.capture, frames), 'ReleaseBuffer')

    def close(self):
        if self.started:
            self.started = False
            _method(self.client, STOP, HRESULT)(self.client)
        while self.refs:
            interface = self.refs.pop()
            _method(interface, RELEASE, UINT)(interface)
        if self.ole is not None:
            self.ole.CoUninitialize()
            self.ole = None

    def __exit__(self, *args):
        self.close()
