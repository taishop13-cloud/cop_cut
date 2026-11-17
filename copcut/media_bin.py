"""Left-hand media library."""
from __future__ import annotations

from PySide6.QtCore import QMimeData, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap, QColor, QPainter
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                               QVBoxLayout, QWidget, QAbstractItemView)

from .cache import ThumbnailCache
from .ffmpeg_tools import MediaInfo, fmt_time
from .timeline import MEDIA_MIME


class MediaList(QListWidget):
    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setViewMode(QListWidget.IconMode)
        self.setIconSize(QSize(128, 72))
        self.setGridSize(QSize(140, 108))
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setWordWrap(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)

    def mimeData(self, items):
        md = QMimeData()
        md.setData(MEDIA_MIME, "\n".join(i.data(Qt.UserRole) for i in items).encode("utf-8"))
        return md

    def mimeTypes(self):
        return [MEDIA_MIME, "text/uri-list"]

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        if e.mimeData().hasUrls():
            self.files_dropped.emit([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])
            e.acceptProposedAction()


class MediaBin(QWidget):
    import_requested = Signal()
    add_requested = Signal(list)     # paths
    files_dropped = Signal(list)

    def __init__(self, thumbs: ThumbnailCache, parent=None):
        super().__init__(parent)
        self.thumbs = thumbs
        self.items: dict[str, QListWidgetItem] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Media")
        title.setObjectName("panelTitle")
        lay.addWidget(title)
        row = QHBoxLayout()
        row.setContentsMargins(8, 4, 8, 4)
        b = QPushButton("＋ Nhập tệp")
        b.setObjectName("accentBtn")
        b.clicked.connect(self.import_requested)
        row.addWidget(b)
        b2 = QPushButton("Thêm vào timeline")
        b2.setToolTip("Video → nối vào cuối rãnh chính; âm thanh → đặt tại đầu phát")
        b2.clicked.connect(self._add_selected)
        row.addWidget(b2)
        lay.addLayout(row)
        self.list = MediaList()
        self.list.itemDoubleClicked.connect(lambda it: self.add_requested.emit([it.data(Qt.UserRole)]))
        self.list.files_dropped.connect(self.files_dropped)
        lay.addWidget(self.list, 1)
        hint = QLabel("Kéo tệp vào đây hoặc kéo thẳng xuống timeline.\nNháy đúp để thêm vào cuối.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        thumbs.ready.connect(self._refresh_icons)

    def _add_selected(self):
        paths = [i.data(Qt.UserRole) for i in self.list.selectedItems()]
        if paths:
            self.add_requested.emit(paths)

    def clear(self):
        self.list.clear()
        self.items.clear()

    def add(self, info: MediaInfo):
        if info.path in self.items:
            return
        name = info.path.replace("\\", "/").rsplit("/", 1)[-1]
        it = QListWidgetItem(f"{name}\n{fmt_time(info.duration)}")
        it.setData(Qt.UserRole, info.path)
        it.setData(Qt.UserRole + 1, info.has_video)
        it.setToolTip(f"{info.path}\n{info.width}×{info.height}  {info.fps:.2f} fps" if info.has_video
                      else info.path)
        it.setIcon(self._placeholder(info.has_video))
        self.list.addItem(it)
        self.items[info.path] = it
        self._refresh_icons()

    def _placeholder(self, video: bool) -> QIcon:
        pm = QPixmap(128, 72)
        pm.fill(QColor("#26324a" if video else "#1f5a47"))
        qp = QPainter(pm)
        qp.setPen(QColor("#cfd6e6"))
        f = qp.font(); f.setPointSize(20); qp.setFont(f)
        qp.drawText(pm.rect(), Qt.AlignCenter, "🎬" if video else "🎵")
        qp.end()
        return QIcon(pm)

    def _refresh_icons(self):
        for path, it in self.items.items():
            if it.data(Qt.UserRole + 2) or not it.data(Qt.UserRole + 1):
                continue
            img = self.thumbs.get(path, 1.0)
            if img is not None:
                it.setIcon(QIcon(QPixmap.fromImage(img).scaled(128, 72, Qt.KeepAspectRatio,
                                                               Qt.SmoothTransformation)))
                it.setData(Qt.UserRole + 2, True)
