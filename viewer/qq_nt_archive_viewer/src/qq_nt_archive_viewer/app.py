from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import Event

from PySide6.QtCore import QSettings, QSize, Qt, QUrl, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices, QImageReader, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
    QPlainTextEdit,
)

from .archive_store import (
    PAGE_SIZE,
    ArchiveError,
    ArchiveStore,
    open_archive,
    Conversation,
    MediaAsset,
    MediaLink,
    Message,
    SearchHit,
    display_sender,
    display_time,
    extract_plain_content,
)
from .publication import publication_accounts
from .background import ReadSignals, ReadTask
from .summary_cache import default_directory


APP_STYLE = """
QMainWindow, QWidget { background: #f5f7fb; color: #192133; }
QToolBar { background: #ffffff; border: none; border-bottom: 1px solid #dfe5ef; spacing: 8px; padding: 6px; }
QFrame#sidebar, QFrame#timeline { background: #ffffff; border: 1px solid #e0e6ef; border-radius: 12px; }
QLineEdit, QComboBox {
    min-height: 34px; padding: 0 10px; background: #f7f9fc;
    border: 1px solid #d9e0ea; border-radius: 8px;
}
QLineEdit:focus, QComboBox:focus { border: 1px solid #5b7cfa; background: white; }
QPushButton {
    min-height: 30px; padding: 0 12px; border-radius: 8px;
    border: 1px solid #d4dce8; background: #ffffff;
}
QPushButton:hover { background: #eef3ff; border-color: #9cb1ec; }
QListWidget { border: none; background: transparent; outline: none; }
QListWidget::item { padding: 7px; border-bottom: 1px solid #eef1f5; }
QListWidget::item:selected { background: #e7efff; color: #172033; }
QStatusBar { background: #ffffff; border-top: 1px solid #dfe5ef; }
"""


def asset_label(asset: MediaAsset | None) -> str:
    if asset is None:
        return "无本地文件"
    return asset.original_name or Path(asset.archive_relative).name


