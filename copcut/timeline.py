"""Custom-painted, virtualised timeline. Only what is on screen is drawn, so
an 8-hour project costs the same to paint as an 8-second one."""
from __future__ import annotations

import bisect
import math
import os

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal, QMimeData, QRect
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF,
                           QLinearGradient, QAction)
from PySide6.QtWidgets import (QHBoxLayout, QMenu, QScrollArea, QScrollBar, QSlider, QToolButton,
                               QVBoxLayout, QWidget, QLabel, QSizePolicy)

from .cache import PEAK_RATE, ThumbnailCache, WaveformCache
from . import ffmpeg_tools as ff
from .ffmpeg_tools import fmt_time
from .model import Clip, ImageClip, Project, TextClip, fmt_db

MEDIA_MIME = "application/x-copcut-media"

HEADER_W = 78
RULER_H = 26
TEXT_H = 30
BLUR_H = 24
MAIN_H = 74
THUMB_H = 52
AUDIO_H = 46
TRACK_GAP = 4
NEW_ZONE_H = 22       # empty strips above the top text track / under the last audio track:
                      # drop a clip there to create a new track
EDGE = 7
SNAP_PX = 12          # clip drag snapping distance
SCRUB_JOIN_PX = 18    # playhead snaps to video joins from this far
SCRUB_EDGE_PX = 9     # playhead snaps to text/music edges
MAX_PPS = 600.0

C_BG = QColor("#16181d")
C_HEAD = QColor("#1f2229")
C_LANE = QColor("#1c1f25")
C_RULER = QColor("#1f2229")
C_TICK = QColor("#5b6170")
C_TEXT = QColor("#c9ced8")
C_MAIN = QColor("#2b3a57")
C_AUDIO = QColor("#1f5a47")
C_AUDIO_W = QColor("#4fd3a0")
C_SUB = QColor("#6a4bc4")
C_BLUR = QColor("#3f6f8f")
C_IMAGE = QColor("#8a6a2f")
C_SEL = QColor("#ffcc33")
C_ACCENT = QColor("#4f8cff")
C_PLAYHEAD = QColor("#ffffff")
C_WAVE = QColor(150, 190, 255, 170)

TICK_STEPS = [1 / 30, 1 / 10, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200]


