"""Reusable baseline JPEG encoder; TurboJPEG is an optional native fast path.

The native path accepts owned packed pixels, avoiding a BGRA -> Pillow RGB ->
packed RGB round trip. CDLL calls release the GIL; each capture worker owns its
own compressor and destination buffer. JPEG quality/chroma sampling stay under
the caller's control, including when falling back to Pillow.
"""
import ctypes
from ctypes.util import find_library
from dataclasses import dataclass
from functools import lru_cache
from importlib.util import find_spec
import io
from pathlib import Path
import sys


# Stable TurboJPEG 2.x API, also exported by libjpeg-turbo 3.x:
# https://github.com/libjpeg-turbo/libjpeg-turbo/blob/main/src/turbojpeg.h
_PIXEL_FORMATS = {'RGB': (0, 3), 'BGR': (1, 3), 'RGBX': (2, 4),
                  'BGRX': (3, 4), 'RGBA': (7, 4), 'BGRA': (8, 4)}
_NOREALLOC = 1024
_FASTDCT = 2048
_PTR = ctypes.c_void_p
_ULONG = ctypes.c_ulong  # unsigned long is 32 bits on Windows, 64 on LP64.


@dataclass(frozen=True)
class RawFrame:
    """Top-down packed pixels that remain valid after unmapping the capture API."""

    data: bytes
    size: tuple
    stride: int
    pixel_format: str = 'BGRX'

    def __post_init__(self):
        object.__setattr__(self, 'size', tuple(self.size))
        if not isinstance(self.data, bytes):
            raise TypeError('RawFrame requires owned immutable bytes')
        if self.pixel_format not in _PIXEL_FORMATS:
            raise ValueError('Unsupported packed pixel format')
        if (len(self.size) != 2 or not all(type(n) is int and n > 0 for n in self.size)
                or type(self.stride) is not int
                or self.stride < self.size[0] * _PIXEL_FORMATS[self.pixel_format][1]
                or len(self.data) < self.stride * self.size[1]):
            raise ValueError('Invalid packed frame dimensions or stride')

    @property
    def width(self):
        return self.size[0]

    @property
    def height(self):
        return self.size[1]

    def to_image(self):
        from PIL import Image

        decoder = {'RGBA': 'RGBX', 'BGRA': 'BGRX'}.get(self.pixel_format, self.pixel_format)
        return Image.frombytes('RGB', self.size, self.data, 'raw', decoder, self.stride, 1)


def _library_candidates():
    """Find wheel/system libraries without importing the Python turbojpeg wrapper."""
    roots = [Path(__file__).resolve().parent, Path(sys.executable).resolve().parent]
    try:
        spec = find_spec('turbojpeg')
    except (ImportError, ValueError, AttributeError):
        spec = None
    if spec is not None and spec.origin not in (None, 'built-in', 'frozen'):
        extension = Path(spec.origin)
        roots.insert(0, extension.parent)
        # POSIX dlsym(handle) also searches the extension's linked libraries.
        # Windows GetProcAddress requires the actual turbojpeg DLL instead.
        if sys.platform != 'win32' and extension.suffix in ('.so', '.dylib'):
            yield str(extension)
    suffixes = ('*turbojpeg*.dll',) if sys.platform == 'win32' else (
        '*turbojpeg*.dylib', '*turbojpeg*.so', '*turbojpeg*.so.*')
    seen = set()
    for root in roots:
        for directory in (root, root / 'turbojpeg.libs', root / 'turbojpeg.dylibs', root / '.dylibs',
                          root / 'lib', root / 'turbojpeg' / '.dylibs',
                          root.parent / 'Frameworks'):
            if directory in seen:
                continue
            seen.add(directory)
            for pattern in suffixes:
                for path in sorted(directory.glob(pattern)):
                    if path.is_file():
                        yield str(path)
    try:
        found = find_library('turbojpeg')
    except (OSError, ValueError):
        found = None
    if found:
        yield found


@lru_cache(maxsize=1)
def _load_library():
    errors = []
    required = ('tjInitCompress', 'tjCompress2', 'tjBufSize', 'tjAlloc', 'tjFree', 'tjDestroy')
    for candidate in dict.fromkeys(_library_candidates()):
        try:
            library = ctypes.CDLL(candidate)
            if not all(hasattr(library, name) for name in required):
                continue
            library.tjInitCompress.argtypes = []
            library.tjInitCompress.restype = _PTR
            library.tjCompress2.argtypes = [
                _PTR, _PTR, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.POINTER(_PTR), ctypes.POINTER(_ULONG), ctypes.c_int,
                ctypes.c_int, ctypes.c_int]
            library.tjCompress2.restype = ctypes.c_int
            library.tjBufSize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
            library.tjBufSize.restype = _ULONG
            library.tjAlloc.argtypes = [ctypes.c_int]
            library.tjAlloc.restype = _PTR
            library.tjFree.argtypes = [_PTR]
            library.tjFree.restype = None
            library.tjDestroy.argtypes = [_PTR]
            library.tjDestroy.restype = ctypes.c_int
            if hasattr(library, 'tjGetErrorStr2'):
                library.tjGetErrorStr2.argtypes = [_PTR]
                library.tjGetErrorStr2.restype = ctypes.c_char_p
            elif hasattr(library, 'tjGetErrorStr'):
                library.tjGetErrorStr.argtypes = []
                library.tjGetErrorStr.restype = ctypes.c_char_p
            return library, None
        except OSError as exc:
            errors.append(str(exc))
    reason = errors[-1] if errors else '未找到可用的 libturbojpeg 原生库'
    return None, reason


