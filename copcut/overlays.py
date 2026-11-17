"""Items drawn over the preview video: text, blur boxes and centre guides."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject

from .model import BlurClip, ImageClip, TextClip

SNAP_SCREEN_PX = 10          # how close (on screen) the centre must be to snap
HANDLE_SCREEN_PX = 10
C_GUIDE = QColor(255, 255, 255, 120)
C_GUIDE_HOT = QColor("#ffcc33")
C_SEL = QColor(79, 140, 255)


def blur_radius(strength: float, height: float) -> float:
    """Box-blur radius in pixels for a given strength (1..100) and frame height.
    Shared by preview and export so they look the same."""
    return max(2.0, strength * height / 1080.0 * 0.5)


def _view_scale(item: QGraphicsItem) -> float:
    sc = item.scene()
    if sc and sc.views():
        return max(1e-6, sc.views()[0].transform().m11())
    return 1.0


class Guides(QGraphicsObject):
    """Centre cross (and safe area) on top of the canvas. Turns yellow when something snaps."""

    def __init__(self, w: int, h: int):
        super().__init__()
        self.cw, self.ch = w, h
        self.always = False
        self.dragging = False
        self.hot_x = False
        self.hot_y = False
        self.setZValue(100)
        self.setAcceptedMouseButtons(Qt.NoButton)

    def set_canvas(self, w, h):
        self.prepareGeometryChange()
        self.cw, self.ch = w, h

    def threshold(self) -> float:
        return SNAP_SCREEN_PX / _view_scale(self)

    def set_state(self, dragging: bool, hot_x: bool = False, hot_y: bool = False):
        if (dragging, hot_x, hot_y) != (self.dragging, self.hot_x, self.hot_y):
            self.dragging, self.hot_x, self.hot_y = dragging, hot_x, hot_y
            self.update()

    def snap(self, cx: float, cy: float) -> tuple[float, float, bool, bool]:
        thr = self.threshold()
        sx = abs(cx - self.cw / 2) < thr
        sy = abs(cy - self.ch / 2) < thr
        return (self.cw / 2 if sx else cx), (self.ch / 2 if sy else cy), sx, sy

    def boundingRect(self):
        return QRectF(0, 0, self.cw, self.ch)

    def paint(self, qp: QPainter, opt, widget=None):
        if not (self.always or self.dragging):
            return
        cx, cy = self.cw / 2, self.ch / 2
        for hot, line in ((self.hot_x, (QPointF(cx, 0), QPointF(cx, self.ch))),
                          (self.hot_y, (QPointF(0, cy), QPointF(self.cw, cy)))):
            pen = QPen(C_GUIDE_HOT if hot else C_GUIDE, 2.0 if hot else 1.0,
                       Qt.SolidLine if hot else Qt.DashLine)
            pen.setCosmetic(True)
            qp.setPen(pen)
            qp.drawLine(*line)
        r = 8 / _view_scale(self)
        pen = QPen(C_GUIDE_HOT if (self.hot_x and self.hot_y) else C_GUIDE, 1.5)
        pen.setCosmetic(True)
        qp.setPen(pen)
        qp.setBrush(Qt.NoBrush)
        qp.drawEllipse(QPointF(cx, cy), r, r)
        if self.always:
            pen = QPen(QColor(255, 255, 255, 70), 1, Qt.DotLine)
            pen.setCosmetic(True)
            qp.setPen(pen)
            qp.drawRect(QRectF(self.cw * 0.05, self.ch * 0.05, self.cw * 0.9, self.ch * 0.9))


class TextOverlay(QGraphicsObject):
    moved = Signal(object, float, float)   # clip, x, y (normalised)
    clicked = Signal(object)

    def __init__(self, clip: TextClip, canvas_w: int, canvas_h: int, guides: Guides):
        super().__init__()
        self.clip = clip
        self.guides = guides
        self.cw, self.ch = canvas_w, canvas_h
        self._sig = None
        self._path = QPainterPath()
        self._box = QRectF()
        self._dragging = False
        self.selected_hint = False
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setCursor(Qt.SizeAllCursor)
        self.refresh()

    def refresh(self):
        c, st = self.clip, self.clip.style
        sig = (c.text, st.font, st.size, st.bold, st.italic, st.outline, st.bg, self.cw, self.ch,
               st.color, st.outline_color, st.bg_color)
        if not self._dragging:
            self.setPos(st.x * self.cw, st.y * self.ch)
        op = max(0.0, min(1.0, getattr(st, "opacity", 1.0)))
        if abs(self.opacity() - op) > 1e-6:
            self.setOpacity(op)
        if sig == self._sig:
            return
        self._sig = sig
        self.prepareGeometryChange()
        font = QFont(st.font)
        font.setPixelSize(max(1, int(st.size)))
        font.setBold(st.bold)
        font.setItalic(st.italic)
        fm = QFontMetricsF(font)
        path = QPainterPath()
        y = 0.0
        for line in (c.text.split("\n") or [""]):
            w = fm.horizontalAdvance(line)
            path.addText(-w / 2, y + fm.ascent(), font, line)
            y += fm.lineSpacing()
        path.translate(0, -y / 2)
        self._path = path
        br = path.boundingRect()
        if br.isEmpty():
            br = QRectF(-10, -fm.height() / 2, 20, fm.height())
        if st.bg:
            pad = max(4.0, st.size * 0.18)
            self._box = QRectF(br.left() - pad, -y / 2 - pad * 0.3, br.width() + 2 * pad, y + pad * 0.6)
        else:
            m = st.outline + 2
            self._box = br.adjusted(-m, -m, m, m)
        self.update()

    def boundingRect(self):
        return self._box.adjusted(-2, -2, 2, 2)

    def paint(self, qp: QPainter, opt, widget=None):
        st = self.clip.style
        qp.setRenderHint(QPainter.Antialiasing, True)
        if st.bg:
            qp.fillRect(self._box, QColor(st.bg_color))
        elif st.outline > 0:
            pen = QPen(QColor(st.outline_color), st.outline * 2)
            pen.setJoinStyle(Qt.RoundJoin)
            qp.strokePath(self._path, pen)
        qp.fillPath(self._path, QBrush(QColor(st.color)))
        if self._dragging or self.selected_hint:
            pen = QPen(C_SEL, 1.5, Qt.DashLine)
            pen.setCosmetic(True)
            qp.setPen(pen)
            qp.setBrush(Qt.NoBrush)
            qp.drawRect(self._box)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange and self._dragging:
            p = QPointF(value)
            x, y, sx, sy = self.guides.snap(p.x(), p.y())
            self.guides.set_state(True, sx, sy)
            return QPointF(x, y)
        return super().itemChange(change, value)

    def mousePressEvent(self, e):
        self._dragging = True
        self.guides.set_state(True)
        self.clicked.emit(self.clip)
        self.update()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        self._dragging = False
        self.guides.set_state(False)
        self.update()
        x = min(1.0, max(0.0, self.pos().x() / self.cw))
        y = min(1.0, max(0.0, self.pos().y() / self.ch))
        if abs(x - self.clip.style.x) > 1e-6 or abs(y - self.clip.style.y) > 1e-6:
            self.moved.emit(self.clip, x, y)


class BlurOverlay(QGraphicsObject):
    """A blur box: drag to move (snaps its centre to the frame centre), corners to resize."""
    changed = Signal(object, float, float, float, float)   # clip, x, y, w, h (normalised)
    clicked = Signal(object)

    def __init__(self, clip: BlurClip, canvas_w: int, canvas_h: int, guides: Guides):
        super().__init__()
        self.clip = clip
        self.guides = guides
        self.cw, self.ch = canvas_w, canvas_h
        self.rect = QRectF()
        self.pix: QPixmap | None = None
        self.pix_rect = QRectF()          # where pix goes, in canvas coords
        self.show_frame = True
        self.selected_hint = False
        self._drag = None
        self.recompute = None             # callable set by the engine to refresh the blur image
        self.aspect: float | None = None  # width / height to keep while resizing (images)
        self.setAcceptHoverEvents(True)
        self.setZValue(5)
        self.refresh()

    def refresh(self):
        if self._drag:
            return
        c = self.clip
        r = QRectF(c.x * self.cw, c.y * self.ch, c.w * self.cw, c.h * self.ch)
        if r != self.rect:
            self.prepareGeometryChange()
            self.rect = r
            self.update()

    def boundingRect(self):
        m = HANDLE_SCREEN_PX / _view_scale(self)
        return self.rect.adjusted(-m, -m, m, m)

    def set_blur(self, pix: QPixmap | None, target: QRectF):
        self.pix, self.pix_rect = pix, target
        self.update()

    def paint(self, qp: QPainter, opt, widget=None):
        if self.pix is not None and not self.pix.isNull():
            qp.drawPixmap(self.pix_rect, self.pix, QRectF(self.pix.rect()))
        else:
            qp.fillRect(self.rect, QColor(128, 128, 128, 170))
        if not self.show_frame:
            return
        pen = QPen(C_SEL if (self.selected_hint or self._drag) else QColor(255, 255, 255, 150),
                   1.5, Qt.DashLine)
        pen.setCosmetic(True)
        qp.setPen(pen)
        qp.setBrush(Qt.NoBrush)
        qp.drawRect(self.rect)
        if self.selected_hint or self._drag:
            hs = HANDLE_SCREEN_PX / _view_scale(self)
            qp.setBrush(QColor("#ffffff"))
            for pt in self._corners().values():
                qp.drawRect(QRectF(pt.x() - hs / 2, pt.y() - hs / 2, hs, hs))

    def _corners(self):
        r = self.rect
        return {"tl": r.topLeft(), "tr": r.topRight(), "bl": r.bottomLeft(), "br": r.bottomRight()}

    def _handle_at(self, p: QPointF) -> str | None:
        hs = HANDLE_SCREEN_PX / _view_scale(self)
        for k, pt in self._corners().items():
            if abs(p.x() - pt.x()) <= hs and abs(p.y() - pt.y()) <= hs:
                return k
        return None

    def hoverMoveEvent(self, e):
        h = self._handle_at(e.pos())
        if h in ("tl", "br"):
            self.setCursor(Qt.SizeFDiagCursor)
        elif h in ("tr", "bl"):
            self.setCursor(Qt.SizeBDiagCursor)
        elif self.rect.contains(e.pos()):
            self.setCursor(Qt.SizeAllCursor)
        else:
            self.unsetCursor()

    def shape(self):
        path = QPainterPath()
        path.addRect(self.boundingRect())
        return path

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            e.ignore()
            return
        h = self._handle_at(e.pos())
        if h is None and not self.rect.contains(e.pos()):
            e.ignore()
            return
        self._drag = {"mode": h or "move", "p0": e.scenePos(), "r0": QRectF(self.rect)}
        self.clicked.emit(self.clip)
        if not h:
            self.guides.set_state(True)
        e.accept()

    def mouseMoveEvent(self, e):
        d = self._drag
        if not d:
            return
        delta = e.scenePos() - d["p0"]
        r = QRectF(d["r0"])
        minsz = 16.0
        if d["mode"] == "move":
            r.translate(delta)
            cx, cy, sx, sy = self.guides.snap(r.center().x(), r.center().y())
            r.moveCenter(QPointF(cx, cy))
            self.guides.set_state(True, sx, sy)
        else:
            if "l" in d["mode"]:
                r.setLeft(min(r.right() - minsz, r.left() + delta.x()))
            if "r" in d["mode"]:
                r.setRight(max(r.left() + minsz, r.right() + delta.x()))
            if "t" in d["mode"]:
                r.setTop(min(r.bottom() - minsz, r.top() + delta.y()))
            if "b" in d["mode"]:
                r.setBottom(max(r.top() + minsz, r.bottom() + delta.y()))
            if self.aspect:
                # keep the picture's proportions: height follows width, opposite corner fixed
                h = r.width() / self.aspect
                if "t" in d["mode"]:
                    r.setTop(r.bottom() - h)
                else:
                    r.setBottom(r.top() + h)
        # keep inside the frame
        r = r.intersected(QRectF(-r.width() * 0.5, -r.height() * 0.5,
                                 self.cw + r.width(), self.ch + r.height()))
        self.prepareGeometryChange()
        self.rect = r
        if self.recompute:
            self.recompute()
        self.update()

    def mouseReleaseEvent(self, e):
        d = self._drag
        self._drag = None
        self.guides.set_state(False)
        if not d:
            return
        r = self.rect
        if r != d["r0"]:
            self.changed.emit(self.clip, r.x() / self.cw, r.y() / self.ch, r.width() / self.cw,
                              r.height() / self.ch)
        self.update()


_logo_cache: dict[tuple[str, bool], QImage] = {}
_pix_cache: dict[tuple[str, bool], QPixmap] = {}


def logo_image(path: str, circle: bool) -> QImage:
    """The picture a logo shows. With `circle` it is centre-cropped to a square (the parts
    outside are cut off) and everything outside the inscribed circle becomes transparent.
    QImage only, so the export thread can use it too."""
    key = (path, circle)
    img = _logo_cache.get(key)
    if img is not None:
        return img
    img = QImage(path)
    if not img.isNull():
        img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        if circle:
            side = min(img.width(), img.height())
            sq = img.copy((img.width() - side) // 2, (img.height() - side) // 2, side, side)
            out = QImage(side, side, QImage.Format_ARGB32_Premultiplied)
            out.fill(Qt.transparent)
            qp = QPainter(out)
            qp.setRenderHint(QPainter.Antialiasing)
            path_ = QPainterPath()
            path_.addEllipse(QRectF(0, 0, side, side))
            qp.setClipPath(path_)
            qp.drawImage(0, 0, sq)
            qp.end()
            img = out
    _logo_cache[key] = img
    return img


def logo_pixmap(path: str, circle: bool) -> QPixmap:
    key = (path, circle)
    pm = _pix_cache.get(key)
    if pm is None:
        pm = QPixmap.fromImage(logo_image(path, circle))
        _pix_cache[key] = pm
    return pm


class ImageOverlay(BlurOverlay):
    """A logo / still image: same move + corner-resize as a blur box, keeps its aspect
    (square when shown in the round frame)."""

    def __init__(self, clip: ImageClip, canvas_w: int, canvas_h: int, guides: Guides):
        self._circle = None
        self.image = QPixmap()
        super().__init__(clip, canvas_w, canvas_h, guides)
        self.setZValue(6)

    def refresh(self):
        if self._circle != self.clip.circle:
            self._circle = self.clip.circle
            self.image = logo_pixmap(self.clip.path, self.clip.circle)
            self.aspect = (self.image.width() / self.image.height()
                           if not self.image.isNull() and self.image.height() else None)
            self.update()
        super().refresh()

    def paint(self, qp: QPainter, opt, widget=None):
        if self.image.isNull():
            qp.fillRect(self.rect, QColor(160, 60, 60, 170))
            qp.setPen(QColor("#ffffff"))
            qp.drawText(self.rect, Qt.AlignCenter, "Không tìm thấy ảnh")
        else:
            qp.save()
            qp.setOpacity(max(0.0, min(1.0, self.clip.opacity)))
            qp.setRenderHint(QPainter.SmoothPixmapTransform)
            qp.drawPixmap(self.rect, self.image, QRectF(self.image.rect()))
            qp.restore()
        if not self.show_frame or not (self.selected_hint or self._drag):
            return
        pen = QPen(C_SEL, 1.5, Qt.DashLine)
        pen.setCosmetic(True)
        qp.setPen(pen)
        qp.setBrush(Qt.NoBrush)
        qp.drawRect(self.rect)
        hs = HANDLE_SCREEN_PX / _view_scale(self)
        qp.setBrush(QColor("#ffffff"))
        for pt in self._corners().values():
            qp.drawRect(QRectF(pt.x() - hs / 2, pt.y() - hs / 2, hs, hs))
