"""Native Nuitka builds. No project dependencies are imported by this driver."""
import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {'Darwin': 'macos', 'Linux': 'linux', 'Windows': 'windows'}
BACKENDS = {'macos': 'darwin', 'linux': 'xorg', 'windows': 'win32'}
PYTHON_INFO = (
    'import json,sys,platform; print(json.dumps({'
    '"prefix":sys.base_prefix,"exec_prefix":sys.base_exec_prefix,'
    '"version":list(sys.version_info[:2]),"implementation":sys.implementation.name,'
    '"machine":platform.machine().lower()}))'
)


def architecture():
    name = platform.machine().lower()
    return {'aarch64': 'arm64', 'amd64': 'x86_64'}.get(name, name)


def environment_matches(info):
    # Framework venvs can copy the executable and report that copy as
    # _base_executable. Compare the underlying Python installation instead.
    return (info['version'] == list(sys.version_info[:2])
            and info['implementation'] == sys.implementation.name
            and info['machine'] == platform.machine().lower()
            and Path(info['prefix']).resolve() == Path(sys.base_prefix).resolve()
            and Path(info['exec_prefix']).resolve() == Path(sys.base_exec_prefix).resolve())


def find_msvc():
    """Locate VS 2022+ C++ tools, including installations not on PATH."""
    roots = (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles'))
    candidates = [Path(root) / 'Microsoft Visual Studio/Installer/vswhere.exe'
                  for root in roots if root]
    on_path = shutil.which('vswhere.exe')
    if on_path:
        candidates.insert(0, Path(on_path))
    component = ('Microsoft.VisualStudio.Component.VC.Tools.ARM64' if architecture() == 'arm64'
                 else 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64')
    for executable in candidates:
        if not executable.is_file():
            continue
        result = subprocess.run([str(executable), '-utf8', '-latest', '-products', '*', '-version', '[17.0,)',
                                 '-requires', component, '-property', 'installationPath'],
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            vcvars = Path(result.stdout.strip()) / 'VC/Auxiliary/Build/vcvarsall.bat'
            if vcvars.is_file():
                return vcvars
    return None


def windows_compiler(requested, check=True):
    """Resolve compiler policy before Nuitka spends time generating C code."""
    vcvars = find_msvc() if requested == 'auto' or (check and requested != 'mingw64') else None
    mingw_supported = sys.version_info[:2] <= (3, 12) and architecture() in ('x86_64', 'x86', 'i386', 'i686')
    selected = ('msvc' if vcvars or not mingw_supported else 'mingw64') if requested == 'auto' else requested
    if selected == 'mingw64':
        if not mingw_supported:
            raise RuntimeError('MinGW64 构建仅支持 Python 3.12 及以下的 x86/x64 Python。'
                               '当前环境请安装 Visual Studio 2022+ C++ Build Tools 和 Windows SDK，使用 -Compiler msvc。')
        return selected
    if not check:
        return selected
    guide = ('请在 Visual Studio Installer 安装或修改 Build Tools 2022+，勾选“使用 C++ 的桌面开发”、'
             'MSVC 编译工具和 Windows 10/11 SDK，然后在普通 PowerShell 中重试。'
             'Python 3.10–3.12 x64 也可选择 -Compiler mingw64，由 Nuitka 下载匹配的工具链。')
    if vcvars is None:
        raise RuntimeError('未找到 Visual Studio 2022+ C++ 编译工具。' + guide)
    arch = {'x86_64':'x64', 'arm64':'arm64'}.get(architecture(), 'x86')
    active = os.environ.get('VSCMD_ARG_TGT_ARCH')
    if active and active.lower() != arch:
        raise RuntimeError(f'当前开发者终端目标为 {active}，Python 需要 {arch}。请退出该终端，使用普通 PowerShell 重试。')
    with tempfile.TemporaryDirectory(prefix='share-input-msvc-') as temp:
        (Path(temp) / 'probe.c').write_text('#include <windows.h>\nint main(void) { return 0; }\n', encoding='ascii')
        probe = f'call "{vcvars}" {arch} && cl /nologo probe.c /Foprobe.obj /Feprobe.exe'
        result = subprocess.run(['cmd.exe', '/d', '/c', probe], cwd=temp,
                                capture_output=True, text=True, errors='replace', timeout=60)
        if result.returncode:
            detail = (result.stdout + result.stderr)[-3000:]
            raise RuntimeError('MSVC/Windows SDK 编译链接预检查失败。' + guide + '\n' + detail)
    return selected


def command(target, python, output, jobs, compiler='msvc', fast_jpeg=False):
    args = [str(python), '-m', 'nuitka',
            '--mode=app-dist' if target == 'macos' else '--mode=standalone',
            '--enable-plugin=pyside6',
            '--include-qt-plugins=platforms,platforminputcontexts,iconengines,imageformats,multimedia',
            '--noinclude-qt-plugins=styles,tls',
            '--noinclude-dlls=*qtvirtualkeyboard*',
            '--noinclude-qt-translations', '--lto=yes',
            '--python-flag=no_docstrings', '--python-flag=isolated',
            '--noinclude-pytest-mode=nofollow', '--noinclude-unittest-mode=nofollow',
            '--nofollow-import-to=tkinter,PyQt5,PyQt6,PySide2,IPython,numpy',
            f'--jobs={jobs}', f'--output-dir={output}', '--output-folder-name=ShareInput',
            '--output-filename=ShareInput.exe' if target == 'windows' else '--output-filename=ShareInput',
            f'--report={output / "compilation-report.xml"}',
            f'--user-package-configuration-file={ROOT / "tools/nuitka-package.config.yml"}',
            f'--include-data-dir={ROOT / "src/assets"}=assets']
    # These imports are selected dynamically by pynput / mss at runtime.
    backend = BACKENDS[target]
    modules = ['server', 'client', 'src.qt_runtime',
               f'pynput.keyboard._{backend}', f'pynput.mouse._{backend}',
               f'pynput._util.{backend}',
               f'mss.{"windows" if target == "windows" else "linux" if target == "linux" else "darwin"}']
    if target == 'macos':
        modules += ['ScreenCaptureKit', 'CoreMedia', 'dispatch']
    if fast_jpeg:
        # Include the wheel's native extension and its linked libturbojpeg.
        # Runtime calls the shared C API directly, without importing its wrapper.
        modules.append('turbojpeg')
    # Windows CPython embeds _json. Only external extensions need explicit inclusion.
    if '_json' not in sys.builtin_module_names:
        modules.append('_json')
    # main.py adds src to sys.path at runtime; make that search path explicit at build time.
    excluded = {'__init__', 'server', 'client'}
    modules += [path.stem for path in sorted((ROOT / 'src').glob('*.py'))
                if path.stem not in excluded
                and (target == 'macos' or not path.stem.startswith('macos_'))
                and (target == 'windows' or not path.stem.startswith('windows_'))
                and (target == 'linux' or not path.stem.startswith('linux_'))]
    args += [f'--include-module={name}' for name in modules]
    if target == 'macos':
        # Nuitka's downloaded Intel ccache can start Apple's tools under
        # Rosetta, while current Command Line Tools ship ARM-only libxcrun.
        args += ['--disable-cache=ccache', '--macos-app-name=ShareInput',
                 f'--macos-app-icon={ROOT / "packaging/icons/app-icon.icns"}',
                 '--macos-signed-app-name=org.shareinput.desktop',
                 '--macos-app-version=1.0.0',
                 '--macos-app-category-type=public.app-category.utilities']
    elif target == 'windows':
        args += ['--mingw64' if compiler == 'mingw64' else '--msvc=latest', '--windows-console-mode=attach',
                 f'--windows-icon-from-ico={ROOT / "packaging/icons/app-icon.ico"}']
    else:
        args += ['--include-package=Xlib.ext']
    return args + [str(ROOT / 'main.py')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=BACKENDS, required=True)
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--skip-install', action='store_true', help='Reuse installed build dependencies')
    parser.add_argument('--dry-run', action='store_true', help='Print command without installing or compiling')
    parser.add_argument('--windows-compiler', choices=('msvc', 'mingw64', 'auto'), default='msvc')
    args = parser.parse_args()
    if TARGETS.get(platform.system()) != args.target:
        parser.error('Run each script on its matching OS; cross compilation is not supported.')
    if sys.version_info[:2] not in ((3,10), (3,11), (3,12), (3,13)):
        parser.error('Use CPython 3.10-3.13 (recommended: python.org CPython 3.12).')
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    tag = f'{args.target}-{architecture()}'
    env_dir = ROOT / '.build-venv' / tag
    python = env_dir / ('Scripts/python.exe' if args.target == 'windows' else 'bin/python')
    output = ROOT / 'dist' / tag
    compiler = args.windows_compiler
    if args.target == 'windows':
        try:
            compiler = windows_compiler(compiler, check=not args.dry_run)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            parser.error(str(exc))
    cmd = command(args.target, python, output, args.jobs, compiler)
    if args.dry_run:
        print(subprocess.list2cmdline(cmd) if args.target == 'windows' else shlex.join(cmd))
        return
    if args.target == 'macos' and not shutil.which('clang'):
        parser.error('Install Xcode Command Line Tools first: xcode-select --install')
    if args.target == 'linux' and (not shutil.which('gcc') or not shutil.which('patchelf')):
        parser.error('Install build-essential, python3-dev, python3-venv and patchelf first.')
    if not python.exists():
        if args.skip_install:
            parser.error('Build environment missing; run once without --skip-install.')
        venv.EnvBuilder(with_pip=True).create(env_dir)
    info = json.loads(subprocess.check_output([str(python), '-c', PYTHON_INFO], text=True))
    if not environment_matches(info):
        parser.error(f'Build environment uses another Python installation or architecture. '
                     f'Environment: {info["prefix"]} (Python {info["version"]}, {info["machine"]}); '
                     f'current: {sys.base_prefix} (Python {list(sys.version_info[:2])}, {platform.machine()}). '
                     f'Remove {env_dir} and retry only if you intended to change Python.')
    if not args.skip_install:
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt'),
                        '-r', str(ROOT / 'requirements-build.txt'),
                        'PySide6-Essentials==6.11.3', 'PySide6-Addons==6.11.3', 'shiboken6==6.11.3'], check=True)
        optional = subprocess.run([str(python), '-m', 'pip', 'install', '--only-binary=:all:',
                                   'turbojpeg==0.0.2'])
        if optional.returncode:
            print('Optional TurboJPEG wheel unavailable; continuing with Pillow JPEG encoding.')
    fast_jpeg = subprocess.check_output([
        str(python), '-c',
        'import importlib.util; print(int(importlib.util.find_spec("turbojpeg") is not None))',
    ], text=True).strip() == '1'
    cmd = command(args.target, python, output, args.jobs, compiler, fast_jpeg=fast_jpeg)
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join((str(ROOT), str(ROOT / 'src')))
    # Do not inherit local test backend overrides into the executable's build analysis.
    for key in ('PYNPUT_BACKEND', 'PYNPUT_BACKEND_KEYBOARD', 'PYNPUT_BACKEND_MOUSE'):
        env.pop(key, None)
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)
    with (output / 'build-requirements.txt').open('w', encoding='utf-8') as manifest:
        subprocess.run([str(python), '-m', 'pip', 'freeze'], check=True, stdout=manifest)
    (output / 'build-info.json').write_text(json.dumps({
        'target': args.target, 'architecture': architecture(), 'command': cmd,
        'python': sys.version, 'host': platform.platform(),
    }, indent=2), encoding='utf-8')
    print(f'Build complete: {output}')
    print('Distribute the entire ShareInput.app / ShareInput.dist directory, not just its executable.')


if __name__ == '__main__':
    main()
