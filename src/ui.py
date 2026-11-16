"""PySide6 主界面和设备选择。"""
import socket
import threading
import queue
from pathlib import Path
from qt_runtime import connect_signal, C, W, Control, PeerList, application
from device_info import local_ipv4_addresses

GREEN, MUTED = '#27855b', '#737d8d'
STYLE = '''
QWidget { background:#ffffff; color:#202632; font-size:13px; }
QWidget#sidebar { background:#f7f8fa; border-right:1px solid #e6e9ef; }
QWidget#sidebar QLabel { background:transparent; }
QLabel[muted="true"] { color:#737d8d; background:transparent; }
QLabel#title { font-size:26px; font-weight:600; }
QPushButton { border:1px solid #e6e9ef; border-radius:7px; padding:9px 14px; background:white; }
QPushButton:hover { background:#f1f4fa; }
QPushButton:disabled { color:#a3aab6; background:#f7f8fa; }
QPushButton[primary="true"] { background:#3264d8; color:white; border:0; }
QPushButton[primary="true"]:disabled { background:#e1e6ef; color:#929bab; }
QPushButton[nav="true"] { text-align:left; background:transparent; border:0; padding:12px; }
QPushButton:checked { background:#edf2ff; color:#3264d8; }
QLineEdit, QComboBox, QTextEdit { border:1px solid #e6e9ef; border-radius:7px; padding:10px; background:#f7f8fa; selection-background-color:#dbe6ff; }
QComboBox { padding:10px 36px 10px 12px; min-height:18px; }
QComboBox::drop-down { subcontrol-origin:padding; subcontrol-position:top right; width:30px; border:0; border-top-right-radius:7px; border-bottom-right-radius:7px; background:transparent; }
QComboBox::down-arrow { image:url("$DOWN"); width:12px; height:12px; }
QComboBox QAbstractItemView { border:1px solid #e6e9ef; padding:4px; background:#ffffff; selection-background-color:#edf2ff; selection-color:#202632; outline:0; }
QTreeWidget { border:0; alternate-background-color:#fafbfc; }
QTreeWidget::item { padding:14px 8px; border-bottom:1px solid #eef0f4; }
QHeaderView::section { background:#f7f8fa; color:#737d8d; border:0; padding:10px; }
QScrollBar:vertical { width:9px; background:#f7f8fa; } QScrollBar::handle:vertical { background:#d4dae4; border-radius:4px; min-height:20px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QMenu { border:1px solid #e6e9ef; padding:6px; }
QMenu::item { padding:8px 32px 8px 14px; border-radius:4px; }
QMenu::item:selected { background:#edf2ff; }
QMenu::item:disabled { color:#8a94a4; }
QMenu::right-arrow { image:url("$RIGHT"); width:10px; height:10px; }
QMenu::separator { height:1px; background:#e6e9ef; margin:5px 8px; }
'''
STYLE = STYLE.replace('$RIGHT', (Path(__file__).parent / 'assets' / 'chevron-right.svg').as_posix())
STYLE = STYLE.replace('$DOWN', (Path(__file__).parent / 'assets' / 'chevron-down.svg').as_posix())


def apply_theme(root):
    app = application()
    if not app.property('shareInputFusion'):
        app.setStyle('Fusion')
        app.setProperty('shareInputFusion', True)
    app.setStyleSheet(STYLE)

def label(text='', muted=False):
    widget = W.QLabel(text); widget.setProperty('muted', muted); widget.setWordWrap(True)
    return widget

def button(text, command, primary=False):
    widget = W.QPushButton(text); widget.setProperty('primary',primary); connect_signal(widget.clicked, command)
    return widget

def choose_value(root, title, labels, current=0):
    dialog = W.QDialog(root)
    dialog.setWindowTitle(title)
    dialog.setObjectName('valueDialog')
    dialog.setMinimumWidth(340)
    layout = W.QVBoxLayout(dialog)
    layout.setContentsMargins(20, 20, 20, 20)
    layout.setSpacing(14)
    layout.addWidget(label(title))
    choices = W.QComboBox()
    choices.addItems(labels)
    choices.setCurrentIndex(current)
    layout.addWidget(choices)
    buttons = W.QDialogButtonBox(W.QDialogButtonBox.Ok | W.QDialogButtonBox.Cancel)
    buttons.button(W.QDialogButtonBox.Ok).setText('确定')
    buttons.button(W.QDialogButtonBox.Ok).setProperty('primary', True)
    buttons.button(W.QDialogButtonBox.Ok).setDefault(True)
    buttons.button(W.QDialogButtonBox.Cancel).setAutoDefault(False)
    buttons.button(W.QDialogButtonBox.Cancel).setText('取消')
    connect_signal(buttons.accepted, dialog.accept)
    connect_signal(buttons.rejected, dialog.reject)
    layout.addWidget(buttons)
    choices.setFocus()
    if dialog.exec() != W.QDialog.Accepted:
        return None
    return choices.currentIndex()

