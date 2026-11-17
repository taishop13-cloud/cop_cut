"""Background thumbnail + waveform generation with memory and disk caches.

Designed for very long files: thumbnails are extracted one frame at a time with a
fast keyframe seek (only for what is visible on screen), and the waveform is
decoded once into a compact peak table (100 peaks / second, uint8) stored on disk.
"""
from __future__ import annotations

import collections
import hashlib
import os
import threading
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from . import ffmpeg_tools as ff

PEAK_RATE = 100        # waveform peaks per second of source
THUMB_H = 72           # thumbnail height in pixels (stored size)


def cache_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    p = Path(base) / "CopCut" / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def file_key(path: str) -> str:
    try:
        st = os.stat(path)
        sig = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        sig = path
    return hashlib.sha1(sig.encode("utf-8")).hexdigest()[:20]


class ThumbnailCache(QObject):
    ready = Signal()

    def __init__(self, workers: int = 3, mem_items: int = 2500):
        super().__init__()
        self._mem: collections.OrderedDict[tuple, QImage] = collections.OrderedDict()
        self._mem_items = mem_items
        self._queue: collections.deque[tuple] = collections.deque()
        self._pending: set[tuple] = set()
        self._failed: set[tuple] = set()
        self._cv = threading.Condition()
        self._keys: dict[str, str] = {}
        self._dir = cache_root() / "thumbs"
        self._stop = False
        for _ in range(workers):
            threading.Thread(target=self._work, daemon=True).start()

    def _fkey(self, path):
        k = self._keys.get(path)
        if k is None:
            k = self._keys[path] = file_key(path)
        return k

    def get(self, path: str, t: float) -> QImage | None:
        """Return a cached thumbnail for source time t (ms resolution) or schedule it."""
        key = (path, int(round(t * 1000)))
        img = self._mem.get(key)
        if img is not None:
            self._mem.move_to_end(key)
            return img
        if key in self._failed:
            return None
        with self._cv:
            if key not in self._pending:
                self._pending.add(key)
                self._queue.append(key)
                # newest requests first; drop stale ones that scrolled away
                while len(self._queue) > 300:
                    old = self._queue.popleft()
                    self._pending.discard(old)
                self._cv.notify()
        return None

    def _store(self, key, img: QImage):
        with self._cv:
            self._mem[key] = img
            while len(self._mem) > self._mem_items:
                self._mem.popitem(last=False)
            self._pending.discard(key)

    def _work(self):
        while True:
            with self._cv:
                while not self._queue:
                    self._cv.wait()
                key = self._queue.pop()   # LIFO: most recently requested first
            path, ms = key
            d = self._dir / self._fkey(path)
            f = d / f"{ms}.jpg"
            img = QImage()
            if f.is_file():
                img.load(str(f))
            if img.isNull():
                data = self._extract(path, ms / 1000.0)
                if data:
                    img.loadFromData(data, "JPG")
                    if not img.isNull():
                        try:
                            d.mkdir(parents=True, exist_ok=True)
                            f.write_bytes(data)
                        except OSError:
                            pass
            if img.isNull():
                with self._cv:
                    self._failed.add(key)
                    self._pending.discard(key)
                continue
            self._store(key, img)
            try:
                self.ready.emit()
            except RuntimeError:  # app shutting down
                return

    @staticmethod
    def _extract(path: str, t: float) -> bytes | None:
        if not ff.FFMPEG:
            return None
        for tt in (t, max(0.0, t - 1.0)):
            try:
                r = ff.run([ff.FFMPEG, "-v", "error", "-noaccurate_seek", "-ss", f"{tt:.3f}",
                            "-i", path, "-frames:v", "1", "-an", "-sn", "-dn",
                            "-vf", f"scale=-2:{THUMB_H}", "-q:v", "6",
                            "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"], timeout=30)
            except Exception:
                return None
            if r.returncode == 0 and r.stdout:
                return r.stdout
        return None


class WaveformCache(QObject):
    ready = Signal(str)

    def __init__(self):
        super().__init__()
        self._data: dict[str, np.ndarray] = {}
        self._queued: set[str] = set()
        self._queue: collections.deque[str] = collections.deque()
        self._cv = threading.Condition()
        self._dir = cache_root() / "waves"
        self._dir.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._work, daemon=True).start()

    def get(self, path: str) -> np.ndarray | None:
        arr = self._data.get(path)
        if arr is not None:
            return arr
        with self._cv:
            if path not in self._queued:
                self._queued.add(path)
                self._queue.append(path)
                self._cv.notify()
        return None

    def _work(self):
        while True:
            with self._cv:
                while not self._queue:
                    self._cv.wait()
                path = self._queue.popleft()
            f = self._dir / f"{file_key(path)}.npy"
            arr = None
            if f.is_file():
                try:
                    arr = np.load(f)
                except Exception:
                    arr = None
            if arr is None:
                try:
                    arr = self._decode(path)
                except Exception:  # noqa: BLE001 - keep the worker alive
                    arr = None
                if arr is not None:
                    try:
                        np.save(f, arr)
                    except OSError:
                        pass
            if arr is None:
                arr = np.zeros(1, dtype=np.uint8)
            self._data[path] = arr
            try:
                self.ready.emit(path)
            except RuntimeError:  # app shutting down
                return

    @staticmethod
    def _decode(path: str) -> np.ndarray | None:
        if not ff.FFMPEG:
            return None
        sr = 8000
        block = sr // PEAK_RATE
        p = ff.popen([ff.FFMPEG, "-v", "error", "-i", path, "-vn", "-sn", "-dn",
                      "-ac", "1", "-ar", str(sr), "-f", "s16le", "pipe:1"],
                     stdout=ff.subprocess.PIPE, stderr=ff.subprocess.DEVNULL)
        parts = []
        rest = np.zeros(0, dtype=np.int16)
        chunk_bytes = sr * 2 * 60   # one minute at a time
        try:
            while True:
                buf = p.stdout.read(chunk_bytes)
                if not buf:
                    break
                if len(buf) % 2:
                    buf = buf[:-1]
                s = np.concatenate([rest, np.frombuffer(buf, dtype=np.int16)])
                n = len(s) // block * block
                if n:
                    blk = np.abs(s[:n].astype(np.int32)).reshape(-1, block).max(axis=1)
                    parts.append(np.minimum(255, blk >> 7).astype(np.uint8))
                rest = s[n:]
        finally:
            p.stdout.close()
            p.wait()
        if not parts:
            return None
        return np.concatenate(parts)
