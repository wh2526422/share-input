"""Synchronous Desktop Duplication capture with GPU downsampling.

The capture thread owns one D3D11 device and one set of reusable textures. Only
the scaled image is read back into CPU memory; no capture thread or frame queue
is created here. Unsupported desktops/drivers raise OSError for the caller's
GDI fallback. Windows DLLs and calling conventions are resolved at construction.
"""
import ctypes
import uuid


PTR = ctypes.c_void_p
PPTR = ctypes.POINTER(PTR)
UINT = ctypes.c_uint32
INT = HRESULT = ctypes.c_int32
FLOAT = ctypes.c_float
SIZE_T = ctypes.c_size_t

DXGI_ERROR_NOT_FOUND = 0x887A0002
DXGI_ERROR_WAIT_TIMEOUT = 0x887A0027
BGRA8 = 87  # DXGI_FORMAT_B8G8R8A8_UNORM
ROTATION_IDENTITY = 1

# ABI slots counted from IUnknown, including every inherited method, in the
# Microsoft WinSDK C vtables:
# https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/shared/dxgi.h
# https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/shared/dxgi1_2.h
# https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/um/d3d11.h
# ID3DBlob (ID3D10Blob): um/d3dcommon.h in the same SDK directory.
QUERY_INTERFACE = 0
RELEASE = 2
FACTORY_ENUM_ADAPTERS1 = 12
ADAPTER_ENUM_OUTPUTS = 7
OUTPUT_GET_DESC = 7
OUTPUT1_DUPLICATE_OUTPUT = 22
DUPLICATION_GET_DESC = 7
DUPLICATION_ACQUIRE = 8
DUPLICATION_RELEASE_FRAME = 14
TEXTURE_GET_DESC = 10
DEVICE_CREATE_TEXTURE2D = 5
DEVICE_CREATE_SRV = 7
DEVICE_CREATE_RTV = 9
DEVICE_CREATE_VS = 12
DEVICE_CREATE_PS = 15
DEVICE_CREATE_SAMPLER = 23
CONTEXT_PS_SET_RESOURCES = 8
CONTEXT_PS_SET_SHADER = 9
CONTEXT_PS_SET_SAMPLERS = 10
CONTEXT_VS_SET_SHADER = 11
CONTEXT_DRAW = 13
CONTEXT_MAP = 14
CONTEXT_UNMAP = 15
CONTEXT_SET_TOPOLOGY = 24
CONTEXT_SET_RENDER_TARGETS = 33
CONTEXT_SET_VIEWPORTS = 44
CONTEXT_COPY_RESOURCE = 47
CONTEXT_CLEAR_STATE = 110
BLOB_GET_POINTER = 3
BLOB_GET_SIZE = 4


