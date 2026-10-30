import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from videoflow.concat import Concat, ConcatError, find_binary
from videoflow.services import media


@pytest.fixture(scope="module")
def sample(tmp_path_factory) -> Path:
    """Video 6 detik + audio sine, dibuat ffmpeg betulan."""
    p = tmp_path_factory.mktemp("media") / "src.mp4"
    media.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(p),
    ])
    return p


def test_probe_reads_real_metadata(sample):
    info = media.probe(sample)
    assert info["width"] == 1920 and info["height"] == 1080
    assert 5.5 < info["duration"] < 6.5
    assert info["fps_num"] == 30
    assert info["has_audio"] is True


def test_cut_produces_requested_duration(sample, tmp_path):
    spec = media.ClipSpec(index=0, start=1.0, dur=2.0)
    out = media.cut(sample, spec, tmp_path / "c.mp4")
    assert abs(media.probe(out)["duration"] - 2.0) < 0.2


def test_cut_starts_at_requested_offset(sample, tmp_path):
    """Potongan mulai di 3.0s harus berisi frame berbeda dengan potongan 0.0s,
    dan durasi tidak boleh meluber ke klip berikutnya."""
    a = media.cut(sample, media.ClipSpec(index=0, start=0.0, dur=2.0), tmp_path / "a.mp4")
    b = media.cut(sample, media.ClipSpec(index=1, start=3.0, dur=2.0), tmp_path / "b.mp4")
    assert a.read_bytes() != b.read_bytes()
    assert abs(media.probe(b)["duration"] - 2.0) < 0.2


def test_cut_is_idempotent(sample, tmp_path):
    """Retry job tidak boleh memotong ulang file yang sudah ada."""
    dest = tmp_path / "c.mp4"
    spec = media.ClipSpec(index=0, start=0.0, dur=1.0)
    first = media.cut(sample, spec, dest)
    mtime = dest.stat().st_mtime_ns
    assert media.cut(sample, spec, dest) == first
    assert dest.stat().st_mtime_ns == mtime


def test_plan_clips_even_spread():
    specs = media.plan_clips(600.0, 3, 30, 90, "even")
    assert len(specs) == 3
    assert all(30 <= s.dur <= 90 for s in specs)
    assert specs[0].start == 0.0
    assert max(s.end for s in specs) <= 600.0
    assert [s.start for s in specs] == sorted(s.start for s in specs)


def test_plan_clips_auto_skips_intro_and_outro():
    specs = media.plan_clips(600.0, 3, 30, 90, "auto")
    assert specs[0].start >= 0.05 * 600 - 1
    assert specs[-1].end <= 0.95 * 600 + 1


def test_plan_clips_short_source_falls_back_to_full_window():
    """Video 20 detik: window 5-95% cuma 18dtk < min_s, harus kembali ke penuh."""
    specs = media.plan_clips(20.0, 1, 30, 90, "auto")
    assert specs[0].start == 0.0
    assert specs[0].dur <= 20.0


def test_plan_clips_respects_max():
    specs = media.plan_clips(3600.0, 4, 30, 90, "even")
    assert all(s.dur <= 90.0 for s in specs)


# ---------- transpor MCP: framing + unwrap tanpa concat.exe ----------


class Peer:
    """stdin/stdout/stderr palsu untuk `_request`/`_recv`."""

    def __init__(self, lines: list[str]):
        self.stdout = io.StringIO("".join(l if l.endswith("\n") else l + "\n" for l in lines))
        self.stdin = io.StringIO()
        self.stderr = io.StringIO("")
        self.returncode = None

    def poll(self):
        return self.returncode


def client(lines: list[str], tools=("concat_version", "media_import")) -> Concat:
    c = Concat.__new__(Concat)
    c._p, c._id, c._stderr, c._dead, c.timeout = Peer(lines), 0, [], False, 5.0
    c.tools = {t: {} for t in tools}
    return c


def rpc(result) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": result})


def test_call_unwraps_structured_content():
    c = client([rpc({"structuredContent": {"createdId": "m1"}})])
    assert c.call("media_import", path="/p", file="/a.mp4") == {"createdId": "m1"}


def test_call_falls_back_to_text_content():
    c = client([rpc({"content": [{"type": "text", "text": '{"job": "j1"}'}]})])
    assert c.call("media_import", path="/p") == {"job": "j1"}


def test_tool_error_becomes_concat_error():
    c = client([rpc({"isError": True, "content": [{"type": "text", "text": "no video stream"}]})])
    with pytest.raises(ConcatError) as exc:
        c.call("media_import", path="/p")
    assert exc.value.tool == "media_import"
    assert "no video stream" in str(exc.value)


def test_jsonrpc_error_keeps_code():
    c = client([json.dumps({"jsonrpc": "2.0", "id": 1,
                            "error": {"code": -32603, "message": "busy"}})])
    with pytest.raises(ConcatError) as exc:
        c.call("media_import", path="/p")
    assert exc.value.code == -32603
    assert "busy" in str(exc.value)


