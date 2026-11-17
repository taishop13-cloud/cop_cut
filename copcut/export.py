"""Turn a Project into FFmpeg jobs.

* Each main-track clip becomes its own input with fast input seeking
  (`-ss in -t len -i file`), so an 8-hour source is never decoded from the start
  and nothing is buffered between segments.
* Projects with many clips are rendered in chunks (video in the final codec,
  audio as FLAC) and joined with the concat demuxer, then music is mixed in a
  final pass that copies the video stream.
* "Fast export" (stream copy) is offered when nothing needs re-encoding.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from . import ffmpeg_tools as ff
from .model import Clip, ImageClip, Project
from .overlays import blur_radius
from .subtitles import build_ass

CHUNK_CLIPS = 24


@dataclass
class ExportSettings:
    out_path: str
    width: int
    height: int
    fps: float
    encoder: str = "libx264"
    quality: str = "high"       # high | medium | low
    audio_kbps: int = 192
    fast_copy: bool = False


@dataclass
class Job:
    args: list[str]
    duration: float             # seconds of output this job produces (for progress)
    weight: float               # share of total progress
    label: str


def atempo_chain(speed: float) -> list[str]:
    parts = []
    s = speed
    while s > 2.0:
        parts.append(2.0)
        s /= 2.0
    while s < 0.5:
        parts.append(0.5)
        s /= 0.5
    if abs(s - 1.0) > 1e-6 or not parts:
        parts.append(s)
    return [f"atempo={p:.6f}" for p in parts if abs(p - 1.0) > 1e-6]


def video_codec_args(st: ExportSettings) -> list[str]:
    q = {"high": 0, "medium": 1, "low": 2}.get(st.quality, 0)
    if st.encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str((19, 24, 29)[q]),
                "-b:v", "0", "-pix_fmt", "yuv420p"]
    if st.encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", str((20, 25, 30)[q]),
                "-pix_fmt", "nv12"]
    if st.encoder == "h264_amf":
        qp = str((20, 25, 30)[q])
        return ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp",
                "-qp_i", qp, "-qp_p", qp, "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str((18, 23, 28)[q]),
            "-pix_fmt", "yuv420p"]


def can_fast_copy(p: Project) -> tuple[bool, str]:
    if not p.main:
        return False, "Chưa có video trên timeline."
    if p.texts:
        return False, "Có phụ đề/chữ (cần mã hóa lại để in chữ lên hình)."
    if p.blurs:
        return False, "Có vùng che mờ hoặc logo (cần mã hóa lại)."
    if p.audio:
        return False, "Có nhạc nền trên rãnh âm thanh."
    if any(abs(c.speed - 1) > 1e-6 or abs(c.volume - 1) > 1e-6 for c in p.main):
        return False, "Có clip đổi tốc độ hoặc âm lượng."
    sig = {(p.media[c.path].vcodec, p.media[c.path].width, p.media[c.path].height,
            p.media[c.path].acodec) for c in p.main}
    if len(sig) > 1:
        return False, "Các clip có định dạng khác nhau."
    return True, ""


def _q(path: str) -> str:
    return "'" + path.replace("\\", "/").replace("'", "'\\''") + "'"


def _blur_filters(p: Project, st: ExportSettings, offset: float, seg_dur: float,
                  label: str, logos: dict | None = None) -> tuple[list[str], str]:
    """Chain one crop+boxblur+overlay per blur box overlapping [offset, offset+seg_dur),
    then one overlay per image (logo) so logos sit on top of blurred areas."""
    W, H = st.width, st.height
    out, cur = [], label
    items = p.blurs_in(offset, offset + seg_dur)
    for k, b in enumerate(items):
        if isinstance(b, ImageClip):
            continue
        x0 = max(0, int(round(b.x * W)))
        y0 = max(0, int(round(b.y * H)))
        x1 = min(W, int(round((b.x + b.w) * W)))
        y1 = min(H, int(round((b.y + b.h) * H)))
        x0 -= x0 % 2
        y0 -= y0 % 2
        w = (x1 - x0) // 2 * 2
        h = (y1 - y0) // 2 * 2
        if w < 8 or h < 8:
            continue
        lr = int(max(1, min(blur_radius(b.strength, H), min(w, h) / 2 - 1)))
        cr = int(max(1, min(lr / 2, min(w, h) / 4 - 1)))
        t0 = max(b.start, offset) - offset
        t1 = min(b.end, offset + seg_dur) - offset
        en = f"enable='between(t,{t0:.4f},{t1:.4f})'"
        nxt = f"bl{k}"
        out.append(f"[{cur}]split[{nxt}m][{nxt}c]")
        out.append(f"[{nxt}c]crop={w}:{h}:{x0}:{y0},boxblur=lr={lr}:lp=2:cr={cr}:cp=2:{en}[{nxt}b]")
        out.append(f"[{nxt}m][{nxt}b]overlay={x0}:{y0}:{en}[{nxt}]")
        cur = nxt
    for k, b in enumerate(items):
        if not isinstance(b, ImageClip) or b.opacity <= 0:
            continue
        src = (logos or {}).get(b.id, b.path)     # round-frame logos are pre-rendered PNGs
        if not os.path.isfile(src):
            continue
        w = max(2, int(round(b.w * W)))
        h = max(2, int(round(b.h * H)))
        x = int(round(b.x * W))
        y = int(round(b.y * H))
        t0 = max(b.start, offset) - offset
        t1 = min(b.end, offset + seg_dur) - offset
        chain = f"movie=filename={_fesc(src)},scale={w}:{h}:flags=lanczos,format=rgba"
        if b.opacity < 0.999:
            chain += f",colorchannelmixer=aa={b.opacity:.3f}"
        nxt = f"lg{k}"
        out.append(f"{chain}[{nxt}i]")
        out.append(f"[{cur}][{nxt}i]overlay={x}:{y}:eof_action=repeat:format=auto:"
                   f"enable='between(t,{t0:.4f},{t1:.4f})',format=yuv420p[{nxt}]")
        cur = nxt
    return out, cur


def _segment_graph(p: Project, clips: list[Clip], st: ExportSettings, first_input: int,
                   subs_file: str | None, offset: float,
                   logos: dict | None = None) -> tuple[list[str], str, str, str]:
    """Return (input args, filter_complex, video label, audio label)."""
    inputs, fl, cat = [], [], []
    W, H, fps = st.width, st.height, st.fps
    for k, c in enumerate(clips):
        idx = first_input + k
        src_len = c.src_out - c.src_in
        dur = c.duration
        inputs += ["-ss", f"{c.src_in:.6f}", "-t", f"{src_len:.6f}", "-i", c.path]
        fl.append(f"[{idx}:v]setpts=(PTS-STARTPTS)/{c.speed:.6f},fps={fps:.6f},"
                  f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,format=yuv420p,"
                  f"trim=duration={dur:.6f},setpts=PTS-STARTPTS[v{k}]")
        info = p.media.get(c.path)
        if info and info.has_audio and c.volume > 0:
            chain = ["asetpts=PTS-STARTPTS"] + atempo_chain(c.speed)
            if abs(c.volume - 1) > 1e-6:
                chain.append(f"volume={c.volume:.4f}")
            chain += ["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo",
                      f"atrim=duration={dur:.6f}"]
            fl.append(f"[{idx}:a]{','.join(chain)}[a{k}]")
        else:
            fl.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={dur:.6f},"
                      f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{k}]")
        cat.append(f"[v{k}][a{k}]")
    fl.append(f"{''.join(cat)}concat=n={len(clips)}:v=1:a=1[vcat][acat]")
    blur_f, vlab = _blur_filters(p, st, offset, sum(c.duration for c in clips), "vcat", logos)
    fl += blur_f
    if subs_file:
        if offset:
            fl.append(f"[{vlab}]setpts=PTS+{offset:.6f}/TB,subtitles={subs_file},"
                      f"setpts=PTS-STARTPTS[vsub]")
        else:
            fl.append(f"[{vlab}]subtitles={subs_file}[vsub]")
        vlab = "vsub"
    return inputs, ";".join(fl), vlab, "acat"


MUSIC_DIRECT = 12      # up to this many audio clips: one ffmpeg input each


def _fesc(path: str) -> str:
    """Escape a file name for use as a filter option value inside a filtergraph."""
    def esc(s, chars):
        return "".join("\\" + ch if ch in chars else ch for ch in s)
    return esc(esc(path.replace("\\", "/"), "\\':"), "\\'[],;")


def _music_lanes(music: list[Clip]) -> list[list[Clip]]:
    """Group clips into lanes with no overlap (normally one lane per audio track)."""
    lanes: list[list[Clip]] = []
    for tr in sorted({c.track for c in music}):
        sub: list[list[Clip]] = []
        for c in sorted((c for c in music if c.track == tr), key=lambda c: c.start):
            for lane in sub:
                last = lane[-1]
                if last.start + last.duration <= c.start + 1e-6:
                    lane.append(c)
                    break
            else:
                sub.append([c])
        lanes += sub
    return lanes


def _music_graph_lanes(music: list[Clip], main_audio: str, end: float) -> tuple[list[str], str, str]:
    """Many clips (e.g. one per subtitle line from text-to-speech): instead of one ffmpeg
    input + adelay per clip (which overflows the Windows command line and makes amix
    crawl through hours of silence per input), each lane is a single concatenated stream
    of silence gaps and `amovie` sources, all inside the filter script."""
    fmt = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    fl, labs = [], []
    n = 0
    for li, lane in enumerate(_music_lanes(music)):
        segs, t = [], 0.0
        for c in lane:
            s = max(c.start, t)
            dur = min(c.start + c.duration, end) - s
            if dur <= 1e-3:
                continue
            if s - t > 1e-3:
                lab = f"g{n}"
                n += 1
                fl.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={s - t:.6f},{fmt}[{lab}]")
                segs.append(f"[{lab}]")
            skip = (s - c.start) * c.speed                 # source time cut off by an overlap
            src_in = c.src_in + skip
            chain = [f"amovie=filename={_fesc(c.path)}" + (f":seek_point={src_in:.6f}" if src_in > 0.01 else ""),
                     f"atrim=duration={c.src_out - src_in:.6f}", "asetpts=PTS-STARTPTS"]
            chain += atempo_chain(c.speed)
            if abs(c.volume - 1) > 1e-6:
                chain.append(f"volume={c.volume:.4f}")
            chain += [fmt, f"apad=whole_dur={dur:.6f}", f"atrim=duration={dur:.6f}"]
            lab = f"s{n}"
            n += 1
            fl.append(",".join(chain) + f"[{lab}]")
            segs.append(f"[{lab}]")
            t = s + dur
        if not segs:
            continue
        if len(segs) == 1:
            labs.append(segs[0])
            continue
        fl.append(f"{''.join(segs)}concat=n={len(segs)}:v=0:a=1[ln{li}]")
        labs.append(f"[ln{li}]")
    if not labs:
        return [], "", main_audio
    fl.append(f"[{main_audio}]{''.join(labs)}amix=inputs={len(labs) + 1}:duration=first:"
              f"normalize=0:dropout_transition=0[amix]")
    return [], ";".join(fl), "amix"


def _music_graph(p: Project, first_input: int, main_audio: str, end: float) -> tuple[list[str], str, str]:
    music = [c for c in p.audio if c.start < end and c.volume > 0]
    if not music:
        return [], "", main_audio
    if len(music) > MUSIC_DIRECT:
        return _music_graph_lanes(music, main_audio, end)
    inputs, fl, labs = [], [], []
    for k, c in enumerate(music):
        idx = first_input + k
        inputs += ["-ss", f"{c.src_in:.6f}", "-t", f"{c.src_out - c.src_in:.6f}", "-i", c.path]
        chain = ["asetpts=PTS-STARTPTS"] + atempo_chain(c.speed)
        if abs(c.volume - 1) > 1e-6:
            chain.append(f"volume={c.volume:.4f}")
        ms = int(round(c.start * 1000))
        chain += ["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo",
                  f"adelay={ms}:all=1"]
        fl.append(f"[{idx}:a]{','.join(chain)}[m{k}]")
        labs.append(f"[m{k}]")
    fl.append(f"[{main_audio}]{''.join(labs)}amix=inputs={len(labs) + 1}:duration=first:"
              f"normalize=0:dropout_transition=0[amix]")
    return inputs, ";".join(fl), "amix"


def _render_logos(p: Project, tmp: Path) -> dict[int, str]:
    """Save each round-frame logo (cropped + circle mask, same as the preview) as a PNG."""
    from .overlays import logo_image
    out, done = {}, {}
    for b in p.blurs:
        if not isinstance(b, ImageClip) or not b.circle or not os.path.isfile(b.path):
            continue
        f = done.get(b.path)
        if f is None:
            img = logo_image(b.path, True)
            if img.isNull():
                continue
            f = str(tmp / f"logo{len(done)}.png")
            img.save(f, "PNG")
            done[b.path] = f
        out[b.id] = f
    return out


def build_jobs(p: Project, st: ExportSettings, tmp: Path) -> list[Job]:
    if not ff.FFMPEG:
        raise RuntimeError("Không tìm thấy ffmpeg.")
    total = p.main_end()
    base = [ff.FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error", "-progress", "pipe:1"]
    vargs = video_codec_args(st)
    aargs = ["-c:a", "aac", "-b:a", f"{st.audio_kbps}k"]

    if st.fast_copy:
        lst = tmp / "list.txt"
        rows = ["ffconcat version 1.0"]
        for c in p.main:
            rows += [f"file {_q(c.path)}", f"inpoint {c.src_in:.6f}", f"outpoint {c.src_out:.6f}"]
        lst.write_text("\n".join(rows) + "\n", encoding="utf-8")
        args = base + ["-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0:v:0",
                       "-map", "0:a:0?", "-c", "copy", "-avoid_negative_ts", "make_zero", st.out_path]
        return [Job(args, total, 1.0, "Xuất nhanh (không mã hóa lại)")]

    scale = st.height / p.height if p.height else 1.0
    subs = None
    if p.texts:
        (tmp / "subs.ass").write_text(
            build_ass(p.texts, st.width, st.height, scale=scale, t1=total), encoding="utf-8")
        subs = "subs.ass"

    logos = _render_logos(p, tmp)

    if len(p.main) <= CHUNK_CLIPS:
        inp, graph, vl, al = _segment_graph(p, p.main, st, 0, subs, 0.0, logos)
        minp, mgraph, al = _music_graph(p, len(p.main), al, total)
        if mgraph:
            graph += ";" + mgraph
        gfile = tmp / "graph.txt"
        gfile.write_text(graph, encoding="utf-8")
        args = (base + inp + minp + ff.filter_script_args("graph.txt") + ["-map", f"[{vl}]", "-map", f"[{al}]"]
                + vargs + aargs + ["-r", f"{st.fps:.6f}", "-t", f"{total:.6f}", st.out_path])
        return [Job(args, total, 1.0, "Đang xuất video")]

    # ---- chunked render ----
    jobs, files = [], []
    starts = p.starts()
    chunk_count = (len(p.main) + CHUNK_CLIPS - 1) // CHUNK_CLIPS
    for ci in range(chunk_count):
        a, b = ci * CHUNK_CLIPS, min(len(p.main), (ci + 1) * CHUNK_CLIPS)
        clips = p.main[a:b]
        offset = starts[a]
        dur = sum(c.duration for c in clips)
        inp, graph, vl, al = _segment_graph(p, clips, st, 0, subs, offset, logos)
        gname = f"graph{ci}.txt"
        (tmp / gname).write_text(graph, encoding="utf-8")
        out = tmp / f"part{ci:04d}.mkv"
        files.append(out)
        args = (base + inp + ff.filter_script_args(gname) + ["-map", f"[{vl}]", "-map", f"[{al}]"]
                + vargs + ["-c:a", "flac", "-r", f"{st.fps:.6f}", "-t", f"{dur:.6f}", str(out)])
        jobs.append(Job(args, dur, 0.92 * dur / total, f"Đang xuất phần {ci + 1}/{chunk_count}"))
    lst = tmp / "parts.txt"
    lst.write_text("ffconcat version 1.0\n" + "".join(f"file {_q(str(f))}\n" for f in files),
                   encoding="utf-8")
    minp, mgraph, al = _music_graph(p, 1, "0:a", total)
    args = base + ["-f", "concat", "-safe", "0", "-i", str(lst)] + minp
    if mgraph:
        (tmp / "mix.txt").write_text(mgraph, encoding="utf-8")
        args += ff.filter_script_args("mix.txt") + ["-map", "0:v", "-map", f"[{al}]"]
    else:
        args += ["-map", "0:v", "-map", "0:a"]
    args += ["-c:v", "copy"] + aargs + ["-t", f"{total:.6f}", st.out_path]
    jobs.append(Job(args, total, 0.08, "Đang ghép các phần"))
    return jobs


def fontconfig_env(tmp: Path) -> dict:
    """libass in Windows FFmpeg builds uses fontconfig; point it at the Windows font folders."""
    env = dict(os.environ)
    if ff.IS_WIN:
        win = os.environ.get("WINDIR", r"C:\Windows")
        user = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts")
        cache = tmp / "fccache"
        conf = tmp / "fonts.conf"
        conf.write_text(
            "<?xml version=\"1.0\"?><!DOCTYPE fontconfig SYSTEM \"fonts.dtd\"><fontconfig>"
            f"<dir>{win}\\Fonts</dir><dir>{user}</dir><cachedir>{cache}</cachedir>"
            "</fontconfig>", encoding="utf-8")
        env["FONTCONFIG_FILE"] = str(conf)
    return env


class ExportRunner(QObject):
    progress = Signal(float, str)
    finished = Signal(bool, str)

    def __init__(self, project: Project, settings: ExportSettings):
        super().__init__()
        self.p = project
        self.st = settings
        self._proc: subprocess.Popen | None = None
        self._cancel = False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self):
        self._cancel = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass

    def _run(self):
        try:
            with tempfile.TemporaryDirectory(prefix="copcut_") as td:
                tmp = Path(td)
                jobs = build_jobs(self.p, self.st, tmp)
                env = fontconfig_env(tmp)
                done = 0.0
                for job in jobs:
                    if self._cancel:
                        break
                    ok, err = self._run_job(job, tmp, env, done)
                    if not ok:
                        if self._cancel:
                            break
                        self._cleanup_output()
                        self.finished.emit(False, err)
                        return
                    done += job.weight
            if self._cancel:
                self._cleanup_output()
                self.finished.emit(False, "Đã hủy.")
            else:
                self.progress.emit(1.0, "Hoàn tất")
                self.finished.emit(True, self.st.out_path)
        except Exception as e:  # noqa: BLE001
            self.finished.emit(False, str(e))

    def _cleanup_output(self):
        try:
            if os.path.exists(self.st.out_path):
                os.remove(self.st.out_path)
        except OSError:
            pass

    def _run_job(self, job: Job, tmp: Path, env: dict, done: float) -> tuple[bool, str]:
        self._proc = ff.popen(job.args, cwd=str(tmp), env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        errs: list[str] = []

        def read_err():
            for line in self._proc.stderr:
                errs.append(line)
                del errs[:-40]

        threading.Thread(target=read_err, daemon=True).start()
        speed = ""
        for line in self._proc.stdout:
            k, _, v = line.strip().partition("=")
            if k == "out_time_us" and v.lstrip("-").isdigit() and job.duration > 0:
                frac = min(1.0, max(0.0, int(v) / 1e6 / job.duration))
                self.progress.emit(done + job.weight * frac, f"{job.label}  {speed}")
            elif k == "speed":
                speed = f"({v.strip()})" if v.strip() not in ("N/A", "") else ""
        code = self._proc.wait()
        if code != 0:
            msg = "".join(errs).strip()
            msg = re.sub(r"\s+\n", "\n", msg)[-1500:]
            return False, msg or f"ffmpeg thoát với mã {code}"
        return True, ""
