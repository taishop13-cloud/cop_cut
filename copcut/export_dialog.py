"""Export dialog."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QVBoxLayout)

from . import ffmpeg_tools as ff
from .export import ExportRunner, ExportSettings, can_fast_copy
from .model import Project


class ExportDialog(QDialog):
    def __init__(self, project: Project, default_path: str, parent=None):
        super().__init__(parent)
        self.p = project
        self.runner: ExportRunner | None = None
        self.setWindowTitle("Xuất video")
        self.setMinimumWidth(520)
        v = QVBoxLayout(self)
        form = QFormLayout()

        row = QHBoxLayout()
        self.path = QLineEdit(default_path)
        b = QPushButton("Chọn…")
        b.clicked.connect(self._browse)
        row.addWidget(self.path, 1)
        row.addWidget(b)
        form.addRow("Lưu tại", row)

        self.res = QComboBox()
        w, h = project.width, project.height
        self.res.addItem(f"Gốc dự án ({w}×{h})", (w, h))
        for target in (2160, 1440, 1080, 720, 480):
            short = min(w, h)
            if target >= short:
                continue
            k = target / short
            tw, th = int(round(w * k / 2) * 2), int(round(h * k / 2) * 2)
            self.res.addItem(f"{target}p ({tw}×{th})", (tw, th))
        form.addRow("Độ phân giải", self.res)

        self.fps = QComboBox()
        self.fps.addItem(f"Theo dự án ({project.fps:g})", project.fps)
        for f in (24, 25, 30, 50, 60):
            if abs(f - project.fps) > 0.01:
                self.fps.addItem(str(f), float(f))
        form.addRow("Tốc độ khung hình", self.fps)

        self.quality = QComboBox()
        self.quality.addItem("Cao (tệp lớn hơn)", "high")
        self.quality.addItem("Vừa", "medium")
        self.quality.addItem("Thấp (tệp nhỏ)", "low")
        form.addRow("Chất lượng", self.quality)

        self.enc = QComboBox()
        encs = ff.detect_encoders()
        for enc, label in encs:
            self.enc.addItem(label, enc)
        if len(encs) > 1:
            self.enc.setCurrentIndex(1)   # prefer GPU when available
        form.addRow("Bộ mã hóa", self.enc)

        ok, why = can_fast_copy(project)
        self.fast = QCheckBox("Xuất nhanh – không mã hóa lại (cắt theo keyframe, cực nhanh)")
        self.fast.setEnabled(ok)
        self.fast.setToolTip("Chỉ dùng được khi chỉ cắt/ghép, không đổi tốc độ/âm lượng, không có chữ "
                             "hay nhạc nền. Điểm cắt có thể lệch tới vài giây do keyframe.")
        form.addRow(self.fast)
        if not ok:
            lbl = QLabel("Không thể xuất nhanh: " + why)
            lbl.setObjectName("hint")
            lbl.setWordWrap(True)
            form.addRow(lbl)
        self.fast.toggled.connect(self._fast_toggled)
        v.addLayout(form)

        if project.duration() > project.main_end() + 0.01:
            warn = QLabel("Lưu ý: chữ/nhạc nằm sau video chính sẽ bị cắt; video xuất dài bằng rãnh chính.")
            warn.setWordWrap(True)
            warn.setObjectName("hint")
            v.addWidget(warn)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.status = QLabel("")
        self.status.setObjectName("hint")
        v.addWidget(self.bar)
        v.addWidget(self.status)
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.go = QPushButton("Xuất")
        self.go.setObjectName("accentBtn")
        self.go.clicked.connect(self._start)
        self.close_btn = QPushButton("Đóng")
        self.close_btn.clicked.connect(self._close)
        btns.addWidget(self.go)
        btns.addWidget(self.close_btn)
        v.addLayout(btns)
        self._t0 = 0.0

    def _fast_toggled(self, on):
        for w in (self.res, self.fps, self.quality, self.enc):
            w.setEnabled(not on)

    def _browse(self):
        p, _ = QFileDialog.getSaveFileName(self, "Lưu video", self.path.text(), "Video MP4 (*.mp4)")
        if p:
            self.path.setText(p if p.lower().endswith(".mp4") else p + ".mp4")

    def _start(self):
        out = self.path.text().strip()
        if not out:
            return
        if not out.lower().endswith(".mp4"):
            out += ".mp4"
            self.path.setText(out)
        srcs = {os.path.normcase(os.path.abspath(c.path)) for c in self.p.main + self.p.audio}
        if os.path.normcase(os.path.abspath(out)) in srcs:
            QMessageBox.warning(self, "Xuất video", "Không thể ghi đè lên tệp nguồn đang dùng.")
            return
        if os.path.exists(out) and QMessageBox.question(
                self, "Xuất video", "Tệp đã tồn tại. Ghi đè?") != QMessageBox.Yes:
            return
        w, h = self.res.currentData()
        st = ExportSettings(out_path=out, width=w, height=h, fps=self.fps.currentData(),
                            encoder=self.enc.currentData(), quality=self.quality.currentData(),
                            fast_copy=self.fast.isChecked())
        self.runner = ExportRunner(self.p, st)
        self.runner.progress.connect(self._progress)
        self.runner.finished.connect(self._done)
        self.go.setEnabled(False)
        self.close_btn.setText("Hủy")
        self._t0 = time.monotonic()
        self.status.setText("Đang chuẩn bị…")
        self.runner.start()

    def _progress(self, frac: float, label: str):
        self.bar.setValue(int(frac * 1000))
        el = time.monotonic() - self._t0
        eta = ""
        if frac > 0.01:
            rem = el / frac - el
            eta = f" — còn khoảng {ff.fmt_time(rem)}"
        self.status.setText(f"{label}  {frac * 100:.1f}%{eta}")

    def _done(self, ok: bool, msg: str):
        self.runner = None
        self.go.setEnabled(True)
        self.close_btn.setText("Đóng")
        if ok:
            self.bar.setValue(1000)
            self.status.setText(f"Đã xuất xong trong {ff.fmt_time(time.monotonic() - self._t0)}: {msg}")
            if QMessageBox.question(self, "Xuất video", "Xuất xong! Mở thư mục chứa video?") == QMessageBox.Yes:
                os.startfile(os.path.dirname(msg)) if ff.IS_WIN else None
        else:
            self.status.setText("Lỗi / đã dừng.")
            if msg != "Đã hủy.":
                QMessageBox.critical(self, "Xuất video thất bại", msg)

    def _close(self):
        if self.runner:
            self.runner.cancel()
            return
        self.accept()

    def closeEvent(self, e):
        if self.runner:
            self.runner.cancel()
        super().closeEvent(e)
