"""Project data model: one magnetic main video track, any number of audio and text tracks."""
from __future__ import annotations

import bisect
import itertools
import math
from dataclasses import dataclass, field, asdict, fields

from .ffmpeg_tools import MediaInfo

_ids = itertools.count(1)


def new_id() -> int:
    return next(_ids)


# Volume is stored as a linear gain (1.0 = unchanged) but shown in decibels like CapCut.
DB_MIN = -60.0     # at / below this the clip is muted (shown as -∞)
DB_MAX = 20.0      # +20 dB = 10x gain


def gain_to_db(g: float) -> float:
    if g <= 0:
        return DB_MIN
    return max(DB_MIN, min(DB_MAX, 20 * math.log10(g)))


def db_to_gain(db: float) -> float:
    if db <= DB_MIN + 1e-9:
        return 0.0
    return 10 ** (min(DB_MAX, db) / 20)


def fmt_db(g: float) -> str:
    if g <= 0:
        return "-∞ dB"
    db = gain_to_db(g)
    return f"{db:+.1f} dB"


@dataclass
class Clip:
    """A piece of a media file. Used on the main track and the audio track."""
    path: str
    src_in: float
    src_out: float
    speed: float = 1.0
    volume: float = 1.0          # linear gain, 1.0 = 0 dB (UI shows dB)
    start: float = 0.0           # timeline start (audio track only; main track is magnetic)
    track: int = 0               # audio track index (0 = closest to the main track)
    id: int = field(default_factory=new_id)

    @property
    def duration(self) -> float:
        return (self.src_out - self.src_in) / self.speed

    def src_at(self, t_in_clip: float) -> float:
        return self.src_in + t_in_clip * self.speed

    def to_dict(self):
        d = asdict(self)
        d.pop("id")
        return d

    @staticmethod
    def from_dict(d) -> "Clip":
        return Clip(**{k: d[k] for k in ("path", "src_in", "src_out", "speed", "volume", "start", "track")
                       if k in d})


@dataclass
class TextStyle:
    font: str = "Arial"
    size: int = 56               # pixels at project height
    color: str = "#ffffff"
    bold: bool = True
    italic: bool = False
    outline_color: str = "#000000"
    outline: float = 3.0
    bg: bool = False
    bg_color: str = "#000000"
    x: float = 0.5               # centre, normalised 0..1
    y: float = 0.88
    opacity: float = 1.0         # 0 = invisible, 1 = solid

    def copy(self) -> "TextStyle":
        return TextStyle(**asdict(self))


STYLE_KEYS = [f.name for f in fields(TextStyle)]


@dataclass
class TextClip:
    text: str
    start: float
    duration: float
    style: TextStyle = field(default_factory=TextStyle)
    track: int = 0               # text track index (0 = closest to the main track)
    id: int = field(default_factory=new_id)

    @property
    def end(self) -> float:
        return self.start + self.duration

    def to_dict(self):
        return {"text": self.text, "start": self.start, "duration": self.duration,
                "style": asdict(self.style), "track": self.track}

    @staticmethod
    def from_dict(d) -> "TextClip":
        st = TextStyle(**{k: v for k, v in d.get("style", {}).items() if k in STYLE_KEYS})
        return TextClip(d["text"], d["start"], d["duration"], st, int(d.get("track", 0)))


@dataclass
class BlurClip:
    """A blurred rectangle shown between start and end. Geometry is normalised (0..1)."""
    start: float
    duration: float
    x: float = 0.35          # top-left
    y: float = 0.35
    w: float = 0.30
    h: float = 0.30
    strength: int = 50       # 1..100
    id: int = field(default_factory=new_id)

    @property
    def end(self) -> float:
        return self.start + self.duration

    def copy_at(self, start: float, duration: float) -> "BlurClip":
        return BlurClip(start, duration, self.x, self.y, self.w, self.h, self.strength)

    def to_dict(self):
        d = asdict(self)
        d.pop("id")
        return d

    @staticmethod
    def from_dict(d) -> "BlurClip":
        if d.get("path"):
            return ImageClip.from_dict(d)
        return BlurClip(**{k: d[k] for k in ("start", "duration", "x", "y", "w", "h", "strength") if k in d})


