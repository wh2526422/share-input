import os
import sys


if sys.platform.startswith('linux'):
    import faulthandler
    from datetime import datetime
    from pathlib import Path

    try:
        _crash_path = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'share-input/crash.log'
        _crash_path.parent.mkdir(parents=True, exist_ok=True)
        # faulthandler retains a file descriptor; keep its Python owner alive.
        _crash_output = _crash_path.open('a', encoding='utf-8')
        print(f'\n{datetime.now().isoformat()} ShareInput pid={os.getpid()} Python={sys.version}',
              file=_crash_output, flush=True)
        faulthandler.enable(file=_crash_output, all_threads=True)
    except OSError:
        faulthandler.enable()


src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src')
if src_path not in sys.path:
    sys.path.insert(1, src_path)

from ui import apply_theme
import qt_runtime as tk
from qt_runtime import W


class ShareInputApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('share-input')
        self.root.geometry('980x700')
        self.root.minsize(800, 620)
        apply_theme(self.root)
        self.mode = 'server'
        self.root._share_input_switch_mode = self.switch_mode
        self.layout = W.QVBoxLayout(self.root)
        self.layout.setContentsMargins(0,0,0,0)
        self.app_container = None
        self.current_app = None
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.switch_mode()

    def switch_mode(self, mode=None):
        if mode is not None:
            if mode == self.mode and self.current_app is not None:
                return
            self.mode = mode
        if self.current_app:
            self.current_app.stop()
            self.current_app = None
        if self.app_container is not None:
            self.layout.removeWidget(self.app_container)
            self.app_container.destroy()
        self.app_container = tk.Window(self.root)
        self.app_container._share_input_switch_mode = self.switch_mode
        self.layout.addWidget(self.app_container)
        if self.mode == 'server':
            from server import ServerGUI
            self.current_app = ServerGUI(parent=self.app_container)
            current = self.current_app
            self.root.after(100, lambda: current.start_server()
                            if self.current_app is current else None)
        else:
            from client import ClientGUI
            self.current_app = ClientGUI(parent=self.app_container)

    def on_close(self):
        if self.current_app:
            self.current_app.stop()
        self.root.destroy()


if __name__ == '__main__':
    if '--build-smoke-test' in sys.argv:
        # Verify deployed resources/backends without starting listeners or injecting input.
        import io
        import json
        from pathlib import Path
        from PIL import Image
        import ui
        import server
        import client
        import pynput
        import mss
        from config import default_config_file
        from qt_runtime import application
        qt_app = application()
        for name in ('chevron-down.svg', 'chevron-right.svg'):
            path = Path(ui.__file__).parent / 'assets' / name
            if not path.is_file():
                raise RuntimeError(f'Missing resource: {path}')
        buffer = io.BytesIO()
        Image.new('RGB', (16,16)).save(buffer, 'JPEG')
        Image.open(io.BytesIO(buffer.getvalue())).load()
        print(json.dumps({'status':'ok', 'platform':sys.platform,
                          'config':default_config_file()}, ensure_ascii=False))
    else:
        app = ShareInputApp()
        app.root.mainloop()
