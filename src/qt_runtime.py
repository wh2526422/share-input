"""Qt 主线程调度及业务层控件接口；不创建 Tk 解释器。"""
import itertools
import ctypes
import os
import queue
import sys
import threading
from functools import partial
from pathlib import Path
# 二进制依赖由当前 Python 环境提供，不能加载从其他系统复制的测试目录。
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W

END, NORMAL, DISABLED, CENTER = 'end', 'normal', 'disabled', 'center'
TclError = RuntimeError
_app = None


def connect_signal(signal, callback):
    # PySide6 can over-release Nuitka compiled methods during QObject teardown.
    # A partial is owned as an ordinary callable and releases its callback once.
    return signal.connect(partial(callback))


def check_qt_version():
    """拒绝已知会破坏 Python 单例引用计数的 Qt for Python wheel。"""
    if sys.version_info >= (3, 12):
        return
    import PySide6
    import shiboken6
    if '6.12.0' in (PySide6.__version__, shiboken6.__version__):
        raise SystemExit(
            "当前 PySide6 / shiboken6 6.12.0 与 Python 3.10/3.11 存在已知引用计数缺陷"
            "（PYSIDE-3474），可能导致 none_dealloc 崩溃。\n"
            "请使用启动软件的 Python 执行：\n"
            "python -m pip install --upgrade --force-reinstall -r requirements.txt\n"
            "然后重新启动软件。"
        )


def check_xcb_dependencies():
    """在 Qt 创建窗口前报告 X11 插件缺库，避免 QPA 初始化直接 abort。"""
    if not sys.platform.startswith('linux'):
        return
    platform = os.environ.get('QT_QPA_PLATFORM', '').split(':', 1)[0]
    if platform != 'xcb' and (platform or not os.environ.get('DISPLAY')
                              or os.environ.get('WAYLAND_DISPLAY')):
        return
    # Nuitka relocates plugins and sets libraryPaths() during QtCore import;
    # QLibraryInfo can still point to the original wheel's Qt/plugins tree.
    roots = C.QCoreApplication.libraryPaths()
    if not roots:
        roots = [C.QLibraryInfo.path(C.QLibraryInfo.PluginsPath)]
    candidates = [Path(path) / 'libqxcb.so'
                  for path in os.environ.get('QT_QPA_PLATFORM_PLUGIN_PATH', '').split(os.pathsep)
                  if path]
    candidates += [Path(root) / 'platforms' / 'libqxcb.so' for root in roots]
    plugin = next((path for path in candidates if path.is_file()), None)
    if plugin is None:
        checked = '\n'.join(str(path) for path in candidates)
        raise SystemExit(
            f'无法启动 Qt X11 界面：未找到插件文件 libqxcb.so。\n已检查路径：\n{checked}\n'
            '请检查 Qt 插件搜索路径及完整的 ShareInput.dist 发布目录；'
            '这不表示缺少系统 xcb 依赖库。')
    try:
        ctypes.CDLL(str(plugin))
    except OSError as exc:
        message = f"无法启动 Qt X11 界面：xcb 插件依赖加载失败。\n{exc}\n"
        if 'libxcb-cursor.so' in str(exc):
            message += "Ubuntu/Debian 请先执行：sudo apt install libxcb-cursor0\n"
        message += f'可检查其他缺失库：ldd "{plugin}"\n'
        message += "这些是 Linux 系统库，需要使用系统包管理器安装。"
        raise SystemExit(message) from exc


def application():
    global _app
    if W.QApplication.instance() is None:
        check_qt_version()
        check_xcb_dependencies()
        C.QCoreApplication.setAttribute(C.Qt.AA_MacDontSwapCtrlAndMeta)
    _app = W.QApplication.instance() or W.QApplication(sys.argv)
    if _app.windowIcon().isNull():
        _app.setWindowIcon(G.QIcon(str(Path(__file__).parent / 'assets/app-icon.png')))
    return _app