@dataclass
class ImageClip(BlurClip):
    """A still image (logo) laid over the video. Shares the blur lane and its geometry
    (normalised x, y, w, h); `strength` is unused. Resizing keeps the image's aspect.
    With `circle` the image is centre-cropped to a square and masked to a circle."""
    path: str = ""
    opacity: float = 1.0     # 0 = invisible, 1 = solid
    circle: bool = True      # logo shown inside a round frame (extra parts cut off)

    def copy_at(self, start: float, duration: float) -> "ImageClip":
        return ImageClip(start, duration, self.x, self.y, self.w, self.h, path=self.path,
                         opacity=self.opacity, circle=self.circle)

    def pixel_aspect(self, img_w: int, img_h: int) -> float:
        """Width / height of the shown picture in pixels (1 for the round frame)."""
        if self.circle or img_w <= 0 or img_h <= 0:
            return 1.0
        return img_w / img_h

    def fit_aspect(self, canvas_w: int, canvas_h: int, img_w: int, img_h: int):
        """Set the height from the width so the picture is not stretched; keeps the centre."""
        cy = self.y + self.h / 2
        self.h = self.w * canvas_w / max(1, canvas_h) / self.pixel_aspect(img_w, img_h)
        self.y = cy - self.h / 2

    @staticmethod
    def from_dict(d) -> "ImageClip":
        return ImageClip(**{k: d[k] for k in ("start", "duration", "x", "y", "w", "h", "path", "opacity",
                                              "circle") if k in d})


