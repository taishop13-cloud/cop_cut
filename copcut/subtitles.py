"""SRT import/export and ASS generation for burning styled text into the export."""
from __future__ import annotations

import re

from PySide6.QtGui import QFont, QFontMetricsF

from .model import TextClip, TextStyle

_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def _t(s: str) -> float:
    m = _TIME.search(s)
    if not m:
        raise ValueError(s)
    h, mi, se, frac = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(se) + int(frac) / (10 ** len(frac))


def read_text_file(path: str) -> str:
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-16", "cp1258", "cp1252"):
        try:
            txt = raw.decode(enc)
            if enc == "utf-16" and not raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                continue
            return txt
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def parse_srt(text: str, style: TextStyle) -> list[TextClip]:
    out = []
    blocks = re.split(r"\r?\n\s*\r?\n", text.replace("\r\n", "\n").strip())
    for b in blocks:
        lines = [l for l in b.split("\n")]
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        a, _, c = lines[idx].partition("-->")
        try:
            t0, t1 = _t(a), _t(c)
        except ValueError:
            continue
        body = "\n".join(lines[idx + 1:]).strip()
        body = re.sub(r"</?[ibu]>|<font[^>]*>|</font>", "", body)
        if body and t1 > t0:
            out.append(TextClip(body, t0, t1 - t0, style.copy()))
    return out


def place_on_source(subs: list[TextClip], main: list, starts: list[float], path: str) -> list[TextClip]:
    """Map subtitles timed against a source file onto every main-track clip cut from that file.

    Handles clip position, trimmed in/out points (lines outside are dropped or clipped)
    and clip speed.
    """
    import bisect
    subs = sorted(subs, key=lambda s: s.start)
    sub_starts = [s.start for s in subs]
    max_len = max((s.duration for s in subs), default=0.0)
    out = []
    for i, c in enumerate(main):
        if c.path != path:
            continue
        lo = bisect.bisect_left(sub_starts, c.src_in - max_len)
        hi = bisect.bisect_right(sub_starts, c.src_out)
        for s in subs[lo:hi]:
            a, b = max(s.start, c.src_in), min(s.end, c.src_out)
            if b - a < 0.05:
                continue
            out.append(TextClip(s.text, starts[i] + (a - c.src_in) / c.speed, (b - a) / c.speed,
                                s.style.copy()))
    return out


def _srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(texts: list[TextClip]) -> str:
    rows = []
    for i, c in enumerate(sorted(texts, key=lambda c: c.start), 1):
        rows.append(f"{i}\n{_srt_time(c.start)} --> {_srt_time(c.end)}\n{c.text}\n")
    return "\n".join(rows)


# ---------------- ASS ----------------

def _ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _ass_color(hex_color: str, alpha: int = 0) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        h = "ffffff"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


_ratio_cache: dict[tuple, float] = {}


def ass_font_size(st: TextStyle) -> float:
    """libass sizes fonts by line height; Qt by em size. Convert Qt pixels -> ASS size."""
    key = (st.font, st.bold, st.italic)
    r = _ratio_cache.get(key)
    if r is None:
        f = QFont(st.font)
        f.setPixelSize(100)
        f.setBold(st.bold)
        f.setItalic(st.italic)
        r = QFontMetricsF(f).height() / 100.0
        r = min(1.6, max(1.0, r))
        _ratio_cache[key] = r
    return st.size * r


def _escape(text: str) -> str:
    text = text.replace("\\", "\\​").replace("{", "｛").replace("}", "｝")
    return text.replace("\r", "").replace("\n", "\\N")


def build_ass(texts: list[TextClip], width: int, height: int, offset: float = 0.0,
              t0: float = 0.0, t1: float = float("inf"), scale: float = 1.0) -> str:
    """Build an ASS script. Events are shifted by -offset; only events overlapping [t0,t1)."""
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Line,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,5,10,10,10,1\n"
        "Style: Box,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,3,8,0,5,10,10,10,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [head]
    for c in texts:
        if c.end <= t0 or c.start >= t1:
            continue
        st = c.style
        alpha = int(round((1.0 - max(0.0, min(1.0, getattr(st, "opacity", 1.0)))) * 255))
        x = st.x * width
        y = st.y * height
        fs = ass_font_size(st) * scale
        if st.bg:
            tags = (f"\\an5\\pos({x:.0f},{y:.0f})\\fn{st.font}\\fs{fs:.1f}\\b{int(st.bold)}"
                    f"\\i{int(st.italic)}\\c&H{_ass_color(st.color)[4:]}&"
                    f"\\3c&H{_ass_color(st.bg_color)[4:]}&\\bord{max(4.0, st.size * 0.18 * scale):.1f}\\shad0")
            style = "Box"
        else:
            tags = (f"\\an5\\pos({x:.0f},{y:.0f})\\fn{st.font}\\fs{fs:.1f}\\b{int(st.bold)}"
                    f"\\i{int(st.italic)}\\c&H{_ass_color(st.color)[4:]}&"
                    f"\\3c&H{_ass_color(st.outline_color)[4:]}&\\bord{st.outline * scale:.1f}\\shad0")
            style = "Line"
        if alpha:
            tags += f"\\1a&H{alpha:02X}&\\3a&H{alpha:02X}&"
        start = max(c.start, t0) - offset
        end = min(c.end, t1) - offset
        layer = getattr(c, "track", 0)     # higher text tracks are drawn on top
        lines.append(f"Dialogue: {layer},{_ass_time(start)},{_ass_time(end)},{style},,0,0,0,,"
                     f"{{{tags}}}{_escape(c.text)}\n")
    return "".join(lines)
