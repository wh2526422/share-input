# 编译与发布

采用 Nuitka 4.3.5，优先考虑低延迟和运行性能，其次是资源占用，最后是启动速度。
使用 standalone 目录发布，macOS 使用 `.app`。接收方不需要安装 Python。
目前三个脚本统一使用 Nuitka，不切换为 PyInstaller。

## 在各自系统编译

使用 CPython 3.10–3.13，建议 python.org 的 3.12。Python 与目标 CPU 架构必须一致。
脚本自动创建 `.build-venv/<系统>-<架构>`，安装依赖，不改全局 Python 环境。
Qt 构建固定在 6.11.3，避开项目已经确认的 6.12.0 引用计数崩溃。
脚本会额外尝试安装 `turbojpeg==0.0.2` 的平台 wheel 并打包其原生库；缺少匹配 wheel 时继续使用 Pillow，不影响构建。
所有命令在项目目录执行；脚本也支持从其他工作目录启动。

| 平台 | 预备环境 | 构建命令 | 产物 |
| --- | --- | --- | --- |
| macOS | Xcode Command Line Tools：`xcode-select --install` | `./build_macos.sh` | `dist/macos-<架构>/ShareInput.app` |
| Linux | GCC、Python 开发头、venv、patchelf | `./build_linux.sh` | `dist/linux-<架构>/ShareInput.dist/ShareInput` |
| Windows | Python；Visual Studio 2022 Build Tools 的“使用 C++ 的桌面开发”、Windows SDK | `powershell -ExecutionPolicy Bypass -File .\build_windows.ps1` | `dist/windows-<架构>/ShareInput.dist/ShareInput.exe` |

Ubuntu/Debian 构建准备：

```sh
sudo apt install build-essential python3-dev python3-venv patchelf
```

Linux 运行仍需要桌面图形系统库。Qt X11 所需的 `libxcb-cursor0` 等依赖不会因编译消失。
Nuitka 的插件通常位于 `ShareInput.dist/PySide6/qt-plugins/platforms/`；
启动预检查使用 Qt 生效的搜索路径，分别报告插件文件缺失和插件依赖缺库。
剪贴板需要系统安装 `xclip` 或 `xsel`；屏幕采集和全局输入当前要求 X11。
在需要兼容的最旧 Linux 发行版上构建，以减少 glibc 版本不兼容。
Mac 发布构建建议使用 python.org Python；Homebrew Python 生成的程序可能仅支持较新 macOS。
ARM 和 x86_64 分别编译，本项目脚本不做跨系统交叉编译。

## 构建参数

```sh
PYTHON=/path/to/python3.12 ./build_macos.sh
./build_linux.sh --jobs 2
./build_macos.sh --skip-install  # 后续源码改动，复用构建依赖
./build_macos.sh --dry-run      # 仅查看命令，不安装、不编译
```

Windows 对应参数：`-Python C:\Python312\python.exe -Jobs 2 -Compiler msvc -SkipInstall -DryRun`。
更换 Python 小版本时使用新的构建虚拟环境，不能直接复用旧解释器的环境。
环境检查按基础安装目录、Python 版本和架构匹配；macOS 虚拟环境复制出来的启动器也可正常复用。

### Windows 编译器选择

默认仍使用 MSVC。脚本先通过 Visual Studio Installer 的 `vswhere` 寻找 VS 2022+ C++ 工具，
并编译、链接一个包含 Windows SDK 头文件的小程序；缺少编译器或 SDK 时会在安装依赖、生成 C 代码之前报出具体原因。
在 Visual Studio Installer 中勾选“使用 C++ 的桌面开发”、MSVC x64/x86 工具以及 Windows 10/11 SDK，建议安装默认英文语言包。
使用普通 PowerShell 运行即可，不必手动激活开发者终端；如果激活了目标架构错误的开发者终端，请重新打开普通 PowerShell。

```powershell
.\build_windows.ps1 -Compiler msvc
# Python 3.10–3.12 的 x64 环境，可以让 Nuitka 下载匹配的 MinGW64：
.\build_windows.ps1 -Compiler mingw64
# 优先已安装的 MSVC；没有 MSVC 且 Python/架构支持时选择 MinGW64：
.\build_windows.ps1 -Compiler auto
```

Python 3.13 及以上、ARM64 环境不使用 MinGW64 方案，需要安装 MSVC 工具和 SDK。
`auto` 不会安装 Visual Studio，也不会在检测到已安装但不完整的 MSVC/SDK 时静默更换工具链。
Miniconda 本身不等于编译失败；本次 `cannot locate suitable C compiler` 表示缺少可用 C 工具链。
若遇到 Conda 特有依赖兼容问题，建议改用 python.org 的 Python 3.12，并重新创建该架构的构建虚拟环境。

Windows CPython 通常将 `_json` 内建到解释器中，脚本不再对内建模块强加 `--include-module=_json`，
从而消除 `Did not follow import to unused '_json'` 警告；非内建平台仍显式包含 JSON 加速扩展。
该警告与 C 编译器缺失是两件事。编译器选择和预检查已做模拟回归，尚未在 Windows 实机编译验证。