class Project:
    MIN_DUR = 1 / 30

    def __init__(self):
        self.width = 1920
        self.height = 1080
        self.fps = 30.0
        self.media: dict[str, MediaInfo] = {}
        self.main: list[Clip] = []
        self.audio: list[Clip] = []
        self.texts: list[TextClip] = []
        self.blurs: list[BlurClip] = []
        self.text_style = TextStyle()
        self._rev = 0
        self._cache_rev = -1
        self._starts: list[float] = []
        self._text_starts: list[float] = []
        self._text_maxdur = 0.0

    # ---- change tracking / derived layout ----
    def touch(self):
        self._rev += 1

    def _layout(self):
        if self._cache_rev == self._rev:
            return
        acc = 0.0
        self._starts = []
        for c in self.main:
            self._starts.append(acc)
            acc += c.duration
        self._main_end = acc
        self.texts.sort(key=lambda c: c.start)
        self.audio.sort(key=lambda c: c.start)
        self._text_starts = [c.start for c in self.texts]
        self._text_maxdur = max((c.duration for c in self.texts), default=0.0)
        self.blurs.sort(key=lambda c: c.start)
        self._blur_starts = [c.start for c in self.blurs]
        self._blur_maxdur = max((c.duration for c in self.blurs), default=0.0)
        self._audio_starts = [c.start for c in self.audio]
        self._audio_maxdur = max((c.duration for c in self.audio), default=0.0)
        self._cache_rev = self._rev

    def starts(self) -> list[float]:
        self._layout()
        return self._starts

    def main_end(self) -> float:
        self._layout()
        return self._main_end

    def duration(self) -> float:
        self._layout()
        d = self._main_end
        if self.audio:
            d = max(d, max(c.start + c.duration for c in self.audio))
        if self.texts:
            d = max(d, max(c.end for c in self.texts))
        if self.blurs:
            d = max(d, max(c.end for c in self.blurs))
        return d

    def main_index_at(self, t: float) -> int:
        """Index of main clip covering timeline time t, or -1."""
        st = self.starts()
        if not st or t < 0 or t >= self._main_end:
            return -1
        return max(0, bisect.bisect_right(st, t) - 1)

    def main_range(self, t0: float, t1: float) -> range:
        st = self.starts()
        if not st:
            return range(0)
        a = max(0, bisect.bisect_right(st, t0) - 1)
        b = bisect.bisect_left(st, t1)
        return range(a, min(b, len(st)))

    def texts_in(self, t0: float, t1: float) -> list[TextClip]:
        self._layout()
        a = bisect.bisect_left(self._text_starts, t0 - self._text_maxdur)
        b = bisect.bisect_right(self._text_starts, t1)
        return [c for c in self.texts[a:b] if c.end > t0 and c.start < t1]

    def texts_at(self, t: float) -> list[TextClip]:
        return [c for c in self.texts_in(t, t + 1e-6) if c.start <= t < c.end]

    def blurs_in(self, t0: float, t1: float) -> list[BlurClip]:
        self._layout()
        a = bisect.bisect_left(self._blur_starts, t0 - self._blur_maxdur)
        b = bisect.bisect_right(self._blur_starts, t1)
        return [c for c in self.blurs[a:b] if c.end > t0 and c.start < t1]

    def blurs_at(self, t: float) -> list[BlurClip]:
        return [c for c in self.blurs_in(t, t + 1e-6) if c.start <= t < c.end]

    def audio_in(self, t0: float, t1: float) -> list[Clip]:
        self._layout()
        a = bisect.bisect_left(self._audio_starts, t0 - self._audio_maxdur)
        b = bisect.bisect_right(self._audio_starts, t1)
        return [c for c in self.audio[a:b] if c.start + c.duration > t0 and c.start < t1]

    def audio_at(self, t: float, track: int | None = None) -> Clip | None:
        hits = [c for c in self.audio_in(t, t + 1e-6) if c.start <= t < c.start + c.duration
                and (track is None or c.track == track)]
        return hits[-1] if hits else None

    # ---- multi-track helpers (text + audio) ----
    def track_count(self, kind: str) -> int:
        lst = self.texts if kind == "text" else self.audio
        return max(1, max((c.track for c in lst), default=0) + 1)

    def overlaps(self, kind: str, track: int, start: float, end: float, exclude=()) -> bool:
        """True if [start, end) covers part of another clip on that text/audio track."""
        lst = self.texts if kind == "text" else self.audio
        return any(c.track == track and c not in exclude and c.start < end - 1e-6
                   and c.start + c.duration > start + 1e-6 for c in lst)

    def free_track(self, kind: str, spans: list[tuple[float, float]], exclude=(), min_track: int = 0) -> int:
        """Lowest track (>= min_track) where none of the [start, end) spans overlaps an
        existing clip. Returns a new track number if every existing track is busy."""
        lst = self.texts if kind == "text" else self.audio
        n = self.track_count(kind)
        busy: dict[int, list[tuple[float, float]]] = {}
        lo = min((a for a, _ in spans), default=0.0)
        hi = max((b for _, b in spans), default=0.0)
        for c in lst:
            if c in exclude:
                continue
            s, e = c.start, c.start + c.duration
            if e > lo and s < hi:
                busy.setdefault(c.track, []).append((s, e))
        for k in range(min(min_track, n), n):
            occ = sorted(busy.get(k, []))
            if not occ:
                return k
            starts = [s for s, _ in occ]
            maxd = max(e - s for s, e in occ)
            ok = True
            for a, b in spans:
                i = bisect.bisect_left(starts, a - maxd)
                for s, e in occ[i:]:
                    if s >= b - 1e-6:
                        break
                    if e > a + 1e-6:
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                return k
        return n

    def compact_tracks(self, kind: str):
        """Renumber tracks so there are no empty ones in between."""
        lst = self.texts if kind == "text" else self.audio
        used = sorted({c.track for c in lst})
        remap = {t: i for i, t in enumerate(used)}
        for c in lst:
            c.track = remap[c.track]

    def lane_list(self, kind: str) -> list:
        return {"main": self.main, "audio": self.audio, "text": self.texts, "blur": self.blurs}[kind]

    def find(self, kind: str, cid: int):
        lst = self.lane_list(kind)
        for i, c in enumerate(lst):
            if c.id == cid:
                return i, c
        return -1, None

    def snap_frame(self, t: float) -> float:
        return round(t * self.fps) / self.fps

    def adopt_settings_from(self, info: MediaInfo):
        if info.has_video and info.width and info.height:
            self.width = info.width - info.width % 2
            self.height = info.height - info.height % 2
            self.fps = round(info.fps, 3) if info.fps else 30.0

    # ---- serialisation ----
    def to_dict(self):
        return {
            "version": 1,
            "width": self.width, "height": self.height, "fps": self.fps,
            "media": {p: m.to_dict() for p, m in self.media.items()},
            "main": [c.to_dict() for c in self.main],
            "audio": [c.to_dict() for c in self.audio],
            "texts": [c.to_dict() for c in self.texts],
            "blurs": [c.to_dict() for c in self.blurs],
            "text_style": asdict(self.text_style),
        }

    def load_dict(self, d):
        self.width = d.get("width", 1920)
        self.height = d.get("height", 1080)
        self.fps = d.get("fps", 30.0)
        self.media = {p: MediaInfo.from_dict(m) for p, m in d.get("media", {}).items()}
        self.main = [Clip.from_dict(c) for c in d.get("main", [])]
        self.audio = [Clip.from_dict(c) for c in d.get("audio", [])]
        self.texts = [TextClip.from_dict(c) for c in d.get("texts", [])]
        self.blurs = [BlurClip.from_dict(c) for c in d.get("blurs", [])]
        self.text_style = TextStyle(**{k: v for k, v in d.get("text_style", {}).items() if k in STYLE_KEYS})
        self.touch()

    def restore(self, d, keep_ids: dict | None = None):
        """Load a snapshot (used by undo)."""
        self.load_dict(d)
