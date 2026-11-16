"""Synchronous GDI capture scaled before reading pixels into Pillow.

Only a target-sized DIB is retained. Use on the capture thread with physical
monitor coordinates; process DPI awareness remains owned by Qt/MSS.
"""
import ctypes


HANDLE = ctypes.c_void_p
INT = ctypes.c_int32
UINT = DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
BOOL = ctypes.c_int32
HALFTONE = 4
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
HGDI_ERROR = ctypes.c_void_p(-1).value


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', DWORD), ('biWidth', INT), ('biHeight', INT),
                ('biPlanes', WORD), ('biBitCount', WORD), ('biCompression', DWORD),
                ('biSizeImage', DWORD), ('biXPelsPerMeter', INT), ('biYPelsPerMeter', INT),
                ('biClrUsed', DWORD), ('biClrImportant', DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', ctypes.c_ubyte * 4)]


class POINT(ctypes.Structure):
    _fields_ = [('x', INT), ('y', INT)]


def _load_dll(name):
    loader = getattr(ctypes, 'WinDLL', None)
    if loader is None:
        raise OSError('Windows GDI capture is only available on Windows')
    return loader(name, use_last_error=True)


def _failed(operation):
    code = getattr(ctypes, 'get_last_error', lambda: 0)()
    if hasattr(ctypes, 'WinError'):
        error = ctypes.WinError(code)
        error.strerror = f'Windows GDI {operation} failed: {error.strerror}'
        return error
    return OSError(code, f'Windows GDI {operation} failed (error {code})')


class WindowsCapture:
    def __init__(self, display, resolution):
        values = [display.get(key) for key in ('left', 'top', 'width', 'height')]
        if (not all(type(value) is int for value in values)
                or values[2] <= 0 or values[3] <= 0
                or len(resolution) != 2
                or not all(type(value) is int and value > 0 for value in resolution)):
            raise ValueError('Invalid Windows capture display or resolution')
        self.display = dict(zip(('left', 'top', 'width', 'height'), values))
        width, height = values[2:]
        scale = min(1.0, resolution[0] / width, resolution[1] / height)
        self.size = max(1, round(width * scale)), max(1, round(height * scale))
        self._srcdc = self._memdc = self._bitmap = self._original_bitmap = None
        self._pixels = None
        self._closed = False
        self._user = _load_dll('user32')
        self._gdi = _load_dll('gdi32')
        self._bind_functions()
        try:
            self._srcdc = self._user.GetDC(None)
            if not self._srcdc:
                raise _failed('GetDC')
            self._memdc = self._gdi.CreateCompatibleDC(self._srcdc)
            if not self._memdc:
                raise _failed('CreateCompatibleDC')
            info = BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            info.bmiHeader.biWidth, info.bmiHeader.biHeight = self.size[0], -self.size[1]
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            bits = HANDLE()
            self._bitmap = self._gdi.CreateDIBSection(
                self._memdc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
            if not self._bitmap or not bits.value:
                raise _failed('CreateDIBSection')
            original = self._gdi.SelectObject(self._memdc, self._bitmap)
            if not original or original == HGDI_ERROR:
                raise _failed('SelectObject')
            self._original_bitmap = original
            if not self._gdi.SetStretchBltMode(self._memdc, HALFTONE):
                raise _failed('SetStretchBltMode')
            if not self._gdi.SetBrushOrgEx(self._memdc, 0, 0, None):
                raise _failed('SetBrushOrgEx')
            self._pixels = (ctypes.c_ubyte * (self.size[0] * self.size[1] * 4)).from_address(bits.value)
        except BaseException:
            self.close()
            raise

    def _bind_functions(self):
        functions = (
            (self._user.GetDC, [HANDLE], HANDLE),
            (self._user.ReleaseDC, [HANDLE, HANDLE], INT),
            (self._gdi.CreateCompatibleDC, [HANDLE], HANDLE),
            (self._gdi.CreateDIBSection,
             [HANDLE, ctypes.POINTER(BITMAPINFO), UINT, ctypes.POINTER(HANDLE), HANDLE, DWORD], HANDLE),
            (self._gdi.SelectObject, [HANDLE, HANDLE], HANDLE),
            (self._gdi.SetStretchBltMode, [HANDLE, INT], INT),
            (self._gdi.SetBrushOrgEx, [HANDLE, INT, INT, ctypes.POINTER(POINT)], BOOL),
            (self._gdi.StretchBlt, [HANDLE, INT, INT, INT, INT, HANDLE, INT, INT, INT, INT, DWORD], BOOL),
            (self._gdi.GdiFlush, [], BOOL),
            (self._gdi.DeleteObject, [HANDLE], BOOL),
            (self._gdi.DeleteDC, [HANDLE], BOOL),
        )
        for function, arguments, result in functions:
            function.argtypes = arguments
            function.restype = result

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def grab(self, raw=False):
        if self._closed:
            raise RuntimeError('Windows capture is closed')
        try:
            from PIL import Image
            source = self.display
            if not self._gdi.StretchBlt(
                    self._memdc, 0, 0, *self.size, self._srcdc,
                    source['left'], source['top'], source['width'], source['height'], SRCCOPY | CAPTUREBLT):
                raise _failed('StretchBlt')
            if not self._gdi.GdiFlush():
                raise _failed('GdiFlush')
            # Top-down 32-bit DIB rows are naturally DWORD aligned. Pillow owns
            # the returned RGB pixels; the DIB can be reused on the next grab.
            if raw:
                from jpeg_codec import RawFrame
                return RawFrame(bytes(self._pixels), self.size, self.size[0] * 4)
            return Image.frombytes('RGB', self.size, self._pixels, 'raw', 'BGRX', self.size[0] * 4, 1)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._pixels = None
        srcdc, memdc, bitmap, original = self._srcdc, self._memdc, self._bitmap, self._original_bitmap
        self._srcdc = self._memdc = self._bitmap = self._original_bitmap = None

        def release(function, *arguments):
            try:
                return function(*arguments)
            except Exception:
                return 0

        # A selected bitmap must be removed from the DC before DeleteObject.
        # If restoring the stock bitmap fails, delete the memory DC first.
        if memdc and original:
            restored = release(self._gdi.SelectObject, memdc, original)
            if not restored or restored == HGDI_ERROR:
                release(self._gdi.DeleteDC, memdc)
                memdc = None
        if bitmap:
            release(self._gdi.DeleteObject, bitmap)
        if memdc:
            release(self._gdi.DeleteDC, memdc)
        if srcdc:
            release(self._user.ReleaseDC, None, srcdc)