class GUID(ctypes.Structure):
    _fields_ = [('Data1', UINT), ('Data2', ctypes.c_uint16),
                ('Data3', ctypes.c_uint16), ('Data4', ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


IID_FACTORY1 = GUID.parse('770aae78-f26f-4dba-a829-253c83d1b387')
IID_OUTPUT1 = GUID.parse('00cddea8-939b-4b83-a340-a685226666cc')
IID_TEXTURE2D = GUID.parse('6f15aaf2-d208-4e89-9ab4-489535d34f9c')


class RECT(ctypes.Structure):
    _fields_ = [('left', INT), ('top', INT), ('right', INT), ('bottom', INT)]


class POINT(ctypes.Structure):
    _fields_ = [('x', INT), ('y', INT)]


class OUTPUT_DESC(ctypes.Structure):
    # WCHAR is always 16 bits in the Windows ABI, including on 64-bit Windows.
    _fields_ = [('DeviceName', ctypes.c_uint16 * 32), ('DesktopCoordinates', RECT),
                ('AttachedToDesktop', INT), ('Rotation', UINT), ('Monitor', PTR)]


class RATIONAL(ctypes.Structure):
    _fields_ = [('Numerator', UINT), ('Denominator', UINT)]


class MODE_DESC(ctypes.Structure):
    _fields_ = [('Width', UINT), ('Height', UINT), ('RefreshRate', RATIONAL),
                ('Format', UINT), ('ScanlineOrdering', UINT), ('Scaling', UINT)]


class DUPLICATION_DESC(ctypes.Structure):
    _fields_ = [('ModeDesc', MODE_DESC), ('Rotation', UINT), ('DesktopImageInSystemMemory', INT)]


class POINTER_POSITION(ctypes.Structure):
    _fields_ = [('Position', POINT), ('Visible', INT)]


class FRAME_INFO(ctypes.Structure):
    _fields_ = [('LastPresentTime', ctypes.c_int64), ('LastMouseUpdateTime', ctypes.c_int64),
                ('AccumulatedFrames', UINT), ('RectsCoalesced', INT),
                ('ProtectedContentMaskedOut', INT), ('PointerPosition', POINTER_POSITION),
                ('TotalMetadataBufferSize', UINT), ('PointerShapeBufferSize', UINT)]


class SAMPLE_DESC(ctypes.Structure):
    _fields_ = [('Count', UINT), ('Quality', UINT)]


class TEXTURE_DESC(ctypes.Structure):
    _fields_ = [('Width', UINT), ('Height', UINT), ('MipLevels', UINT), ('ArraySize', UINT),
                ('Format', UINT), ('SampleDesc', SAMPLE_DESC), ('Usage', UINT),
                ('BindFlags', UINT), ('CPUAccessFlags', UINT), ('MiscFlags', UINT)]


class SAMPLER_DESC(ctypes.Structure):
    _fields_ = [('Filter', UINT), ('AddressU', UINT), ('AddressV', UINT), ('AddressW', UINT),
                ('MipLODBias', FLOAT), ('MaxAnisotropy', UINT), ('ComparisonFunc', UINT),
                ('BorderColor', FLOAT * 4), ('MinLOD', FLOAT), ('MaxLOD', FLOAT)]


class VIEWPORT(ctypes.Structure):
    _fields_ = [('TopLeftX', FLOAT), ('TopLeftY', FLOAT), ('Width', FLOAT),
                ('Height', FLOAT), ('MinDepth', FLOAT), ('MaxDepth', FLOAT)]


class MAPPED_RESOURCE(ctypes.Structure):
    _fields_ = [('pData', PTR), ('RowPitch', UINT), ('DepthPitch', UINT)]


def _check(result, operation):
    code = result & 0xFFFFFFFF
    if code & 0x80000000:
        raise OSError(f'Windows DXGI {operation} failed (HRESULT 0x{code:08X})')


class DXGICapture:
    def __init__(self, display, resolution):
        values = [display.get(key) for key in ('left', 'top', 'width', 'height')]
        if (not all(type(value) is int for value in values)
                or values[2] <= 0 or values[3] <= 0
                or len(resolution) != 2
                or not all(type(value) is int and value > 0 for value in resolution)):
            raise ValueError('Invalid DXGI capture display or resolution')
        self.display = dict(zip(('left', 'top', 'width', 'height'), values))
        self._source_size = tuple(values[2:])
        scale = min(1.0, resolution[0] / values[2], resolution[1] / values[3])
        self.size = tuple(max(1, round(dimension * scale)) for dimension in values[2:])
        self._scaled = self.size != self._source_size
        self._refs = []
        self._closed = False
        self._frame_held = self._has_frame = False
        self._context = self._duplication = None
        self._release_frame = None
        try:
            self._load_api()
            self._find_output()
            self._create_device()
            self._create_duplication()
            self._create_pipeline()
        except BaseException:
            self.close()
            raise

    def _load_api(self):
        loader = getattr(ctypes, 'WinDLL', None)
        self._functype = getattr(ctypes, 'WINFUNCTYPE', None)
        if loader is None or self._functype is None:
            raise OSError('Windows Desktop Duplication is only available on Windows')
        self._dxgi = loader('dxgi.dll', use_last_error=True)
        self._d3d11 = loader('d3d11.dll', use_last_error=True)
        self._dxgi.CreateDXGIFactory1.argtypes = [ctypes.POINTER(GUID), PPTR]
        self._dxgi.CreateDXGIFactory1.restype = HRESULT
        self._d3d11.D3D11CreateDevice.argtypes = [
            PTR, UINT, PTR, UINT, ctypes.POINTER(UINT), UINT, UINT, PPTR,
            ctypes.POINTER(UINT), PPTR]
        self._d3d11.D3D11CreateDevice.restype = HRESULT
        if self._scaled:
            self._compiler = loader('d3dcompiler_47.dll', use_last_error=True)
            self._compiler.D3DCompile.argtypes = [
                PTR, SIZE_T, ctypes.c_char_p, PTR, PTR, ctypes.c_char_p,
                ctypes.c_char_p, UINT, UINT, PPTR, PPTR]
            self._compiler.D3DCompile.restype = HRESULT

    def _method(self, interface, slot, result, *arguments):
        table = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(PTR))).contents
        function = self._functype(result, PTR, *arguments)(table[slot])
        return lambda *values: function(interface, *values)

    def _release(self, interface):
        if interface:
            self._method(interface, RELEASE, UINT)()

    def _own(self, interface):
        if interface:
            self._refs.append(interface)
        return interface

    def _query(self, interface, iid):
        result = PTR()
        status = self._method(interface, QUERY_INTERFACE, HRESULT, ctypes.POINTER(GUID), PPTR)(
            ctypes.byref(iid), ctypes.byref(result))
        try:
            _check(status, 'QueryInterface')
            if not result:
                raise OSError('Windows DXGI QueryInterface returned no interface')
            return result
        except BaseException:
            self._release(result)
            raise

    def _find_output(self):
        factory = PTR()
        status = self._dxgi.CreateDXGIFactory1(ctypes.byref(IID_FACTORY1), ctypes.byref(factory))
        self._own(factory)
        _check(status, 'CreateDXGIFactory1')
        if not factory:
            raise OSError('Windows DXGI returned no factory')
        enum_adapters = self._method(factory, FACTORY_ENUM_ADAPTERS1, HRESULT, UINT, PPTR)
        expected = (self.display['left'], self.display['top'],
                    self.display['left'] + self.display['width'],
                    self.display['top'] + self.display['height'])
        adapter_index = 0
        while True:
            adapter = PTR()
            status = enum_adapters(adapter_index, ctypes.byref(adapter))
            if (status & 0xFFFFFFFF) == DXGI_ERROR_NOT_FOUND:
                break
            try:
                _check(status, 'EnumAdapters1')
                if not adapter:
                    raise OSError('Windows DXGI returned no adapter')
                enum_outputs = self._method(adapter, ADAPTER_ENUM_OUTPUTS, HRESULT, UINT, PPTR)
                output_index = 0
                while True:
                    output = PTR()
                    status = enum_outputs(output_index, ctypes.byref(output))
                    if (status & 0xFFFFFFFF) == DXGI_ERROR_NOT_FOUND:
                        break
                    try:
                        _check(status, 'EnumOutputs')
                        if not output:
                            raise OSError('Windows DXGI returned no output')
                        desc = OUTPUT_DESC()
                        _check(self._method(output, OUTPUT_GET_DESC, HRESULT,
                                            ctypes.POINTER(OUTPUT_DESC))(ctypes.byref(desc)), 'GetOutputDesc')
                        rect = desc.DesktopCoordinates
                        if desc.AttachedToDesktop and (rect.left, rect.top, rect.right, rect.bottom) == expected:
                            if desc.Rotation != ROTATION_IDENTITY:
                                raise OSError('Windows DXGI rotated monitors use GDI capture')
                            self._adapter = self._own(adapter)
                            adapter = None
                            self._output = self._own(self._query(output, IID_OUTPUT1))
                            return
                    finally:
                        self._release(output)
                    output_index += 1
            finally:
                self._release(adapter)
            adapter_index += 1
        raise OSError('Windows DXGI could not match the monitor physical coordinates')

    def _create_device(self):
        device, context, actual_level = PTR(), PTR(), UINT()
        # Explicit output adapter requires D3D_DRIVER_TYPE_UNKNOWN (0).
        # BGRA_SUPPORT=0x20; SDK_VERSION=7. SM4 shaders support FL10 and FL11.
        levels = (UINT * 3)(0xB000, 0xA100, 0xA000)
        status = self._d3d11.D3D11CreateDevice(
            self._adapter, 0, None, 0x20, levels, len(levels), 7,
            ctypes.byref(device), ctypes.byref(actual_level), ctypes.byref(context))
        self._device = self._own(device)
        self._context = self._own(context)
        _check(status, 'D3D11CreateDevice')
        if not device or not context:
            raise OSError('Windows DXGI returned no Direct3D device/context')

    def _create_duplication(self):
        duplication = PTR()
        status = self._method(self._output, OUTPUT1_DUPLICATE_OUTPUT, HRESULT, PTR, PPTR)(
            self._device, ctypes.byref(duplication))
        self._duplication = self._own(duplication)
        _check(status, 'DuplicateOutput')
        if not duplication:
            raise OSError('Windows DXGI returned no desktop duplication interface')
        self._release_frame = self._method(duplication, DUPLICATION_RELEASE_FRAME, HRESULT)
        self._acquire = self._method(duplication, DUPLICATION_ACQUIRE, HRESULT,
                                     UINT, ctypes.POINTER(FRAME_INFO), PPTR)
        desc = DUPLICATION_DESC()
        self._method(duplication, DUPLICATION_GET_DESC, None,
                     ctypes.POINTER(DUPLICATION_DESC))(ctypes.byref(desc))
        if (desc.Rotation != ROTATION_IDENTITY or desc.ModeDesc.Format != BGRA8
                or (desc.ModeDesc.Width, desc.ModeDesc.Height) != self._source_size):
            raise OSError('Windows DXGI desktop geometry/format requires GDI capture')

    def _texture(self, size, usage, bind_flags, cpu_access=0):
        desc = TEXTURE_DESC()
        desc.Width, desc.Height = size
        desc.MipLevels = desc.ArraySize = desc.SampleDesc.Count = 1
        desc.Format = BGRA8
        desc.Usage, desc.BindFlags, desc.CPUAccessFlags = usage, bind_flags, cpu_access
        texture = PTR()
        status = self._method(self._device, DEVICE_CREATE_TEXTURE2D, HRESULT,
                              ctypes.POINTER(TEXTURE_DESC), PTR, PPTR)(
            ctypes.byref(desc), None, ctypes.byref(texture))
        self._own(texture)
        _check(status, 'CreateTexture2D')
        if not texture:
            raise OSError('Windows DXGI returned no texture')
        return texture

    def _view(self, texture, slot, operation):
        view = PTR()
        status = self._method(self._device, slot, HRESULT, PTR, PTR, PPTR)(
            texture, None, ctypes.byref(view))
        self._own(view)
        _check(status, operation)
        if not view:
            raise OSError(f'Windows DXGI {operation} returned no view')
        return view

    def _shader(self, source, entry, target, slot):
        source = source.encode('ascii')
        source_buffer = ctypes.create_string_buffer(source)
        blob, errors, shader = PTR(), PTR(), PTR()
        try:
            status = self._compiler.D3DCompile(
                ctypes.byref(source_buffer), len(source), b'ShareInput', None, None,
                entry, target, (1 << 11) | (1 << 15), 0, ctypes.byref(blob), ctypes.byref(errors))
            if status < 0:
                detail = ''
                if errors:
                    address = self._method(errors, BLOB_GET_POINTER, PTR)()
                    length = self._method(errors, BLOB_GET_SIZE, SIZE_T)()
                    if address and length:
                        detail = ctypes.string_at(address, min(length, 2048)).decode('utf-8', 'replace')
                raise OSError(f'Windows DXGI D3DCompile {target.decode()} failed '
                              f'(HRESULT 0x{status & 0xFFFFFFFF:08X}): {detail}')
            if not blob:
                raise OSError('Windows DXGI D3DCompile returned no shader bytecode')
            address = self._method(blob, BLOB_GET_POINTER, PTR)()
            length = self._method(blob, BLOB_GET_SIZE, SIZE_T)()
            status = self._method(self._device, slot, HRESULT, PTR, SIZE_T, PTR, PPTR)(
                address, length, None, ctypes.byref(shader))
            self._own(shader)
            _check(status, 'CreateShader')
            if not shader:
                raise OSError('Windows DXGI returned no shader')
            return shader
        finally:
            self._release(errors)
            self._release(blob)

    def _create_pipeline(self):
        self._staging = self._texture(self.size, 3, 0, 0x20000)
        context = self._context
        self._copy = self._method(context, CONTEXT_COPY_RESOURCE, None, PTR, PTR)
        self._map = self._method(context, CONTEXT_MAP, HRESULT, PTR, UINT, UINT, UINT,
                                 ctypes.POINTER(MAPPED_RESOURCE))
        self._unmap = self._method(context, CONTEXT_UNMAP, None, PTR, UINT)
        if not self._scaled:
            # Native resolution needs only one readback texture, no shaders,
            # render target, source copy or compiler DLL.
            return
        # DEFAULT source with SHADER_RESOURCE binding, scaled DEFAULT render
        # target, and one CPU_READ STAGING texture. Reuse all three per frame.
        self._source = self._texture(self._source_size, 0, 0x08)
        self._srv = self._view(self._source, DEVICE_CREATE_SRV, 'CreateShaderResourceView')
        self._render = self._texture(self.size, 0, 0x20)
        self._rtv = self._view(self._render, DEVICE_CREATE_RTV, 'CreateRenderTargetView')
        vertex = '''
float4 main(uint id : SV_VertexID) : SV_Position {
    float2 corner = float2((id << 1) & 2, id & 2);
    return float4(corner.x * 2 - 1, 1 - corner.y * 2, 0, 1);
}
'''
        # Sample across each destination pixel's footprint. A 1.55x reduction
        # uses four bilinear taps; larger reductions use more taps to avoid
        # dropping thin text/lines. Counts are constant when compiling shaders,
        # bounded to 8 per axis; no intermediate image or extra texture is used.
        taps_x = min(8, (self._source_size[0] + self.size[0] - 1) // self.size[0])
        taps_y = min(8, (self._source_size[1] + self.size[1] - 1) // self.size[1])
        pixel = f'''
Texture2D<float4> desktop : register(t0);
SamplerState linearClamp : register(s0);
float4 main(float4 position : SV_Position) : SV_Target {{
    float2 uv = position.xy / float2({self.size[0]}.0, {self.size[1]}.0);
    float3 color = float3(0, 0, 0);
    [unroll] for (int y = 0; y < {taps_y}; ++y) {{
        [unroll] for (int x = 0; x < {taps_x}; ++x) {{
            float2 offset = (float2(x + 0.5, y + 0.5)
                            / float2({taps_x}.0, {taps_y}.0) - 0.5)
                            / float2({self.size[0]}.0, {self.size[1]}.0);
            color += desktop.SampleLevel(linearClamp, uv + offset, 0).rgb;
        }}
    }}
    return float4(color / {taps_x * taps_y}.0, 1);
}}
'''
        vs = self._shader(vertex, b'main', b'vs_4_0', DEVICE_CREATE_VS)
        ps = self._shader(pixel, b'main', b'ps_4_0', DEVICE_CREATE_PS)
        sampler_desc = SAMPLER_DESC()
        sampler_desc.Filter = 0x15  # MIN_MAG_MIP_LINEAR
        sampler_desc.AddressU = sampler_desc.AddressV = sampler_desc.AddressW = 3  # CLAMP
        sampler_desc.MaxAnisotropy = sampler_desc.ComparisonFunc = 1
        sampler_desc.MaxLOD = 3.402823466e+38
        sampler = PTR()
        status = self._method(self._device, DEVICE_CREATE_SAMPLER, HRESULT,
                              ctypes.POINTER(SAMPLER_DESC), PPTR)(
            ctypes.byref(sampler_desc), ctypes.byref(sampler))
        self._own(sampler)
        _check(status, 'CreateSamplerState')
        if not sampler:
            raise OSError('Windows DXGI returned no sampler')
        self._method(context, CONTEXT_VS_SET_SHADER, None, PTR, PPTR, UINT)(vs, None, 0)
        self._method(context, CONTEXT_PS_SET_SHADER, None, PTR, PPTR, UINT)(ps, None, 0)
        self._method(context, CONTEXT_PS_SET_SAMPLERS, None, UINT, UINT, PPTR)(0, 1, ctypes.byref(sampler))
        self._method(context, CONTEXT_SET_TOPOLOGY, None, UINT)(4)  # TRIANGLELIST
        viewport = VIEWPORT(0, 0, *self.size, 0, 1)
        self._method(context, CONTEXT_SET_VIEWPORTS, None, UINT, ctypes.POINTER(VIEWPORT))(
            1, ctypes.byref(viewport))
        self._method(context, CONTEXT_SET_RENDER_TARGETS, None, UINT, PPTR, PTR)(
            1, ctypes.byref(self._rtv), None)
        self._set_source = self._method(context, CONTEXT_PS_SET_RESOURCES, None, UINT, UINT, PPTR)
        self._draw = self._method(context, CONTEXT_DRAW, None, UINT, UINT)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def grab(self, raw=False):
        if self._closed:
            raise OSError('Windows DXGI capture is closed')
        resource, texture = PTR(), PTR()
        try:
            info = FRAME_INFO()
            status = self._acquire(10, ctypes.byref(info), ctypes.byref(resource))
            if (status & 0xFFFFFFFF) == DXGI_ERROR_WAIT_TIMEOUT:
                return None
            _check(status, 'AcquireNextFrame')
            self._frame_held = True
            try:
                # Pointer-only updates need no video packet. Always read the
                # first successful acquisition, even if its timestamp is zero.
                if self._has_frame and not info.LastPresentTime:
                    return None
                if not resource:
                    raise OSError('Windows DXGI acquired no desktop resource')
                texture = self._query(resource, IID_TEXTURE2D)
                desc = TEXTURE_DESC()
                self._method(texture, TEXTURE_GET_DESC, None, ctypes.POINTER(TEXTURE_DESC))(
                    ctypes.byref(desc))
                if ((desc.Width, desc.Height) != self._source_size or desc.Format != BGRA8
                        or desc.MipLevels != 1 or desc.ArraySize != 1 or desc.SampleDesc.Count != 1):
                    raise OSError('Windows DXGI acquired incompatible desktop geometry/format')
                if self._scaled:
                    # Unbind the source before writing it, then sample into the
                    # target-size render texture. The acquired texture need not
                    # support a shader-resource view itself.
                    empty = PTR()
                    self._set_source(0, 1, ctypes.byref(empty))
                    self._copy(self._source, texture)
                    self._set_source(0, 1, ctypes.byref(self._srv))
                    self._draw(3, 0)
                    self._copy(self._staging, self._render)
                else:
                    self._copy(self._staging, texture)
                mapped = MAPPED_RESOURCE()
                _check(self._map(self._staging, 0, 1, 0, ctypes.byref(mapped)), 'Map')  # MAP_READ
                try:
                    if not mapped.pData or mapped.RowPitch < self.size[0] * 4:
                        raise OSError('Windows DXGI returned invalid mapped pixels/RowPitch')
                    pixels = (ctypes.c_ubyte * (mapped.RowPitch * self.size[1])).from_address(mapped.pData)
                    if raw:
                        from jpeg_codec import RawFrame
                        image = RawFrame(bytes(pixels), self.size, mapped.RowPitch)
                    else:
                        from PIL import Image
                        image = Image.frombytes('RGB', self.size, pixels, 'raw', 'BGRX', mapped.RowPitch, 1)
                    self._has_frame = True
                    return image
                finally:
                    self._unmap(self._staging, 0)
            finally:
                self._release(texture)
                texture = None
                self._release(resource)
                resource = None
                self._frame_held = False
                _check(self._release_frame(), 'ReleaseFrame')
        except BaseException:
            self.close()
            raise
        finally:
            self._release(texture)
            self._release(resource)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._frame_held and self._release_frame is not None:
            self._frame_held = False
            try:
                self._release_frame()
            except Exception:
                pass
        # Bound views/shaders retain COM references. Clear them before releasing
        # our references, including when construction stopped part way through.
        if self._context:
            try:
                self._method(self._context, CONTEXT_CLEAR_STATE, None)()
            except Exception:
                pass
        while self._refs:
            try:
                self._release(self._refs.pop())
            except Exception:
                pass
        self._context = self._duplication = None
