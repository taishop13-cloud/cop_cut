"""Right-hand property panel: volume / speed for clips, text + style for subtitles."""
from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QObject, Qt, Signal, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractSlider, QAbstractSpinBox, QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFontComboBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSlider,
                               QSpinBox, QStackedWidget, QGridLayout, QSizePolicy, QToolButton, QVBoxLayout, QWidget, QScrollArea)

from .ffmpeg_tools import fmt_time
from .model import DB_MAX, DB_MIN, BlurClip, Clip, ImageClip, TextClip, TextStyle, db_to_gain, gain_to_db

SPEED_PRESETS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 5.0]


class ColorButton(QPushButton):
    changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.setFixedSize(46, 24)
        self._c = "#ffffff"
        self.clicked.connect(self._pick)

    def set_color(self, c: str):
        self._c = c
        self.setStyleSheet(f"background:{c}; border:1px solid #555; border-radius:4px;")

    def _pick(self):
        c = QColorDialog.getColor(QColor(self._c), self, "Chọn màu")
        if c.isValid():
            self.set_color(c.name())
            self.changed.emit(c.name())


class _WheelOnlyWhenFocused(QObject):
    """Spin boxes, sliders and combo boxes react to the mouse wheel only after they
    were clicked (have focus). Otherwise the wheel scrolls the panel, so moving the
    mouse across the panel never changes a value by accident."""

    def __init__(self, scroll: QScrollArea):
        super().__init__(scroll)
        self._scroll = scroll

    def eventFilter(self, obj, e):
        if e.type() == QEvent.Wheel and not obj.hasFocus():
            QApplication.sendEvent(self._scroll.verticalScrollBar(), e)
            return True
        return False