def test_notification_before_reply_is_skipped():
    """Event yang menyusup di depan balasan tidak boleh dianggap jawaban."""
    c = client([
        json.dumps({"jsonrpc": "2.0", "method": "notifications/progress",
                    "params": {"frame": 1}}),
        rpc({"structuredContent": {"state": "done"}}),
    ])
    assert c.call("concat_version") == {"state": "done"}


def test_content_length_framing_is_supported():
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "result": {"structuredContent": {"ok": True}}})
    c = client([f"Content-Length: {len(body)}", "", body])
    assert c.call("concat_version") == {"ok": True}


def test_unknown_tool_is_refused_before_sending():
    c = client([rpc({})], tools=("concat_version",))
    with pytest.raises(ConcatError, match="tidak punya tool media_import"):
        c.call("media_import", path="/p")
    assert c._p.stdin.getvalue() == "", "tidak ada yang boleh dikirim"


def test_dead_child_is_reported_with_stderr():
    c = client([])
    c._p.returncode = 1
    c._stderr = ["panic: GPU hilang"]
    with pytest.raises(ConcatError) as exc:
        c.call("concat_version")
    assert "panic: GPU hilang" in str(exc.value)
    assert "exit 1" in str(exc.value)


def test_find_binary_honours_env(tmp_path, monkeypatch):
    fake_bin = tmp_path / "concat.exe"
    fake_bin.write_bytes(b"")
    monkeypatch.setenv("VF_CONCAT_BIN", str(fake_bin))
    assert find_binary() == str(fake_bin)


def test_find_binary_raises_when_missing(tmp_path, monkeypatch):
    """Candidate umum (LOCALAPPDATA/Program Files/PATH) disempitkan ke tmp."""
    monkeypatch.setenv("VF_CONCAT_BIN", str(tmp_path / "none.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ConcatError, match="VF_CONCAT_BIN"):
        find_binary()


def test_template_validates_from_db_row():
    """Kolom jsonb `style` + kolom `offset_y` harus masuk model tanpa konversi manual."""
    from videoflow.types import Template

    row = {
        "id": "bold-yellow-1", "name": "Bold Kuning", "kind": "clipper",
        "offset_y": 0.30,
        "style": {"fontFamily": "Inter", "fontSize": 0.085, "color": "#FFF"},
    }
    t = Template.model_validate(row)
    assert t.offsetY == 0.30
    assert t.style.model_dump(exclude_none=True) == {
        "fontFamily": "Inter", "fontSize": 0.085, "color": "#FFF"
    }


def test_template_default_offset_is_below_centre():
    from videoflow.types import Template

    t = Template.model_validate({
        "id": "x", "name": "x", "kind": "clipper", "style": {},
    })
    assert t.offsetY == pytest.approx(0.35)


def test_video_id_is_the_cache_key_not_the_job():
    """Cache transkrip dikunci video id supaya job berikutnya di video yang
    sama tidak perlu Whisper lagi."""
    assert media.video_id("https://www.youtube.com/watch?v=4MfclxIU1rc") == "4MfclxIU1rc"
    assert media.video_id("https://youtu.be/4MfclxIU1rc?t=12") == "4MfclxIU1rc"
    assert media.video_id("https://www.youtube.com/shorts/4MfclxIU1rc") == "4MfclxIU1rc"
    assert media.video_id("https://www.youtube.com/embed/4MfclxIU1rc") == "4MfclxIU1rc"
    assert media.video_id("https://vimeo.com/12345") == media.video_id("https://vimeo.com/12345")
    assert media.video_id("https://vimeo.com/12345") != media.video_id("https://vimeo.com/99999")


class _Seg:
    """Segmen tiruan berbentuk seperti keluaran faster-whisper."""

    def __init__(self, i: int, dur: float = 2.0):
        self.start, self.end = i * dur, (i + 1) * dur
        self.text = f"kata {i}"
        self.words = [SimpleNamespace(start=self.start, end=self.end, word="kata")]


def test_asr_progress_follows_audio_time_not_cue_count(monkeypatch):
    """Satu klip 40 detik hanya punya belasan segmen. Kalau ambangnya jumlah cue,
    progres tidak pernah dikirim dan bar diam sepanjang tahap itu."""
    from pathlib import Path

    segs = [_Seg(i) for i in range(20)]
    info = SimpleNamespace(duration=40.0)
    asr = SimpleNamespace(transcribe=lambda *a, **k: (iter(segs), info))
    monkeypatch.setattr(media, "_asr", lambda m, d: asr)

    seen: list[tuple[float, float]] = []
    cues = media.transcribe(Path("clip00.mp4"), "id", "small", "cpu",
                            lambda end, total: seen.append((end, total)))

    assert len(cues) == 20
    assert seen, "progres harus tetap keluar walau segmennya sedikit"
    assert [e for e, _ in seen] == sorted(e for e, _ in seen), "maju terus, tidak mundur"
    assert len(seen) < len(segs), "dibatasi oleh waktu audio, bukan satu panggilan per segmen"
    assert {t for _, t in seen} == {40.0}
