"""Main window: wires media bin, preview, inspector and timeline together."""
from __future__ import annotations

import json
import os
import time

from PySide6.QtCore import Qt, QTimer, QSettings
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QFileDialog, QMainWindow, QMessageBox, QSplitter,
                               QVBoxLayout, QWidget, QProgressDialog, QInputDialog)

from . import ffmpeg_tools as ff
from .cache import ThumbnailCache, WaveformCache
from .export_dialog import ExportDialog
from .inspector import Inspector
from .media_bin import MediaBin
from .model import BlurClip, Clip, Project, TextClip
from .preview import PreviewPanel
from .subtitles import parse_srt, place_on_source, read_text_file, write_srt
from .timeline import TimelinePanel, TimelineWidget
from . import tts

PROJECT_FILTER = "Dự án CopCut (*.copcut)"
MEDIA_FILTER = ("Media (" + " ".join("*" + e for e in sorted(ff.MEDIA_EXT)) + ");;Tất cả (*.*)")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("CopCut", "CopCut")
        self.project = Project()
        self.thumbs = ThumbnailCache()
        self.waves = WaveformCache()
        self.project_path: str | None = None
        self.dirty = False
        self._undo: list[dict] = []
        self._redo: list[dict] = []
        self._ck_key = None
        self._ck_time = 0.0

        self.bin = MediaBin(self.thumbs)
        self.preview = PreviewPanel(self.project)
        self.engine = self.preview.engine
        self.inspector = Inspector()
        self.timeline = TimelineWidget(self.project, self.thumbs, self.waves)
        self.tpanel = TimelinePanel(self.timeline)

        top = QSplitter(Qt.Horizontal)
        top.addWidget(self.bin)
        top.addWidget(self.preview)
        top.addWidget(self.inspector)
        top.setStretchFactor(0, 0)
        top.setStretchFactor(1, 1)
        top.setStretchFactor(2, 0)
        top.setSizes([300, 900, 330])
        vs = QSplitter(Qt.Vertical)
        vs.addWidget(top)
        vs.addWidget(self.tpanel)
        vs.setStretchFactor(0, 1)
        vs.setStretchFactor(1, 0)
        vs.setSizes([580, 360])
        self.setCentralWidget(vs)

        # signals
        self.bin.import_requested.connect(self.import_dialog)
        self.bin.add_requested.connect(lambda paths: self.add_to_timeline(paths))
        self.bin.files_dropped.connect(self.import_files)
        self.timeline.seek_requested.connect(self.seek)
        self.timeline.selection_changed.connect(self._selection_changed)
        self.timeline.edit_begin.connect(lambda: self.checkpoint())
        self.timeline.edited.connect(self._timeline_edited)
        self.timeline.media_dropped.connect(self._media_dropped)
        self.timeline.context_action.connect(self._action)
        self.tpanel.action.connect(self._action)
        self.engine.time_changed.connect(self._time_changed)
        self.engine.text_moved.connect(self._text_moved)
        self.engine.blur_changed.connect(self._blur_changed)
        self.engine.overlay_clicked.connect(lambda kind, clip: self.timeline.select(kind, clip.id))
        self.inspector.before_edit.connect(self.checkpoint)
        self.inspector.changed.connect(self._inspector_changed)
        self.inspector.style_all.connect(lambda: self._action("style_all"))

        self._build_menus()
        self.setAcceptDrops(True)
        self.resize(1500, 950)
        self._update_title()
        QTimer.singleShot(0, self._startup)

    def _startup(self):
        if not ff.FFMPEG or not ff.FFPROBE:
            QMessageBox.warning(self, "Thiếu FFmpeg",
                                "Không tìm thấy ffmpeg/ffprobe.\n\nHãy chạy:  python setup_ffmpeg.py\n"
                                "hoặc chép ffmpeg.exe và ffprobe.exe vào thư mục ffmpeg\\bin của CopCut.")
        # warm up GPU encoder detection in the background
        import threading
        threading.Thread(target=ff.detect_encoders, daemon=True).start()

    # ------------------------------------------------------------------ menus
    def _build_menus(self):
        mb = self.menuBar()

        def act(menu, text, key, fn, shortcut_ctx=Qt.WindowShortcut):
            a = QAction(text, self)
            if key:
                a.setShortcuts([QKeySequence(k) for k in key] if isinstance(key, list) else [QKeySequence(key)])
                a.setShortcutContext(shortcut_ctx)
            a.triggered.connect(fn)
            menu.addAction(a)
            return a

        m = mb.addMenu("&Tệp")
        act(m, "Dự án mới", "Ctrl+N", self.new_project)
        act(m, "Mở dự án…", "Ctrl+O", self.open_project)
        act(m, "Lưu", "Ctrl+S", self.save_project)
        act(m, "Lưu thành…", "Ctrl+Shift+S", lambda: self.save_project(as_new=True))
        m.addSeparator()
        act(m, "Nhập media…", "Ctrl+I", self.import_dialog)
        act(m, "Nhập phụ đề SRT…", None, self.import_srt)
        act(m, "Xuất phụ đề SRT…", None, self.export_srt)
        m.addSeparator()
        act(m, "Xuất video…", "Ctrl+E", self.export_video)
        m.addSeparator()
        act(m, "Thoát", "Ctrl+Q", self.close)

        e = mb.addMenu("&Chỉnh sửa")
        act(e, "Hoàn tác", "Ctrl+Z", self.undo)
        act(e, "Làm lại", ["Ctrl+Y", "Ctrl+Shift+Z"], self.redo)
        e.addSeparator()
        act(e, "Tách tại đầu phát", "Ctrl+B", lambda: self._action("split"))
        act(e, "Xóa phần bên trái đầu phát", "Q", lambda: self._action("trim_left"))
        act(e, "Xóa phần bên phải đầu phát", "W", lambda: self._action("trim_right"))
        act(e, "Xóa clip đang chọn", ["Delete", "Backspace"], lambda: self._action("delete"))
        e.addSeparator()
        act(e, "Thêm chữ tại đầu phát", "Ctrl+T", lambda: self._action("add_text"))
        act(e, "Thêm vùng che mờ tại đầu phát", "Ctrl+M", lambda: self._action("add_blur"))
        act(e, "Xóa toàn bộ phụ đề…", "Ctrl+Shift+Delete", lambda: self._action("delete_all_text"))
        act(e, "Chuyển phụ đề thành giọng nói…", "Ctrl+Shift+T", lambda: self._action("tts"))

        p = mb.addMenu("&Phát")
        act(p, "Phát / Tạm dừng", "Space", self.engine.toggle)
        act(p, "Lùi 1 khung hình", "Left", lambda: self.step(-1 / self.project.fps))
        act(p, "Tới 1 khung hình", "Right", lambda: self.step(1 / self.project.fps))
        act(p, "Lùi 1 giây", "Shift+Left", lambda: self.step(-1))
        act(p, "Tới 1 giây", "Shift+Right", lambda: self.step(1))
        act(p, "Lùi 10 giây", "Ctrl+Left", lambda: self.step(-10))
        act(p, "Tới 10 giây", "Ctrl+Right", lambda: self.step(10))
        act(p, "Tới điểm cắt trước", "Up", lambda: self.jump_edit(-1))
        act(p, "Tới điểm cắt sau", "Down", lambda: self.jump_edit(1))
        act(p, "Về đầu", "Home", lambda: self.seek(0))
        act(p, "Tới cuối", "End", lambda: self.seek(self.project.duration()))

        v = mb.addMenu("&Xem")
        act(v, "Phóng to timeline", ["=", "+", "Ctrl+="], lambda: self.timeline.set_zoom(self.timeline.pps * 1.5))
        act(v, "Thu nhỏ timeline", ["-", "Ctrl+-"], lambda: self.timeline.set_zoom(self.timeline.pps / 1.5))
        act(v, "Xem toàn bộ timeline", "Shift+Z", self.timeline.zoom_fit)

        h = mb.addMenu("Trợ &giúp")
        act(h, "Phím tắt", "F1", self.show_help)

    # ------------------------------------------------------------------ undo
    def checkpoint(self, key: str | None = None):
        now = time.monotonic()
        if key is not None and key == self._ck_key and now - self._ck_time < 1.5:
            self._ck_time = now
            return
        self._ck_key, self._ck_time = key, now
        self._undo.append(self.project.to_dict())
        del self._undo[:-150]
        self._redo.clear()
        self._set_dirty()

    def _restore(self, d):
        self.engine.pause()
        res_before = (self.project.width, self.project.height, self.project.fps)
        self.project.load_dict(d)
        self.timeline.selection = None
        self.timeline.select(None)
        self.inspector.show_clip(None, None)
        if res_before != (self.project.width, self.project.height, self.project.fps):
            self.preview.set_canvas()
        self._after_edit(refresh_inspector=False)
        self._ck_key = None

    def undo(self):
        if self._undo:
            self._redo.append(self.project.to_dict())
            self._restore(self._undo.pop())

    def redo(self):
        if self._redo:
            self._undo.append(self.project.to_dict())
            self._restore(self._redo.pop())

    # ------------------------------------------------------------------ state sync
    def _set_dirty(self):
        if not self.dirty:
            self.dirty = True
            self._update_title()

    def _update_title(self):
        name = os.path.basename(self.project_path) if self.project_path else "Dự án chưa lưu"
        self.setWindowTitle(f"CopCut — {name}{' *' if self.dirty else ''}")

    def _after_edit(self, refresh_inspector=True):
        self.project.touch()
        self.timeline.relayout()
        self.timeline.update()
        self.tpanel.sync()
        self.preview.update_duration()
        self.engine.refresh()
        if refresh_inspector:
            self.inspector.refresh()
        self._set_dirty()

    def _timeline_edited(self):
        self.project.touch()
        self.preview.update_duration()
        self.engine.refresh()
        self.inspector.refresh()
        self._set_dirty()

    def _inspector_changed(self):
        c = self.inspector.clip
        if isinstance(c, TextClip):
            self.project.text_style = c.style.copy()
        self.project.touch()
        self.timeline.update()
        self.preview.update_duration()
        self.engine.update_overlays()
        if not isinstance(c, TextClip):
            self.engine.refresh()
        self._set_dirty()

    def _selection_changed(self):
        sel = self.timeline.selection
        self.inspector.show_clip(sel[0] if sel else None, self.timeline.selected(),
                                 self.timeline.selected_group())
        self.engine.set_selection(sel)

    def _time_changed(self, t):
        self.timeline.set_playhead(t)
        if self.engine.playing:
            self.timeline.ensure_visible(t)

    def _text_moved(self, clip, x, y):
        self.checkpoint()
        clip.style.x, clip.style.y = x, y
        self.project.text_style.x, self.project.text_style.y = x, y
        self.timeline.select("text", clip.id)
        self.inspector.show_clip("text", clip)
        self._set_dirty()

    def _blur_changed(self, clip, x, y, w, h):
        self.checkpoint()
        clip.x, clip.y, clip.w, clip.h = x, y, w, h
        self.project.touch()
        self.engine.update_overlays()
        if self.inspector.clip is clip:
            self.inspector.refresh()
        self._set_dirty()

    def seek(self, t: float):
        self.engine.seek(t)
        self.timeline.set_playhead(self.engine.t)

    def step(self, dt: float):
        if self.engine.playing:
            self.engine.pause()
        self.seek(self.project.snap_frame(self.engine.t + dt))

    def jump_edit(self, direction: int):
        t = self.engine.t
        pts = sorted({0.0, self.project.main_end(), *self.project.starts(),
                      *(c.start for c in self.project.texts_in(t - 3600, t + 3600)),
                      *(c.end for c in self.project.texts_in(t - 3600, t + 3600))})
        eps = 0.5 / self.project.fps
        cand = [p for p in pts if (p > t + eps if direction > 0 else p < t - eps)]
        if cand:
            self.seek(cand[0] if direction > 0 else cand[-1])
            self.timeline.ensure_visible(self.engine.t)

    # ------------------------------------------------------------------ media
    def import_dialog(self):
        start = self.settings.value("last_dir", "")
        paths, _ = QFileDialog.getOpenFileNames(self, "Nhập media", start, MEDIA_FILTER)
        if paths:
            self.settings.setValue("last_dir", os.path.dirname(paths[0]))
            self.import_files(paths)

    def import_files(self, paths: list[str]) -> list[str]:
        ok, errors = [], []
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for p in paths:
                p = os.path.normpath(p)
                if os.path.isdir(p):
                    continue
                if p.lower().endswith(".srt"):
                    self.import_srt(p)
                    continue
                if p in self.project.media:
                    ok.append(p)
                    continue
                try:
                    info = ff.probe(p)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{os.path.basename(p)}: {e}")
                    continue
                self.project.media[p] = info
                self.bin.add(info)
                ok.append(p)
                self.waves.get(p)   # start building waveform early
        finally:
            QApplication.restoreOverrideCursor()
        if errors:
            QMessageBox.warning(self, "Không nhập được", "\n".join(errors[:15]))
        return ok

    def _media_dropped(self, paths, lane, t, index, track=None):
        paths = self.import_files(paths)
        if paths:
            self.add_to_timeline(paths, lane=lane, t=t, index=index, track=track)

    def add_to_timeline(self, paths: list[str], lane: str | None = None, t: float | None = None,
                        index: int | None = None, track: int | None = None):
        infos = [self.project.media[p] for p in paths if p in self.project.media]
        if not infos:
            return
        self.checkpoint()
        p = self.project
        canvas_changed = False
        idx = len(p.main) if index is None else index
        at = self.engine.t if t is None else t
        last = None
        for info in infos:
            if info.has_video and lane != "audio":
                if not p.main:
                    p.adopt_settings_from(info)
                    canvas_changed = True
                c = Clip(info.path, 0.0, info.duration)
                p.main.insert(idx, c)
                idx += 1
                last = ("main", c)
            elif info.has_audio:
                c = Clip(info.path, 0.0, info.duration, start=at)
                if track is not None and lane == "audio":
                    # dropped on a specific track: use it, or the next free one below it
                    # (a brand-new track) if it would cover another clip
                    tr = min(track, p.track_count("audio") if p.audio else 0)
                    c.track = p.free_track("audio", [(at, at + c.duration)], min_track=tr)
                else:
                    c.track = p.free_track("audio", [(at, at + c.duration)])
                p.audio.append(c)
                at += c.duration
                last = ("audio", c)
        p.touch()
        if canvas_changed:
            self.preview.set_canvas()
        self._after_edit()
        if last:
            self.timeline.select(last[0], last[1].id)
        if canvas_changed or self.timeline.pps * p.duration() < 200:
            self.timeline.zoom_fit()

    # ------------------------------------------------------------------ edit actions
    def _action(self, key: str):
        p = self.project
        t = self.engine.t
        fmin = 1.0 / p.fps
        sel = self.timeline.selection
        selc = self.timeline.selected()
        if key == "undo":
            return self.undo()
        if key == "redo":
            return self.redo()
        if key == "add_text":
            self.checkpoint()
            c = TextClip("Nhập chữ", t, 3.0, p.text_style.copy())
            c.track = p.free_track("text", [(t, t + 3.0)])
            p.texts.append(c)
            p.touch()
            self._after_edit()
            self.timeline.select("text", c.id)
            self.inspector.focus_text()
            return
        if key == "edit_text":
            self.inspector.focus_text()
            return
        if key == "tts":
            return self.text_to_speech()
        if key == "add_blur":
            self.checkpoint()
            i = p.main_index_at(t)
            end = p.starts()[i] + p.main[i].duration if i >= 0 else t + 5.0
            dur = max(1.0, min(5.0, end - t)) if end - t > 0.2 else 5.0
            c = BlurClip(t, dur)
            p.blurs.append(c)
            p.touch()
            self._after_edit()
            self.timeline.select("blur", c.id)
            self.statusBar().showMessage("Đã thêm vùng che mờ — kéo khung trên màn hình xem trước để đặt vị trí", 6000)
            return
        if key in ("style_all", "style_all_tracks"):
            if isinstance(selc, TextClip):
                every = key == "style_all_tracks"
                targets = [c for c in p.texts if every or c.track == selc.track]
                if every and p.track_count("text") > 1 and QMessageBox.question(
                        self, "Áp dụng cho mọi rãnh chữ",
                        f"Áp dụng kiểu này cho cả {len(targets)} dòng chữ trên mọi rãnh?\n"
                        "Các rãnh như tên kênh / thông báo cũng sẽ bị đổi kiểu.") != QMessageBox.Yes:
                    return
                self.checkpoint()
                for c in targets:
                    c.style = selc.style.copy()
                p.text_style = selc.style.copy()
                self.engine.update_overlays(force=True)
                self._after_edit()
                where = "mọi rãnh chữ" if every else f"rãnh chữ {selc.track + 1}"
                self.statusBar().showMessage(f"Đã áp dụng kiểu cho {len(targets)} dòng trên {where}", 5000)
            return
        if key == "delete_all_text":
            n = len(p.texts)
            if not n:
                self.statusBar().showMessage("Không có phụ đề nào để xóa", 3000)
                return
            if QMessageBox.question(self, "Xóa toàn bộ phụ đề",
                                    f"Xóa toàn bộ {n} dòng phụ đề/chữ trên timeline?\n"
                                    "Có thể hoàn tác bằng Ctrl+Z.") != QMessageBox.Yes:
                return
            self.checkpoint()
            p.texts.clear()
            if sel and sel[0] == "text":
                self.timeline.select(None)
            self.engine.update_overlays()
            self._after_edit()
            self.statusBar().showMessage(f"Đã xóa {n} dòng phụ đề", 5000)
            return
        if key == "new_track":
            if sel and sel[0] in ("text", "audio") and selc is not None:
                self.checkpoint()
                selc.track = p.track_count(sel[0])
                p.compact_tracks(sel[0])
                self._after_edit()
            return
        if key == "delete":
            if not sel or selc is None:
                return
            group = self.timeline.selected_group()
            if len(group) > 1:
                self.checkpoint()
                ids = {c.id for c in group}
                lst = p.lane_list(sel[0])
                lst[:] = [c for c in lst if c.id not in ids]
                if sel[0] in ("text", "audio"):
                    p.compact_tracks(sel[0])
                self.timeline.select(None)
                self._after_edit()
                what = {"text": "dòng phụ đề", "audio": "đoạn âm thanh", "main": "đoạn video"}.get(sel[0], "đoạn")
                self.statusBar().showMessage(f"Đã xóa {len(group)} {what}", 5000)
                return
            self.checkpoint()
            p.lane_list(sel[0]).remove(selc)
            if sel[0] in ("text", "audio"):
                p.compact_tracks(sel[0])
            self.timeline.select(None)
            self._after_edit()
            return

        # split / trim operate on the selected clip if it is under the playhead,
        # otherwise on the main-track clip under the playhead
        target = None
        if sel and selc is not None:
            s = p.starts()[p.find("main", selc.id)[0]] if sel[0] == "main" else selc.start
            if s + fmin <= t <= s + selc.duration - fmin:
                target = (sel[0], selc, s)
        if target is None:
            i = p.main_index_at(t)
            if i >= 0:
                target = ("main", p.main[i], p.starts()[i])
        if target is None:
            return
        lane, c, s = target
        rel = t - s
        if rel < fmin or c.duration - rel < fmin:
            return
        self.checkpoint()
        if key == "split":
            if lane == "text":
                n = TextClip(c.text, t, c.end - t, c.style.copy(), c.track)
                c.duration = rel
                p.texts.append(n)
            elif lane == "blur":
                n = c.copy_at(t, c.end - t)
                c.duration = rel
                p.blurs.append(n)
            else:
                mid = c.src_at(rel)
                n = Clip(c.path, mid, c.src_out, c.speed, c.volume, start=t, track=c.track)
                c.src_out = mid
                lst = p.main if lane == "main" else p.audio
                lst.insert(lst.index(c) + 1, n)
            self._after_edit()
            self.timeline.select(lane, n.id)
        elif key == "trim_left":
            if lane in ("text", "blur"):
                c.start, c.duration = t, c.end - t
            elif lane == "audio":
                c.src_in = c.src_at(rel)
                c.start = t
            else:
                c.src_in = c.src_at(rel)
                p.touch()
                self.seek(s)
            self._after_edit()
        elif key == "trim_right":
            if lane in ("text", "blur"):
                c.duration = rel
            else:
                c.src_out = c.src_at(rel)
            self._after_edit()

    # ------------------------------------------------------------------ text to speech
    def text_to_speech(self):
        from .tts_dialog import TTSDialog, run_tts
        p = self.project
        if not p.texts:
            QMessageBox.information(self, "Chuyển thành giọng nói",
                                    "Chưa có phụ đề nào. Hãy nhập SRT hoặc thêm chữ trước.")
            return
        self.engine.pause()
        pre = {c.id for c in self.timeline.selected_texts()}
        if not pre:
            pre = {c.id for c in p.texts_at(self.engine.t)}
        dlg = TTSDialog(p.texts, pre, self.engine.t, self)
        if dlg.exec() != TTSDialog.Accepted:
            return
        opt = dlg.options()
        by_id = {c.id: c for c in p.texts}
        jobs = []
        for cid in dlg.checked_ids():
            c = by_id.get(cid)
            text = tts.clean_text(c.text) if c else ""
            if text:
                jobs.append(tts.TTSJob(cid, text))
        if not jobs:
            return
        results, errors = run_tts(self, jobs, opt["voice"], opt["rate"])
        if not results:
            if errors and errors != ["Đã hủy."]:
                QMessageBox.warning(self, "Chuyển thành giọng nói",
                                    "Không tạo được giọng nói:\n" + "\n".join(errors[:8]))
            return
        self.checkpoint()
        made = []
        # running it again (e.g. to retry failed lines) must not stack duplicates
        have = {(a.path, round(a.start, 3)) for a in p.audio}
        for r in results:
            c = by_id.get(r.key)
            if c is None or (r.path, round(c.start, 3)) in have:
                continue
            p.media[r.path] = r.info
            a = Clip(r.path, 0.0, r.info.duration, volume=opt["volume"], start=c.start)
            if opt["fit"] and r.info.duration > c.duration + 0.05 and c.duration > 0.2:
                a.speed = round(min(2.0, r.info.duration / c.duration), 3)
            made.append(a)
        # the whole batch goes on one audio track that is free over all its spans (a new
        # track under the existing ones if needed); lines that still overlap each other
        # (speech longer than the subtitle) spill onto the next free track
        base = p.free_track("audio", [(a.start, a.start + a.duration) for a in made])
        for a in sorted(made, key=lambda x: x.start):
            a.track = p.free_track("audio", [(a.start, a.start + a.duration)], min_track=base)
            p.audio.append(a)
        if not made:
            self._undo.pop()
            self.statusBar().showMessage("Các câu đã chọn đều đã có giọng nói trên timeline", 6000)
        else:
            p.touch()
            self._after_edit()
            self.timeline.select("audio", made[0].id)
            tracks = sorted({a.track + 1 for a in made})
            msg = f"Đã tạo {len(made)} đoạn giọng nói trên rãnh âm thanh {', '.join(map(str, tracks))}"
            self.statusBar().showMessage(msg, 8000)
        if errors:
            QMessageBox.warning(self, "Chuyển thành giọng nói",
                                f"Có {len(jobs) - len(results)} câu chưa tạo được:\n" + "\n".join(errors[:8])
                                + "\n\nChạy lại chức năng này với cùng các câu để tạo tiếp — "
                                "các câu đã có giọng sẽ không bị tạo trùng.")

    # ------------------------------------------------------------------ subtitles
    def import_srt(self, path: str | None = None):
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Nhập phụ đề", self.settings.value("last_dir", ""),
                                                  "Phụ đề SRT (*.srt)")
            if not path:
                return
        try:
            clips = parse_srt(read_text_file(path), self.project.text_style)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Nhập phụ đề", str(e))
            return
        if not clips:
            QMessageBox.information(self, "Nhập phụ đề", "Không tìm thấy dòng phụ đề nào.")
            return
        target = self._srt_target(path)
        if target is False:
            return
        n_src = len(clips)
        if target:
            clips = place_on_source(clips, self.project.main, self.project.starts(), target)
            if not clips:
                QMessageBox.information(self, "Nhập phụ đề",
                                        "Không có dòng phụ đề nào nằm trong phần video đang dùng trên timeline.")
                return
        self.checkpoint()
        # put the file on the lowest text track where it overlaps nothing, so a second
        # language (or a second SRT for the same time range) lands on its own track
        tr = self.project.free_track("text", [(c.start, c.end) for c in clips])
        for c in clips:
            c.track = tr
        self.project.texts.extend(clips)
        self._after_edit()
        msg = f"Đã nhập {len(clips)} dòng phụ đề" + (f" vào rãnh chữ {tr + 1}" if tr else "")
        if target:
            msg += f" cho {os.path.basename(target)}"
        self.statusBar().showMessage(msg, 6000)
        if target and len(clips) < n_src:
            self.statusBar().showMessage(msg + f" (bỏ qua các dòng thuộc đoạn video đã cắt; SRT có {n_src} dòng)", 8000)

    def _srt_target(self, srt_path: str):
        """Which source video the SRT is timed against. '' = timeline from 00:00, False = cancelled."""
        p = self.project
        videos = list(dict.fromkeys(c.path for c in p.main))
        if not videos:
            return ""
        stem = os.path.splitext(os.path.basename(srt_path))[0].lower()
        default = next((v for v in videos if os.path.splitext(os.path.basename(v))[0].lower() == stem), None)
        if default is None:
            selc = self.timeline.selected()
            if self.timeline.selection and self.timeline.selection[0] == "main" and selc is not None:
                default = selc.path
        if default is None:
            i = p.main_index_at(self.engine.t)
            default = p.main[i].path if i >= 0 else videos[0]
        if len(videos) == 1:
            return videos[0]
        starts = p.starts()
        labels = []
        for k, v in enumerate(videos):
            first = next(i for i, c in enumerate(p.main) if c.path == v)
            labels.append(f"Video {k + 1}: {os.path.basename(v)}   (bắt đầu {ff.fmt_time(starts[first])})")
        labels.append("Không gắn với video — đặt theo timeline từ 00:00:00")
        item, ok = QInputDialog.getItem(
            self, "Nhập phụ đề",
            f"Phụ đề “{os.path.basename(srt_path)}” thuộc video nào?\n"
            "Phụ đề sẽ được đặt đúng vị trí của video đó trên timeline.",
            labels, videos.index(default), False)
        if not ok:
            return False
        idx = labels.index(item)
        return videos[idx] if idx < len(videos) else ""

    def export_srt(self):
        if not self.project.texts:
            QMessageBox.information(self, "Xuất phụ đề", "Chưa có phụ đề nào.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Xuất phụ đề", "", "Phụ đề SRT (*.srt)")
        if path:
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write(write_srt(self.project.texts))

    # ------------------------------------------------------------------ project files
    def _confirm_discard(self) -> bool:
        if not self.dirty or (not self.project.main and not self.project.texts and not self.project.audio):
            return True
        r = QMessageBox.question(self, "CopCut", "Dự án có thay đổi chưa lưu. Lưu lại?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.save_project()
        return r == QMessageBox.Discard

    def _load_project(self, d: dict, path: str | None):
        self.engine.release_media()
        self.project.load_dict(d)
        self.project_path = path
        self._undo.clear()
        self._redo.clear()
        self.bin.clear()
        missing = []
        for m in self.project.media.values():
            if os.path.exists(m.path):
                if not tts.is_tts_file(m.path):      # generated speech stays out of the bin
                    self.bin.add(m)
            else:
                missing.append(m.path)
        self.timeline.selection = None
        self.timeline.select(None)
        self.inspector.show_clip(None, None)
        self.preview.set_canvas()
        self._after_edit(refresh_inspector=False)
        self.dirty = False
        self._update_title()
        self.seek(0)
        self.timeline.zoom_fit()
        if missing:
            QMessageBox.warning(self, "Thiếu tệp", "Không tìm thấy các tệp sau:\n" + "\n".join(missing[:20]))

    def new_project(self):
        if self._confirm_discard():
            self._load_project(Project().to_dict(), None)

    def open_project(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Mở dự án", self.settings.value("last_dir", ""),
                                              PROJECT_FILTER)
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Mở dự án", str(e))
            return
        self._load_project(d, path)

    def save_project(self, as_new: bool = False) -> bool:
        path = self.project_path
        if as_new or not path:
            path, _ = QFileDialog.getSaveFileName(self, "Lưu dự án", self.settings.value("last_dir", ""),
                                                  PROJECT_FILTER)
            if not path:
                return False
            if not path.lower().endswith(".copcut"):
                path += ".copcut"
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.project.to_dict(), f, ensure_ascii=False)
        os.replace(tmp, path)
        self.project_path = path
        self.dirty = False
        self._update_title()
        self.statusBar().showMessage("Đã lưu", 3000)
        return True

    def export_video(self):
        if not self.project.main:
            QMessageBox.information(self, "Xuất video", "Hãy thêm ít nhất một video vào rãnh chính.")
            return
        self.engine.pause()
        base = (os.path.splitext(self.project_path)[0] if self.project_path
                else os.path.splitext(self.project.main[0].path)[0] + "_copcut")
        dlg = ExportDialog(self.project, base + ".mp4", self)
        dlg.exec()

    def show_help(self):
        QMessageBox.information(self, "Phím tắt", (
            "Space  — Phát / tạm dừng\n"
            "← / →  — Lùi / tới 1 khung hình   (Shift: 1 giây, Ctrl: 10 giây)\n"
            "↑ / ↓  — Nhảy tới điểm cắt trước / sau\n"
            "Ctrl+B — Tách clip tại đầu phát\n"
            "Q / W  — Xóa phần bên trái / bên phải đầu phát\n"
            "Delete — Xóa clip đang chọn (rãnh chính tự dồn lại)\n"
            "Ctrl+T — Thêm chữ tại đầu phát\n"
            "Ctrl+Shift+Delete — Xóa toàn bộ phụ đề\n"
            "Ctrl+Shift+T — Chuyển phụ đề thành giọng nói\n"
            "Ctrl+nhấp / Shift+nhấp — Chọn nhiều đoạn (phụ đề, âm thanh, video) để xóa,\n"
            "    chỉnh âm lượng hoặc tốc độ cùng lúc; chuột phải → Chọn mọi đoạn trên rãnh\n"
            "Ctrl+M — Thêm vùng che mờ (kéo khung/4 góc trên màn hình xem trước)\n"
            "Kéo đầu phát gần điểm nối video sẽ tự hút vào (giữ Alt để tắt hút)\n"
            "Ctrl+Z / Ctrl+Y — Hoàn tác / làm lại\n"
            "Ctrl+lăn chuột — Phóng to/thu nhỏ timeline;  lăn chuột — cuộn ngang\n"
            "+ / − / Shift+Z — Phóng to / thu nhỏ / xem toàn bộ\n"
            "Kéo mép clip để cắt bớt; kéo thân clip để đổi vị trí.\n"
            "Kéo chữ trên khung xem trước để đổi vị trí.\n"
            "Nhiều rãnh: kéo clip chữ lên vùng trống trên cùng (clip âm thanh xuống vùng trống dưới cùng)\n"
            "để tạo rãnh mới; thả đè lên clip khác thì clip tự sang rãnh mới;\n"
            "Shift+lăn chuột — cuộn lên/xuống giữa các rãnh."))

    # ------------------------------------------------------------------ window drops / close
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        self.import_files(paths)

    def closeEvent(self, e):
        if not self._confirm_discard():
            e.ignore()
            return
        self.engine.release_media()
        e.accept()