class Inspector(QWidget):
    before_edit = Signal(str)   # coalescing key -> main window takes undo snapshot
    changed = Signal()          # model changed
    style_all = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.clip = None
        self.kind = None
        self.group: list = []
        self._loading = False
        self.canvas_size = lambda: (1920, 1080)   # replaced by the main window (project size)
        self.stack = QStackedWidget()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Thuộc tính")
        title.setObjectName("panelTitle")
        lay.addWidget(title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMinimumWidth(300)
        scroll.setWidget(self.stack)
        lay.addWidget(scroll, 1)

        empty = QLabel("Chọn một clip trên timeline\nđể chỉnh âm lượng, tốc độ\nhoặc kiểu chữ.")
        empty.setAlignment(Qt.AlignCenter)
        empty.setObjectName("hint")
        self.stack.addWidget(empty)
        self.stack.addWidget(self._build_clip_page())
        self.stack.addWidget(self._build_text_page())
        self.stack.addWidget(self._build_blur_page())
        self.stack.addWidget(self._build_image_page())

        # mouse wheel changes a value only after the field was clicked
        self._wheel_guard = _WheelOnlyWhenFocused(scroll)
        for w in self.stack.findChildren(QWidget):
            if isinstance(w, (QAbstractSpinBox, QAbstractSlider, QComboBox)):
                w.setFocusPolicy(Qt.StrongFocus)     # wheel no longer grabs focus
                w.installEventFilter(self._wheel_guard)
        # clicking an empty part of the panel takes focus away from the last field
        for i in range(self.stack.count()):
            self.stack.widget(i).setFocusPolicy(Qt.ClickFocus)

    # ------------------------------------------------------------ clip page
    def _build_clip_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 8, 12, 8)
        self.clip_name = QLabel()
        self.clip_name.setWordWrap(True)
        self.clip_name.setObjectName("clipName")
        v.addWidget(self.clip_name)
        self.clip_info = QLabel()
        self.clip_info.setObjectName("hint")
        v.addWidget(self.clip_info)

        g = QGroupBox("Âm lượng")
        f = QHBoxLayout(g)
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(int(DB_MIN * 10), int(DB_MAX * 10))   # 0.1 dB steps
        self.vol_slider.setToolTip("Nhấp đúp để về 0 dB (giữ nguyên âm lượng gốc)")
        self.vol_spin = QDoubleSpinBox()
        self.vol_spin.setRange(DB_MIN, DB_MAX)
        self.vol_spin.setDecimals(1)
        self.vol_spin.setSingleStep(1.0)
        self.vol_spin.setSuffix(" dB")
        self.vol_spin.setSpecialValueText("-∞ dB")    # shown at the minimum = muted
        self.vol_spin.setToolTip("0 dB = âm lượng gốc; -6 dB ≈ nhỏ một nửa; +6 dB ≈ to gấp đôi")
        self.reset_vol_btn = QToolButton()
        self.reset_vol_btn.setText("0 dB")
        self.reset_vol_btn.setToolTip("Về âm lượng gốc")
        self.mute_btn = QToolButton()
        self.mute_btn.setText("🔇")
        self.mute_btn.setToolTip("Tắt tiếng / bật lại")
        f.addWidget(self.vol_slider, 1)
        f.addWidget(self.vol_spin)
        f.addWidget(self.reset_vol_btn)
        f.addWidget(self.mute_btn)
        self.vol_slider.valueChanged.connect(lambda x: self.vol_spin.setValue(x / 10))
        self.vol_spin.valueChanged.connect(lambda x: self.vol_slider.setValue(int(round(x * 10))))
        self.vol_spin.valueChanged.connect(self._vol)
        self.reset_vol_btn.clicked.connect(lambda: self.vol_spin.setValue(0.0))
        self.mute_btn.clicked.connect(self._mute)
        v.addWidget(g)
        note = QLabel("Xem trước tối đa 0 dB; phần tăng trên 0 dB được áp dụng khi xuất video.")
        note.setWordWrap(True)
        note.setObjectName("hint")
        v.addWidget(note)

        g2 = QGroupBox("Tốc độ")
        f2 = QVBoxLayout(g2)
        row = QHBoxLayout()
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(10, 1000)   # 0.1x .. 10x, x100
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.1, 10.0)
        self.speed_spin.setSingleStep(0.1)
        self.speed_spin.setDecimals(2)
        self.speed_spin.setSuffix(" x")
        row.addWidget(self.speed_slider, 1)
        row.addWidget(self.speed_spin)
        f2.addLayout(row)
        pr = QGridLayout()
        for s in SPEED_PRESETS:
            b = QToolButton()
            b.setText(f"{s:g}x")
            b.clicked.connect(lambda _=False, s=s: self.speed_spin.setValue(s))
            pr.addWidget(b, SPEED_PRESETS.index(s) // 5, SPEED_PRESETS.index(s) % 5)
        f2.addLayout(pr)
        self.speed_slider.valueChanged.connect(lambda x: self.speed_spin.setValue(x / 100))
        self.speed_spin.valueChanged.connect(lambda x: self.speed_slider.setValue(int(round(x * 100))))
        self.speed_spin.valueChanged.connect(self._speed)
        v.addWidget(g2)
        v.addStretch(1)
        return w

    # ------------------------------------------------------------ text page
    def _build_text_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 8, 12, 8)
        v.addWidget(QLabel("Nội dung"))
        self.text_edit = QPlainTextEdit()
        self.text_edit.setFixedHeight(80)
        self.text_edit.textChanged.connect(self._text)
        v.addWidget(self.text_edit)

        g = QGroupBox("Phông chữ")
        fl = QFormLayout(g)
        self.font_box = QFontComboBox()
        # the default size policy makes it as wide as the longest font name and
        # pushes the whole panel wider than its column
        self.font_box.setSizeAdjustPolicy(QFontComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.font_box.setMinimumContentsLength(10)
        self.font_box.currentFontChanged.connect(lambda f: self._style("font", f.family()))
        fl.addRow("Phông", self.font_box)
        self.size_spin = QSpinBox()
        self.size_spin.setRange(8, 400)
        self.size_spin.setSuffix(" px")
        self.size_spin.valueChanged.connect(lambda x: self._style("size", x))
        fl.addRow("Cỡ chữ", self.size_spin)
        row = QHBoxLayout()
        self.bold_btn = QToolButton(); self.bold_btn.setText("B"); self.bold_btn.setCheckable(True)
        f = QFont(); f.setBold(True); self.bold_btn.setFont(f)
        self.italic_btn = QToolButton(); self.italic_btn.setText("I"); self.italic_btn.setCheckable(True)
        f = QFont(); f.setItalic(True); self.italic_btn.setFont(f)
        self.bold_btn.toggled.connect(lambda x: self._style("bold", x))
        self.italic_btn.toggled.connect(lambda x: self._style("italic", x))
        row.addWidget(self.bold_btn); row.addWidget(self.italic_btn); row.addStretch(1)
        fl.addRow("Kiểu", row)
        self.color_btn = ColorButton()
        self.color_btn.changed.connect(lambda c: self._style("color", c))
        fl.addRow("Màu chữ", self.color_btn)
        v.addWidget(g)

        g2 = QGroupBox("Viền chữ / Nền")
        fl2 = QFormLayout(g2)
        self.outline_btn = ColorButton()
        self.outline_btn.changed.connect(lambda c: self._style("outline_color", c))
        fl2.addRow("Màu viền", self.outline_btn)
        self.outline_spin = QDoubleSpinBox()
        self.outline_spin.setRange(0, 30)
        self.outline_spin.setSingleStep(0.5)
        self.outline_spin.valueChanged.connect(lambda x: self._style("outline", x))
        fl2.addRow("Độ dày viền", self.outline_spin)
        self.bg_check = QCheckBox("Hộp nền sau chữ")
        self.bg_check.toggled.connect(lambda x: self._style("bg", x))
        fl2.addRow(self.bg_check)
        self.bg_btn = ColorButton()
        self.bg_btn.changed.connect(lambda c: self._style("bg_color", c))
        fl2.addRow("Màu nền", self.bg_btn)
        v.addWidget(g2)

        g4 = QGroupBox("Độ trong suốt")
        h4 = QHBoxLayout(g4)
        self.op_slider = QSlider(Qt.Horizontal)
        self.op_slider.setRange(0, 100)
        self.op_spin = QSpinBox()
        self.op_spin.setRange(0, 100)
        self.op_spin.setSuffix(" %")
        tip = "Độ hiện của chữ: 100% = rõ hoàn toàn, càng thấp chữ càng trong suốt (áp dụng cả viền và nền)"
        self.op_slider.setToolTip(tip)
        self.op_spin.setToolTip(tip)
        self.op_slider.valueChanged.connect(self.op_spin.setValue)
        self.op_spin.valueChanged.connect(self.op_slider.setValue)
        self.op_spin.valueChanged.connect(lambda x: self._style("opacity", x / 100))
        h4.addWidget(QLabel("Độ hiện"))
        h4.addWidget(self.op_slider, 1)
        h4.addWidget(self.op_spin)
        v.addWidget(g4)

        g3 = QGroupBox("Vị trí (kéo chữ trực tiếp trên khung xem trước)")
        fl3 = QFormLayout(g3)
        self.x_spin = QDoubleSpinBox(); self.x_spin.setRange(0, 100); self.x_spin.setSuffix(" %")
        self.y_spin = QDoubleSpinBox(); self.y_spin.setRange(0, 100); self.y_spin.setSuffix(" %")
        self.x_spin.valueChanged.connect(lambda x: self._style("x", x / 100))
        self.y_spin.valueChanged.connect(lambda x: self._style("y", x / 100))
        fl3.addRow("Ngang", self.x_spin)
        fl3.addRow("Dọc", self.y_spin)
        pos_row = QHBoxLayout()
        for label, yy in (("Trên", 0.1), ("Giữa", 0.5), ("Dưới", 0.88)):
            b = QToolButton(); b.setText(label)
            b.clicked.connect(lambda _=False, yy=yy: (self.x_spin.setValue(50), self.y_spin.setValue(yy * 100)))
            pos_row.addWidget(b)
        fl3.addRow(pos_row)
        hc = QPushButton("↔ Căn giữa ngang")
        hc.setToolTip("Đưa chữ về chính giữa theo chiều ngang, giữ nguyên độ cao")
        hc.clicked.connect(lambda: self.x_spin.setValue(50))
        fl3.addRow(hc)
        v.addWidget(g3)

        g4 = QGroupBox("Thời gian")
        fl4 = QFormLayout(g4)
        self.t_start = QDoubleSpinBox(); self.t_start.setRange(0, 10 ** 7); self.t_start.setDecimals(2); self.t_start.setSuffix(" s")
        self.t_dur = QDoubleSpinBox(); self.t_dur.setRange(0.05, 10 ** 6); self.t_dur.setDecimals(2); self.t_dur.setSuffix(" s")
        self.t_start.valueChanged.connect(lambda x: self._timing("start", x))
        self.t_dur.valueChanged.connect(lambda x: self._timing("duration", x))
        fl4.addRow("Bắt đầu", self.t_start)
        fl4.addRow("Thời lượng", self.t_dur)
        v.addWidget(g4)

        b = QPushButton("Áp dụng kiểu này cho cả rãnh chữ này")
        b.setToolTip("Chỉ áp dụng cho các dòng chữ cùng rãnh với dòng đang chọn.\n"
                     "Các rãnh khác (tên kênh, thông báo…) giữ nguyên kiểu riêng.")
        b.clicked.connect(self.style_all)
        v.addWidget(b)
        v.addStretch(1)
        return w

    # ------------------------------------------------------------ blur page
    def _build_blur_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 8, 12, 8)
        t = QLabel("▦ Vùng che mờ")
        t.setObjectName("clipName")
        v.addWidget(t)
        hint = QLabel("Kéo khung trên màn hình xem trước để di chuyển, kéo 4 góc để đổi kích thước. "
                      "Mỗi vùng mờ là độc lập — thêm bao nhiêu vùng tùy ý.")
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        v.addWidget(hint)

        g = QGroupBox("Độ mờ")
        f = QHBoxLayout(g)
        self.blur_slider = QSlider(Qt.Horizontal)
        self.blur_slider.setRange(1, 100)
        self.blur_spin = QSpinBox()
        self.blur_spin.setRange(1, 100)
        f.addWidget(self.blur_slider, 1)
        f.addWidget(self.blur_spin)
        self.blur_slider.valueChanged.connect(self.blur_spin.setValue)
        self.blur_spin.valueChanged.connect(self.blur_slider.setValue)
        self.blur_spin.valueChanged.connect(lambda x: self._blur("strength", int(x)))
        v.addWidget(g)

        g2 = QGroupBox("Vị trí và kích thước (% khung hình)")
        fl = QFormLayout(g2)
        self.bx = QDoubleSpinBox(); self.by = QDoubleSpinBox()
        self.bw = QDoubleSpinBox(); self.bh = QDoubleSpinBox()
        for sp, key, label in ((self.bx, "x", "Trái"), (self.by, "y", "Trên"),
                               (self.bw, "w", "Rộng"), (self.bh, "h", "Cao")):
            sp.setRange(-50 if key in "xy" else 1, 150 if key in "xy" else 100)
            sp.setDecimals(1)
            sp.setSuffix(" %")
            sp.valueChanged.connect(lambda val, key=key: self._blur(key, val / 100))
            fl.addRow(label, sp)
        row = QHBoxLayout()
        b1 = QToolButton(); b1.setText("↔ Giữa ngang")
        b1.clicked.connect(lambda: self.bx.setValue(50 - self.bw.value() / 2))
        b2 = QToolButton(); b2.setText("↕ Giữa dọc")
        b2.clicked.connect(lambda: self.by.setValue(50 - self.bh.value() / 2))
        row.addWidget(b1); row.addWidget(b2); row.addStretch(1)
        fl.addRow(row)
        v.addWidget(g2)

        g3 = QGroupBox("Thời gian")
        fl3 = QFormLayout(g3)
        self.b_start = QDoubleSpinBox(); self.b_start.setRange(0, 10 ** 7); self.b_start.setDecimals(2); self.b_start.setSuffix(" s")
        self.b_dur = QDoubleSpinBox(); self.b_dur.setRange(0.05, 10 ** 6); self.b_dur.setDecimals(2); self.b_dur.setSuffix(" s")
        self.b_start.valueChanged.connect(lambda x: self._blur("start", x))
        self.b_dur.valueChanged.connect(lambda x: self._blur("duration", x))
        fl3.addRow("Bắt đầu", self.b_start)
        fl3.addRow("Thời lượng", self.b_dur)
        v.addWidget(g3)
        v.addStretch(1)
        return w

    # ------------------------------------------------------------ image (logo) page
    def _build_image_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 8, 12, 8)
        self.img_name = QLabel()
        self.img_name.setObjectName("clipName")
        self.img_name.setWordWrap(True)
        v.addWidget(self.img_name)
        hint = QLabel("Kéo logo trên màn hình xem trước để di chuyển, kéo 4 góc để đổi kích thước "
                      "(giữ nguyên tỉ lệ ảnh). Kéo hai mép trên timeline để đổi thời gian hiện.")
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        v.addWidget(hint)

        self.img_circle = QCheckBox("Khung tròn (tự cắt phần thừa)")
        self.img_circle.setToolTip("Cắt ảnh thành hình vuông ở giữa rồi bo tròn; phần nằm ngoài vòng tròn bị bỏ.\n"
                                   "Bỏ chọn để hiện ảnh nguyên hình gốc.")
        self.img_circle.toggled.connect(self._img_circle)
        v.addWidget(self.img_circle)

        g = QGroupBox("Độ trong suốt")
        h = QHBoxLayout(g)
        self.img_op_slider = QSlider(Qt.Horizontal)
        self.img_op_slider.setRange(0, 100)
        self.img_op_spin = QSpinBox()
        self.img_op_spin.setRange(0, 100)
        self.img_op_spin.setSuffix(" %")
        self.img_op_slider.valueChanged.connect(self.img_op_spin.setValue)
        self.img_op_spin.valueChanged.connect(self.img_op_slider.setValue)
        self.img_op_spin.valueChanged.connect(lambda x: self._blur("opacity", x / 100))
        h.addWidget(QLabel("Độ hiện"))
        h.addWidget(self.img_op_slider, 1)
        h.addWidget(self.img_op_spin)
        v.addWidget(g)

        g2 = QGroupBox("Kích thước và vị trí (% khung hình)")
        fl = QFormLayout(g2)
        self.img_size = QDoubleSpinBox()
        self.img_size.setRange(1, 100)
        self.img_size.setDecimals(1)
        self.img_size.setSuffix(" %")
        self.img_size.setToolTip("Chiều rộng logo so với khung hình; chiều cao tự theo tỉ lệ ảnh")
        self.img_size.valueChanged.connect(self._img_resize)
        fl.addRow("Rộng", self.img_size)
        self.ix = QDoubleSpinBox(); self.iy = QDoubleSpinBox()
        for sp, key, label in ((self.ix, "x", "Trái"), (self.iy, "y", "Trên")):
            sp.setRange(-50, 150)
            sp.setDecimals(1)
            sp.setSuffix(" %")
            sp.valueChanged.connect(lambda val, key=key: self._blur(key, val / 100))
            fl.addRow(label, sp)
        grid = QGridLayout()
        m = 3.0   # margin from the frame edge, %
        for i, (label, fx, fy) in enumerate((("↖ Trên trái", 0, 0), ("↗ Trên phải", 1, 0),
                                              ("↙ Dưới trái", 0, 1), ("↘ Dưới phải", 1, 1))):
            b = QToolButton(); b.setText(label)
            b.clicked.connect(lambda _=False, fx=fx, fy=fy: self._img_corner(fx, fy, m))
            grid.addWidget(b, i // 2, i % 2)
        fl.addRow(grid)
        v.addWidget(g2)

        g3 = QGroupBox("Thời gian")
        fl3 = QFormLayout(g3)
        self.i_start = QDoubleSpinBox(); self.i_start.setRange(0, 10 ** 7); self.i_start.setDecimals(2); self.i_start.setSuffix(" s")
        self.i_dur = QDoubleSpinBox(); self.i_dur.setRange(0.05, 10 ** 6); self.i_dur.setDecimals(2); self.i_dur.setSuffix(" s")
        self.i_start.valueChanged.connect(lambda x: self._blur("start", x))
        self.i_dur.valueChanged.connect(lambda x: self._blur("duration", x))
        fl3.addRow("Bắt đầu", self.i_start)
        fl3.addRow("Thời lượng", self.i_dur)
        v.addWidget(g3)
        v.addStretch(1)
        return w

    def _img_circle(self, on: bool):
        c = self.clip
        if self._loading or not isinstance(c, ImageClip) or c.circle == on:
            return
        from PySide6.QtGui import QImageReader
        size = QImageReader(c.path).size()
        cw, ch = self.canvas_size()
        self.before_edit.emit(f"imgcircle{c.id}")
        c.circle = on
        c.fit_aspect(cw, ch, size.width(), size.height())
        self.changed.emit()
        self._load_image_pos()

    def _img_resize(self, pct):
        c = self.clip
        if self._loading or not isinstance(c, ImageClip) or c.w <= 0:
            return
        nw = pct / 100
        if abs(nw - c.w) < 1e-6:
            return
        ratio = c.h / c.w
        cx, cy = c.x + c.w / 2, c.y + c.h / 2      # resize around the centre
        self.before_edit.emit(f"imgsize{c.id}")
        c.w, c.h = nw, nw * ratio
        c.x, c.y = cx - c.w / 2, cy - c.h / 2
        self.changed.emit()
        self._load_image_pos()

    def _img_corner(self, fx, fy, margin):
        c = self.clip
        if not isinstance(c, ImageClip):
            return
        self.before_edit.emit(f"imgpos{c.id}")
        c.x = margin / 100 if fx == 0 else 1 - margin / 100 - c.w
        c.y = margin / 100 if fy == 0 else 1 - margin / 100 - c.h
        self.changed.emit()
        self._load_image_pos()

    def _load_image_pos(self):
        self._loading = True
        try:
            self.ix.setValue(self.clip.x * 100)
            self.iy.setValue(self.clip.y * 100)
        finally:
            self._loading = False

    def _blur(self, key, value):
        if self._loading or not isinstance(self.clip, BlurClip):
            return
        if abs(getattr(self.clip, key) - value) < 1e-9:
            return
        self.before_edit.emit(f"blur{key}{self.clip.id}")
        setattr(self.clip, key, value)
        self.changed.emit()

    # ------------------------------------------------------------ binding
    def _page(self, idx: int):
        """Show one page; hidden pages are ignored for sizing so the widest page
        does not force the panel wider than its column."""
        for i in range(self.stack.count()):
            pol = QSizePolicy.Preferred if i == idx else QSizePolicy.Ignored
            self.stack.widget(i).setSizePolicy(pol, pol)
        self.stack.setCurrentIndex(idx)
        self.stack.adjustSize()

    def show_clip(self, kind: str | None, clip, group: list | None = None):
        self.kind, self.clip = kind, clip
        # Ctrl/Shift+click selection: volume / speed edits apply to every clip in it
        group = [c for c in (group or []) if isinstance(c, Clip)]
        self.group = group if clip in group and len(group) > 1 else ([clip] if isinstance(clip, Clip) else [])
        self._loading = True
        try:
            if clip is None:
                self._page(0)
            elif isinstance(clip, ImageClip):
                self._page(4)

                self.img_name.setText("🖼 " + os.path.basename(clip.path))
                self.img_name.setToolTip(clip.path)
                self.img_op_spin.setValue(int(round(clip.opacity * 100)))
                self.img_circle.setChecked(clip.circle)
                self.img_size.setValue(clip.w * 100)
                self.ix.setValue(clip.x * 100)
                self.iy.setValue(clip.y * 100)
                self.i_start.setValue(clip.start)
                self.i_dur.setValue(clip.duration)
            elif isinstance(clip, BlurClip):
                self._page(3)
                self.blur_spin.setValue(clip.strength)
                self.bx.setValue(clip.x * 100)
                self.by.setValue(clip.y * 100)
                self.bw.setValue(clip.w * 100)
                self.bh.setValue(clip.h * 100)
                self.b_start.setValue(clip.start)
                self.b_dur.setValue(clip.duration)
            elif isinstance(clip, TextClip):
                self._page(2)
                st = clip.style
                if self.text_edit.toPlainText() != clip.text:
                    self.text_edit.setPlainText(clip.text)
                self.font_box.setCurrentFont(QFont(st.font))
                self.size_spin.setValue(int(st.size))
                self.bold_btn.setChecked(st.bold)
                self.italic_btn.setChecked(st.italic)
                self.color_btn.set_color(st.color)
                self.outline_btn.set_color(st.outline_color)
                self.outline_spin.setValue(st.outline)
                self.bg_check.setChecked(st.bg)
                self.bg_btn.set_color(st.bg_color)
                self.op_spin.setValue(int(round(st.opacity * 100)))
                self.x_spin.setValue(st.x * 100)
                self.y_spin.setValue(st.y * 100)
                self.t_start.setValue(clip.start)
                self.t_dur.setValue(clip.duration)
            else:
                self._page(1)
                name = clip.path.replace("\\", "/").rsplit("/", 1)[-1]
                icon = "🎬 " if kind == "main" else "🎵 "
                if len(self.group) > 1:
                    what = "đoạn video" if kind == "main" else "đoạn âm thanh"
                    self.clip_name.setText(f"{icon}Đã chọn {len(self.group)} {what}")
                else:
                    self.clip_name.setText(icon + name)
                self._show_info()
                self.vol_spin.setValue(round(gain_to_db(clip.volume), 1))
                self.speed_spin.setValue(clip.speed)
        finally:
            self._loading = False

    def refresh(self):
        self.show_clip(self.kind, self.clip, self.group)

    def _show_info(self):
        if len(self.group) > 1:
            total = sum(c.duration for c in self.group)
            self.clip_info.setText(f"Tổng thời lượng trên timeline: {fmt_time(total)}\n"
                                   "Âm lượng và tốc độ áp dụng cho cả nhóm.")
        else:
            c = self.clip
            self.clip_info.setText(
                f"Đoạn nguồn: {fmt_time(c.src_in)} → {fmt_time(c.src_out)}\n"
                f"Thời lượng trên timeline: {fmt_time(c.duration)}")

    def _group_key(self) -> str:
        return ",".join(str(c.id) for c in self.group)

    def focus_text(self):
        if isinstance(self.clip, TextClip):
            self.text_edit.setFocus()
            self.text_edit.selectAll()

    # ------------------------------------------------------------ edits
    def _vol(self, v):
        if self._loading or not isinstance(self.clip, Clip):
            return
        g = db_to_gain(v)
        if all(abs(c.volume - g) < 1e-9 for c in self.group):
            return
        self.before_edit.emit(f"vol{self._group_key()}")
        for c in self.group:
            c.volume = g
        self.changed.emit()

    def _mute(self):
        if not isinstance(self.clip, Clip):
            return
        self.vol_spin.setValue(DB_MIN if self.clip.volume > 0 else 0.0)

    def _speed(self, s):
        if self._loading or not isinstance(self.clip, Clip):
            return
        s = max(0.1, min(10.0, s))
        if all(abs(s - c.speed) < 1e-6 for c in self.group):
            return
        self.before_edit.emit(f"speed{self._group_key()}")
        for c in self.group:
            c.speed = s
        self.changed.emit()
        self._show_info()

    def _text(self):
        if self._loading or not isinstance(self.clip, TextClip):
            return
        self.before_edit.emit(f"text{self.clip.id}")
        self.clip.text = self.text_edit.toPlainText()
        self.changed.emit()

    def _style(self, key, value):
        if self._loading or not isinstance(self.clip, TextClip):
            return
        if getattr(self.clip.style, key) == value:
            return
        self.before_edit.emit(f"style{key}{self.clip.id}")
        setattr(self.clip.style, key, value)
        self.changed.emit()

    def _timing(self, key, value):
        if self._loading or not isinstance(self.clip, TextClip):
            return
        self.before_edit.emit(f"timing{key}{self.clip.id}")
        setattr(self.clip, key, value)
        self.changed.emit()
