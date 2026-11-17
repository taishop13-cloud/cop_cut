"""Dialog: pick subtitle lines and a voice, then turn them into speech clips."""
from __future__ import annotations

import subprocess
import sys

from PySide6.QtCore import QObject, Qt, QUrl, QSettings, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QProgressDialog, QSpinBox, QDoubleSpinBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from . import tts
from .ffmpeg_tools import fmt_time
from .model import DB_MAX, DB_MIN, TextClip, db_to_gain, gain_to_db


class TTSDialog(QDialog):
    def __init__(self, texts: list[TextClip], preselected: set[int], playhead: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chuyển phụ đề thành giọng nói")
        self.setMinimumSize(820, 580)
        self.settings = QSettings("CopCut", "CopCut")
        self.texts = sorted(texts, key=lambda c: (c.start, c.track))
        self.playhead = playhead
        self._player = None
        self._runner = None
        v = QVBoxLayout(self)

        # ---- subtitle list
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Tìm trong phụ đề…")
        self.search.textChanged.connect(self._filter)
        self.track = QComboBox()
        self.track.addItem("Mọi rãnh chữ", -1)
        for k in sorted({c.track for c in texts}):
            self.track.addItem(f"Rãnh chữ {k + 1}", k)
        self.track.currentIndexChanged.connect(self._filter)
        top.addWidget(self.search, 1)
        top.addWidget(self.track)
        v.addLayout(top)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Thời gian", "Rãnh", "Nội dung"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.itemChanged.connect(lambda *_: self._count())
        self.tree.itemDoubleClicked.connect(lambda it, _c: self._preview(it))
        self.tree.setUpdatesEnabled(False)
        for c in self.texts:
            it = QTreeWidgetItem([f"{fmt_time(c.start)}  →  {fmt_time(c.end)}", str(c.track + 1),
                                  tts.clean_text(c.text)])
            it.setData(0, Qt.UserRole, c.id)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Checked if c.id in preselected else Qt.Unchecked)
            self.tree.addTopLevelItem(it)
        self.tree.setUpdatesEnabled(True)
        self.tree.setColumnWidth(0, 190)
        self.tree.setColumnWidth(1, 44)
        v.addWidget(self.tree, 1)

        quick = QHBoxLayout()
        for label, fn in (("Chọn tất cả", lambda: self._check_all(True)),
                          ("Bỏ chọn", lambda: self._check_all(False)),
                          ("Từ đầu phát trở đi", self._from_playhead),
                          ("Chọn các dòng đang bôi đen", self._check_highlighted)):
            b = QPushButton(label)
            b.setAutoDefault(False)
            b.clicked.connect(fn)
            quick.addWidget(b)
        quick.addStretch(1)
        self.count = QLabel()
        quick.addWidget(self.count)
        v.addLayout(quick)

        # ---- voice settings
        form = QFormLayout()
        vrow = QHBoxLayout()
        self.voice = QComboBox()
        for vid, label in tts.all_voices():
            self.voice.addItem(label, vid)
        last = self.settings.value("tts_voice", tts.DEFAULT_VOICE)
        i = self.voice.findData(last)
        self.voice.setCurrentIndex(max(0, i))
        self.voice.currentIndexChanged.connect(self._voice_changed)
        self.listen = QPushButton("▶ Nghe thử")
        self.listen.setAutoDefault(False)
        self.listen.setToolTip("Đọc thử dòng đang bôi đen (hoặc dòng được chọn đầu tiên). Nhấp đúp một dòng cũng nghe thử.")
        self.listen.clicked.connect(lambda: self._preview(None))
        vrow.addWidget(self.voice, 1)
        vrow.addWidget(self.listen)
        form.addRow("Giọng đọc", vrow)

        self.rate = QSpinBox()
        self.rate.setRange(-50, 100)
        self.rate.setSingleStep(5)
        self.rate.setSuffix(" %")
        self.rate.setValue(int(self.settings.value("tts_rate", 0)))
        self.rate.setToolTip("Tốc độ đọc so với bình thường")
        form.addRow("Tốc độ đọc", self.rate)

        self.volume = QDoubleSpinBox()
        self.volume.setRange(DB_MIN, DB_MAX)
        self.volume.setDecimals(1)
        self.volume.setSingleStep(1.0)
        self.volume.setSuffix(" dB")
        self.volume.setSpecialValueText("-∞ dB")
        self.volume.setToolTip("0 dB = âm lượng gốc; -6 dB ≈ nhỏ một nửa; +6 dB ≈ to gấp đôi")
        db = self.settings.value("tts_volume_db")
        if db is None:   # older settings stored a percentage
            db = gain_to_db(int(self.settings.value("tts_volume", 100)) / 100.0)
        self.volume.setValue(float(db))
        form.addRow("Âm lượng", self.volume)

        self.fit = QCheckBox("Tự đọc nhanh hơn khi giọng đọc dài hơn câu phụ đề (tối đa 2x)")
        self.fit.setChecked(self.settings.value("tts_fit", "true") in (True, "true"))
        form.addRow("", self.fit)
        v.addLayout(form)

        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color:#8a91a0")
        v.addWidget(self.note)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_btn = bb.button(QDialogButtonBox.Ok)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._voice_changed()
        self._count()
        first = next((self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())
                      if self.tree.topLevelItem(i).checkState(0) == Qt.Checked), None)
        if first is not None:
            self.tree.scrollToItem(first)

    # ---------------------------------------------------------------- list helpers
    def _items(self):
        return (self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount()))

    def _filter(self):
        q = self.search.text().strip().lower()
        tr = self.track.currentData()
        for it in self._items():
            ok = (not q or q in it.text(2).lower()) and (tr == -1 or it.text(1) == str(tr + 1))
            it.setHidden(not ok)

    def _check_all(self, on: bool):
        self.tree.blockSignals(True)
        for it in self._items():
            if not it.isHidden():
                it.setCheckState(0, Qt.Checked if on else Qt.Unchecked)
        self.tree.blockSignals(False)
        self._count()

    def _from_playhead(self):
        by_id = {c.id: c for c in self.texts}
        self.tree.blockSignals(True)
        for it in self._items():
            c = by_id[it.data(0, Qt.UserRole)]
            if not it.isHidden():
                it.setCheckState(0, Qt.Checked if c.end > self.playhead else Qt.Unchecked)
        self.tree.blockSignals(False)
        self._count()

    def _check_highlighted(self):
        self.tree.blockSignals(True)
        for it in self.tree.selectedItems():
            it.setCheckState(0, Qt.Checked)
        self.tree.blockSignals(False)
        self._count()

    def checked_ids(self) -> list[int]:
        return [it.data(0, Qt.UserRole) for it in self._items() if it.checkState(0) == Qt.Checked]

    def _count(self):
        n = len(self.checked_ids())
        self.count.setText(f"Đã chọn {n}/{len(self.texts)} câu")
        self.ok_btn.setText(f"Tạo giọng nói ({n} câu)")
        self.ok_btn.setEnabled(n > 0)

    def _voice_changed(self):
        vid = self.voice.currentData() or ""
        pkg, ok = tts.engine_package(vid)
        if not ok:
            self.note.setText(f"⚠ Chưa cài gói {pkg} cho giọng này — bấm “Tạo giọng nói” hoặc “Nghe thử” "
                              "để cài tự động.")
        elif vid.startswith("piper:"):
            self.note.setText("Giọng ngoại tuyến chạy trên máy (mô hình của H2DevTool TTS): không cần Internet. "
                              "Lần đầu mỗi giọng mất vài giây để nạp mô hình.")
        else:
            self.note.setText("Giọng online (Microsoft Edge): cần kết nối Internet khi tạo. "
                              "Tệp giọng nói được lưu lại, không cần mạng khi xem/xuất video.")
        if not any(v.startswith("piper:") for v, _ in tts.all_voices()):
            self.note.setText(self.note.text() + "\nKhông thấy mô hình Ngọc Huyền / Thiện Tâm: cần cài H2DevTool TTS.")

    # ---------------------------------------------------------------- settings
    def voice_id(self) -> str:
        return self.voice.currentData()

    def options(self) -> dict:
        return {"voice": self.voice_id(), "rate": self.rate.value(),
                "volume": db_to_gain(self.volume.value()), "fit": self.fit.isChecked()}

    def _ensure_engine(self) -> bool:
        pkg, ok = tts.engine_package(self.voice_id())
        if ok:
            return True
        if QMessageBox.question(self, f"Cài {pkg}",
                                f"Giọng này cần gói Python “{pkg}”. Cài ngay bây giờ?") != QMessageBox.Yes:
            return False
        QApplication.setOverrideCursor(Qt.WaitCursor)
        r, err = None, ""
        try:
            exe = sys.executable
            if exe.lower().endswith("pythonw.exe"):
                exe = exe[:-5] + ".exe"
            r = subprocess.run([exe, "-m", "pip", "install", pkg], capture_output=True,
                               creationflags=0x08000000 if sys.platform == "win32" else 0, timeout=600)
        except Exception as e:  # noqa: BLE001
            err = str(e)
        finally:
            QApplication.restoreOverrideCursor()
        import importlib
        importlib.invalidate_caches()
        if r is None or r.returncode != 0 or not tts.engine_package(self.voice_id())[1]:
            msg = err if r is None else r.stderr.decode("utf-8", "replace")[-800:]
            QMessageBox.warning(self, f"Cài {pkg}", f"Không cài được {pkg}:\n" + msg)
            return False
        self._voice_changed()
        return True

    def _accept(self):
        if not self._ensure_engine():
            return
        self.settings.setValue("tts_voice", self.voice_id())
        self.settings.setValue("tts_rate", self.rate.value())
        self.settings.setValue("tts_volume_db", self.volume.value())
        self.settings.setValue("tts_fit", "true" if self.fit.isChecked() else "false")
        self._stop_preview()
        self.accept()

    # ---------------------------------------------------------------- preview
    def _preview(self, item):
        if item is None:
            sel = self.tree.selectedItems()
            item = sel[0] if sel else next((it for it in self._items() if it.checkState(0) == Qt.Checked), None)
        if item is None or not item.text(2):
            QMessageBox.information(self, "Nghe thử", "Hãy chọn một dòng phụ đề để nghe thử.")
            return
        if not self._ensure_engine():
            return
        self._stop_preview()
        self.listen.setEnabled(False)
        self.listen.setText("… đang tạo")
        r = tts.TTSRunner([tts.TTSJob(0, item.text(2))], self.voice_id(), self.rate.value())
        r.finished.connect(self._preview_ready)
        self._runner = r
        r.start()

    def _preview_ready(self, results, errors):
        self.listen.setEnabled(True)
        self.listen.setText("▶ Nghe thử")
        if not results:
            QMessageBox.warning(self, "Nghe thử", "Không tạo được giọng nói:\n" + "\n".join(errors[:5]))
            return
        if self._player is None:
            self._player = QMediaPlayer(self)
            self._out = QAudioOutput(self)
            self._player.setAudioOutput(self._out)
        self._out.setVolume(min(1.0, db_to_gain(self.volume.value())))
        self._player.setSource(QUrl.fromLocalFile(results[0].path))
        self._player.play()

    def _stop_preview(self):
        if self._player is not None:
            self._player.stop()

    def reject(self):
        self._stop_preview()
        super().reject()


