"""Analisis sumber YouTube: unduh + transkrip penuh, tanpa render Concat.

Dipakai dashboard untuk menampilkan topik dan prompt siap pakai sebelum job
Clipper dijalankan, dan hasil transkripnya dipakai lagi oleh strategi `prompt`.
"""

import json
from collections import Counter
from pathlib import Path

from ..config import get_settings
from ..progress import Tracker
from ..services import media
from ..types import Analysis, AnalyzeInput, ClipWindow, Cue


def prompt_for(window: ClipWindow) -> str:
    """Prompt clipping dari isi satu jendela: kata khas + rentang waktunya.

    Rentang waktunya ikut ditulis supaya manusia bisa mencocokkannya dengan
    video, tapi pipeline tetap memotong berdasarkan skor kata, bukan angka itu.
    """
    ranked = [t for t, _ in Counter(media.tokens(window.text)).most_common(3)]
    span = media.mmss(window.start)
    return f"bagian tentang {', '.join(ranked)} (mulai {span})"


def build_analysis(
    cues: list[Cue], duration: float, min_s: float, max_s: float, top: int
) -> Analysis:
    weights = media.topic_weights(cues)
    windows = media.rank_windows(cues, weights, min_s, max_s, top)
    return Analysis(
        duration=round(duration, 3),
        transcript=" ".join(c.text for c in cues),
        topics=[t for t, _ in weights.most_common(12)],
        windows=windows,
        prompts=[prompt_for(w) for w in windows],
    )


def source_cache(s, url: str) -> Path:
    """Lokasi cache transkrip satu sumber, dikunci video id — bukan id job.

    Model dan bahasa ikut di nama file karena transkrip `large-v3` tidak boleh
    dibaca sebagai hasil `small`: cache yang salah model akan memberi subtitle
    yang tampak normal tapi isinya dari percobaan lama.
    """
    return s.cues / f"{media.video_id(url)}.{s.whisper_model}.{s.whisper_lang}.json"


def transcribe_source(src: Path, cache: Path, s, on_progress=None) -> list[Cue]:
    """Transkrip sekali per sumber; hasil mentahnya dipakai ulang oleh setiap
    job berikutnya pada video yang sama, jadi Whisper hanya dibayar satu kali."""
    if cache.exists():
        return [Cue.model_validate(x) for x in json.loads(cache.read_text(encoding="utf-8"))]
    cues = media.transcribe(src, s.whisper_lang, s.whisper_model, s.whisper_device, on_progress)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps([q.model_dump() for q in cues]), encoding="utf-8")
    return cues


def run(job_id: str, payload: dict, tracker: Tracker) -> dict:
    inp = AnalyzeInput.model_validate(payload)
    s = get_settings()

    inbox = s.inbox / job_id
    inbox.mkdir(parents=True, exist_ok=True)

    tracker.declare("download", "probe", "transcribe", "rank")
    tracker.start("download")
    src = media.download_youtube(inp.youtube_url, inbox, inp.cookie_file)
    tracker.done("download", src.name)

    tracker.start("probe")
    info = media.probe(src)
    tracker.done("probe", f'{info["duration"]:.0f}s')

    tracker.start("transcribe")
    cues = transcribe_source(src, source_cache(s, inp.youtube_url), s, tracker.asr_progress)
    tracker.done("transcribe", f"{len(cues)} segmen")

    tracker.start("rank")
    analysis = build_analysis(cues, info["duration"], inp.clip_min_s, inp.clip_max_s, inp.top_windows)
    tracker.done("rank", f"{len(analysis.windows)} jendela")
    return analysis.model_dump()
