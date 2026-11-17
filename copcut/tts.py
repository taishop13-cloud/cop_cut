"""Text-to-speech for subtitle lines (Vietnamese voices only).

Two engines:
* "piper" – Ngọc Huyền, Ngọc Huyền Mới, Thiện Tâm: Piper models (.onnx + .onnx.json)
            run offline on the CPU through `piper-tts`. The models are not shipped with
            CopCut; they are read from the folder of the H2DevTool TTS app installed on
            this PC (or any folder set as "tts_model_dir" in CopCut's settings).
* "edge"  – Hoài My, Nam Minh: Microsoft Edge neural voices through `edge-tts`
            (needs internet while generating).

Generated files are kept in %LOCALAPPDATA%/CopCut/tts, named by a hash of
(voice, rate, text), so the same line is never synthesised twice.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QSettings, Signal

from . import ffmpeg_tools as ff

PIPER_VOICES = [
    ("ngochuyen", "Ngọc Huyền (nữ)"),
    ("ngochuyennew", "Ngọc Huyền Mới (nữ)"),
    ("thientam", "Thiện Tâm (nam)"),
]
EDGE_VOICES = [
    ("vi-VN-HoaiMyNeural", "Hoài My (nữ)"),
    ("vi-VN-NamMinhNeural", "Nam Minh (nam)"),
]
DEFAULT_MODEL_DIRS = [
    r"C:\Program Files\H2DevTool TTS\resources\models",
    r"C:\Program Files (x86)\H2DevTool TTS\resources\models",
]
DEFAULT_VOICE = "piper:ngochuyennew"

EDGE_CONCURRENCY = 3
PIPER_WORKERS = max(2, min(8, (os.cpu_count() or 4) - 2))
SENTENCE_PAUSE = 0.15        # seconds of silence between sentences of one subtitle line


def tts_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    p = Path(base) / "CopCut" / "tts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def is_tts_file(path: str) -> bool:
    try:
        return Path(path).resolve().parent == tts_dir().resolve()
    except OSError:
        return False


def _importable(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


def edge_available() -> bool:
    return _importable("edge_tts")


def piper_available() -> bool:
    return _importable("piper") and _importable("onnxruntime")


def engine_package(voice: str) -> tuple[str, bool]:
    """(pip package the voice needs, whether it is installed)."""
    if voice.startswith("piper:"):
        return "piper-tts", piper_available()
    return "edge-tts", edge_available()


def model_dirs() -> list[str]:
    custom = QSettings("CopCut", "CopCut").value("tts_model_dir", "")
    return ([custom] if custom else []) + DEFAULT_MODEL_DIRS


def piper_model(name: str) -> str | None:
    """Path of <name>.onnx (with its .onnx.json next to it), or None."""
    for d in model_dirs():
        p = os.path.join(d, name + ".onnx")
        if os.path.isfile(p) and os.path.isfile(p + ".json"):
            return p
    return None


def all_voices() -> list[tuple[str, str]]:
    """[(voice id, label)]. Voice id is 'piper:<model name>' or 'edge:<name>'."""
    out = [("piper:" + n, lab + "  · ngoại tuyến") for n, lab in PIPER_VOICES if piper_model(n)]
    out += [("edge:" + v, lab + "  · online") for v, lab in EDGE_VOICES]
    return out


_TAG = re.compile(r"<[^>]*>|\{[^}]*\}")


def clean_text(text: str) -> str:
    """Strip SRT/ASS markup and join lines so the voice reads one sentence."""
    t = _TAG.sub("", text).replace("\\N", " ").replace("\\n", " ")
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class TTSJob:
    key: object          # caller's id (TextClip id)
    text: str


@dataclass
class TTSResult:
    key: object
    path: str
    info: ff.MediaInfo


def out_path(voice: str, rate: int, text: str) -> Path:
    h = hashlib.sha1(f"{voice}|{rate}|{text}".encode("utf-8")).hexdigest()[:24]
    ext = ".wav" if voice.startswith("piper:") else ".mp3"
    return tts_dir() / (h + ext)


def _done(p: Path) -> bool:
    try:
        return p.stat().st_size > 256
    except OSError:
        return False


# ---------------------------------------------------------------- piper (offline)
_piper_models: dict[str, object] = {}
_piper_load_lock = threading.Lock()
_phonemize_lock = threading.Lock()      # espeak-ng keeps global state


def _piper_load(path: str):
    with _piper_load_lock:
        m = _piper_models.get(path)
        if m is None:
            import onnxruntime as ort
            from piper import PiperVoice
            m = PiperVoice.load(path, config_path=path + ".json")
            # one core per line, several lines in parallel: faster than letting ONNX split cores
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            m.session = ort.InferenceSession(path, sess_options=opts, providers=["CPUExecutionProvider"])
            _piper_models[path] = m
        return m


def _piper_one(model_path: str, text: str, rate: int, out: Path):
    from piper import SynthesisConfig
    m = _piper_load(model_path)
    with _phonemize_lock:
        sentences = [m.phonemes_to_ids(ph) for ph in m.phonemize(text) if ph]
    cfg = SynthesisConfig(length_scale=1.0 / max(0.3, 1 + rate / 100), normalize_audio=False)
    sr = m.config.sample_rate
    pause = np.zeros(int(SENTENCE_PAUSE * sr), dtype=np.float32)
    parts = []
    for ids in sentences:
        if parts:
            parts.append(pause)
        parts.append(np.asarray(m.phoneme_ids_to_audio(ids, cfg), dtype=np.float32).reshape(-1))
    if not parts:
        raise RuntimeError("không đọc được câu này")
    audio = np.concatenate(parts)
    peak = float(np.abs(audio).max()) or 1.0
    pcm = (np.clip(audio * (0.89 / peak), -1, 1) * 32767).astype(np.int16)
    tmp = out.with_suffix(".part")
    with wave.open(str(tmp), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    os.replace(tmp, out)


def _piper_batch(items: list[tuple[str, Path]], name: str, rate: int, on_done, cancelled):
    model_path = piper_model(name)
    if model_path is None:
        raise RuntimeError(f"Không tìm thấy mô hình giọng “{name}.onnx”. Cần cài H2DevTool TTS "
                           "hoặc đặt thư mục mô hình.")
    _piper_load(model_path)            # load once before the workers start

    def work(item):
        text, path = item
        if cancelled():
            return
        try:
            _piper_one(model_path, text, rate, path)
            on_done(path, None)
        except Exception as e:  # noqa: BLE001
            on_done(path, e)

    with ThreadPoolExecutor(max_workers=PIPER_WORKERS) as ex:
        list(ex.map(work, items))


# ---------------------------------------------------------------- edge (online)
def _edge_batch(items: list[tuple[str, Path]], voice: str, rate: int, on_done, cancelled):
    import edge_tts

    async def one(sem, text, path, tries, report=True):
        async with sem:
            if cancelled():
                return None
            err = None
            for attempt in range(tries):
                if cancelled():
                    break
                tmp = path.with_suffix(".part")
                try:
                    comm = edge_tts.Communicate(text, voice, rate=f"{rate:+d}%")
                    await comm.save(str(tmp))
                    os.replace(tmp, path)
                    err = None
                    break
                except Exception as e:  # noqa: BLE001
                    err = e
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                    if attempt + 1 < tries:
                        await asyncio.sleep(min(8.0, 1.0 * 2 ** attempt))
            if report or err is None:
                on_done(path, err)
            return err

    async def main():
        # the free service throttles bursts ("No audio was received"): a few parallel
        # requests with back-off, then one slow sequential pass for whatever failed
        sem = asyncio.Semaphore(EDGE_CONCURRENCY)
        errs = await asyncio.gather(*(one(sem, t, p, 4, report=False) for t, p in items))
        retry = [(t, p) for (t, p), e in zip(items, errs) if e is not None]
        if retry and not cancelled():
            await asyncio.sleep(3.0)
            one_at_a_time = asyncio.Semaphore(1)
            for t, p in retry:
                await one(one_at_a_time, t, p, 3)

    asyncio.run(main())


# ---------------------------------------------------------------- runner
class TTSRunner(QObject):
    """Synthesises a list of TTSJob in a background thread, then probes every file."""
    progress = Signal(int, int)               # done, total
    finished = Signal(list, list)             # [TTSResult], [error strings]

    def __init__(self, jobs: list[TTSJob], voice: str, rate: int = 0):
        super().__init__()
        self.jobs = jobs
        self.voice = voice
        self.rate = int(rate)
        self._cancel = False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self):
        self._cancel = True

    def _run(self):
        errors: list[str] = []
        results: list[TTSResult] = []
        try:
            engine, voice = self.voice.split(":", 1)
            paths = {j.key: out_path(self.voice, self.rate, j.text) for j in self.jobs}
            todo: dict[Path, str] = {}
            for j in self.jobs:
                p = paths[j.key]
                if not _done(p):
                    todo[p] = j.text
            total = len(todo) + len(self.jobs)      # synthesis + probing
            done = [0]
            lock = threading.Lock()
            failed: dict[Path, str] = {}

            def on_done(path, err):
                with lock:
                    done[0] += 1
                    if err is not None:
                        failed[path] = str(err)
                self.progress.emit(done[0], total)

            items = list((t, p) for p, t in todo.items())
            if items:
                pkg, ok = engine_package(self.voice)
                if not ok:
                    raise RuntimeError(f"Chưa cài gói {pkg}.")
                batch = _piper_batch if engine == "piper" else _edge_batch
                batch(items, voice, self.rate, on_done, lambda: self._cancel)
            if self._cancel:
                self.finished.emit([], ["Đã hủy."])
                return
            for p, msg in list(failed.items())[:5]:
                errors.append(f"“{todo[p][:40]}”: {msg}")
            if len(failed) > 5:
                errors.append(f"… và {len(failed) - 5} câu khác")

            def probe(job):
                p = paths[job.key]
                if p in failed or not _done(p):
                    return None
                try:
                    return TTSResult(job.key, str(p), ff.probe(str(p)))
                except Exception as e:  # noqa: BLE001
                    with lock:
                        errors.append(f"“{job.text[:40]}”: {e}")
                    return None

            with ThreadPoolExecutor(max_workers=6) as ex:
                for r in ex.map(probe, self.jobs):
                    with lock:
                        done[0] += 1
                    self.progress.emit(done[0], total)
                    if r is not None and r.info.duration > 0:
                        results.append(r)
                    if self._cancel:
                        break
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
        self.finished.emit(results, errors)