def choose_devices(parent, title, clients, multiple=True):
    dialog = W.QDialog(parent); dialog.setWindowTitle(title); dialog.resize(440,360)
    layout = W.QVBoxLayout(dialog); layout.setContentsMargins(24,24,24,24); layout.setSpacing(14)
    layout.addWidget(label(title))
    peers = W.QListWidget(); peers.setSelectionMode(W.QAbstractItemView.NoSelection)
    for index, peer in enumerate(clients):
        item = W.QListWidgetItem(f'{peer.name or peer.address[0]}\n{peer.system} · {peer.address[0]}')
        peers.addItem(item)
        item.setFlags(item.flags() | C.Qt.ItemIsUserCheckable)
        item.setCheckState(C.Qt.Checked if index == 0 else C.Qt.Unchecked)
    guard = False
    def changed(item):
        nonlocal guard
        if guard: return
        guard=True
        if not multiple and item.checkState() == C.Qt.Checked:
            for i in range(peers.count()):
                if peers.item(i) is not item: peers.item(i).setCheckState(C.Qt.Unchecked)
        if multiple:
            all_box.setChecked(all(peers.item(i).checkState() == C.Qt.Checked for i in range(peers.count())))
        guard=False
    connect_signal(peers.itemChanged, changed); layout.addWidget(peers)
    if multiple:
        all_box = W.QCheckBox('所有在线客户端')
        connect_signal(all_box.clicked[bool], lambda checked: [peers.item(i).setCheckState(C.Qt.Checked if checked else C.Qt.Unchecked) for i in range(peers.count())])
        layout.addWidget(all_box)
    buttons=W.QDialogButtonBox(W.QDialogButtonBox.Ok | W.QDialogButtonBox.Cancel)
    buttons.button(W.QDialogButtonBox.Ok).setText('确认'); buttons.button(W.QDialogButtonBox.Cancel).setText('取消')
    connect_signal(buttons.accepted, lambda: dialog.accept() if any(peers.item(i).checkState()==C.Qt.Checked for i in range(peers.count())) else None)
    connect_signal(buttons.rejected, dialog.reject); layout.addWidget(buttons)
    if dialog.exec()!=W.QDialog.Accepted: return ()
    return tuple(peer for i,peer in enumerate(clients) if peers.item(i).checkState()==C.Qt.Checked)


