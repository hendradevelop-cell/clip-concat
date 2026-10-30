"""Knob subtitle: preset, override, dan pemecahan baris. Murni, tanpa ffmpeg."""

import pytest
from pydantic import ValidationError

from videoflow.services.media import group_cues
from videoflow.types import (
    CAPTION_FONTS,
    CAPTION_PRESETS,
    CaptionOpts,
    ClipperInput,
    Cue,
    Template,
    Word,
    frame_size,
    resolve_template,
)


def _tpl(**over):
    row = {"id": "t1", "name": "T", "kind": "clipper", "offset_y": 0.5,
           "style": {"fontFamily": "Inter", "fontSize": 0.07, "maxHeight": 0.2}}
    return Template.model_validate({**row, **over})


def test_preset_fonts_actually_resolve_in_concat():
    """Font tak dikenal dirender diam-diam dengan face default, jadi preset tidak
    boleh memakainya. Daftar diverifikasi videoflow.scripts.font_check."""
    for name, preset in CAPTION_PRESETS.items():
        assert preset["fontFamily"] in CAPTION_FONTS, name


def test_preset_fills_only_what_the_client_omitted():
    cap = CaptionOpts.model_validate({"preset": "formal"})
    assert (cap.maxWords, cap.fontSize, cap.offsetY) == (6, 0.062, 0.4)
    assert cap.shadow is False and cap.background == ""

    explicit = CaptionOpts.model_validate({"preset": "formal", "maxWords": 3})
    assert explicit.maxWords == 3 and explicit.fontSize == 0.062


def test_default_preset_starts_at_ten_pixel_letters():
    cap = CaptionOpts()
    assert cap.preset == "default" and cap.enabled is True
    assert cap.fontFamily == "Montserrat" and cap.fontWeight == 800 and cap.maxWords == 2
    assert cap.fontSize == pytest.approx(10 / 1920), "10 piksel pada frame 1080p"
    assert cap.strokeWidth == 0.0005, "garis tepi harus kecil di bawah huruf 10px"


def test_karaoke_preset_carries_a_plate_and_one_word_lines():
    cap = CaptionOpts.model_validate({"preset": "karaoke"})
    assert cap.maxWords == 1 and cap.background == "#000000B3"


def test_resolve_template_keeps_unrelated_style_and_wins_on_controls():
    tpl = resolve_template(_tpl(), CaptionOpts.model_validate({"color": "#123456"}))
    assert tpl.style.color == "#123456"
    assert tpl.style.fontFamily == "Montserrat", "preset mengalahkan template"
    assert tpl.style.maxHeight == 0.2, "field yang tidak disentuh UI dipertahankan"
    assert tpl.offsetY == 0.34


def test_group_cues_uses_word_boundaries():
    cues = [Cue(start=0.0, dur=3.0, text="satu dua tiga empat", words=[
        Word(start=0.0, dur=0.8, text="satu"), Word(start=0.9, dur=0.6, text="dua"),
        Word(start=1.6, dur=0.7, text="tiga"), Word(start=2.4, dur=0.6, text="empat"),
    ])]
    rows = group_cues(cues, 3)
    assert [r.text for r in rows] == ["satu dua tiga", "empat"]
    assert (rows[0].start, rows[0].start + rows[0].dur) == (0.0, 2.3)
    assert (rows[1].start, rows[1].start + rows[1].dur) == (2.4, 3.0)


def test_group_cues_falls_back_to_even_times_without_words():
    """Cue dari cache transkripsi lama tidak punya batas kata."""
    rows = group_cues([Cue(start=1.0, dur=2.0, text="a b c d")], 2)
    assert [r.text for r in rows] == ["a b", "c d"]
    assert [r.start for r in rows] == [1.0, 2.0]


def test_group_cues_keeps_short_lines_intact():
    assert [r.text for r in group_cues([Cue(start=0.0, dur=1.0, text="cuma satu")], 4)] == ["cuma satu"]


def test_input_accepts_caption_and_prompt():
    inp = ClipperInput.model_validate({
        "youtube_url": "https://youtu.be/x", "template_id": "t1",
        "caption": {"preset": "karaoke", "maxWords": 2}, "ai_prompt": "fokus ke bagian harga",
    })
    assert inp.caption.maxWords == 2 and inp.ai_prompt.startswith("fokus")


def test_frame_size_uses_short_edge_for_the_quality_label():
    """480p/720p/1080p = sisi pendek, jadi 9:16 @720p adalah 720x1280."""
    assert frame_size("9:16", "720p") == {"width": 720, "height": 1280, "rateNum": 30, "rateDen": 1}
    assert frame_size("16:9", "720p") == {"width": 1280, "height": 720, "rateNum": 30, "rateDen": 1}
    assert frame_size("9:16", "1080p") == {
        "width": 1080, "height": 1920, "rateNum": 30, "rateDen": 1
    }, "default harus sama dengan ASPECT_PRESETS lama"


def test_frame_sizes_stay_even_and_keep_the_aspect():
    for aspect in ("9:16", "16:9"):
        for q in ("480p", "720p", "1080p"):
            f = frame_size(aspect, q)
            assert f["width"] % 2 == 0 and f["height"] % 2 == 0, (aspect, q)
            ratio = max(f["width"], f["height"]) / min(f["width"], f["height"])
            assert abs(ratio - 16 / 9) < 0.02, (aspect, q, ratio)


def test_quality_defaults_to_1080p_and_rejects_other_values():
    base = {"youtube_url": "https://youtu.be/x", "template_id": "t1"}
    assert ClipperInput.model_validate(base).quality == "1080p"
    assert ClipperInput.model_validate({**base, "quality": "480p"}).quality == "480p"
    with pytest.raises(ValidationError):
        ClipperInput.model_validate({**base, "quality": "2160p"})


@pytest.mark.parametrize("bad", [
    {"caption": {"fontSize": 0.9}},
    {"caption": {"maxWords": 0}},
    {"caption": {"offsetY": 2.0}},
    {"ai_prompt": "x" * 601},
])
def test_out_of_range_controls_are_rejected(bad):
    with pytest.raises(ValidationError):
        ClipperInput.model_validate({"youtube_url": "https://youtu.be/x", "template_id": "t1", **bad})
