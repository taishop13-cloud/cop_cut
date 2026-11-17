"""Preview canvas (video + live text overlay) and the timeline playback engine."""
from __future__ import annotations

from PySide6.QtCore import QElapsedTimer, QObject, QPointF, QRectF, QSizeF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QBrush
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsObject, QGraphicsRectItem, QGraphicsScene,
                               QGraphicsView, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget)

from PySide6.QtGui import QImage, QPixmap

from .ffmpeg_tools import fmt_time
from .model import BlurClip, Project, TextClip
from .overlays import BlurOverlay, Guides, TextOverlay, blur_radius


class PreviewView(QGraphicsView):
    def __init__(self, scene: QGraphicsScene):
        super().__init__(scene)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QColor("#0e0f12"))
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setViewportUpdateMode(QGraphicsView.BoundingRectViewportUpdate)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)


class PlaybackEngine(QObject):
    """Plays the timeline: main-track video through one QMediaPlayer, plus one player per
    audio track that has a clip under the playhead."""
    time_changed = Signal(float)
    state_changed = Signal(bool)
    text_moved = Signal(object, float, float)
    blur_changed = Signal(object, float, float, float, float)
    overlay_clicked = Signal(str, object)

    def __init__(self, project: Project, video_item: QGraphicsVideoItem, scene: QGraphicsScene,
                 guides: Guides):
        super().__init__()
        self.p = project
        self.scene = scene
        self.video_item = video_item
        self.v = QMediaPlayer(self)
        self.va = QAudioOutput(self)
        self.v.setAudioOutput(self.va)
        self.v.setVideoOutput(video_item)
        self.music: dict[int, list] = {}      # audio track -> [player, output, current clip id]
        self._pending: dict[QMediaPlayer, tuple[int, bool]] = {}
        self.v.mediaStatusChanged.connect(lambda s: self._status(self.v, s))
        self.t = 0.0
        self.playing = False
        self.cur_idx = -1
        self._mode = "clock"
        self._clock = QElapsedTimer()
        self._clock_t0 = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(15)
        self._timer.timeout.connect(self._tick)
        self._scrub = QTimer(self)
        self._scrub.setSingleShot(True)
        self._scrub.setInterval(25)
        self._scrub.timeout.connect(self._show_frame)
        self.overlays: dict[int, TextOverlay] = {}
        self.blur_overlays: dict[int, BlurOverlay] = {}
        self.guides = guides
        self.selection: tuple[str, int] | None = None
        self._last_img: QImage | None = None
        video_item.videoSink().videoFrameChanged.connect(self._on_frame)
        self._stall_pos, self._stall_since = -1, None
        self.cw, self.ch = project.width, project.height

    # ---------------------------------------------------------- media helpers
    def _status(self, player: QMediaPlayer, status):
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia) and player in self._pending:
            # Qt ignores play() issued from inside the status callback; apply on the next loop turn.
            QTimer.singleShot(0, lambda: self._apply_pending(player))

    def _apply_pending(self, player: QMediaPlayer):
        if player not in self._pending:
            return
        ms, play = self._pending.pop(player)
        if play:
            player.play()
            player.setPosition(ms)
        else:
            player.pause()
            player.setPosition(ms)
        self._stall_pos, self._stall_since = -1, None

    def _load(self, player: QMediaPlayer, path: str, sec: float, play: bool):
        ms = int(round(max(0.0, sec) * 1000))
        url = QUrl.fromLocalFile(path)
        if player.source() != url:
            self._pending[player] = (ms, play)
            player.setSource(url)
            return
        if player in self._pending:
            self._pending[player] = (ms, play)
            return
        player.setPosition(ms)
        if play:
            if player.playbackState() != QMediaPlayer.PlayingState:
                player.play()
        else:
            player.pause()

    def release_media(self):
        self.pause()
        self.v.setSource(QUrl())
        for pl, _out, _cid in self.music.values():
            pl.setSource(QUrl())
        self._pending.clear()

    # ---------------------------------------------------------- public API
    def set_canvas(self, w: int, h: int):
        self.cw, self.ch = w, h
        for ov in list(self.overlays.values()) + list(self.blur_overlays.values()):
            self.scene.removeItem(ov)
        self.overlays.clear()
        self.blur_overlays.clear()
        self.guides.set_canvas(w, h)
        self.update_overlays()

    def seek(self, t: float):
        t = max(0.0, min(t, max(self.p.duration(), 0.0)))
        self.t = t
        if self.playing:
            self._enter(t)
        else:
            if not self._scrub.isActive():
                self._scrub.start()
        self.update_overlays()
        self.time_changed.emit(self.t)

    def refresh(self):
        """Re-show the current frame after an edit."""
        if not self.playing:
            self._scrub.start()
        self.update_overlays()

    def toggle(self):
        self.pause() if self.playing else self.play()

    def play(self):
        dur = self.p.duration()
        if dur <= 0:
            return
        if self.t >= dur - 1e-3:
            self.t = 0.0
        self.playing = True
        for ov in self.blur_overlays.values():
            ov.show_frame = False
            ov.update()
        self._enter(self.t)
        self._timer.start()
        self.state_changed.emit(True)

    def pause(self):
        if not self.playing:
            return
        self.playing = False
        for ov in self.blur_overlays.values():
            ov.show_frame = True
            ov.update()
        self._timer.stop()
        self.v.pause()
        for m in self.music.values():
            m[0].pause()
        for pl in list(self._pending):
            ms, _ = self._pending[pl]
            self._pending[pl] = (ms, False)
        self.state_changed.emit(False)

    # ---------------------------------------------------------- internals
    def _apply_clip_params(self, player, out, clip):
        if abs(player.playbackRate() - clip.speed) > 1e-6:
            player.setPlaybackRate(clip.speed)
        out.setVolume(min(1.0, clip.volume))

    def _show_frame(self):
        idx = self.p.main_index_at(self.t)
        if idx < 0:
            self.video_item.setVisible(False)
            self.cur_idx = -1
            return
        c = self.p.main[idx]
        self.video_item.setVisible(True)
        self.cur_idx = idx
        src = c.src_at(self.t - self.p.starts()[idx])
        self._load(self.v, c.path, src, False)

    def _enter(self, t: float):
        idx = self.p.main_index_at(t)
        self.cur_idx = idx
        if idx >= 0:
            c = self.p.main[idx]
            self.video_item.setVisible(True)
            self._apply_clip_params(self.v, self.va, c)
            self._load(self.v, c.path, c.src_at(t - self.p.starts()[idx]), True)
            self._mode = "video"
        else:
            self.video_item.setVisible(False)
            self.v.pause()
            self._mode = "clock"
        self._clock.restart()
        self._clock_t0 = t
        for m in self.music.values():
            m[2] = None
        self._sync_music()

    def _tick(self):
        p = self.p
        if self._mode == "video":
            if self.cur_idx < 0 or self.cur_idx >= len(p.main):
                self._enter(self.t)
                return
            c = p.main[self.cur_idx]
            start = p.starts()[self.cur_idx]
            self._apply_clip_params(self.v, self.va, c)
            if self.v not in self._pending and self.v.playbackState() == QMediaPlayer.PlayingState:
                self._watchdog()
                pos = self.v.position() / 1000.0
                if pos >= c.src_in - 0.25:
                    self.t = start + max(0.0, pos - c.src_in) / c.speed
                ended = self.v.mediaStatus() == QMediaPlayer.EndOfMedia
                if pos >= c.src_out - 0.5 / p.fps or ended:
                    self._advance()
            elif self.v not in self._pending and self.v.mediaStatus() == QMediaPlayer.EndOfMedia:
                self._advance()
        else:
            self.t = self._clock_t0 + self._clock.elapsed() / 1000.0
            if p.main_index_at(self.t) >= 0:
                self._enter(self.t)
        if self.t >= p.duration():
            self.t = p.duration()
            self.pause()
        self._sync_music()
        self.update_overlays()
        self.time_changed.emit(self.t)

    def _watchdog(self):
        """Kick the player if it claims to be playing but its position is frozen."""
        pos = self.v.position()
        if pos != self._stall_pos:
            self._stall_pos, self._stall_since = pos, None
            return
        if self._stall_since is None:
            self._stall_since = QElapsedTimer()
            self._stall_since.start()
        elif self._stall_since.elapsed() > 400:
            self._stall_since = None
            self.v.pause()
            self.v.play()

    def _advance(self):
        p = self.p
        c = p.main[self.cur_idx]
        nxt = self.cur_idx + 1
        if nxt < len(p.main):
            n = p.main[nxt]
            self.cur_idx = nxt
            self.t = p.starts()[nxt]
            self._apply_clip_params(self.v, self.va, n)
            if not (n.path == c.path and abs(n.src_in - c.src_out) < 0.05):
                self._load(self.v, n.path, n.src_in, True)
        else:
            self.t = p.main_end()
            self.v.pause()
            self.video_item.setVisible(False)
            self._mode = "clock"
            self._clock.restart()
            self._clock_t0 = self.t

    def _music_player(self, track: int) -> list:
        m = self.music.get(track)
        if m is None:
            pl = QMediaPlayer(self)
            out = QAudioOutput(self)
            pl.setAudioOutput(out)
            pl.mediaStatusChanged.connect(lambda s, pl=pl: self._status(pl, s))
            m = self.music[track] = [pl, out, None]
        return m

    def _sync_music(self):
        active: dict[int, object] = {}
        if self.playing:
            t = self.t
            for c in self.p.audio_in(t, t + 1e-6):
                if c.start <= t < c.start + c.duration and c.volume > 0:
                    active[c.track] = c
        for track, m in self.music.items():
            if track not in active:
                if m[2] is not None or m[0].playbackState() == QMediaPlayer.PlayingState:
                    m[0].pause()
                m[2] = None
        for track, a in active.items():
            m = self._music_player(track)
            pl, out = m[0], m[1]
            expected = a.src_at(self.t - a.start)
            self._apply_clip_params(pl, out, a)
            if m[2] != a.id:
                m[2] = a.id
                self._load(pl, a.path, expected, True)
            elif pl not in self._pending:
                actual = pl.position() / 1000.0
                if abs(actual - expected) > 0.35:
                    pl.setPosition(int(expected * 1000))
                if pl.playbackState() != QMediaPlayer.PlayingState:
                    pl.play()

    def set_selection(self, sel):
        self.selection = sel
        for kind, d in (("text", self.overlays), ("blur", self.blur_overlays)):
            for cid, ov in d.items():
                hint = sel == (kind, cid)
                if ov.selected_hint != hint:
                    ov.selected_hint = hint
                    ov.update()

    def update_overlays(self, force: bool = False):
        active = {c.id: c for c in self.p.texts_at(self.t)}
        for cid in list(self.overlays):
            ov = self.overlays[cid]
            if cid not in active or ov.clip is not active[cid]:
                self.scene.removeItem(ov)
                del self.overlays[cid]
        for cid, c in active.items():
            ov = self.overlays.get(cid)
            if ov is None:
                ov = TextOverlay(c, self.cw, self.ch, self.guides)
                ov.setZValue(10)
                ov.moved.connect(self.text_moved)
                ov.clicked.connect(lambda clip: self.overlay_clicked.emit("text", clip))
                ov.selected_hint = self.selection == ("text", cid)
                self.scene.addItem(ov)
                self.overlays[cid] = ov
            else:
                ov.refresh()
            if force:
                ov._sig = None
                ov.refresh()
        self._update_blur_items()

    # ---------------------------------------------------------- blur boxes
    def _update_blur_items(self):
        active = {c.id: c for c in self.p.blurs_at(self.t)}
        changed = False
        for cid in list(self.blur_overlays):
            ov = self.blur_overlays[cid]
            if cid not in active or ov.clip is not active[cid]:
                self.scene.removeItem(ov)
                del self.blur_overlays[cid]
        for cid, c in active.items():
            ov = self.blur_overlays.get(cid)
            if ov is None:
                ov = BlurOverlay(c, self.cw, self.ch, self.guides)
                ov.show_frame = not self.playing
                ov.selected_hint = self.selection == ("blur", cid)
                ov.changed.connect(self.blur_changed)
                ov.clicked.connect(lambda clip: self.overlay_clicked.emit("blur", clip))
                ov.recompute = lambda o=ov: self._blur_pix(o)
                self.scene.addItem(ov)
                self.blur_overlays[cid] = ov
                changed = True
            else:
                old = (QRectF(ov.rect), getattr(ov, "_strength", None))
                ov.refresh()
                if (ov.rect, c.strength) != old:
                    changed = True
            ov._strength = c.strength
        if changed:
            for ov in self.blur_overlays.values():
                self._blur_pix(ov)
        if not self.blur_overlays:
            self._last_img = None

    def _on_frame(self, frame):
        if not self.blur_overlays:
            return
        img = frame.toImage()
        if img.isNull():
            return
        self._last_img = img
        for ov in self.blur_overlays.values():
            self._blur_pix(ov)

    def _blur_pix(self, ov: BlurOverlay):
        """Approximate the export box-blur by down- then up-scaling the covered video area."""
        img = self._last_img
        if img is None or not self.video_item.isVisible():
            ov.set_blur(None, QRectF())
            return
        iw, ih = img.width(), img.height()
        s = min(self.cw / iw, self.ch / ih)
        ox, oy = (self.cw - iw * s) / 2, (self.ch - ih * s) / 2
        target = ov.rect.intersected(QRectF(ox, oy, iw * s, ih * s))
        if target.width() < 2 or target.height() < 2:
            ov.set_blur(None, QRectF())
            return
        src = QRectF((target.x() - ox) / s, (target.y() - oy) / s, target.width() / s,
                     target.height() / s).toAlignedRect().intersected(img.rect())
        if src.width() < 2 or src.height() < 2:
            ov.set_blur(None, QRectF())
            return
        radius_img = blur_radius(ov.clip.strength, self.ch) / s
        k = max(1.0, radius_img * 0.9)
        part = img.copy(src)
        small = part.scaled(max(1, int(src.width() / k)), max(1, int(src.height() / k)),
                            Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        # a second, smaller pass softens the blocky look
        small = small.scaled(max(1, small.width() // 2 + 1), max(1, small.height() // 2 + 1),
                             Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        big = small.scaled(src.width(), src.height(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        ov.set_blur(QPixmap.fromImage(big), target)


class PreviewPanel(QWidget):
    def __init__(self, project: Project, parent=None):
        super().__init__(parent)
        self.p = project
        self.scene = QGraphicsScene(self)
        self.bg = QGraphicsRectItem()
        self.bg.setBrush(QColor("#000000"))
        self.bg.setPen(Qt.NoPen)
        self.scene.addItem(self.bg)
        self.video = QGraphicsVideoItem()
        self.video.setAspectRatioMode(Qt.KeepAspectRatio)
        self.scene.addItem(self.video)
        self.guides = Guides(project.width, project.height)
        self.scene.addItem(self.guides)
        self.view = PreviewView(self.scene)
        self.engine = PlaybackEngine(project, self.video, self.scene, self.guides)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.view, 1)
        bar = QHBoxLayout()
        bar.setContentsMargins(10, 4, 10, 4)
        self.time_lbl = QLabel("00:00:00:00")
        self.time_lbl.setObjectName("timecode")
        self.dur_lbl = QLabel("/ 00:00:00:00")
        self.dur_lbl.setObjectName("timecodeDim")
        self.play_btn = QToolButton()
        self.play_btn.setText("▶")
        self.play_btn.setObjectName("playBtn")
        self.play_btn.setToolTip("Phát / Tạm dừng (Space)")
        self.play_btn.clicked.connect(self.engine.toggle)
        self.res_lbl = QLabel()
        self.res_lbl.setObjectName("timecodeDim")
        self.guide_btn = QToolButton()
        self.guide_btn.setText("⌖ Đường căn")
        self.guide_btn.setCheckable(True)
        self.guide_btn.setToolTip("Luôn hiện đường căn giữa và khung an toàn.\n"
                                  "Khi kéo chữ / vùng mờ, đường căn tự hiện và hút vào tâm khung.")
        self.guide_btn.toggled.connect(self._toggle_guides)
        bar.addWidget(self.time_lbl)
        bar.addWidget(self.dur_lbl)
        bar.addStretch(1)
        bar.addWidget(self.play_btn)
        bar.addStretch(1)
        bar.addWidget(self.guide_btn)
        bar.addWidget(self.res_lbl)
        lay.addLayout(bar)
        self.engine.state_changed.connect(lambda pl: self.play_btn.setText("⏸" if pl else "▶"))
        self.engine.time_changed.connect(self._time)
        self.set_canvas()

    def set_canvas(self):
        w, h = self.p.width, self.p.height
        self.scene.setSceneRect(0, 0, w, h)
        self.bg.setRect(0, 0, w, h)
        self.video.setSize(QSizeF(w, h))
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)
        self.engine.set_canvas(w, h)
        self.res_lbl.setText(f"{w}×{h}  {self.p.fps:g} fps")
        self.update_duration()

    def _toggle_guides(self, on: bool):
        self.guides.always = on
        self.guides.update()

    def update_duration(self):
        self.dur_lbl.setText("/ " + fmt_time(self.p.duration(), self.p.fps))

    def _time(self, t):
        self.time_lbl.setText(fmt_time(t, self.p.fps))