class MainView:
    def __init__(self, owner, mode):
        self.owner=owner; apply_theme(owner.root)
        if owner.is_standalone:
            owner.root.resize(980,700)
            owner.root.setMinimumSize(800,620)
        root_layout=W.QHBoxLayout(owner.root); root_layout.setContentsMargins(0,0,0,0); root_layout.setSpacing(0)
        sidebar=W.QWidget(); sidebar.setObjectName('sidebar'); sidebar.setFixedWidth(180)
        nav=W.QVBoxLayout(sidebar); nav.setContentsMargins(16,28,16,20); nav.setSpacing(8)
        brand=label('●  ShareInput'); brand.setStyleSheet('font-size:17px; font-weight:600; background:transparent'); nav.addWidget(brand); nav.addSpacing(24)
        self.nav={}; self.stack=W.QStackedWidget(); self.pages={}
        for name in ('设备','剪贴板','消息','连接信息','运行日志'):
            b=button(name,lambda checked=False,n=name:self.show(n)); b.setProperty('nav',True); b.setCheckable(True)
            nav.addWidget(b); self.nav[name]=b
            page=W.QWidget(); self.pages[name]=W.QVBoxLayout(page); self.pages[name].setContentsMargins(0,0,0,0); self.stack.addWidget(page)
        nav.addStretch(); nav.addWidget(label('当前设备',True)); nav.addWidget(label(socket.gethostname()))
        self.ip=label('读取地址中…',True); nav.addWidget(self.ip)
        self.addresses=queue.SimpleQueue()
        def refresh_addresses():
            connection=getattr(owner,'socket',None)
            threading.Thread(target=lambda:self.addresses.put(local_ipv4_addresses(connection)),daemon=True).start()
        self.timer=C.QTimer(owner.root)
        def read_addresses():
            try: self.ip.setText('\n'.join(self.addresses.get_nowait()) or '未获取到地址')
            except queue.Empty: pass
        connect_signal(self.timer.timeout, read_addresses); self.timer.start(200)
        self.refresh=C.QTimer(owner.root); connect_signal(self.refresh.timeout, refresh_addresses); self.refresh.start(5000); refresh_addresses()
        root_layout.addWidget(sidebar)
        content=W.QVBoxLayout(); content.setContentsMargins(30,26,30,20); content.setSpacing(18); root_layout.addLayout(content,1)
        header=W.QHBoxLayout(); self.title=label('设备'); self.title.setObjectName('title'); header.addWidget(self.title); header.addStretch()
        switch=getattr(owner.root,'_share_input_switch_mode',None)
        group=W.QButtonGroup(owner.root)
        for value,text in (('server','服务端'),('client','客户端')):
            b=button(text,lambda checked=False,v=value:switch(v) if switch else None); b.setCheckable(True); b.setChecked(value==mode); b.setEnabled(bool(switch)); group.addButton(b); header.addWidget(b)
        content.addLayout(header); content.addWidget(label('在设备之间，自由工作。',True))
        status=W.QHBoxLayout(); owner.status_label=Control(label('● 未连接',True)); owner.status_label.widget.setWordWrap(False); status.addWidget(owner.status_label.widget); status.addStretch()
        name='service_btn' if mode=='server' else 'disconnect_btn'
        action=owner.toggle_server if mode=='server' else owner.disconnect
        widget=button('启动服务' if mode=='server' else '断开连接',action); setattr(owner,name,Control(widget)); status.addWidget(widget)
        if mode=='client': widget.setEnabled(False)
        content.addLayout(status); content.addWidget(self.stack,1)
        owner.bottom_status=Control(label('已就绪',True)); content.addWidget(owner.bottom_status.widget)
        for page,attr in (('剪贴板','clipboard_text'),('消息','message_text'),('连接信息','info_text'),('运行日志','log_text')):
            if page=='运行日志':
                performance=W.QCheckBox('显示投屏性能日志'); performance.setChecked(owner.desktop.performance_logging)
                connect_signal(performance.toggled, lambda enabled:setattr(owner.desktop,'performance_logging',enabled))
                self.pages[page].addWidget(performance)
            widget=W.QTextEdit(); widget.setReadOnly(page!='剪贴板'); setattr(owner,attr,Control(widget)); self.pages[page].addWidget(widget)
            if page=='运行日志': widget.document().setMaximumBlockCount(1000)
        compose=W.QHBoxLayout(); owner.message_entry=Control(W.QLineEdit()); owner.message_entry.widget.setPlaceholderText('输入消息…'); connect_signal(owner.message_entry.widget.returnPressed, owner.send_text_message)
        owner.send_msg_btn=Control(button('发送',owner.send_text_message,True)); owner.send_msg_btn.widget.setEnabled(False); compose.addWidget(owner.message_entry.widget,1); compose.addWidget(owner.send_msg_btn.widget); self.pages['消息'].addLayout(compose)
        device=self.pages['设备']
        if mode=='server':
            owner.device_count_label=Control(label('在线设备  0',True)); device.addWidget(owner.device_count_label.widget)
            peers=W.QTreeWidget(); peers.setHeaderLabels(['设备','IP 地址','系统','分辨率']); peers.setRootIsDecorated(False); peers.setSelectionMode(W.QAbstractItemView.NoSelection); peers.header().setSectionResizeMode(W.QHeaderView.Stretch)
            owner.client_list=PeerList(peers); device.addWidget(peers,1)
            owner.empty_label=Control(label('暂无在线设备 · 等待客户端连接',True)); device.addWidget(owner.empty_label.widget)
        else:
            device.addWidget(label('服务端地址',True)); address=W.QHBoxLayout(); owner.server_entry=Control(W.QLineEdit(owner.config.get_last_server() or '192.168.10.162'))
            connect_signal(owner.server_entry.widget.returnPressed, owner.connect_to_server); address.addWidget(owner.server_entry.widget,1)
            owner.connect_btn=Control(button('连接',owner.connect_to_server,True)); address.addWidget(owner.connect_btn.widget); device.addLayout(address)
            history=W.QHBoxLayout(); history.addWidget(label('连接历史',True)); owner.history_combo=Control(W.QComboBox()); connect_signal(owner.history_combo.widget.activated, lambda _:owner._on_history_selected(None)); history.addWidget(owner.history_combo.widget,1); device.addLayout(history); owner._update_history_list()
            owner.peer_label=Control(label('尚未连接服务端',True)); device.addWidget(owner.peer_label.widget)
        actions=W.QHBoxLayout(); actions.addStretch()
        for attr,text,command,primary in (('send_file_btn','发送文件',owner.send_file,False),('desktop_btn','查看并控制',owner.toggle_desktop,True)):
            widget=button(text,command,primary); widget.setEnabled(False); setattr(owner,attr,Control(widget)); actions.addWidget(widget)
        device.addLayout(actions); device.addWidget(label('共享本机输入',True)); shared=W.QHBoxLayout()
        for kind,title in (('keyboard','键盘'),('mouse','鼠标')):
            item=W.QVBoxLayout(); row=W.QHBoxLayout(); row.addWidget(label(title)); row.addStretch()
            action=(owner.toggle_keyboard_sharing if kind=='keyboard' else owner.toggle_mouse_sharing) if mode=='server' else lambda checked=False,k=kind:owner._toggle_input_sharing(k)
            b=button('开始共享',action); b.setEnabled(False); setattr(owner,kind+'_share_btn',Control(b)); row.addWidget(b); item.addLayout(row)
            target=Control(label('未共享',True)); setattr(owner,kind+'_target_label',target); item.addWidget(target.widget); shared.addLayout(item)
        device.addLayout(shared); owner.sharing_label=Control(label('',True)); device.addWidget(owner.sharing_label.widget)
        if mode=='client': device.addStretch()
        self.show('设备')

    def show(self,name):
        self.stack.setCurrentIndex(list(self.pages).index(name)); self.title.setText(name)
        for key,b in self.nav.items(): b.setChecked(key==name)