每个输出目录另有 `compilation-report.xml`、`build-requirements.txt` 和 `build-info.json`，
记录实际模块、依赖版本、编译参数与主机环境。发布时只复制整个 `.app` 或 `.dist` 文件夹。
`.build-venv` 与 `ShareInput.build` 是构建缓存，不属于发布包，可以在构建结束后删除。

## 优化取舍

- 优先使用 Windows DXGI/GPU 缩放和 macOS ScreenCaptureKit 连续采集，降低同步读屏与复制开销；保持有界缓冲并及时消费最新画面。
- 采集/编码与发送并行，待发 JPEG 只保留最新一张；高精度帧率限速仅作用于 Windows 采集线程。
- 可用时复用 TurboJPEG 压缩器，直接编码 BGRA、释放 GIL，保持默认 quality 85 / 4:4:4；原生快路径失败后回退 Pillow。
- 编译 Python 业务代码，开启 LTO；保留原生 JSON 扩展处理协议。
- 使用 PySide6 Essentials 和需要的 Qt 插件族；移除无用的 Qt 样式、TLS、虚拟键盘插件与翻译。
- 排除 Tk、其他 Qt 绑定、NumPy、IPython 和测试框架；不打包当前机器的配置和连接历史。
- 不使用 onefile 自解压，也不使用 UPX。
- macOS 构建禁用 ccache，防止自动下载的 Intel 版缓存工具通过 Rosetta 调起开发工具，
  与新版本 ARM-only `libxcrun` 产生架构冲突。此项仅影响重复编译耗时，保留 LTO 和程序运行优化。
- JPEG、采屏和 Qt 绘制原本主要使用原生库，编译不能保证投屏帧率或内存显著改善。
  包体体积、空闲内存和投屏峰值内存需要分别测量；降低投屏分辨率、帧率通常更直接影响帧缓冲资源。

## 配置与首次运行

源码运行仍使用当前目录的 `config.json`；二进制使用用户可写配置位置：

| 系统 | 配置路径 |
| --- | --- |
| macOS | `~/Library/Application Support/share-input/config.json` |
| Linux | `${XDG_CONFIG_HOME:-~/.config}/share-input/config.json` |
| Windows | `%LOCALAPPDATA%\share-input\config.json` |

可将原来的配置手动复制到相应位置。程序不会把当前开发机的连接历史打进安装包。
Mac 首次运行需为 ShareInput 授予设备控制权限及屏幕录制权限。
请先把 `.app` 放到固定位置（例如 `/Applications/ShareInput.app`），双击应用启动，
在 Mac 的“系统设置 → 隐私与安全性 → 设备控制和数据访问”添加并启用这份应用，再完全退出并重开。
“设备控制和数据访问”是 macOS 27 起的名称；macOS 26 及更早版本称为“辅助功能”。
源码运行时给终端或 Python 的授权不会自动授予打包后的应用。
重新编译替换应用后若权限条目已开启但仍被拒绝，可移除旧条目，重新添加当前 `.app` 并授权。
当前脚本生成本机可测试的应用；面向其他用户分发时，Mac 还应使用 Developer ID 签名和公证，
Windows 可使用代码签名证书签署程序。

## 部署自检

```sh
QT_QPA_PLATFORM=offscreen dist/macos-arm64/ShareInput.app/Contents/MacOS/ShareInput --build-smoke-test
dist/linux-x86_64/ShareInput.dist/ShareInput --build-smoke-test
```

Windows 从终端执行 `ShareInput.exe --build-smoke-test`。
自检加载 Qt、键鼠后端、图标和 JPEG 编解码，不启动网络服务、不监听或注入实际输入。
Linux 自检需要 X11 会话，因为 pynput 后端加载也要连接 X server。
通过自检仍需进行跨设备的剪贴板、文件、投屏、组合键、鼠标操作验证。

## 本机验证记录（2026-11-13）

- Mac ARM64 应用已实际构建，包含选定的 A 图标，包体约 91 MiB；签名校验和项目外目录的部署自检通过。
- 使用本机 Homebrew Python 构建的这份样品要求 macOS 27.0 或更新版本。
  要发布到较旧 macOS，请使用相应兼容的 python.org Python 重新构建。
- 构建环境中 178 项回归测试通过；Linux 和 Windows 脚本尚未在对应系统实际编译验证。
- 相同依赖下单次启动自检峰值 RSS：源码约 104 MiB、Nuitka 约 108 MiB、临时 PyInstaller 对照包约 99 MiB。
  这只是加载 Qt、输入后端和 JPEG 的自检路径，不能代表主窗口或投屏的长期内存和帧率，未据此宣称 Nuitka 更省内存。
  按当前选择继续使用 Nuitka；临时 PyInstaller 包已清理。

参考：[Nuitka 文档](https://nuitka.net/user-documentation/user-manual.html)、
[Qt 官方 Nuitka 部署](https://doc.qt.io/qtforpython-6/deployment/deployment-nuitka.html)。