def preview_pixmap(asset: MediaAsset, width: int = 420, height: int = 280) -> QPixmap:
    """Decode an image at preview size instead of expanding a full original."""

    if not asset.path:
        return QPixmap()
    reader = QImageReader(str(asset.path))
    source_size = reader.size()
    if source_size.isValid():
        source_size.scale(
            width,
            height,
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        reader.setScaledSize(source_size)
    return QPixmap.fromImage(reader.read())


class SearchDialog(QDialog):
    page_requested = Signal(int)
    jump_requested = Signal(object)

    def __init__(self, hits: list[SearchHit], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("搜索结果")
        self.resize(820, 560)
        self.selected: SearchHit | None = None
        layout = QVBoxLayout(self)
        self.page = 0
        self.label = QLabel("正在搜索，可随时关闭取消。")
        layout.addWidget(self.label)
        self.listing = QListWidget()
        self.listing.itemDoubleClicked.connect(self.choose)
        layout.addWidget(self.listing, 1)
        buttons = QHBoxLayout()
        self.previous = QPushButton("上一页")
        self.more = QPushButton("下一页")
        self.previous.clicked.connect(lambda: self.request_page(self.page - 1))
        self.more.clicked.connect(lambda: self.request_page(self.page + 1))
        self.previous.setEnabled(False)
        self.more.setEnabled(False)
        buttons.addWidget(self.previous)
        buttons.addWidget(self.more)
        open_button = QPushButton("跳转")
        open_button.clicked.connect(self.choose_current)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(open_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def choose_current(self) -> None:
        item = self.listing.currentItem()
        if item:
            self.choose(item)

    def choose(self, item: QListWidgetItem) -> None:
        self.selected = item.data(Qt.ItemDataRole.UserRole)
        self.accept()
        self.jump_requested.emit(self.selected)

    def request_page(self, page: int) -> None:
        self.previous.setEnabled(False)
        self.more.setEnabled(False)
        self.label.setText("正在搜索，可随时关闭取消。")
        self.page_requested.emit(page)

    def set_hits(self, hits: list[SearchHit], page: int) -> None:
        self.page = page
        self.listing.clear()
        for hit in hits[:200]:
            item = QListWidgetItem(
                f"{hit.conversation.label}  |  {display_time(hit.timestamp)}\n"
                f"{display_sender(hit.sender_id, hit.sender_id_kind, hit.sender_label, hit.sender_label_source)}：{hit.preview}"
            )
            item.setData(Qt.ItemDataRole.UserRole, hit)
            self.listing.addItem(item)
        self.label.setText(f"第 {page + 1} 页 · 本页 {min(len(hits), 200)} 条；按时间倒序，双击跳转。")
        self.previous.setEnabled(page > 0)
        self.more.setEnabled(len(hits) > 200)


class VideoDialog(QDialog):
    def __init__(self, asset: MediaAsset, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.asset = asset
        self.setWindowTitle("本地视频播放")
        self.resize(860, 620)
        layout = QVBoxLayout(self)
        self.video = QVideoWidget()
        self.video.setMinimumSize(480, 300)
        layout.addWidget(self.video, 1)
        self.status = QLabel(asset_label(asset))
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        external = QPushButton("用系统播放器打开")
        external.clicked.connect(lambda: open_local(asset.path, self, asset.original_name))
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        buttons.addWidget(external)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.errorOccurred.connect(lambda error, text: self.status.setText('视频播放失败：' + text))
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        if asset.path:
            self.player.setSource(QUrl.fromLocalFile(str(asset.path)))
            self.player.play()

    def closeEvent(self, event) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        self.player.setVideoOutput(None)
        super().closeEvent(event)


def open_local(path: Path | None, parent: QWidget, filename=None) -> None:
    if not path or not path.is_file():
        QMessageBox.warning(parent, "文件不可用", "归档中的目标文件不存在或不在允许媒体目录内。")
        return
    if path.parent.parent.name=='objects':
        from qq_archive_next.media import external_copy
        try:path=external_copy(path,filename)
        except (ValueError,OSError) as exc:
            QMessageBox.warning(parent,'无法准备本地副本',str(exc))
            return
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
        QMessageBox.warning(parent, "无法打开", "系统默认应用未能打开该本地文件。")


class MainWindow(QMainWindow):
    def __init__(
        self,
        initial_manifest: Path | None = None,
        initial_account: str | None = None,
        *, settings_app: str = 'QQNtArchiveViewer',
    ) -> None:
        super().__init__()
        self.settings = QSettings("LocalArchiveTools", settings_app)
        self.store: ArchiveStore | None = None
        self._closing = False
        self._generation = 0
        self._reads = {}
        self._signals = ReadSignals(self)
        self._signals.completed.connect(self._read_completed)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._search_dialog = None
        self._live_revision = None
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(3000)
        self._live_timer.timeout.connect(self._refresh_live)
        self._live_timer.start()
        self.publication_manifest: Path | None = None
        self.current_conversation: Conversation | None = None
        self.current_messages: list[Message] = []
        self.current_media: dict[int, list[MediaLink]] = {}
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.9)
        self.audio_player = QMediaPlayer(self)
        self.audio_player.setAudioOutput(self.audio)
        self.audio_player.errorOccurred.connect(lambda error, text: self.statusBar().showMessage('语音播放失败：' + text))
        self.audio_player.mediaStatusChanged.connect(self._audio_status_changed)
        self.setWindowTitle("QQ NT 聊天记录查看器")
        self.setMinimumSize(1080, 700)
        self.resize(self.settings.value("window_size", QSize(1420, 900)))
        self._build_ui()
        if initial_manifest:
            self.open_manifest(initial_manifest, initial_account)
        else:
            recent = self.settings.value("last_manifest", "")
            if recent and Path(str(recent)).is_file():
                self.open_manifest(Path(str(recent)))

    def _build_ui(self) -> None:
        self.setStyleSheet(APP_STYLE)
        toolbar = QToolBar("工具栏")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        open_action = QAction("打开归档", self)
        open_action.triggered.connect(self.choose_manifest)
        toolbar.addAction(open_action)
        info_action = QAction("归档信息", self)
        info_action.triggered.connect(self.show_info)
        toolbar.addAction(info_action)
        verify_action = QAction("完整核验", self)
        verify_action.triggered.connect(self.verify_archive)
        toolbar.addAction(verify_action)
        refresh_action = QAction("刷新最近消息", self)
        refresh_action.triggered.connect(lambda: self.select_conversation(self.current_conversation) if self.current_conversation else self._refresh_live())
        toolbar.addAction(refresh_action)
        toolbar.addSeparator()
        toolbar.addWidget(QLabel("全文搜索："))
        self.global_search = QLineEdit()
        self.global_search.setPlaceholderText("至少 2 个字符；只搜索本地文本")
        self.global_search.setMinimumWidth(300)
        self.global_search.returnPressed.connect(self.search)
        toolbar.addWidget(self.global_search)
        search_button = QPushButton("搜索")
        search_button.clicked.connect(self.search)
        toolbar.addWidget(search_button)

        central = QWidget()
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)
        main_layout.addWidget(self._sidebar())
        main_layout.addWidget(self._timeline(), 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("请选择 archive_manifest.json 或统一 viewer_manifest.json")

    def _sidebar(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("sidebar")
        frame.setMinimumWidth(330)
        frame.setMaximumWidth(520)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 14, 14, 14)
        heading = QLabel("会话")
        heading.setStyleSheet("font-size:20px;font-weight:700;color:#172033")
        layout.addWidget(heading)
        self.kind = QComboBox()
        self.kind.addItems(["全部", "私聊", "群聊"])
        self.kind.currentTextChanged.connect(self.refresh_conversations)
        layout.addWidget(self.kind)
        self.conversation_search = QLineEdit()
        self.conversation_search.setPlaceholderText("QQ号、群号或会话类型")
        self.conversation_search.setClearButtonEnabled(True)
        self.conversation_search.textChanged.connect(self.refresh_conversations)
        layout.addWidget(self.conversation_search)
        self.conversations = QListWidget()
        self.conversations.itemSelectionChanged.connect(self.select_current_conversation)
        layout.addWidget(self.conversations, 1)
        self.conversation_status = QLabel("未打开归档")
        self.conversation_status.setStyleSheet("color:#748094;padding:4px")
        layout.addWidget(self.conversation_status)
        return frame

    def _timeline(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("timeline")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 14, 14, 10)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        self.title = QLabel("请选择会话")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setStyleSheet("font-size:20px;font-weight:700;color:#172033")
        self.subtitle = QLabel("消息纯文本渲染；媒体只使用归档中的验证路径。")
        self.subtitle.setStyleSheet("color:#758094")
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        header.addLayout(titles, 1)
        self.older = QPushButton("更早 200 条")
        self.older.clicked.connect(self.load_older)
        self.newer = QPushButton("更晚 200 条")
        self.newer.clicked.connect(self.load_newer)
        self.older.setEnabled(False)
        self.newer.setEnabled(False)
        header.addWidget(self.older)
        header.addWidget(self.newer)
        layout.addLayout(header)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.message_container = QWidget()
        self.message_layout = QVBoxLayout(self.message_container)
        self.message_layout.setContentsMargins(8, 8, 8, 8)
        self.message_layout.setSpacing(8)
        self.message_layout.addStretch(1)
        self.scroll.setWidget(self.message_container)
        layout.addWidget(self.scroll, 1)
        self.page_status = QLabel("未选择会话")
        self.page_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_status.setStyleSheet("color:#778295;padding:5px")
        layout.addWidget(self.page_status)
        return frame

    def _submit_read(self, kind, function, done, *, deep=False) -> None:
        if not self.store or self._closing:
            return
        previous = self._reads.get(kind)
        if previous:
            previous[1].set()
        self._generation += 1
        generation, cancel = self._generation, Event()
        path, label = self.store.manifest_path, self.store.account_id
        conversations = self.store._conversation_cache

        def run(event):
            store = open_archive(path, label, deep_validation=deep,
                                 cache_dir=default_directory(), cancelled=event.is_set)
            try:
                if conversations is not None and store.archive_schema not in {'qq-next-viewer/1','qq-next-viewer/2'}:
                    store._set_conversations(conversations)
                return function(store, event)
            finally:
                store.close()

        self._reads[kind] = (generation, cancel, done)
        self._pool.start(ReadTask(self._signals, kind, generation, cancel, run))

    @Slot(str, int, object, object)
    def _read_completed(self, kind, generation, value, error) -> None:
        current = self._reads.get(kind)
        if self._closing or not current or current[0] != generation:
            return
        del self._reads[kind]
        if current[1].is_set():
            return
        if error:
            self.statusBar().showMessage("后台读取失败：" + error)
            if kind == "search" and self._search_dialog:
                self._search_dialog.label.setText("搜索失败：" + error)
            return
        current[2](value)

    def _conversations_ready(self, value) -> None:
        revision, overview, conversations = value
        if not self.store:
            return
        self.store._set_conversations(conversations)
        self._live_revision = revision
        if revision is not None:
            self.store._data_version = revision
        media_text=(f"已归档媒体 {overview['media_objects']:,} 个对象" if 'media_objects' in overview else f"媒体映射 {overview['media_links']:,} 条")
        self.statusBar().showMessage(
            f"只读归档 · 私聊 {overview['c2c_messages']:,} 条 · 群聊 {overview['group_messages']:,} 条 · "
            f"{media_text} · 尚未执行本次完整核验"
        )
        self.refresh_conversations()

    @staticmethod
    def _summary_read(store, cancel):
        if hasattr(store, 'summaries'):
            return store.summaries()
        return None, store.overview(), store.conversations()

    def _refresh_live(self) -> None:
        if not self.store or self._closing or self.store.archive_schema not in {'qq-next-viewer/1','qq-next-viewer/2'} or 'conversations' in self._reads:
            return
        try:
            if self.store.revision() != self._live_revision:
                self._submit_read('conversations', self._summary_read, self._conversations_ready)
        except (sqlite3.Error, OSError) as exc:
            self.statusBar().showMessage('等待归档刷新：' + str(exc))

    def verify_archive(self) -> None:
        if not self.store:
            return
        self.statusBar().showMessage("正在后台完整核验；可以继续浏览，关闭程序可取消。")
        self._submit_read("verify", lambda store, cancel: True,
                          lambda result: self.statusBar().showMessage("本次归档完整核验通过。"), deep=True)

    def choose_manifest(self) -> None:
        start = str(Path(self.settings.value("last_manifest", "")).parent)
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "选择 QQ 归档或统一发布清单",
            start,
            "QQ manifests (archive_manifest.json viewer_manifest.json);;JSON files (*.json)",
        )
        if filename:
            self.open_manifest(Path(filename))

    def open_manifest(self, path: Path, preferred_account: str | None = None) -> None:
        try:
            candidates = publication_accounts(path)
            selected = candidates[0]
            if preferred_account:
                matches = [item for item in candidates if preferred_account in item.selectors]
                if len(matches) != 1:
                    raise ArchiveError("统一发布清单中找不到指定账号")
                selected = matches[0]
            elif len(candidates) > 1:
                labels = [
                    f"{item.display_id} · {item.logical_messages:,} 条逻辑消息 · {item.source_runs} 个来源"
                    for item in candidates
                ]
                label, accepted = QInputDialog.getItem(
                    self, "选择账号", "统一发布包含多个账号：", labels, 0, False
                )
                if not accepted:
                    return
                selected = candidates[labels.index(label)]
            replacement = open_archive(
                selected.manifest_path, account_label=selected.display_id,
                cache_dir=default_directory(),
            )
        except (ArchiveError, FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
            QMessageBox.critical(self, "无法打开归档", str(exc))
            return
        if self.store:
            self.store.close()
        for _, cancel, _ in self._reads.values():
            cancel.set()
        self._reads.clear()
        if self._search_dialog:
            self._search_dialog.close()
        self.store = replacement
        self._live_revision = None
        self.publication_manifest = path.resolve() if len(candidates) > 1 else None
        self.settings.setValue("last_manifest", str(path.resolve()))
        self.setWindowTitle(f"QQ NT 聊天记录查看器 - {self.store.account_id}")
        self.statusBar().showMessage("已只读打开；正在后台准备会话。尚未执行完整核验。")
        self.current_conversation = None
        self.current_messages = []
        self.current_media = {}
        self.conversations.clear()
        self.conversation_status.setText("正在准备会话摘要…")
        self._submit_read("conversations", self._summary_read, self._conversations_ready)
        self.clear_messages("请选择左侧会话。")

    def show_info(self) -> None:
        if not self.store:
            QMessageBox.information(self, "归档信息", "尚未打开归档。")
            return
        info = self.store.overview()
        QMessageBox.information(
            self,
            "归档信息",
            "\n".join(
                [
                    f"账号：{info['account_id']}",
                    f"私聊消息：{info['c2c_messages']:,}",
                    f"群聊消息：{info['group_messages']:,}",
                    (f"已归档媒体对象：{info['media_objects']:,}；按消息原字节身份关联" if 'media_objects' in info else f"媒体关联：{info['media_links']:,}"),
                    (
                        "历史群成员名称："
                        f"{info['identity_labels']:,} 条（按消息当时的群名片/昵称）"
                        if info["identity_coverage"] is not None
                        else ("名称：使用消息随附的历史群名片/昵称" if 'media_objects' in info else "身份侧车库：未提供（仅显示 QQ/UID）")
                    ),
                    (
                        "资料快照："
                        f"{info['identity_snapshot_id']}（名称/备注均保留资料快照标识）"
                        if info["identity_snapshot_id"]
                        else "资料快照：未提供"
                    ),
                    "模式：严格只读，不访问 QQ、CDN 或网络。",
                    f"清单：{info['manifest']}",
                ]
            ),
        )

    def refresh_conversations(self) -> None:
        if not self.store or self.store._conversation_cache is None:
            return
        old_key = None
        selected = self.conversations.currentItem()
        if selected:
            old = selected.data(Qt.ItemDataRole.UserRole)
            old_key = (
                old.source_table,
                old.id_column,
                old.conversation_id,
            )
        # Filter the already-loaded summaries on the UI thread. A newer live
        # revision is loaded by a worker, never through this text-change slot.
        items = ArchiveStore.conversations(self.store,
            self.conversation_search.text(), self.kind.currentText()
        )
        self.conversations.blockSignals(True)
        self.conversations.clear()
        row_to_select = -1
        for number, conversation in enumerate(items):
            item = QListWidgetItem(
                f"{conversation.label}\n"
                f"{conversation.message_count:,} 条 · "
                f"至 {display_time(conversation.last_timestamp)}"
            )
            item.setData(Qt.ItemDataRole.UserRole, conversation)
            self.conversations.addItem(item)
            if old_key == (
                conversation.source_table,
                conversation.id_column,
                conversation.conversation_id,
            ):
                row_to_select = number
        if row_to_select >= 0:
            self.conversations.setCurrentRow(row_to_select)
            selected_conversation = items[row_to_select]
            self.current_conversation = selected_conversation
            self.set_conversation_header(selected_conversation)
        self.conversations.blockSignals(False)
        self.conversation_status.setText(f"{len(items):,} 个会话")

    def select_current_conversation(self) -> None:
        item = self.conversations.currentItem()
        if not item:
            return
        conversation = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(conversation, Conversation):
            self.select_conversation(conversation)

    def select_conversation(self, conversation: Conversation) -> None:
        if not self.store:
            return
        self.current_conversation = conversation
        self.set_conversation_header(conversation)
        self._load_page(lambda store: store.latest_messages(conversation), "最近")

    def _load_page(self, function, descriptor):
        if not self.current_conversation:
            return
        conversation = self.current_conversation
        self.older.setEnabled(False)
        self.newer.setEnabled(False)
        self.page_status.setText('正在后台读取…')
        def read(store, cancel):
            messages = function(store)
            return messages, store.media_for_messages(conversation.source_table, messages)
        def done(value):
            if not value[0] and descriptor in ('更早', '更晚'):
                self.older.setEnabled(descriptor != '更早')
                self.newer.setEnabled(descriptor != '更晚')
                self.page_status.setText('已无' + descriptor + '消息。')
                return
            self.current_messages, self.current_media = value
            self.render_page(descriptor, media_ready=True)
        self._submit_read('messages', read, done)

    def set_conversation_header(self, conversation: Conversation) -> None:
        self.title.setText(conversation.label)
        self.subtitle.setText(
            f"{conversation.message_count:,} 条消息 · "
            f"{display_time(conversation.first_timestamp)} 至 "
            f"{display_time(conversation.last_timestamp)}"
        )

    def clear_messages(self, message: str) -> None:
        while self.message_layout.count():
            item = self.message_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        label = QLabel(message)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color:#778295;padding:50px")
        self.message_layout.addWidget(label)
        self.message_layout.addStretch(1)
        self.page_status.setText(message)
        self.older.setEnabled(False)
        self.newer.setEnabled(False)

    def render_page(self, descriptor: str, *, media_ready=False) -> None:
        if not self.store or not self.current_conversation:
            return
        if not media_ready:
            self.current_media = self.store.media_for_messages(
                self.current_conversation.source_table, self.current_messages
            )
        while self.message_layout.count():
            item = self.message_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self.current_messages:
            self.clear_messages("此位置没有更多消息。")
            return
        for message in self.current_messages:
            self.message_layout.addWidget(self.make_message_widget(message))
        self.message_layout.addStretch(1)
        self.page_status.setText(
            f"{descriptor} {len(self.current_messages):,} 条 · "
            f"{display_time(self.current_messages[0].timestamp)} 至 "
            f"{display_time(self.current_messages[-1].timestamp)}"
        )
        nullable_timeline=self.store.archive_schema in {'qq-next-viewer/1','qq-next-viewer/2'}
        self.older.setEnabled(nullable_timeline or self.current_messages[0].timestamp is not None)
        self.newer.setEnabled(nullable_timeline or self.current_messages[-1].timestamp is not None)
        self.scroll.verticalScrollBar().setValue(0)

    def load_older(self) -> None:
        if not self.store or not self.current_conversation or not self.current_messages:
            return
        conversation, first = self.current_conversation, self.current_messages[0]
        self._load_page(lambda store: store.older_messages(conversation, first), '更早')

    def load_newer(self) -> None:
        if not self.store or not self.current_conversation or not self.current_messages:
            return
        conversation, last = self.current_conversation, self.current_messages[-1]
        self._load_page(lambda store: store.newer_messages(conversation, last), '更晚')

    def make_message_widget(self, message: Message) -> QFrame:
        bubble = QFrame()
        bubble.setStyleSheet(
            "QFrame { border: 1px solid #dce3ef; border-radius: 11px; "
            + ("background:#dff5e6;" if message.sent else "background:#ffffff;")
            + " }"
        )
        outer = QHBoxLayout(bubble)
        outer.setContentsMargins(12, 8, 12, 8)
        inner = QVBoxLayout()
        inner.setSpacing(5)
        sender = display_sender(
            message.sender_id,
            message.sender_id_kind,
            message.sender_label,
            message.sender_label_source,
            sent=message.sent,
        )
        header = QLabel(
            f"{sender}  ·  {display_time(message.timestamp)}  ·  类型 {message.msg_type}"
        )
        header.setTextFormat(Qt.TextFormat.PlainText)
        header.setStyleSheet("color:#687386;font-size:11px")
        header_layout = QHBoxLayout()
        header_layout.addWidget(header, 1)
        if hasattr(self.store, 'observations'):
            details = QPushButton('详情')
            details.setToolTip('查看来源与原始版本')
            details.setFlat(True)
            details.setMaximumWidth(54)
            details.setStyleSheet('font-size:11px;color:#687386;border:none;background:transparent;padding:0;min-height:18px')
            details.clicked.connect(lambda checked=False, identity=message.msg_id: self.show_observations(identity))
            header_layout.addWidget(details)
        inner.addLayout(header_layout)
        text = QLabel(extract_plain_content(message.text, message.content))
        text.setTextFormat(Qt.TextFormat.PlainText)
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text.setStyleSheet("color:#1e2530;font-size:14px")
        inner.addWidget(text)
        for link in self.current_media.get(message.msg_id, []):
            inner.addWidget(self.make_media_widget(link))
        if message.sent:
            outer.addStretch(1)
            outer.addLayout(inner, 4)
        else:
            outer.addLayout(inner, 4)
            outer.addStretch(1)
        bubble.setMaximumWidth(940)
        return bubble

    def show_observations(self, identity):
        import json
        def done(value):
            dialog = QDialog(self)
            dialog.setWindowTitle('来源与原始版本（只读）')
            dialog.resize(840, 600)
            layout = QVBoxLayout(dialog)
            text = QPlainTextEdit()
            text.setReadOnly(True)
            text.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))
            layout.addWidget(text)
            dialog.show()
        self._submit_read('observations', lambda store, cancel: store.observations(identity), done)

    def make_media_widget(self, link: MediaLink) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            "QFrame { background:#f7f9fc; border:1px solid #d9e1eb; "
            "border-radius:8px; }"
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(9, 7, 9, 7)
        title = QLabel(
            f"{link.media_type} · {link.role} · 证据：{link.verification}"
            + (f" · {link.filename}" if link.filename else "")
        )
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        title.setStyleSheet("font-weight:600;color:#394a63")
        layout.addWidget(title)
        display_asset = link.primary or link.preview or link.candidate
        visual_asset = next(
            (
                asset
                # Weak filename/path candidates and quarantined type conflicts
                # may be opened deliberately, but are never rendered inline as
                # if they were verified message media.
                for asset in (link.primary, link.preview)
                if asset
                and asset.path
                and asset.detected_mime.startswith("image/")
            ),
            None,
        )
        if visual_asset:
            pixmap = preview_pixmap(visual_asset)
            if not pixmap.isNull():
                image = QLabel()
                image.setPixmap(
                    pixmap.scaled(
                        420,
                        280,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                image.setToolTip(visual_asset.archive_relative)
                layout.addWidget(image)
        controls = QHBoxLayout()
        if link.media_type == "voice" and link.playable:
            play = QPushButton("播放语音")
            play.clicked.connect(lambda: self.play_audio(link.playable))
            controls.addWidget(play)
            external = QPushButton("系统播放器")
            external.clicked.connect(lambda: self.play_audio(link.playable, external=True))
            controls.addWidget(external)
        elif link.media_type == "video" and link.playable:
            play = QPushButton("播放视频")
            play.clicked.connect(lambda: self.play_video(link.playable))
            controls.addWidget(play)
            external = QPushButton("系统播放器")
            external.clicked.connect(lambda: open_local(link.playable.path, self, link.playable.original_name))
            controls.addWidget(external)
        elif display_asset and display_asset.path:
            label = (
                "打开候选文件（未验证）"
                if link.candidate is not None and link.primary is None and link.preview is None
                else "打开本地文件"
            )
            open_button = QPushButton(label)
            open_button.clicked.connect(lambda: open_local(display_asset.path, self, display_asset.original_name))
            controls.addWidget(open_button)
        if controls.count():
            controls.addStretch(1)
            layout.addLayout(controls)
        note = QLabel(link.note)
        note.setTextFormat(Qt.TextFormat.PlainText)
        note.setWordWrap(True)
        note.setStyleSheet("color:#67758b;font-size:11px")
        layout.addWidget(note)
        return frame

    def play_audio(self, asset: MediaAsset | None, *, external=False) -> None:
        if not asset or not asset.path:
            open_local(None, self)
            return
        if asset.detected_mime == 'audio/x-qq-silk':
            self.statusBar().showMessage('正在准备本地语音…')
            def prepare(store, cancel):
                from qq_archive_next.media import voice_wav
                path=voice_wav(store.manifest_path.parent, asset.path, cancelled=cancel.is_set)
                return MediaAsset(path.name,'audio/wav','decoded_voice_wav',path,Path(asset.original_name or 'voice').stem+'.wav')
            self._submit_read('audio', prepare, lambda value:self.play_audio(value,external=external))
            return
        if external:
            open_local(asset.path,self,asset.original_name)
            return
        self.audio_player.setSource(QUrl.fromLocalFile(str(asset.path)))
        self.audio_player.play()
        self.statusBar().showMessage(f"正在播放语音：{asset_label(asset)}")

    def _audio_status_changed(self, status) -> None:
        if status in (QMediaPlayer.MediaStatus.EndOfMedia, QMediaPlayer.MediaStatus.InvalidMedia):
            QTimer.singleShot(0, self._release_finished_audio)

    def _release_finished_audio(self) -> None:
        if self.audio_player.mediaStatus() in (QMediaPlayer.MediaStatus.EndOfMedia, QMediaPlayer.MediaStatus.InvalidMedia):
            self.audio_player.stop()
            self.audio_player.setSource(QUrl())

    def play_video(self, asset: MediaAsset | None) -> None:
        if not asset or not asset.path:
            open_local(None, self)
            return
        dialog = VideoDialog(asset, self)
        dialog.exec()

    def search(self) -> None:
        if not self.store:
            return
        query = self.global_search.text().strip()
        if len(query) < 2:
            QMessageBox.information(self, "搜索", "请输入至少 2 个字符。")
            return
        if self._search_dialog:
            self._search_dialog.close()
        dialog = SearchDialog([], self)
        self._search_dialog = dialog
        dialog.page_requested.connect(lambda page: self._search_page(query, page))
        dialog.jump_requested.connect(self._jump_to_hit)
        dialog.finished.connect(self._search_closed)
        dialog.show()
        self._search_page(query, 0)

    def _search_closed(self, result=0) -> None:
        current = self._reads.pop("search", None)
        if current:
            current[1].set()
        self._search_dialog = None

    def _search_page(self, query, page) -> None:
        def done(hits):
            if self._search_dialog:
                self._search_dialog.set_hits(hits, page)
        self._submit_read("search", lambda store, cancel: store.search(
            query, limit=201, offset=page * 200, cancelled=cancel.is_set), done)

    def _jump_to_hit(self, hit) -> None:
        if not self.store:
            return
        self.current_conversation = hit.conversation
        self.set_conversation_header(hit.conversation)
        self._load_page(lambda store: store.messages_around(hit.conversation, hit.msg_id), '搜索定位')

    def closeEvent(self, event) -> None:
        self._closing = True
        for _, cancel, _ in self._reads.values():
            cancel.set()
        self._live_timer.stop()
        if self._pool.activeThreadCount():
            event.ignore()
            self.statusBar().showMessage('正在取消后台读取…')
            QTimer.singleShot(50, self.close)
            return
        self.settings.setValue("window_size", self.size())
        self.audio_player.stop()
        if self.store:
            self.store.close()
        super().closeEvent(event)


def create_application() -> QApplication:
    application = QApplication.instance()
    if application is None:
        application = QApplication([])
    application.setApplicationDisplayName("QQ NT 聊天记录查看器")
    return application