class _TurboEncoder:
    def __init__(self, library):
        self._library = library
        self._handle = library.tjInitCompress()
        self._buffer = _PTR()
        self._capacity = 0
        if not self._handle:
            raise OSError('TurboJPEG 无法创建压缩器')

    def _error(self):
        if hasattr(self._library, 'tjGetErrorStr2'):
            message = self._library.tjGetErrorStr2(self._handle)
        elif hasattr(self._library, 'tjGetErrorStr'):
            message = self._library.tjGetErrorStr()
        else:
            message = None
        return OSError(message.decode('utf-8', 'replace') if message else 'TurboJPEG 编码失败')

    def encode(self, frame, quality, subsampling):
        if not self._handle:
            raise OSError('TurboJPEG 压缩器已关闭')
        if not isinstance(frame, RawFrame):
            if frame.mode != 'RGB':
                frame = frame.convert('RGB')
            # Compatibility path. Native capture should supply RawFrame instead
            # to avoid this extra full-image pack/copy.
            frame = RawFrame(frame.tobytes(), frame.size, frame.width * 3, 'RGB')
        needed = self._library.tjBufSize(frame.width, frame.height, subsampling)
        if not 0 < needed <= 0x7FFFFFFF:
            raise OSError('TurboJPEG 目标缓冲区尺寸无效')
        if needed > self._capacity:
            replacement = self._library.tjAlloc(needed)
            if not replacement:
                raise OSError('TurboJPEG 无法分配目标缓冲区')
            self._library.tjFree(self._buffer)
            self._buffer = _PTR(replacement)
            self._capacity = needed
        output_size = _ULONG(self._capacity)
        source = ctypes.cast(ctypes.c_char_p(frame.data), _PTR)
        status = self._library.tjCompress2(
            self._handle, source, frame.width, frame.stride, frame.height,
            _PIXEL_FORMATS[frame.pixel_format][0], ctypes.byref(self._buffer),
            ctypes.byref(output_size), subsampling, quality, _NOREALLOC | _FASTDCT)
        if status:
            raise self._error()
        if not 0 < output_size.value <= self._capacity:
            raise OSError('TurboJPEG 返回了无效 JPEG 长度')
        # The compressor reuses its destination on the next call. The caller
        # owns these JPEG bytes, allowing encode and network send to overlap.
        return ctypes.string_at(self._buffer, output_size.value)

    def close(self):
        if self._buffer:
            self._library.tjFree(self._buffer)
            self._buffer = _PTR()
        if self._handle:
            self._library.tjDestroy(self._handle)
            self._handle = None
        self._capacity = 0


class JpegEncoder:
    """One encoder per capture worker, with a permanent per-session fallback."""

    def __init__(self):
        self._output = io.BytesIO()
        self._native = None
        self._closed = False
        self.fallback_reason = None
        try:
            library, reason = _load_library()
        except OSError as exc:
            library, reason = None, str(exc)
        if library is not None:
            try:
                self._native = _TurboEncoder(library)
            except OSError as exc:
                reason = str(exc)
        if self._native is None:
            self.fallback_reason = reason

    @property
    def name(self):
        return 'TurboJPEG' if self._native is not None else 'Pillow'

    def encode(self, frame, quality=85, subsampling=0):
        if self._closed:
            raise OSError('JPEG 编码器已关闭')
        if type(quality) is not int or not 1 <= quality <= 100 or subsampling not in (0, 1, 2):
            raise ValueError('Invalid JPEG quality or subsampling')
        if self._native is not None:
            try:
                return self._native.encode(frame, quality, subsampling)
            except (OSError, ValueError, TypeError, OverflowError) as exc:
                self.fallback_reason = str(exc)
                self._native.close()
                self._native = None
        image = frame.to_image() if isinstance(frame, RawFrame) else frame
        if image.mode != 'RGB':
            image = image.convert('RGB')
        self._output.seek(0)
        image.save(self._output, 'JPEG', quality=quality, subsampling=subsampling,
                   optimize=False, progressive=False)
        self._output.truncate()
        return self._output.getvalue()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._native is not None:
            self._native.close()
            self._native = None
        self._output.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