class TimelineWidget(QWidget):
    seek_requested = Signal(float)
    selection_changed = Signal()
    edit_begin = Signal()
    edited = Signal()
    view_changed = Signal()
    media_dropped = Signal(list, str, float, int, int)   # paths, lane, time, main insert index, track
    context_action = Signal(str)

    def __init__(self, project: Project, thumbs: ThumbnailCache, waves: WaveformCache, parent=None):
        super().__init__(parent)
        self.p = project
        self.thumbs = thumbs
        self.waves = waves
        self.playhead = 0.0
        self.pps = 40.0                  # pixels per second
        self.view_start = 0.0            # time at left edge of track area
        self.selection: tuple[str, int] | None = None
        self.snap = True
        self._drag: dict | None = None
        self._drop_hint: tuple[str, float, int] | None = None
        self._small = QFont(self.font())
        self._small.setPointSizeF(8.0)
        self._lanes: list[tuple[str, int, float, float]] = []
        self._geom: dict[tuple[str, int], tuple[float, float]] = {}
        self._lay_key = None
        self._hold = (1, 1)            # keep the track count stable while dragging
        self.multi: set[int] = set()   # selected clip ids, all of kind multi_kind (Ctrl/Shift+click)
        self.multi_kind: str | None = None
        self._anchor: int | None = None
        self._top_zone = (0.0, 0.0)
        self._bottom_zone = (0.0, 0.0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.relayout()
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.ClickFocus)
        thumbs.ready.connect(self.update)
        waves.ready.connect(lambda _p: self.update())

    # ------------------------------------------------------------ lanes
    def relayout(self):
        """Lay out lanes top-down: text tracks (highest on top), blur, main, audio tracks."""
        nt = max(self.p.track_count("text"), self._hold[0])
        na = max(self.p.track_count("audio"), self._hold[1])
        if (nt, na) == self._lay_key:
            return
        self._lay_key = (nt, na)
        lanes = []
        y = RULER_H + 4
        self._top_zone = (y, NEW_ZONE_H)
        y += NEW_ZONE_H + 4
        for k in range(nt - 1, -1, -1):
            lanes.append(("text", k, y, TEXT_H))
            y += TEXT_H + TRACK_GAP
        y += 2
        lanes.append(("blur", 0, y, BLUR_H))
        y += BLUR_H + 8
        lanes.append(("main", 0, y, MAIN_H))
        y += MAIN_H + 8
        for k in range(na):
            lanes.append(("audio", k, y, AUDIO_H))
            y += AUDIO_H + TRACK_GAP
        self._lanes = lanes
        self._geom = {(k, t): (ly, lh) for k, t, ly, lh in lanes}
        self._bottom_zone = (y, NEW_ZONE_H)
        h = int(y + NEW_ZONE_H + 4)
        self.setMinimumHeight(h)
        self.setFixedHeight(h)

    def lane_geom(self, kind: str, track: int = 0) -> tuple[float, float]:
        g = self._geom.get((kind, track))
        if g is None:
            self.relayout()
            g = self._geom.get((kind, track)) or self._geom[(kind, 0)]
        return g

    def lane_track_at(self, y: float) -> tuple[str, int] | None:
        for k, t, ly, lh in self._lanes:
            if ly <= y < ly + lh:
                return k, t
        return None

    def _track_from_y(self, kind: str, y: float, clip) -> int:
        """Track for a text/audio clip dragged to height y. Above the top text track (or
        below the last audio track) means a new track."""
        mine = [(t, ly, lh) for k, t, ly, lh in self._lanes if k == kind]
        others = max((c.track for c in self.p.lane_list(kind) if c is not clip), default=-1) + 1
        if not mine:
            return clip.track
        if kind == "text" and y < min(ly for _, ly, _ in mine) - TRACK_GAP:
            return others
        if kind == "audio" and y >= max(ly + lh for _, ly, lh in mine) + TRACK_GAP:
            return others
        best, bd = clip.track, 1e9
        for t, ly, lh in mine:
            if ly - TRACK_GAP <= y < ly + lh + TRACK_GAP:
                return min(t, others)
            d = min(abs(y - ly), abs(y - ly - lh))
            if d < bd:
                best, bd = t, d
        return min(best, others)

    # ------------------------------------------------------------ geometry
    def track_w(self) -> float:
        return max(10.0, self.width() - HEADER_W)

    def x_of(self, t: float) -> float:
        return HEADER_W + (t - self.view_start) * self.pps

    def t_of(self, x: float) -> float:
        return self.view_start + (x - HEADER_W) / self.pps

    def visible_range(self) -> tuple[float, float]:
        return self.view_start, self.view_start + self.track_w() / self.pps

    def content_len(self) -> float:
        return max(self.p.duration() + 30.0, self.track_w() / self.pps)

    def min_pps(self) -> float:
        return max(0.002, self.track_w() / max(60.0, self.p.duration() * 1.05) * 0.5)

    def set_zoom(self, pps: float, anchor_t: float | None = None):
        pps = min(MAX_PPS, max(self.min_pps(), pps))
        if anchor_t is None:
            anchor_t = self.playhead
        ax = self.x_of(anchor_t)
        self.pps = pps
        self.view_start = max(0.0, anchor_t - (ax - HEADER_W) / pps)
        self._clamp_view()
        self.view_changed.emit()
        self.update()

    def zoom_fit(self):
        d = max(10.0, self.p.duration())
        self.pps = max(0.002, min(MAX_PPS, self.track_w() / (d * 1.03)))
        self.view_start = 0.0
        self.view_changed.emit()
        self.update()

    def set_view_start(self, t: float):
        self.view_start = max(0.0, t)
        self._clamp_view()
        self.view_changed.emit()
        self.update()

    def _clamp_view(self):
        mx = max(0.0, self.content_len() - self.track_w() / self.pps)
        self.view_start = min(max(0.0, self.view_start), mx)

    def ensure_visible(self, t: float):
        a, b = self.visible_range()
        if t < a or t > b - 20 / self.pps:
            self.set_view_start(t - 40 / self.pps)

    def set_playhead(self, t: float):
        if t != self.playhead:
            self.playhead = t
            self.update()

    # ------------------------------------------------------------ selection helpers
    def selected(self):
        if not self.selection:
            return None
        kind, cid = self.selection
        _, c = self.p.find(kind, cid)
        return c

    MULTI_KINDS = ("main", "audio", "text")

    def select(self, kind: str | None, cid: int | None = None, keep_multi: bool = False):
        sel = (kind, cid) if kind else None
        if not keep_multi:
            multi = {cid} if kind in self.MULTI_KINDS else set()
            if multi != self.multi:
                self.multi = multi
                self.update()
            self.multi_kind = kind if multi else None
            self._anchor = cid if multi else None
        if sel != self.selection:
            self.selection = sel
            self.selection_changed.emit()
            self.update()

    def selected_group(self, kind: str | None = None) -> list:
        """All selected clips of `kind` (default: the selection's kind), in timeline order.
        Ctrl/Shift+click adds more clips of the same kind to the group."""
        if not self.selection:
            return []
        kind = kind or self.selection[0]
        if kind != self.selection[0]:
            return []
        ids = set(self.multi) if self.multi_kind == kind else set()
        ids.add(self.selection[1])
        lst = self.p.lane_list(kind)
        if kind == "main":
            return [c for c in lst if c.id in ids]
        return sorted((c for c in lst if c.id in ids),
                      key=lambda c: (c.start, getattr(c, "track", 0)))

    def selected_texts(self) -> list[TextClip]:
        return self.selected_group("text")

    def in_group(self, lane: str, cid: int) -> bool:
        return self.multi_kind == lane and cid in self.multi and len(self.multi) > 1

    def _multi_click(self, lane: str, c, mods) -> bool:
        """Ctrl+click toggles a clip in the selection, Shift+click selects the range from
        the last clicked one (same track; for the main track: by order). Clips of a
        different kind start a new selection. Returns True if handled."""
        if lane not in self.MULTI_KINDS or not (mods & (Qt.ControlModifier | Qt.ShiftModifier)):
            return False
        if self.multi_kind != lane:
            same = self.selection and self.selection[0] == lane
            self.multi = {self.selection[1]} if same else set()
            self.multi_kind = lane
            if not same:
                self._anchor = None
        if mods & Qt.ControlModifier:
            if self.selection and self.selection[0] == lane:
                self.multi.add(self.selection[1])
            if c.id in self.multi and len(self.multi) > 1:
                self.multi.discard(c.id)
                self.select(lane, next(iter(self.multi)), keep_multi=True)
            else:
                self.multi.add(c.id)
                self.select(lane, c.id, keep_multi=True)
            self._anchor = c.id
            self.selection_changed.emit()
            self.update()
            return True
        if self._anchor is not None:
            ia, a = self.p.find(lane, self._anchor)
            if a is not None:
                if lane == "main":
                    ic = self.p.find(lane, c.id)[0]
                    i0, i1 = sorted((ia, ic))
                    self.multi = {x.id for x in self.p.main[i0:i1 + 1]}
                else:
                    t0, t1 = sorted((a.start, c.start))
                    tr = getattr(c, "track", 0)
                    self.multi = {x.id for x in self.p.lane_list(lane)
                                  if getattr(x, "track", 0) == tr
                                  and t0 - 1e-6 <= x.start <= t1 + 1e-6}
                    self.multi.add(a.id)
                self.select(lane, c.id, keep_multi=True)
                self.selection_changed.emit()
                self.update()
                return True
        self.multi = {c.id}
        self._anchor = c.id
        self.select(lane, c.id, keep_multi=True)
        self.selection_changed.emit()
        self.update()
        return True

    # ------------------------------------------------------------ hit testing
    def lane_at(self, y: float) -> str | None:
        h = self.lane_track_at(y)
        return h[0] if h else None

    def hit(self, pos: QPointF):
        """-> (lane, index, clip, part) or None. part: body | left | right."""
        lt = self.lane_track_at(pos.y())
        if lt is None or pos.x() < HEADER_W:
            return None
        lane, track = lt
        t = self.t_of(pos.x())
        tol = EDGE / self.pps
        if lane == "main":
            st = self.p.starts()
            best = None
            for i in self.p.main_range(t - tol, t + tol):
                c = self.p.main[i]
                x0, x1 = self.x_of(st[i]), self.x_of(st[i] + c.duration)
                if x0 - 1 <= pos.x() <= x1 + 1:
                    part = "body"
                    if x1 - x0 > 3 * EDGE:
                        if pos.x() - x0 <= EDGE:
                            part = "left"
                        elif x1 - pos.x() <= EDGE:
                            part = "right"
                    best = (lane, i, c, part)
                    if part != "body" or x0 <= pos.x() < x1:
                        break
            return best
        finder = {"text": self.p.texts_in, "blur": self.p.blurs_in, "audio": self.p.audio_in}[lane]
        items = finder(t - tol, t + tol)
        if lane in ("text", "audio"):
            items = [c for c in items if c.track == track]
        for c in reversed(items):
            s = c.start
            e = c.start + c.duration
            x0, x1 = self.x_of(s), self.x_of(e)
            if x0 - 1 <= pos.x() <= x1 + 1:
                part = "body"
                if x1 - x0 > 3 * EDGE:
                    if pos.x() - x0 <= EDGE:
                        part = "left"
                    elif x1 - pos.x() <= EDGE:
                        part = "right"
                lst = self.p.lane_list(lane)
                return (lane, lst.index(c), c, part)
        return None

    # ------------------------------------------------------------ snapping
    def _snap_points(self, exclude_id: int | None) -> list[float]:
        a, b = self.visible_range()
        pts = [0.0, self.playhead]
        st = self.p.starts()
        for i in self.p.main_range(a, b):
            pts.append(st[i])
            pts.append(st[i] + self.p.main[i].duration)
        for c in self.p.texts_in(a, b):
            if c.id != exclude_id:
                pts += [c.start, c.end]
        for c in self.p.audio_in(a, b):
            if c.id != exclude_id:
                pts += [c.start, c.start + c.duration]
        for c in self.p.blurs_in(a, b):
            if c.id != exclude_id:
                pts += [c.start, c.end]
        return pts

    def _snap(self, t: float, pts: list[float]) -> float:
        if not self.snap:
            return t
        thr = SNAP_PX / self.pps
        best, bd = t, thr
        for p in pts:
            d = abs(p - t)
            if d < bd:
                best, bd = p, d
        return best

    def _join_points(self, a: float, b: float) -> list[float]:
        st = self.p.starts()
        rng = self.p.main_range(a, b)
        pts = [st[i] for i in rng]
        if rng and rng[-1] == len(st) - 1:
            pts.append(self.p.main_end())
        return pts

    def join_at(self, t: float) -> bool:
        """True if t sits exactly on a cut/join of the main track."""
        st = self.p.starts()
        if not st:
            return False
        tol = 1e-6
        i = bisect.bisect_left(st, t - tol)
        if i < len(st) and abs(st[i] - t) <= tol:
            return True
        return abs(self.p.main_end() - t) <= tol

    def _scrub(self, x: float, mods):
        t = max(0.0, self.t_of(max(HEADER_W, x)))
        if self.snap and not (mods & Qt.AltModifier):
            win = SCRUB_JOIN_PX / self.pps
            best, bd = None, win
            for p in self._join_points(t - win, t + win):
                if abs(p - t) < bd:
                    best, bd = p, abs(p - t)
            if best is None:
                win = SCRUB_EDGE_PX / self.pps
                pts = []
                for c in self.p.texts_in(t - win, t + win):
                    pts += [c.start, c.end]
                for c in self.p.audio_in(t - win, t + win):
                    pts += [c.start, c.start + c.duration]
                for p in pts:
                    if abs(p - t) < bd and abs(p - t) < win:
                        best, bd = p, abs(p - t)
            if best is not None:
                t = best
        self.seek_requested.emit(t)

    # ------------------------------------------------------------ mouse
    def mousePressEvent(self, e):
        pos = e.position()
        if e.button() == Qt.RightButton:
            h = self.hit(pos)
            if h:
                self.select(h[0], h[2].id, keep_multi=self.in_group(h[0], h[2].id))
                self._context_menu(e.globalPosition().toPoint(), h[0])
            return
        if e.button() != Qt.LeftButton:
            return
        if pos.y() < RULER_H or pos.x() < HEADER_W:
            self._drag = {"mode": "scrub"}
            self._scrub(pos.x(), e.modifiers())
            return
        h = self.hit(pos)
        if not h:
            self.select(None)
            self._drag = {"mode": "scrub"}
            self._scrub(pos.x(), e.modifiers())
            return
        lane, idx, c, part = h
        if self._multi_click(lane, c, e.modifiers()):
            return
        if self.in_group(lane, c.id):
            self.select(lane, c.id, keep_multi=True)     # keep the group (drag, right-click, TTS)
        else:
            self.select(lane, c.id)
        d = {"lane": lane, "clip": c, "part": part, "x0": pos.x(), "started": False,
             "start": getattr(c, "start", 0.0), "src_in": getattr(c, "src_in", 0.0),
             "src_out": getattr(c, "src_out", 0.0), "dur": c.duration}
        if lane == "main":
            d["mode"] = {"body": "main_move", "left": "main_trim_l", "right": "main_trim_r"}[part]
            d["start"] = self.p.starts()[idx]
        else:
            d["mode"] = {"body": "free_move", "left": "free_trim_l", "right": "free_trim_r"}[part]
        d["snaps"] = self._snap_points(c.id)
        d["y0"] = pos.y()
        self._drag = d

    def mouseMoveEvent(self, e):
        pos = e.position()
        d = self._drag
        if d is None:
            h = self.hit(pos)
            if h and h[3] in ("left", "right"):
                self.setCursor(Qt.SizeHorCursor)
            elif pos.y() < RULER_H:
                self.setCursor(Qt.PointingHandCursor)
            else:
                self.unsetCursor()
            return
        if d["mode"] == "scrub":
            self._scrub(pos.x(), e.modifiers())
            self._autoscroll(pos.x())
            return
        if not d["started"]:
            if abs(pos.x() - d["x0"]) < 3 and abs(pos.y() - d["y0"]) < 6:
                return
            d["started"] = True
            self._hold = self._lay_key or (1, 1)
            self.edit_begin.emit()
        dt = (pos.x() - d["x0"]) / self.pps
        c = d["clip"]
        mode = d["mode"]
        fmin = 1.0 / self.p.fps
        if mode == "main_move":
            d["cursor_t"] = self.t_of(pos.x())
            self._drop_hint = ("main", d["cursor_t"], self._insert_index(d["cursor_t"], exclude=c), 0)
            self.update()
            return
        if mode == "main_trim_r":
            end = self._snap(d["start"] + d["dur"] + dt, d["snaps"])
            new_out = c.src_in + (end - d["start"]) * c.speed
            limit = self.p.media[c.path].duration
            c.src_out = min(limit, max(c.src_in + fmin * c.speed, new_out))
        elif mode == "main_trim_l":
            new_in = d["src_in"] + dt * c.speed
            c.src_in = max(0.0, min(c.src_out - fmin * c.speed, new_in))
        elif mode == "free_move":
            ns = max(0.0, d["start"] + dt)
            s1 = self._snap(ns, d["snaps"])
            s2 = self._snap(ns + d["dur"], d["snaps"]) - d["dur"]
            ns = s1 if abs(s1 - ns) <= abs(s2 - ns) or s2 == ns else s2
            c.start = max(0.0, ns)
            if d["lane"] in ("text", "audio"):
                c.track = self._track_from_y(d["lane"], pos.y(), c)
        elif mode == "free_trim_l":
            end = d["start"] + d["dur"]
            ns = self._snap(max(0.0, d["start"] + dt), d["snaps"])
            if not isinstance(c, Clip):
                ns = min(ns, end - fmin)
                c.start, c.duration = ns, end - ns
            else:
                ns = min(ns, end - fmin)
                new_in = d["src_in"] + (ns - d["start"]) * c.speed
                if new_in < 0:
                    ns -= new_in / c.speed
                    new_in = 0.0
                c.start, c.src_in = ns, new_in
        elif mode == "free_trim_r":
            end = self._snap(d["start"] + d["dur"] + dt, d["snaps"])
            end = max(d["start"] + fmin, end)
            if not isinstance(c, Clip):
                c.duration = end - c.start
            else:
                limit = self.p.media[c.path].duration
                c.src_out = min(limit, c.src_in + (end - c.start) * c.speed)
        self.p.touch()
        self.relayout()
        self.edited.emit()
        self._autoscroll(pos.x())
        self.update()

    def mouseReleaseEvent(self, e):
        d = self._drag
        self._drag = None
        if d and d.get("started") and d.get("lane") in ("text", "audio"):
            c = d["clip"]
            if d["mode"] == "free_move" and self.p.overlaps(d["lane"], c.track, c.start,
                                                            c.start + c.duration, exclude=(c,)):
                # dropped onto another clip: move to the next free track (a new one if needed)
                c.track = self.p.free_track(d["lane"], [(c.start, c.start + c.duration)],
                                            exclude=(c,), min_track=c.track)
            self.p.compact_tracks(d["lane"])
            self.p.touch()
            self._hold = (1, 1)
            self.relayout()
            self.edited.emit()
        if d and d.get("mode") == "main_move" and d.get("started"):
            c = d["clip"]
            t = d.get("cursor_t", d["start"])
            idx = self._insert_index(t, exclude=c)
            self.p.main.remove(c)
            self.p.main.insert(idx, c)
            self.p.touch()
            self.edited.emit()
        self._drop_hint = None
        self.update()

    def mouseDoubleClickEvent(self, e):
        h = self.hit(e.position())
        if h and h[0] == "text":
            self.context_action.emit("edit_text")

    def _autoscroll(self, x: float):
        if x > self.width() - 20:
            self.set_view_start(self.view_start + 30 / self.pps)
        elif x < HEADER_W + 10 and self.view_start > 0:
            self.set_view_start(self.view_start - 30 / self.pps)

    def _insert_index(self, t: float, exclude: Clip | None = None) -> int:
        st = self.p.starts()
        idx = 0
        for i, c in enumerate(self.p.main):
            if c is exclude:
                continue
            if t > st[i] + c.duration / 2:
                idx += 1
            else:
                break
        return idx

    def wheelEvent(self, e):
        delta = e.angleDelta().y() or e.angleDelta().x()
        if e.modifiers() & Qt.ShiftModifier:
            vs = getattr(self, "vscroll", None)      # Shift+wheel: move between tracks
            if vs is not None:
                vs.setValue(vs.value() - int(delta / 120 * 40))
            e.accept()
            return
        if e.modifiers() & Qt.ControlModifier:
            factor = 1.25 ** (delta / 120)
            self.set_zoom(self.pps * factor, self.t_of(e.position().x()))
        else:
            self.set_view_start(self.view_start - delta / 120 * 90 / self.pps)
        e.accept()

    def resizeEvent(self, e):
        self._clamp_view()
        self.view_changed.emit()

    def _context_menu(self, gpos, lane):
        m = QMenu(self)
        for key, label in (("split", "Tách tại đầu phát  (Ctrl+B)"), ("delete", "Xóa  (Delete)")):
            m.addAction(label).setData(key)
        if lane in ("text", "audio"):
            m.addAction("Đưa sang rãnh mới").setData("new_track")
        if lane in self.MULTI_KINDS:
            n = len(self.selected_group())
            if n > 1:
                m.actions()[1].setText(f"Xóa {n} đoạn đã chọn  (Delete)")
            m.addAction("Chọn mọi đoạn trên rãnh này").setData("select_track")
        if lane == "text":
            m.addSeparator()
            n = len(self.selected_texts())
            m.addAction(f"🔊 Chuyển {n} câu thành giọng nói…" if n > 1
                        else "🔊 Chuyển thành giọng nói…").setData("tts")
            m.addAction("Sửa chữ").setData("edit_text")
            m.addAction("Áp dụng kiểu này cho cả rãnh này").setData("style_all")
            m.addAction("Áp dụng kiểu này cho mọi rãnh chữ…").setData("style_all_tracks")
            m.addSeparator()
            m.addAction("Xóa toàn bộ phụ đề…").setData("delete_all_text")
        act = m.exec(gpos)
        if act and act.data() == "select_track":
            self.select_track()
        elif act:
            self.context_action.emit(act.data())

    def select_track(self):
        """Select every clip on the track of the selected clip (for bulk delete/volume/speed)."""
        if not self.selection or self.selection[0] not in self.MULTI_KINDS:
            return
        kind, cid = self.selection
        _, c = self.p.find(kind, cid)
        if c is None:
            return
        tr = getattr(c, "track", 0)
        self.multi = {x.id for x in self.p.lane_list(kind) if getattr(x, "track", 0) == tr}
        self.multi_kind = kind
        self._anchor = cid
        self.selection_changed.emit()
        self.update()

    # ------------------------------------------------------------ drag & drop of media
    def _drop_paths(self, md: QMimeData) -> list[str]:
        if md.hasFormat(MEDIA_MIME):
            return bytes(md.data(MEDIA_MIME)).decode("utf-8").split("\n")
        if md.hasUrls():
            return [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
        return []

    def dragEnterEvent(self, e):
        if self._drop_paths(e.mimeData()):
            e.acceptProposedAction()

    def _drop_target(self, y: float, paths=(), t: float = 0.0) -> tuple[str, int]:
        if paths and all(ff.is_image(q) for q in paths):
            return "blur", 0          # images (logos) always go on the overlay lane
        lt = self.lane_track_at(y)
        if lt and lt[0] == "audio":
            lane, track = lt
        else:
            audio = [(ly, lh) for k, _t, ly, lh in self._lanes if k == "audio"]
            if not (audio and y >= max(ly + lh for ly, lh in audio)):
                return "main", 0
            lane, track = "audio", (self.p.track_count("audio") if self.p.audio else 0)
        # the drop would cover a clip on that track -> it goes to the next free track
        dur = sum(self.p.media[q].duration for q in paths if q in self.p.media)
        if dur > 0 and self.p.overlaps("audio", track, t, t + dur):
            track = self.p.free_track("audio", [(t, t + dur)], min_track=track)
        return lane, track

    def dragMoveEvent(self, e):
        pos = e.position()
        t = max(0.0, self.t_of(pos.x()))
        lane, track = self._drop_target(pos.y(), self._drop_paths(e.mimeData()), t)
        self._drop_hint = (lane, t, self._insert_index(t), track)
        self.update()
        e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._drop_hint = None
        self.update()

    def dropEvent(self, e):
        paths = self._drop_paths(e.mimeData())
        pos = e.position()
        t = max(0.0, self.t_of(pos.x()))
        lane, track = self._drop_target(pos.y(), paths, t)
        self._drop_hint = None
        self.update()
        if paths:
            e.acceptProposedAction()
            self.media_dropped.emit(paths, lane, t, self._insert_index(t), track)

    # ------------------------------------------------------------ painting
    def paintEvent(self, ev):
        self.relayout()
        qp = QPainter(self)
        qp.fillRect(self.rect(), C_BG)
        w = self.width()
        a, b = self.visible_range()
        for _k, _t, ly, lh in self._lanes:
            qp.fillRect(QRectF(HEADER_W, ly, w - HEADER_W, lh), C_LANE)
        qp.setClipRect(QRectF(HEADER_W, 0, w - HEADER_W, self.height()))
        self._paint_ruler(qp, a, b)
        self._paint_main(qp, a, b)
        self._paint_texts(qp, a, b)
        self._paint_texts(qp, a, b, lane="blur")
        self._paint_audio(qp, a, b)
        self._paint_zones(qp)
        self._paint_drop_hint(qp)
        qp.setClipping(False)
        self._paint_headers(qp)
        # playhead
        x = self.x_of(self.playhead)
        if HEADER_W <= x <= w:
            on_join = self.join_at(self.playhead)
            col = C_SEL if on_join else C_PLAYHEAD
            if on_join:
                ly, lh = self.lane_geom("main")
                qp.fillRect(QRectF(x - 3, ly - 4, 6, lh + 8), QColor(255, 204, 51, 110))
                qp.setFont(self._small)
                lab = "✂ Điểm nối"
                fw = QFontMetrics(self._small).horizontalAdvance(lab) + 10
                lx = x + 9 if x + 9 + fw < w else x - 9 - fw
                qp.setPen(Qt.NoPen)
                qp.setBrush(C_SEL)
                qp.drawRoundedRect(QRectF(lx, 4, fw, 16), 4, 4)
                qp.setPen(QColor("#1a1a1a"))
                qp.drawText(QRectF(lx, 4, fw, 16), Qt.AlignCenter, lab)
            qp.setPen(QPen(col, 2.5 if on_join else 1.5))
            qp.drawLine(QPointF(x, 4), QPointF(x, self.height()))
            qp.setBrush(col)
            qp.drawPolygon(QPolygonF([QPointF(x - 6, 2), QPointF(x + 6, 2), QPointF(x + 6, 10),
                                      QPointF(x, 17), QPointF(x - 6, 10)]))
        qp.end()

    def _paint_headers(self, qp: QPainter):
        qp.fillRect(QRectF(0, 0, HEADER_W, self.height()), C_HEAD)
        qp.setPen(QColor("#8a91a0"))
        qp.setFont(self._small)
        names = {"text": "Chữ", "blur": "Mờ / Logo", "main": "Video chính", "audio": "Âm thanh"}
        nt, na = self._lay_key or (1, 1)
        for lane, track, ly, lh in self._lanes:
            label = names[lane]
            if (lane == "text" and nt > 1) or (lane == "audio" and na > 1):
                label += f" {track + 1}"
            qp.drawText(QRectF(8, ly, HEADER_W - 12, lh), Qt.AlignVCenter | Qt.AlignLeft, label)
        qp.setPen(QColor("#2a2e37"))
        qp.drawLine(HEADER_W - 1, 0, HEADER_W - 1, self.height())

    def _paint_zones(self, qp: QPainter):
        """Empty strips above the text tracks and below the audio tracks. While a clip is
        dragged they light up to show that dropping there creates a new track."""
        d = self._drag
        dragging = d.get("lane") if d and d.get("started") and d.get("lane") in ("text", "audio") else None
        dropping = bool(self._drop_hint and self._drop_hint[0] == "audio")
        w = self.width()
        for kind, (zy, zh) in (("text", self._top_zone), ("audio", self._bottom_zone)):
            active = dragging == kind or (kind == "audio" and dropping)
            if not active:
                continue
            r = QRectF(HEADER_W + 2, zy + 1, w - HEADER_W - 4, zh - 2)
            qp.setPen(QPen(C_ACCENT, 1, Qt.DashLine))
            qp.setBrush(QColor(79, 140, 255, 28))
            qp.drawRoundedRect(r, 3, 3)
            qp.setPen(QColor("#9fbfff"))
            label = ("＋ Thả vào đây để tạo rãnh chữ mới" if kind == "text"
                     else "＋ Thả vào đây để tạo rãnh âm thanh mới")
            qp.drawText(r.adjusted(8, 0, 0, 0), Qt.AlignLeft | Qt.AlignVCenter, label)

    def _paint_ruler(self, qp: QPainter, a: float, b: float):
        qp.fillRect(QRectF(HEADER_W, 0, self.width() - HEADER_W, RULER_H), C_RULER)
        step = next((s for s in TICK_STEPS if s * self.pps >= 90), 7200 * max(1, math.ceil(90 / (7200 * self.pps))))
        minor = step / 5
        qp.setFont(self._small)
        k0 = int(math.floor(a / minor))
        k1 = int(math.ceil(b / minor)) + 1
        if k1 - k0 > 4000:
            return
        for k in range(k0, k1):
            t = k * minor
            if t < 0:
                continue
            x = self.x_of(t)
            major = abs(t / step - round(t / step)) < 1e-6
            qp.setPen(C_TICK)
            qp.drawLine(QPointF(x, RULER_H - (10 if major else 4)), QPointF(x, RULER_H))
            if major:
                qp.setPen(QColor("#9aa1b0"))
                lab = fmt_time(t, self.p.fps if step < 1 else None)
                if t < 3600:
                    lab = lab[3:]
                qp.drawText(QPointF(x + 3, 11), lab)

    def _clip_rect(self, s: float, dur: float, lane: str, track: int = 0) -> QRectF:
        ly, lh = self.lane_geom(lane, track)
        x0 = self.x_of(s)
        return QRectF(x0, ly, max(1.0, dur * self.pps), lh)

    def _paint_main(self, qp: QPainter, a: float, b: float):
        st = self.p.starts()
        sel_id = self.selection[1] if self.selection and self.selection[0] == "main" else None
        last_px = -1e9
        for i in self.p.main_range(a, b):
            c = self.p.main[i]
            r = self._clip_rect(st[i], c.duration, "main")
            if r.width() < 1.5 and int(r.x()) == int(last_px):
                continue
            last_px = r.x()
            qp.fillRect(r, C_MAIN)
            if r.width() >= 12:
                self._paint_thumbs(qp, c, r)
                self._paint_wave(qp, c, QRectF(r.x(), r.y() + THUMB_H, r.width(), r.height() - THUMB_H),
                                 C_WAVE, bottom=True)
                self._paint_label(qp, c, r)
            if c.id == sel_id or self.in_group("main", c.id):
                qp.setPen(QPen(C_SEL, 2))
                qp.setBrush(Qt.NoBrush)
                qp.drawRoundedRect(r.adjusted(1, 1, -1, -1), 3, 3)
            else:
                qp.setPen(QPen(QColor(0, 0, 0, 160), 1))
                qp.drawLine(r.topLeft(), r.bottomLeft())

    def _paint_label(self, qp: QPainter, c: Clip, r: QRectF):
        if r.width() < 40:
            return
        name = c.path.replace("\\", "/").rsplit("/", 1)[-1]
        parts = []
        if abs(c.speed - 1) > 1e-3:
            parts.append(f"{c.speed:g}x")
        if abs(c.volume - 1) > 1e-3:
            parts.append("🔇" if c.volume == 0 else fmt_db(c.volume))
        parts.append(fmt_time(c.duration))
        text = "  ".join(parts) + "   " + name
        qp.setFont(self._small)
        fm = QFontMetrics(self._small)
        vis_x = max(r.x(), HEADER_W)
        avail = r.right() - vis_x - 8
        if avail < 20:
            return
        text = fm.elidedText(text, Qt.ElideRight, int(avail))
        tw = fm.horizontalAdvance(text)
        qp.fillRect(QRectF(vis_x + 2, r.y() + 2, tw + 8, 15), QColor(0, 0, 0, 150))
        qp.setPen(QColor("#e8ebf2"))
        qp.drawText(QPointF(vis_x + 6, r.y() + 13), text)

    def _paint_thumbs(self, qp: QPainter, c: Clip, r: QRectF):
        info = self.p.media.get(c.path)
        if not info or not info.has_video:
            return
        aspect = (info.width / info.height) if info.height else 16 / 9
        tw = THUMB_H * aspect
        src_per_px = c.speed / self.pps
        sec_per_thumb = tw * src_per_px
        step = 2.0 ** math.floor(math.log2(max(sec_per_thumb, 1e-6)))
        step = max(step, 0.5)
        vis_x0 = max(r.x(), HEADER_W)
        vis_x1 = min(r.right(), self.width())
        if vis_x1 <= vis_x0:
            return
        s0 = c.src_in + (vis_x0 - r.x()) * src_per_px
        s1 = c.src_in + (vis_x1 - r.x()) * src_per_px
        k0 = int(math.floor(s0 / step))
        k1 = int(math.ceil(s1 / step))
        if k1 - k0 > 400:
            return
        qp.save()
        qp.setClipRect(QRectF(vis_x0, r.y(), vis_x1 - vis_x0, THUMB_H), Qt.IntersectClip)
        maxt = max(0.0, info.duration - 0.5)
        for k in range(k0, k1 + 1):
            ts = k * step
            x = r.x() + (ts - c.src_in) / src_per_px
            region_w = step / src_per_px
            if x + region_w < vis_x0 or x > vis_x1:
                continue
            img = self.thumbs.get(c.path, min(maxt, max(0.0, ts)))
            if img is None:
                qp.fillRect(QRectF(x, r.y(), region_w, THUMB_H), QColor("#26324a"))
                continue
            iw = img.width() * THUMB_H / max(1, img.height())
            if region_w <= iw:
                sx = (iw - region_w) / 2 * img.height() / THUMB_H
                sw = region_w * img.height() / THUMB_H
                qp.drawImage(QRectF(x, r.y(), region_w, THUMB_H), img,
                             QRectF(sx, 0, sw, img.height()))
            else:
                xx = x
                while xx < x + region_w:
                    qp.drawImage(QRectF(xx, r.y(), iw, THUMB_H), img)
                    xx += iw
        qp.restore()

    def _paint_wave(self, qp: QPainter, c: Clip, r: QRectF, color: QColor, bottom: bool):
        info = self.p.media.get(c.path)
        if not info or not info.has_audio or r.width() < 3:
            return
        peaks = self.waves.get(c.path)
        if peaks is None or len(peaks) < 2:
            return
        vis_x0 = int(max(r.x(), HEADER_W))
        vis_x1 = int(min(r.right(), self.width()))
        n = vis_x1 - vis_x0
        if n < 1:
            return
        cols = np.arange(n + 1, dtype=np.float64) + vis_x0
        src = c.src_in + (cols - r.x()) / self.pps * c.speed
        idx = np.clip((src * PEAK_RATE).astype(np.int64), 0, len(peaks) - 1)
        vals = np.maximum.reduceat(peaks, idx[:-1]).astype(np.float32) / 255.0
        gain = min(2.0, c.volume)
        h = r.height() - 2
        hs = np.minimum(1.0, np.sqrt(vals) * gain) * h
        qp.setPen(QPen(color, 1))
        base = r.bottom() - 1
        lines = []
        mid = r.y() + r.height() / 2
        for i, hv in enumerate(hs.tolist()):
            if hv < 0.5:
                continue
            x = vis_x0 + i + 0.5
            if bottom:
                lines.append(QPointF(x, base))
                lines.append(QPointF(x, base - hv))
            else:
                lines.append(QPointF(x, mid - hv / 2))
                lines.append(QPointF(x, mid + hv / 2))
        if lines:
            qp.drawLines(lines)

    def _paint_texts(self, qp: QPainter, a: float, b: float, lane: str = "text"):
        sel_id = self.selection[1] if self.selection and self.selection[0] == lane else None
        qp.setFont(self._small)
        fm = QFontMetrics(self._small)
        last_px: dict[int, float] = {}
        items = self.p.texts_in(a, b) if lane == "text" else self.p.blurs_in(a, b)
        for c in items:
            tr = getattr(c, "track", 0)
            r = self._clip_rect(c.start, c.duration, lane, tr)
            if (r.width() < 1.5 and int(r.x()) == int(last_px.get(tr, -1e9)) and c.id != sel_id
                    and not self.in_group(lane, c.id)):
                continue
            last_px[tr] = r.x()
            qp.setPen(Qt.NoPen)
            img = isinstance(c, ImageClip)
            qp.setBrush(C_SUB if lane == "text" else (C_IMAGE if img else C_BLUR))
            qp.drawRoundedRect(r.adjusted(0, 1, -1, -1), 3, 3)
            if r.width() > 24:
                vis_x = max(r.x(), HEADER_W)
                if lane == "text":
                    label = c.text.replace("\n", " ")
                elif img:
                    label = "🖼 " + os.path.basename(c.path)
                else:
                    label = f"▦ Che mờ {c.strength}"
                txt = fm.elidedText(label, Qt.ElideRight, int(r.right() - vis_x - 8))
                qp.setPen(QColor("#f3efff"))
                qp.drawText(QRectF(vis_x + 5, r.y(), r.right() - vis_x - 6, r.height()),
                            Qt.AlignVCenter | Qt.AlignLeft, txt)
            if c.id == sel_id or self.in_group(lane, c.id):
                qp.setPen(QPen(C_SEL, 2))
                qp.setBrush(Qt.NoBrush)
                qp.drawRoundedRect(r.adjusted(1, 1, -1, -1), 3, 3)

    def _paint_audio(self, qp: QPainter, a: float, b: float):
        sel_id = self.selection[1] if self.selection and self.selection[0] == "audio" else None
        for c in self.p.audio_in(a, b):
            r = self._clip_rect(c.start, c.duration, "audio", c.track)
            qp.setPen(Qt.NoPen)
            qp.setBrush(C_AUDIO)
            qp.drawRoundedRect(r.adjusted(0, 1, -1, -1), 3, 3)
            self._paint_wave(qp, c, r.adjusted(0, 16, 0, -2), C_AUDIO_W, bottom=False)
            self._paint_label(qp, c, r)
            if c.id == sel_id or self.in_group("audio", c.id):
                qp.setPen(QPen(C_SEL, 2))
                qp.setBrush(Qt.NoBrush)
                qp.drawRoundedRect(r.adjusted(1, 1, -1, -1), 3, 3)

    def _paint_drop_hint(self, qp: QPainter):
        if not self._drop_hint:
            return
        lane, t, idx, track = self._drop_hint
        if lane == "main":
            st = self.p.starts()
            x = self.x_of(st[idx] if idx < len(st) else self.p.main_end())
            ly, lh = self.lane_geom("main")
        elif (lane, track) in self._geom:
            x = self.x_of(t)
            ly, lh = self.lane_geom(lane, track)
        else:
            x = self.x_of(t)
            ly, lh = self._bottom_zone
            qp.fillRect(QRectF(x, ly, self.width() - x, lh), QColor(79, 140, 255, 45))
        qp.setPen(QPen(C_ACCENT, 3))
        qp.drawLine(QPointF(x, ly - 3), QPointF(x, ly + lh + 3))


class TimelinePanel(QWidget):
    """Toolbar + timeline + horizontal scrollbar."""
    action = Signal(str)

    def __init__(self, timeline: TimelineWidget, parent=None):
        super().__init__(parent)
        self.tl = timeline
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 4, 8, 4)
        bar.setSpacing(4)

        def btn(text, tip, key):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.clicked.connect(lambda: self.action.emit(key))
            bar.addWidget(b)
            return b

        btn("↶", "Hoàn tác (Ctrl+Z)", "undo")
        btn("↷", "Làm lại (Ctrl+Y)", "redo")
        bar.addSpacing(8)
        btn("✂ Tách", "Tách clip tại đầu phát (Ctrl+B)", "split")
        btn("⇤ Q", "Xóa phần bên trái đầu phát (Q)", "trim_left")
        btn("W ⇥", "Xóa phần bên phải đầu phát (W)", "trim_right")
        btn("🗑 Xóa", "Xóa clip đang chọn (Delete)", "delete")
        bar.addSpacing(8)
        btn("T+ Thêm chữ", "Thêm chữ/phụ đề tại đầu phát", "add_text")
        btn("🗑 Xóa phụ đề", "Xóa toàn bộ phụ đề (Ctrl+Shift+Delete)", "delete_all_text")
        btn("▦ Che mờ", "Thêm vùng che mờ tại đầu phát (Ctrl+M)", "add_blur")
        btn("🖼 Logo", "Thêm ảnh / logo (PNG, JPG…) tại đầu phát (Ctrl+L)\n"
            "Hoặc kéo tệp ảnh thả thẳng xuống timeline.", "add_image")
        btn("🔊 Đọc phụ đề", "Chuyển các câu phụ đề thành giọng nói, đặt trên rãnh âm thanh (Ctrl+Shift+T)\n"
            "Ctrl+nhấp / Shift+nhấp trên rãnh chữ để chọn nhiều câu.", "tts")
        bar.addStretch(1)
        self.snap_btn = QToolButton()
        self.snap_btn.setText("🧲 Hút")
        self.snap_btn.setToolTip("Bật/tắt hút dính: đầu phát hút vào điểm nối video, clip hút vào mép clip.\n"
                                 "Giữ Alt khi kéo để tạm tắt hút.")
        self.snap_btn.setCheckable(True)
        self.snap_btn.setChecked(True)
        self.snap_btn.setAutoRaise(True)
        self.snap_btn.toggled.connect(lambda v: setattr(self.tl, "snap", v))
        bar.addWidget(self.snap_btn)
        bar.addSpacing(8)
        zo = QToolButton(); zo.setText("−"); zo.setAutoRaise(True); zo.setToolTip("Thu nhỏ (-)")
        zo.clicked.connect(lambda: self.tl.set_zoom(self.tl.pps / 1.5))
        zi = QToolButton(); zi.setText("+"); zi.setAutoRaise(True); zi.setToolTip("Phóng to (+)")
        zi.clicked.connect(lambda: self.tl.set_zoom(self.tl.pps * 1.5))
        self.zoom = QSlider(Qt.Horizontal)
        self.zoom.setRange(0, 1000)
        self.zoom.setFixedWidth(140)
        self.zoom.valueChanged.connect(self._slider_zoom)
        fit = QToolButton(); fit.setText("⤢ Vừa khung"); fit.setAutoRaise(True)
        fit.setToolTip("Xem toàn bộ timeline (Shift+Z)")
        fit.clicked.connect(self.tl.zoom_fit)
        for wdg in (zo, self.zoom, zi, fit):
            bar.addWidget(wdg)
        lay.addLayout(bar)
        self.area = QScrollArea()
        self.area.setWidget(self.tl)
        self.area.setWidgetResizable(True)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.area.setFrameShape(QScrollArea.NoFrame)
        self.area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lay.addWidget(self.area, 1)
        self.tl.vscroll = self.area.verticalScrollBar()
        self.sb = QScrollBar(Qt.Horizontal)
        self.sb.valueChanged.connect(self._scrolled)
        lay.addWidget(self.sb)
        self._sync = False
        self.tl.view_changed.connect(self.sync)

    def _slider_zoom(self, v):
        if self._sync:
            return
        lo, hi = math.log(self.tl.min_pps()), math.log(MAX_PPS)
        self.tl.set_zoom(math.exp(lo + (hi - lo) * v / 1000))

    def _scrolled(self, v):
        if self._sync:
            return
        self.tl.set_view_start(v / self.tl.pps)

    def sync(self):
        self._sync = True
        tl = self.tl
        total_px = tl.content_len() * tl.pps
        page = tl.track_w()
        mx = int(max(0, total_px - page))
        self.sb.setRange(0, min(mx, 2_000_000_000))
        self.sb.setPageStep(int(page))
        self.sb.setSingleStep(40)
        self.sb.setValue(int(tl.view_start * tl.pps))
        lo, hi = math.log(tl.min_pps()), math.log(MAX_PPS)
        self.zoom.setValue(int((math.log(tl.pps) - lo) / (hi - lo) * 1000) if hi > lo else 0)
        self._sync = False