def run_tts(parent, jobs: list[tts.TTSJob], voice: str, rate: int):
    """Run synthesis with a modal progress dialog. Returns (results, errors)."""
    dlg = QProgressDialog("Đang tạo giọng nói…", "Hủy", 0, max(1, 2 * len(jobs)), parent)
    dlg.setWindowTitle("Chuyển phụ đề thành giọng nói")
    dlg.setWindowModality(Qt.WindowModal)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    runner = tts.TTSRunner(jobs, voice, rate)
    bridge = _Bridge(dlg)          # lives in the GUI thread -> worker signals arrive queued
    runner.progress.connect(bridge.on_progress)
    runner.finished.connect(bridge.on_finished)
    dlg.canceled.connect(runner.cancel)
    runner.start()
    dlg.exec()
    if bridge.result is None:      # cancelled: wait for the worker to wind down
        runner.cancel()
        wait = QProgressDialog("Đang dừng…", None, 0, 0, parent)
        wait.setWindowModality(Qt.WindowModal)
        wait.setMinimumDuration(0)
        bridge.dlg = wait
        if bridge.result is None:
            wait.exec()
        return [], ["Đã hủy."]
    return bridge.result


class _Bridge(QObject):
    def __init__(self, dlg: QProgressDialog):
        super().__init__(dlg)
        self.dlg = dlg
        self.result = None

    @Slot(int, int)
    def on_progress(self, done, total):
        if isinstance(self.dlg, QProgressDialog) and self.dlg.maximum() > 0:
            self.dlg.setMaximum(max(1, total))
            self.dlg.setValue(min(done, total))
            self.dlg.setLabelText(f"Đang tạo giọng nói… {min(done, total)}/{total}")

    @Slot(list, list)
    def on_finished(self, results, errors):
        self.result = (results, errors)
        self.dlg.done(0)
