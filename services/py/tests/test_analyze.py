"""Penilaian transkrip: topik, jendela terbaik, dan prompt siap pakai. Murni."""

import pytest

from videoflow.pipelines.analyze import (
    build_analysis,
    prompt_for,
    source_cache,
    transcribe_source,
)
from videoflow.services import media
from videoflow.types import AnalyzeInput, ClipperInput, ClipWindow, Cue


def _cues(*texts, step: float = 5.0):
    """Satu cue per teks, 4 detik panjangnya, berjarak `step` detik."""
    return [Cue(start=i * step, dur=4.0, text=t) for i, t in enumerate(texts)]


FILLER = "ya jadi menurut saya itu hal yang paling biasa saja sih"
HOT = "harga beras naik lagi di pasar tradisional minggu ini"
COLD = "pembahasan umum lainnya tentang kegiatan harian biasa"


def test_stopwords_do_not_become_topics():
    weights = media.topic_weights(_cues(FILLER, FILLER, FILLER, HOT))
    assert "beras" in weights
    for filler in ("yang", "saya", "itu", "paling", "menurut"):
        assert filler not in weights


def test_prompt_terms_outrank_generic_topic_density():
    assert media.prompt_weights("bahas harga beras")["beras"] == 3
    assert media.prompt_weights("kucing tanpa tanda baca!!")["kucing"] == 3
    assert media.prompt_weights("yang dan itu") == {}


def test_rank_windows_prefers_the_window_that_says_the_thing():
    cues = _cues(FILLER, FILLER, HOT, COLD, COLD, COLD, COLD)
    windows = media.rank_windows(cues, media.prompt_weights("harga beras"), 4, 12, 2)
    assert windows, "jendela dengan kata kunci harus terpilih"
    assert "beras" in windows[0].text
    assert windows[0].start == 10.0, "titik potong di awal cue, bukan di tengah kalimat"


def test_rank_windows_never_overlaps_and_keeps_chronological_order():
    cues = _cues(HOT, COLD, HOT, COLD, HOT, COLD, HOT)
    windows = media.rank_windows(cues, media.prompt_weights("harga beras"), 4, 6, 4)
    spans = [(w.start, w.start + w.dur) for w in windows]
    assert spans == sorted(spans)
    for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
        assert a1 <= b0, f"jendela tumpang tindih: {a1} > {b0}"


def test_rank_windows_empty_when_nothing_matches():
    cues = _cues(COLD, COLD)
    assert media.rank_windows(cues, media.prompt_weights("kucing tisu"), 4, 6, 3) == []
    assert media.rank_windows([], media.topic_weights(cues), 4, 6, 3) == []
    assert media.rank_windows(cues, media.topic_weights([]), 4, 6, 3) == []


def test_cues_in_window_rebases_time_to_the_clip():
    cues = _cues("satu dua", "tiga empat", "lima enam")  # 0-4, 5-9, 10-14
    sub = media.cues_in_window(cues, 5.0, 9.0)
    assert [c.text for c in sub] == ["tiga empat", "lima enam"]
    assert [(c.start, c.dur) for c in sub] == [(0.0, 4.0), (5.0, 4.0)]
    assert all(c.start + c.dur <= 9.0 for c in sub)


def test_build_analysis_returns_topics_windows_and_prompts():
    cues = _cues(HOT, FILLER, HOT, COLD, "harga beras premium juga naik")
    a = build_analysis(cues, duration=25.0, min_s=4.0, max_s=10.0, top=3)
    assert a.duration == 25.0
    assert "beras" in a.topics
    assert a.windows and all(w.dur >= 4.0 for w in a.windows)
    assert a.prompts and len(a.prompts) == len(a.windows)
    assert a.transcript.startswith(HOT)


def test_prompt_for_carries_topics_and_a_timestamp():
    p = prompt_for(ClipWindow(start=65.0, dur=8.0, score=1.0, text=HOT))
    assert "beras" in p and "1:05" in p


def test_analyze_input_defaults():
    inp = AnalyzeInput.model_validate({"youtube_url": "https://youtu.be/x"})
    assert (inp.clip_min_s, inp.clip_max_s, inp.top_windows) == (30.0, 90.0, 6)


def test_prompt_strategy_requires_a_prompt():
    base = {"youtube_url": "https://youtu.be/x", "template_id": "t1"}
    assert ClipperInput.model_validate({**base, "strategy": "auto"}).strategy == "auto"
    ok = ClipperInput.model_validate({**base, "strategy": "prompt", "ai_prompt": "harga beras"})
    assert ok.strategy == "prompt"
    with pytest.raises(ValueError, match="ai_prompt"):
        ClipperInput.model_validate({**base, "strategy": "prompt", "ai_prompt": "   "})


class _S:
    """Settings tiruan: hanya field yang dibaca `source_cache` / `transcribe_source`."""

    def __init__(self, cues):
        from pathlib import Path

        self.cues = Path(cues)
        self.whisper_model = "small"
        self.whisper_lang = "id"
        self.whisper_device = "cpu"


def test_source_cache_names_the_model_it_came_from(tmp_path):
    p = source_cache(_S(tmp_path), "https://youtu.be/4MfclxIU1rc")
    assert p.name == "4MfclxIU1rc.small.id.json", "model + bahasa ikut di nama file"


def test_transcribe_source_pays_whisper_once_per_video(tmp_path, monkeypatch):
    from pathlib import Path

    calls = []
    monkeypatch.setattr(media, "transcribe", lambda *a, **k: calls.append(1) or _cues(HOT))

    s = _S(tmp_path)
    cache = source_cache(s, "https://youtu.be/4MfclxIU1rc")
    first = transcribe_source(Path("source.mp4"), cache, s)
    second = transcribe_source(Path("source.mp4"), cache, s)

    assert len(calls) == 1, "job kedua pada video yang sama tidak boleh ASR ulang"
    assert [c.text for c in first] == [c.text for c in second]
