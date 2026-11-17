"""Locate FFmpeg binaries, run them without console windows, and probe media."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
IS_WIN = sys.platform.startswith("win")
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW


def find_bin(name: str) -> str | None:
    exe = name + (".exe" if IS_WIN else "")
    for cand in (APP_DIR / "ffmpeg" / "bin" / exe, APP_DIR / "ffmpeg" / exe):
        if cand.is_file():
            return str(cand)
    return shutil.which(name)


FFMPEG = find_bin("ffmpeg")
FFPROBE = find_bin("ffprobe")


def popen(args, **kw) -> subprocess.Popen:
    kw.setdefault("creationflags", NO_WINDOW)
    kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.Popen(args, **kw)


def run(args, timeout=None, **kw) -> subprocess.CompletedProcess:
    kw.setdefault("creationflags", NO_WINDOW)
    kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(args, capture_output=True, timeout=timeout, **kw)


AUDIO_EXT = {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg", ".opus", ".wma"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".m4v",
             ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MEDIA_EXT = AUDIO_EXT | VIDEO_EXT | IMAGE_EXT
IMAGE_DEFAULT_DUR = 5.0      # seconds an image (logo) covers when first placed


def is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXT


@dataclass
class MediaInfo:
    path: str
    duration: float
    has_video: bool
    has_audio: bool
    width: int = 0
    height: int = 0
    fps: float = 30.0
    vcodec: str = ""
    acodec: str = ""
    pix_fmt: str = ""
    sample_rate: int = 0
    channels: int = 0

    @property
    def kind(self) -> str:
        if self.vcodec == "image":
            return "image"
        return "video" if self.has_video else "audio"

    @property
    def is_image(self) -> bool:
        return self.vcodec == "image"

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d) -> "MediaInfo":
        return MediaInfo(**d)


def _parse_rate(s: str) -> float:
    try:
        if "/" in s:
            a, b = s.split("/")
            return float(a) / float(b) if float(b) else 0.0
        return float(s)
    except (ValueError, ZeroDivisionError):
        return 0.0


class ProbeError(Exception):
    pass


def probe(path: str) -> MediaInfo:
    if not FFPROBE:
        raise ProbeError("Không tìm thấy ffprobe. Hãy chạy setup_ffmpeg.py.")
    r = run([FFPROBE, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path], timeout=60)
    if r.returncode != 0:
        raise ProbeError(r.stderr.decode("utf-8", "replace").strip() or "ffprobe lỗi")
    data = json.loads(r.stdout.decode("utf-8", "replace"))
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None and a is None:
        raise ProbeError("Tệp không có hình hoặc tiếng.")
    dur = float(fmt.get("duration") or 0) or float((v or a).get("duration") or 0)
    if dur <= 0:
        raise ProbeError("Không đọc được thời lượng tệp.")
    info = MediaInfo(path=path, duration=dur, has_video=v is not None, has_audio=a is not None)
    if v is not None:
        info.width = int(v.get("width") or 0)
        info.height = int(v.get("height") or 0)
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(abs(float(sd["rotation"])))
        if rot in (90, 270):
            info.width, info.height = info.height, info.width
        fps = _parse_rate(v.get("avg_frame_rate", "0/0")) or _parse_rate(v.get("r_frame_rate", "0/0"))
        info.fps = fps if 1 <= fps <= 240 else 30.0
        info.vcodec = v.get("codec_name", "")
        info.pix_fmt = v.get("pix_fmt", "")
    if a is not None:
        info.acodec = a.get("codec_name", "")
        info.sample_rate = int(a.get("sample_rate") or 0)
        info.channels = int(a.get("channels") or 0)
    return info


_ver: int | None = None


def ffmpeg_major() -> int:
    global _ver
    if _ver is None:
        _ver = 0
        try:
            out = run([FFMPEG, "-hide_banner", "-version"], timeout=20).stdout.decode("utf-8", "replace")
            import re
            m = re.search(r"ffmpeg version n?(\d+)\.", out)
            _ver = int(m.group(1)) if m else 99   # git builds: assume new
        except Exception:
            _ver = 0
    return _ver


def filter_script_args(name: str) -> list[str]:
    """FFmpeg >= 7 reads option values from files with '-/opt file'."""
    return ["-/filter_complex", name] if ffmpeg_major() >= 7 else ["-filter_complex_script", name]


_hw_cache: list[tuple[str, str]] | None = None


def detect_encoders() -> list[tuple[str, str]]:
    """Return [(ffmpeg_encoder, label)] that actually work on this machine."""
    global _hw_cache
    if _hw_cache is not None:
        return _hw_cache
    found = [("libx264", "CPU (x264)")]
    if FFMPEG:
        for enc, label in (("h264_nvenc", "GPU NVIDIA (NVENC)"),
                           ("h264_qsv", "GPU Intel (QuickSync)"),
                           ("h264_amf", "GPU AMD (AMF)")):
            try:
                r = run([FFMPEG, "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                         "color=black:s=256x256:d=0.2", "-c:v", enc, "-f", "null", "-"],
                        timeout=20)
                if r.returncode == 0:
                    found.append((enc, label))
            except Exception:
                pass
    _hw_cache = found
    return found


def fmt_time(t: float, frames_fps: float | None = None) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = int(t % 60)
    if frames_fps:
        f = int(round((t - int(t)) * frames_fps))
        if f >= int(round(frames_fps)):
            f = 0
        return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"