class Window(W.QWidget):
    def __init__(self, parent=None):
        application()
        super().__init__(parent)
        self._jobs, self._ids = {}, itertools.count()
        self._cancelled = set()
        self._pending = queue.SimpleQueue()
        self._closed = False
        self._ui_ident = threading.get_ident()
        self._close_callback = None
        self._pump = C.QTimer(self)
        connect_signal(self._pump.timeout, self._drain)
        self._pump.start(10)

    def _drain(self):
        for _ in range(100):
            try:
                ident, delay, fn, args = self._pending.get_nowait()
            except queue.Empty:
                return
            if self._closed or ident in self._cancelled:
                self._cancelled.discard(ident)
                continue
            timer = C.QTimer(self)
            timer.setSingleShot(True)
            if 0 < delay <= 20:
                # Windows 默认粗定时器会拖慢投屏的 8ms 刷新；长周期任务保持省电模式。
                timer.setTimerType(C.Qt.PreciseTimer)
            self._jobs[ident] = timer
            def run(key=ident, callback=fn, values=args):
                job = self._jobs.pop(key, None)
                if job is not None:
                    job.deleteLater()
                if not self._closed:
                    callback(*values)
            connect_signal(timer.timeout, run)
            timer.start(delay)

    def after(self, delay, fn, *args):
        if self._closed:
            return None
        ident = next(self._ids)
        self._pending.put((ident, delay, fn, args))
        if threading.get_ident() == self._ui_ident:
            self._drain()
        return ident

    def after_cancel(self, ident):
        self._cancelled.add(ident)
        timer = self._jobs.pop(ident, None)
        if timer:
            self._cancelled.discard(ident)
            timer.stop()
            timer.deleteLater()

    def title(self, value): self.setWindowTitle(value)
    def geometry(self, value):
        size = value.split('+')[0].split('x')
        self.resize(int(size[0]), int(size[1]))
    def minsize(self, x, y): self.setMinimumSize(x, y)
    def protocol(self, _, fn): self._close_callback = fn
    def winfo_screenwidth(self): return self.screen().geometry().width()
    def winfo_screenheight(self): return self.screen().geometry().height()
    def winfo_toplevel(self): return self.window()
    def mainloop(self): self.show(); application().exec()
    def destroy(self):
        self._closed = True
        self._pump.stop()
        for timer in self._jobs.values(): timer.stop()
        self._jobs.clear()
        self.hide()
        self.deleteLater()
    def closeEvent(self, event):
        if self._close_callback:
            callback, self._close_callback = self._close_callback, None
            callback()
        event.accept()


Tk = Window


class Control:
    """让现有连接状态处理复用原生 Qt 控件，而不改网络协议。"""
    def __init__(self, widget): self.widget = widget
    def config(self, **values):
        for key, value in values.items():
            if key == 'text': self.widget.setText(value)
            elif key == 'state':
                if isinstance(self.widget, W.QTextEdit): self.widget.setReadOnly(value == DISABLED)
                else: self.widget.setEnabled(value != DISABLED)
            elif key == 'foreground': self.widget.setStyleSheet('color:' + value)
            elif key == 'values': self.widget.clear(); self.widget.addItems(value)
    configure = config
    def __setitem__(self, key, value): self.config(**{key:value})
    def cget(self, key):
        return self.widget.text() if key == 'text' else NORMAL if self.widget.isEnabled() else DISABLED
    def get(self, *args):
        if isinstance(self.widget, W.QComboBox): return self.widget.currentText()
        return self.widget.toPlainText() if isinstance(self.widget, W.QTextEdit) else self.widget.text()
    def delete(self, *args): self.widget.clear()
    def insert(self, index, text, *tags):
        if isinstance(self.widget, W.QTextEdit):
            cursor = self.widget.textCursor()
            cursor.movePosition(G.QTextCursor.End if index == END else G.QTextCursor.Start)
            cursor.insertText(str(text))
            self.widget.setTextCursor(cursor)
        else: self.widget.setText(str(text))
    def see(self, *_): self.widget.ensureCursorVisible()
    def tag_config(self, *args, **kwargs): pass
    def get_children(self): return tuple(range(self.widget.topLevelItemCount()))
    def place_forget(self): self.widget.hide()
    def place(self, **kwargs): self.widget.show()


class PeerList(Control):
    def delete(self, row):
        # 调用方批量清空；倒序删除避免行号移动。
        self.widget.takeTopLevelItem(self.widget.topLevelItemCount()-1)
    def insert(self, parent, index, text='', values=()):
        self.widget.addTopLevelItem(W.QTreeWidgetItem([text, *map(str,values)]))
