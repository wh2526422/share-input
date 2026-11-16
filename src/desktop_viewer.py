"""Qt 等比远程画面、悬浮控制条及窗口内输入。"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from qt_runtime import connect_signal, C, G, W
from desktop_keys import DesktopKeys
from key_mapping import X11_MODIFIERS
from desktop_stream import COORD_MAX, FPS_PROFILES, RESOLUTION_PROFILES


def fit_rect(vw,vh,iw,ih):
    scale=min(vw/iw,vh/ih); w,h=max(1,int(iw*scale)),max(1,int(ih*scale))
    return (vw-w)//2,(vh-h)//2,w,h


def map_point(rect,x,y):
    left,top,width,height=rect; x,y=x-left,y-top
    if not(0<=x<width and 0<=y<height): return None
    return {'x':x*COORD_MAX//max(1,width-1),'y':y*COORD_MAX//max(1,height-1)}


KEYS = {C.Qt.Key_Backspace:'BackSpace', C.Qt.Key_Tab:'Tab', C.Qt.Key_Backtab:'ISO_Left_Tab',
 C.Qt.Key_Return:'Return',C.Qt.Key_Enter:'KP_Enter',C.Qt.Key_Escape:'Escape',C.Qt.Key_Delete:'Delete',
 C.Qt.Key_Insert:'Insert',C.Qt.Key_Home:'Home',C.Qt.Key_End:'End',C.Qt.Key_PageUp:'Prior',C.Qt.Key_PageDown:'Next',
 C.Qt.Key_Left:'Left',C.Qt.Key_Right:'Right',C.Qt.Key_Up:'Up',C.Qt.Key_Down:'Down',
 C.Qt.Key_CapsLock:'Caps_Lock',C.Qt.Key_NumLock:'Num_Lock',C.Qt.Key_Shift:'Shift_L',
 C.Qt.Key_Control:'Control_L',C.Qt.Key_Alt:'Alt_L',C.Qt.Key_AltGr:'ISO_Level3_Shift',C.Qt.Key_Meta:'Meta_L'}
KEYS.update({C.Qt.Key_Super_L:'Super_L',C.Qt.Key_Super_R:'Super_R',
             C.Qt.Key_Hyper_L:'Super_L',C.Qt.Key_Hyper_R:'Super_R'})
KEYS.update({int(C.Qt.Key_F1)+i:f'F{i+1}' for i in range(20)})


def modifier_state(modifiers):
    state=0
    for mask,bit in ((C.Qt.ShiftModifier,1),(C.Qt.ControlModifier,4),(C.Qt.MetaModifier,8),(C.Qt.AltModifier,16)):
        if modifiers & mask: state|=bit
    return state


def key_event(event):
    key=event.key(); native=event.nativeVirtualKey()
    code=native if sys.platform=='darwin' else event.nativeScanCode() or native or int(key)
    name=KEYS.get(key,chr(key).lower() if 32<=key<0x110000 else '')
    if sys.platform=='darwin' and native in (54,60,61,62):
        name={54:'Meta_R',60:'Shift_R',61:'Alt_R',62:'Control_R'}[native]
    elif sys.platform.startswith('linux') and native in X11_MODIFIERS:
        modifier=X11_MODIFIERS[native]
        name={'ctrl_l':'Control_L','ctrl_r':'Control_R','cmd_l':'Meta_L',
              'cmd_r':'Meta_R','alt_gr':'ISO_Level3_Shift'}.get(modifier,modifier)
    state=modifier_state(event.modifiers())
    return SimpleNamespace(keysym=name,char=event.text(),keycode=code<<24 if sys.platform=='darwin' else code,
                           state=state,caps_time_ms=event.timestamp())


class Screen(W.QWidget):
    def __init__(self, viewer):
        super().__init__(viewer.window); self.viewer=viewer
        self.setFocusPolicy(C.Qt.StrongFocus); self.setMouseTracking(True)
        self._wheel_angle = [0, 0]
        self._wheel_pixel = [0, 0]
        self._at_hot_edge = False
    def event(self,event):
        v=getattr(self,'viewer',None)
        if v is not None and v._active and v._image is not None:
            if event.type()==C.QEvent.ShortcutOverride and event.key()!=C.Qt.Key_F11:
                event.accept()
                return True
            if event.type() in (C.QEvent.KeyPress,C.QEvent.KeyRelease) and event.key() in (C.Qt.Key_Tab,C.Qt.Key_Backtab):
                v.key(event,event.type()==C.QEvent.KeyPress)
                return True
        return super().event(event)
    def paintEvent(self,event):
        started=time.perf_counter()
        painter=G.QPainter(self); painter.fillRect(self.rect(),G.QColor('#10151e'))
        v=self.viewer
        if v._image is not None:
            x,y,w,h=fit_rect(self.width(),self.height(),v._image.width(),v._image.height())
            v._rect=(x,y,w,h); painter.setRenderHint(G.QPainter.SmoothPixmapTransform)
            painter.drawImage(C.QRect(x,y,w,h),v._image)
            v.session.record_performance('paint',time.perf_counter()-started)
        else:
            painter.setPen(G.QColor('#a7b0bf')); painter.drawText(self.rect(),C.Qt.AlignCenter,'等待画面…')
    def resizeEvent(self,event):
        self.viewer.layout_toolbar(); super().resizeEvent(event)
    def mouseMoveEvent(self,event):
        v=self.viewer
        pos=event.position().toPoint()
        if not self.rect().contains(pos):
            v.release(); return
        self._at_hot_edge = event.position().y()<=7
        if self._at_hot_edge or (v.toolbar.isVisible() and v.toolbar.geometry().contains(pos)):
            v.show_toolbar(); return
        if v.toolbar.isVisible() and not v.hide_timer.isActive():
            v.schedule_toolbar_hide()
        point=v.point(event)
        if v._active and point:
            v._keys.sync_modifiers(modifier_state(event.modifiers()))
        v._motion={'kind':'move',**point} if v._active and point else None
    def leaveEvent(self,event):
        self._at_hot_edge = False
        if self.viewer._mouse_grabbed:
            self.viewer.release()
        self.viewer.schedule_toolbar_hide()
        super().leaveEvent(event)
    def mousePressEvent(self,event): self.viewer.click(event,True)
    def mouseDoubleClickEvent(self,event): self.viewer.click(event,True,click_count=2)
    def mouseReleaseEvent(self,event): self.viewer.click(event,False)
    def wheelEvent(self,event):
        v=self.viewer; point=v.point(event)
        if point and v._active:
            v._motion=None
            v._keys.sync_modifiers(modifier_state(event.modifiers()))
            delta=event.angleDelta()
            remainder, unit = self._wheel_angle, 120
            if delta.isNull():
                delta=event.pixelDelta()
                remainder, unit = self._wheel_pixel, 40
            values = [delta.x(), delta.y()]
            if event.modifiers() & C.Qt.ShiftModifier:
                values.reverse()
            steps=[]
            for axis,value in enumerate(values):
                total=remainder[axis]+value
                count=(abs(total)//unit) * (1 if total >= 0 else -1)
                remainder[axis]=total-count*unit
                steps.append(count)
            while any(steps):
                # 远端协议只接受整数步数；1.0 在 JSON 解码后仍是 float，会被拒绝。
                packet=[max(-10,min(10,count)) for count in steps]
                v.session.send_input({'kind':'scroll','dx':packet[0],'dy':packet[1],**point})
                steps=[count-sent for count,sent in zip(steps,packet)]
        else:
            self._wheel_angle[:] = [0, 0]
            self._wheel_pixel[:] = [0, 0]
        event.accept()
    def keyPressEvent(self,event): self.viewer.key(event,True)
    def keyReleaseEvent(self,event): self.viewer.key(event,False)
    def focusInEvent(self,event): self.viewer.activate(); super().focusInEvent(event)
    def focusOutEvent(self,event): self.viewer.release(); super().focusOutEvent(event)


class ViewerWindow(W.QWidget):
    def closeEvent(self,event):
        if not self.viewer._destroyed: self.viewer.stop()
        event.accept()
    def changeEvent(self,event):
        if event.type()==C.QEvent.WindowStateChange and hasattr(self,'viewer'):
            self.viewer.fullscreen_changed()
        elif event.type()==C.QEvent.ActivationChange and hasattr(self,'viewer') and not self.isActiveWindow():
            self.viewer.release()
        super().changeEvent(event)


class DesktopViewer(C.QObject):
    def __init__(self,root,session,stop):
        super().__init__(root); self.session=session; self.stop=stop
        self.window=ViewerWindow(root,C.Qt.Window); self.window.viewer=self
        self.window.setWindowTitle('远程桌面 · '+session.label); self.window.resize(960,580); self.window.setMinimumSize(320,240)
        self._destroyed=False; self._image=None; self._rect=(0,0,0,0); self._active=False; self._motion=None; self._buttons=set()
        self._keyboard_grabbed=False
        self._mouse_grabbed=False
        self.native_caps=False
        self._input_epoch=0
        self._keys=DesktopKeys(session.send_input,aqua=sys.platform=='darwin',sync_modifiers=True)
        self.screen=Screen(self); layout=W.QVBoxLayout(self.window); layout.setContentsMargins(0,0,0,0); layout.addWidget(self.screen)
        self.toolbar=W.QFrame(self.window); self.toolbar.setObjectName('remoteToolbar')
        self.toolbar.setStyleSheet('QFrame#remoteToolbar {background:#f7f8fb; border:1px solid #dce1ea; border-radius:10px;} QLabel {background:transparent; color:#5a6474;}')
        row=W.QHBoxLayout(self.toolbar); row.setContentsMargins(10,6,10,6); row.setSpacing(12)
        self.menu=W.QMenu(self.toolbar); connect_signal(self.menu.aboutToShow, self.release); connect_signal(self.menu.aboutToHide, self.schedule_toolbar_hide)
        self.control=W.QPushButton('控制'); self.control.setMenu(self.menu); row.addWidget(self.control)
        self.device=W.QLabel(session.label); self.device.setMaximumWidth(180); row.addWidget(self.device)
        row.addStretch(); self.latency=W.QLabel(); row.addWidget(self.latency); self.status=W.QLabel('等待画面…'); row.addWidget(self.status)
        self.pin=W.QPushButton(); self.pin.setCheckable(True); self.pin.setToolTip('固定悬浮条，保持显示'); self.pin.setAccessibleName('固定悬浮条'); connect_signal(self.pin.toggled, self.pin_toolbar); row.addWidget(self.pin)
        self.full=W.QPushButton(); self.full.setToolTip('进入全屏（F11）'); self.full.setAccessibleName('进入全屏'); connect_signal(self.full.clicked, self.toggle_fullscreen); row.addWidget(self.full)
        for widget in (self.pin,self.full):
            widget.setFixedSize(34,34); widget.setIconSize(C.QSize(18,18)); widget.setStyleSheet('QPushButton {padding:0; border-radius:7px;}')
        self.pin.setIcon(self.toolbar_icon('pin')); self.full.setIcon(self.toolbar_icon('fullscreen'))
        self.groups={}
        profiles=[]
        if session.live_display: profiles.append(('display','显示器',[(i,f'显示器 {i+1} · {s["width"]}×{s["height"]}') for i,s in enumerate(session.displays)]))
        if session.live_resolution: profiles.append(('resolution','分辨率',[(p,p) for p in RESOLUTION_PROFILES]))
        if session.live_fps: profiles.append(('fps','帧率',[(p,f'{p} 帧/秒') for p in FPS_PROFILES]))
        for kind,title,values in profiles:
            submenu=self.menu.addMenu(title); group=G.QActionGroup(submenu); group.setExclusive(True); actions=[]
            for value,text in values:
                action=submenu.addAction(text); action.setCheckable(True); group.addAction(action)
                connect_signal(action.triggered, lambda checked=False,k=kind,v=value:self.select(k,v)); actions.append((value,action))
            self.groups[kind]=(submenu,actions)
        if sys.platform.startswith('linux') and session.host_platform=='Darwin':
            self.menu.addSeparator()
            for char,title in (('c','复制 ⌘C'),('v','粘贴 ⌘V')):
                connect_signal(self.menu.addAction(title).triggered, lambda c=char:self.shortcut(c))
        self.menu.addSeparator(); self.menu.addAction('Shift+Esc 释放键鼠').setEnabled(False)
        connect_signal(self.menu.addAction('结束投屏').triggered, stop)
        self.hide_timer=C.QTimer(self); self.hide_timer.setSingleShot(True); connect_signal(self.hide_timer.timeout, self.hide_toolbar)
        for widget in (self.toolbar,*self.toolbar.findChildren(W.QWidget)): widget.installEventFilter(self)
        self.shortcut_full=G.QShortcut(G.QKeySequence('F11'),self.window); self.shortcut_full.setAutoRepeat(False); connect_signal(self.shortcut_full.activated, self.toggle_fullscreen)
        self.window.showNormal(); self.layout_toolbar(); self.screen.setFocus()
        self.schedule_toolbar_hide()

    def eventFilter(self,obj,event):
        if event.type()==C.QEvent.Enter: self.release(); self.hide_timer.stop()
        elif event.type()==C.QEvent.Leave: self.schedule_toolbar_hide()
        return False
    def layout_toolbar(self):
        width=self.window.width(); self.device.setVisible(width>=850); self.status.setVisible(width>=660)
        self.toolbar.setGeometry(12,12,max(1,width-24),self.toolbar.sizeHint().height()); self.toolbar.raise_()
    def schedule_toolbar_hide(self):
        if self.pin.isChecked(): self.hide_timer.stop()
        else: self.hide_timer.start(600)
    def pin_toolbar(self,pinned):
        self.release(); self.pin.setIcon(self.toolbar_icon('pin-active' if pinned else 'pin'))
        self.pin.setAccessibleName('取消固定悬浮条' if pinned else '固定悬浮条')
        self.pin.setToolTip('取消固定，恢复自动隐藏' if pinned else '固定悬浮条，保持显示')
        self.toolbar.show(); self.toolbar.raise_(); self.schedule_toolbar_hide()
    def show_toolbar(self): self.release(); self.toolbar.show(); self.toolbar.raise_(); self.schedule_toolbar_hide()
    def hide_toolbar(self):
        if not self.pin.isChecked() and not self.screen._at_hot_edge and not self.toolbar.underMouse() and not self.menu.isVisible(): self.toolbar.hide()
    def toggle_fullscreen(self):
        self.release()
        if self.window.isFullScreen(): self.window.showNormal()
        else: self.window.showFullScreen()
    def fullscreen_changed(self):
        self.release(); full=self.window.isFullScreen(); self.full.setIcon(self.toolbar_icon('restore' if full else 'fullscreen'))
        self.full.setToolTip('退出全屏（F11）' if full else '进入全屏（F11）'); self.full.setAccessibleName('退出全屏' if full else '进入全屏')
        self.toolbar.setVisible(self.pin.isChecked()); self.layout_toolbar(); self.screen.setFocus()
    @staticmethod
    def toolbar_icon(name):
        return G.QIcon(str(Path(__file__).parent/'assets'/('toolbar-'+name+'.svg')))
    def update(self):
        if self._destroyed: return None
        if self._motion is not None: self.session.send_input(self._motion); self._motion=None
        session=self.session
        self._update_latency()
        for kind,(menu,actions) in self.groups.items():
            pending=getattr(session,'pending_'+kind)
            current=session.display_index if kind=='display' else session.fps if kind=='fps' else next((p for p,size in RESOLUTION_PROFILES.items() if size==session.resolution),'')
            menu.setEnabled(session.ready.is_set() and pending is None)
            for value,action in actions: action.setChecked(value==(pending if pending is not None else current))
        frame=session.latest_frame(); first=None
        if frame is not None:
            started=time.perf_counter()
            if self._image is None: first=frame.size
            image=frame if frame.mode=='RGB' else frame.convert('RGB'); data=image.tobytes()
            self._image=G.QImage(data,image.width,image.height,image.width*3,G.QImage.Format_RGB888).copy()
            session.record_performance('convert_ui',time.perf_counter()-started)
            self.status.setText(f'{image.width}×{image.height} · {session.fps} 帧'); self.screen.update()
        return first
    def _update_latency(self):
        session=self.session
        rtt=session.network_rtt_ms
        fresh=session.network_rtt_updated_at is not None and time.monotonic()-session.network_rtt_updated_at<4
        text=f'网络延迟 {rtt:.1f} ms（往返）' if rtt is not None and fresh else '网络延迟 -- ms（测量中）' if session.network_latency else '网络延迟 -- ms（对端需更新）'
        if self.window.width() < 660:
            text = text.replace('网络延迟', '网络').split('（')[0]
        self.latency.setText(text)

    def select(self,kind,value):
        if kind=='display': self.select_display(value)
        else: getattr(self.session,'request_'+kind)(value)
        self.screen.setFocus()
    def select_display(self,index):
        if index!=self.session.display_index:
            self.release()
            if self.session.request_display(index): self._image=None; self._rect=(0,0,0,0); self.status.setText('正在切换显示器…'); self.screen.update()
    def shortcut(self,char): self.release(); self._keys.mac_clipboard_shortcut(char); self.screen.setFocus()
    def activate(self):
        self._active=True
        if sys.platform.startswith('linux') and G.QGuiApplication.platformName()=='xcb' and self.window.isActiveWindow():
            # X11 的 Super+鼠标通常用于拖窗口，须同时抓取指针才不会被窗口管理器截走。
            if not self._keyboard_grabbed:
                self.screen.grabKeyboard()
                self._keyboard_grabbed=W.QWidget.keyboardGrabber() is self.screen
            if not self._mouse_grabbed:
                self.screen.grabMouse()
                self._mouse_grabbed=W.QWidget.mouseGrabber() is self.screen
    def release(self):
        self._input_epoch+=1
        if self._mouse_grabbed:
            self._mouse_grabbed=False
            self.screen.releaseMouse()
        if self._keyboard_grabbed:
            self._keyboard_grabbed=False
            self.screen.releaseKeyboard()
        if self._active or self._keys._pressed or self._buttons: self.session.send_input({'kind':'release'})
        self._active=False; self._keys.clear(); self._buttons.clear(); self._motion=None
        self.screen._wheel_angle[:] = [0, 0]
        self.screen._wheel_pixel[:] = [0, 0]
    def point(self,event):
        x,y,w,h=self._rect; px,py=event.position().x()-x,event.position().y()-y
        if not(0<=px<w and 0<=py<h): return None
        return {'x':int(px*COORD_MAX/max(1,w-1)),'y':int(py*COORD_MAX/max(1,h-1))}
    def click(self,event,pressed,click_count=1):
        if pressed and self.toolbar.isVisible() and self.toolbar.geometry().contains(event.position().toPoint()):
            self.show_toolbar(); return
        point=self.point(event); name={C.Qt.LeftButton:'left',C.Qt.RightButton:'right',C.Qt.MiddleButton:'middle'}.get(event.button())
        if not name: return
        if pressed and point:
            self.screen.setFocus(); self.activate(); self._buttons.add(name)
        elif not pressed and name in self._buttons: self._buttons.remove(name)
        else: return
        self._keys.sync_modifiers(modifier_state(event.modifiers()))
        self._motion=None
        self.session.send_input({'kind':'click','button':name,'pressed':pressed,
                                 **({'click_count':click_count} if pressed else {}),**(point or {})})
    def key(self,event,pressed):
        event.accept()
        if self.native_caps and event.key()==C.Qt.Key_CapsLock: return
        if event.key()==C.Qt.Key_F11: return
        if event.key()==C.Qt.Key_Escape and event.modifiers() & C.Qt.ShiftModifier:
            if pressed: self.release()
            return
        if event.isAutoRepeat() and (not pressed or event.key()==C.Qt.Key_CapsLock): return
        translated=key_event(event)
        native_modifiers=event.nativeModifiers()
        local_caps = (sys.platform=='win32' and bool(native_modifiers & 0x100)
                      or sys.platform.startswith('linux') and G.QGuiApplication.platformName()=='xcb'
                      and bool(native_modifiers & 0x02))
        if (local_caps and self.session.host_platform=='Darwin' and len(translated.char)==1
                and translated.char.isascii() and translated.char.isalpha()):
            # Windows CapsLock=0x100; Qt xcb passes X11 LockMask=0x02.
            # text() already includes local Caps; Mac owns its lock/IME state.
            # Undo only Caps' case inversion, retaining physical Shift/layout.
            translated.char=translated.char.swapcase()
        if pressed and self._active and self._image is not None:
            self._keys.press(translated)
            if sys.platform=='darwin' and event.key()==C.Qt.Key_CapsLock: self._keys.release(translated)
        elif not pressed and (self._active or self._keys._pressed): self._keys.release(translated)
    def destroy(self):
        if self._destroyed:return
        self.release(); self._destroyed=True; self.hide_timer.stop(); self.window.hide(); self.window.deleteLater(); self.deleteLater()
