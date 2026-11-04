"""Unified local archive entry; QQ itself is never started/stopped by this app."""
from __future__ import annotations
import json
import datetime as dt
from pathlib import Path
from threading import Event,Thread

from PySide6.QtCore import QObject,QTimer,Signal,Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QComboBox,QDialog,QHBoxLayout,QLabel,QMenu,QPlainTextEdit,QPushButton,QStyle,QSystemTrayIcon,QToolBar,QVBoxLayout

from qq_nt_archive_viewer.app import MainWindow,create_application
from qq_nt_archive_viewer.publication import publication_accounts
from .config import load
from .service import Coordinator,request_action


class BackendSignals(QObject):
    status=Signal(object)


class ControlWindow(MainWindow):
    def __init__(self,root):
        self.state_root,self.config=load(root)
        initial=self.config['sources'][0]['account_key']
        self.backend=None;self.backend_thread=None;self.exiting=False
        super().__init__(self.state_root/'viewer_manifest.json',initial,settings_app='QQArchiveNext')
        self.setWindowTitle('QQ 持续归档')
        self.backend_signals=BackendSignals(self);self.backend_signals.status.connect(self.receive_status)
        toolbar=QToolBar('持续归档',self);toolbar.setMovable(False);self.addToolBar(Qt.ToolBarArea.BottomToolBarArea,toolbar)
        self.source_select=QComboBox();self.choices=[]
        for candidate in publication_accounts(self.state_root/'viewer_manifest.json'):
            self.source_select.addItem(('统一归档 · ' if self.config['schema']=='qq-next-config/2' else '当前归档 · ')+candidate.display_id)
            self.choices.append((self.state_root/'viewer_manifest.json',candidate.account_key))
        legacy=self.config.get('legacy_publication')
        if legacy and Path(legacy).is_file():
            for candidate in publication_accounts(Path(legacy)):
                self.source_select.addItem('历史归档 · '+candidate.display_id)
                self.choices.append((Path(legacy),candidate.account_key or candidate.display_id))
        self.source_select.currentIndexChanged.connect(self.choose_source);toolbar.addWidget(self.source_select)
        self.start_button=QPushButton('开始持续归档');self.start_button.clicked.connect(self.start_capture);toolbar.addWidget(self.start_button)
        self.stop_button=QPushButton('停止采集');self.stop_button.clicked.connect(self.stop_capture);toolbar.addWidget(self.stop_button)
        backup_button=QPushButton('增量备份');backup_button.clicked.connect(self.backup);toolbar.addWidget(backup_button)
        details=QPushButton('采集状态');details.clicked.connect(self.show_capture_status);toolbar.addWidget(details)
        quit_button=QPushButton('退出归档');quit_button.clicked.connect(self.exit_archive);toolbar.addWidget(quit_button)
        self.capture_label=QLabel('采集未启动');toolbar.addWidget(self.capture_label)
        self.status_timer=QTimer(self);self.status_timer.setInterval(2000);self.status_timer.timeout.connect(self.read_status);self.status_timer.start()
        icon=self.style().standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon)
        self.setWindowIcon(icon);self.tray=QSystemTrayIcon(icon,self);self.tray.setToolTip('QQ 持续归档')
        menu=QMenu();show=menu.addAction('打开归档');show.triggered.connect(self.showNormal)
        menu.addAction('开始持续归档',self.start_capture);menu.addAction('停止采集',self.stop_capture)
        menu.addSeparator();menu.addAction('退出归档',self.exit_archive)
        self.tray.setContextMenu(menu);self.tray_menu=menu
        self.tray.activated.connect(lambda reason:self.showNormal() if reason==QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.show();self.read_status()
        if self.config.get('auto_capture') and not self.config.get('restored_from'):QTimer.singleShot(500,self.start_capture)

    def choose_source(self,index):
        path,account=self.choices[index];self.open_manifest(path,account)

    def remote_active(self):
        existing=getattr(self,'latest_status',{})
        if existing.get('state') not in ('stopped','error',None) and existing.get('pid')!=__import__('os').getpid():
            import psutil
            try:active=abs(psutil.Process(existing['pid']).create_time()-existing.get('process_started',0))<.001
            except (psutil.Error,KeyError):active=False
            return active
        return False

    def start_capture(self):
        if self.backend_thread and self.backend_thread.is_alive():return
        self.read_status()
        if self.remote_active():self.capture_label.setText('已有后台归档；此窗口可查看和停止');return
        self.start_worker()

    def start_worker(self,command=None):
        try:self.backend=Coordinator(self.state_root,notify=self.backend_signals.status.emit)
        except Exception as exc:self.capture_label.setText(str(exc));return
        if command:self.backend.commands.put((command,{'repository':self.state_root.parent/'backup-repository'}))
        def run():
            try:self.backend.run(once=bool(command),capture_sources=not command,media=not command)
            except Exception as exc:self.backend_signals.status.emit({'state':'error','error':str(exc)})
        self.backend_thread=Thread(target=run,name='QQArchiveWriter',daemon=False);self.backend_thread.start()

    def stop_capture(self):
        if self.backend_thread and self.backend_thread.is_alive():self.backend.stop.set()
        else:
            try:request_action(self.state_root,'stop')
            except (OSError,ValueError,KeyError):pass

    def backup(self):
        self.queue_maintenance('backup')

    def verify_archive(self):
        if self.store and self.store.archive_schema in {'qq-next-viewer/1','qq-next-viewer/2'}:self.queue_maintenance('verify')
        else:super().verify_archive()

    def queue_maintenance(self,command):
        self.read_status()
        if self.backend_thread and self.backend_thread.is_alive():self.backend.commands.put((command,{'repository':self.state_root.parent/'backup-repository'}))
        elif self.remote_active():
            try:request_action(self.state_root,command)
            except (OSError,ValueError,KeyError) as exc:self.capture_label.setText(str(exc));return
        else:self.start_worker(command)
        self.capture_label.setText(('备份' if command=='backup' else '完整核验')+'已排队，等待当前事务完成')

    def receive_status(self,status):
        self.latest_status=status
        state=status.get('state','unknown')
        translations={'capturing':'正在采集','media':'正在归档媒体','idle':'持续归档已开启','starting':'正在启动',
          'stopping':'正在停止','stopped':'采集已停止','backup':'正在增量备份','verify':'正在核验','error':'发生错误'}
        failures=sum(1 for source in status.get('sources',{}).values() if source.get('state')=='retry')
        text=translations.get(state,state)
        if failures:text+=f' · {failures} 个来源等待重试'
        if status.get('error'):text+=' · '+status['error']
        self.capture_label.setText(text);self.tray.setToolTip('QQ 归档 · '+text)

    def read_status(self):
        path=self.state_root/'status.private.json'
        if path.is_file():
            try:
                status=json.loads(path.read_text(encoding='utf-8'))
                if status.get('state') not in ('stopped','error'):
                    import psutil
                    try:alive=abs(psutil.Process(status['pid']).create_time()-status.get('process_started',0))<.001
                    except (psutil.Error,KeyError):alive=False
                    if not alive:status['state']='后台未运行；显示上次采集结果'
                self.receive_status(status)
            except (OSError,ValueError):pass

    def show_capture_status(self):
        dialog=QDialog(self);dialog.setWindowTitle('采集状态与最后成功时间');dialog.resize(850,650)
        layout=QVBoxLayout(dialog)
        status=getattr(self,'latest_status',{'state':'not_started'})
        def date(value):return dt.datetime.fromtimestamp(value).strftime('%Y-%m-%d %H:%M:%S') if value else '尚无成功记录'
        layout.addWidget(QLabel(self.capture_label.text()))
        for source in self.config['sources']:
            value=status.get('sources',{}).get(source['id'],{})
            title=source.get('account_label') or source['account_key']
            lines=[f"账号 {title}",f"最后成功归档：{date(value.get('last_success'))}"]
            if value.get('import'):lines.append(f"该次新增原始版本：{value['import'].get('versions_added',0):,} 条")
            if value.get('media'):
                m=value['media'];lines.append(f"该次检查媒体 {m['files_seen']:,} 个，新增 {m['observations_added']:,} 份路径记录，暂不可读 {m['unavailable']:,} 个")
            if value.get('error'):lines.append('等待重试：'+value['error'])
            label=QLabel('\n'.join(lines));label.setTextFormat(Qt.TextFormat.PlainText);label.setWordWrap(True);layout.addWidget(label)
        if status.get('progress'):layout.addWidget(QLabel(f"正在准备 {status['progress']['rows_scanned']:,} 行，本批尚未提交"))
        maintenance=status.get('maintenance',{})
        if maintenance:layout.addWidget(QLabel('上次备份/核验：'+date(maintenance.get('finished'))+' · '+maintenance.get('state','')))
        text=QPlainTextEdit();text.setReadOnly(True);text.setPlainText(json.dumps(status,ensure_ascii=False,indent=2));text.hide()
        diagnostics=QPushButton('展开诊断信息');diagnostics.clicked.connect(lambda:text.setVisible(not text.isVisible()))
        layout.addWidget(diagnostics);layout.addWidget(text);layout.addStretch(1)
        self.status_dialog=dialog;dialog.show()

    def exit_archive(self):
        self.exiting=True;self.stop_capture();self.close()

    def closeEvent(self,event):
        if not self.exiting and QSystemTrayIcon.isSystemTrayAvailable():
            self.hide();event.ignore();return
        self.exiting=True;self.stop_capture()
        if self.backend_thread and self.backend_thread.is_alive():
            self.capture_label.setText('正在结束当前操作并保留恢复标记…');event.ignore();QTimer.singleShot(100,self.close);return
        self.tray.hide();self.status_timer.stop();super().closeEvent(event)


def run(root):
    application=create_application();window=ControlWindow(root);window.show()
    return application.exec()
