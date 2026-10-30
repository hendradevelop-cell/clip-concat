"""Tracker = satu-satunya sumber panel "Log aktivitas" di dashboard.

Yang diuji di sini adalah isi `Progress.log`: barisnya harus benar-benar
mencerminkan kejadian (stage mana, level apa, kapan) karena UI cuma membacanya
apa adanya — tidak ada penimbulan di sisi browser.
"""

import time
from types import SimpleNamespace

import pytest
from videoflow import progress as prog
from videoflow.progress import LOG_MAX, Tracker


class FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    def publish(self, channel, payload):
        self.published.append((channel, payload))


def make_tracker(monkeypatch, beat_s: float = 45.0, status: str = "running") -> Tracker:
    """Tracker asli membuka Redis + DB; di sini dua-duanya diganti catatan."""
    r = FakeRedis()
    # Tracker cuma memanggil redis.Redis.from_url(...).publish(...)
    monkeypatch.setattr(prog, "redis", SimpleNamespace(Redis=SimpleNamespace(from_url=lambda url: r)))
    monkeypatch.setattr(prog, "get_settings", lambda: SimpleNamespace(redis_url="redis://x"))
    monkeypatch.setattr(prog, "BEAT_S", beat_s)
    saved: list[dict] = []
    states: list[str] = []
    monkeypatch.setattr(prog.db, "save_progress", lambda job_id, payload: saved.append(payload))
    monkeypatch.setattr(prog.db, "set_status", lambda job_id, s, *a: states.append(s))
    monkeypatch.setattr(prog.db, "status_of", lambda job_id: status)
    t = Tracker("job-1")
    t.saved = saved
    t.pub = r
    t.states = states
    return t


@pytest.fixture
def tracker(monkeypatch):
    t = make_tracker(monkeypatch)
    yield t
    t.close()


def _lines(t):
    return [(s.stage, s.level, s.text) for s in t.p.log]


def test_start_done_skip_each_write_a_line(tracker):
    tracker.declare("download", "cut:clip00")
    tracker.start("download", "sumber.mp4")
    tracker.done("download", "12 MB")
    tracker.skip("cut:clip00", "cached")

    assert _lines(tracker) == [
        ("download", "info", "mulai: sumber.mp4"),
        ("download", "done", "done: 12 MB"),
        ("cut:clip00", "skip", "skipped: cached"),
    ]
    assert tracker.p.percent == 100.0, "kedua tahap selesai (satu skip tetap dihitung)"
    assert tracker.p.stages[1].status == "skipped"


def test_every_line_carries_a_utc_timestamp(tracker):
    tracker.start("probe")
    line = tracker.p.log[-1]
    assert line.ts.endswith("+00:00") or line.ts.endswith("Z"), line.ts


def test_fail_logs_the_error_under_the_active_stage(tracker):
    tracker.declare("export:clip00")
    tracker.start("export:clip00")
    tracker.fail("compositor hangus")

    stage, level, text = _lines(tracker)[-1]
    assert (stage, level) == ("export:clip00", "error")
    assert "compositor" in text


def test_export_progress_logs_frame_counts(tracker):
    tracker.declare("export:clip00", "assemble:clip00")
    tracker.start("export:clip00")
    tracker.export_progress({"state": "running", "frame": 45, "total": 90})

    assert _lines(tracker)[-1] == ("export:clip00", "info", "render frame 45/90 (50%)")
    assert tracker.p.percent == 25.0, "setengah dari satu dari dua tahap"


def test_note_appends_a_free_line_without_touching_stages(tracker):
    tracker.declare("download")
    tracker.note("target 720x1280 @720p (9:16)")

    assert _lines(tracker) == [("job", "info", "target 720x1280 @720p (9:16)")]
    assert tracker.p.stages[0].status == "pending", "catatan bukan pergantian stage"


def test_log_is_trimmed_from_the_back(tracker, monkeypatch):
    monkeypatch.setattr(prog, "LOG_MAX", 5)
    for i in range(9):
        tracker.note(f"baris {i}")

    assert len(tracker.p.log) == 5
    assert [s.text for s in tracker.p.log] == [f"baris {i}" for i in range(4, 9)]


def test_log_travels_to_db_and_sse_on_every_flush(tracker):
    tracker.start("download")
    assert tracker.saved[-1]["log"][-1]["stage"] == "download"
    assert tracker.saved[-1]["percent"] == 0.0


def test_a_quiet_running_stage_still_speaks(monkeypatch):
    """Whisper bisa diam puluhan menit; baris detak membuktikan job hidup."""
    t = make_tracker(monkeypatch, beat_s=0.05)
    try:
        t.start("transcribe:source")
        time.sleep(0.3)
    finally:
        t.close()
    beats = [s for s in t.p.log if s.text.startswith("masih berjalan · ")]
    assert beats, [s.text for s in t.p.log]
    assert {s.stage for s in beats} == {"transcribe:source"}
    assert all(s.level == "info" for s in beats)
    assert t.p.percent == 0.0, "detak tidak boleh memajukan persentase"


def test_no_beat_while_nothing_is_running(monkeypatch):
    t = make_tracker(monkeypatch, beat_s=0.05)
    try:
        t.declare("download")
        t.done("download", "source.mp4")
        time.sleep(0.3)
    finally:
        t.close()
    assert [s.text for s in t.p.log] == ["done: source.mp4"]


def test_start_refuses_when_the_job_is_already_cancelled(monkeypatch):
    t = make_tracker(monkeypatch, status="cancelled")
    with pytest.raises(prog.JobCancelled):
        t.start("download")

    assert _lines(t) == [("download", "skip", "dibatalkan sebelum mulai: download")]
    assert t.p.stages == [], "job batal tidak boleh membuka tahap baru"


def test_cancel_breaks_at_the_next_stage_not_mid_stage(monkeypatch):
    """Satu tahap yang sudah jalan tidak dihentikan paksa: render Concat harus
    selesai dulu supaya file berikutnya tidak menabrak lock export."""
    t = make_tracker(monkeypatch)
    t.start("download")
    t.done("download", "source.mp4")
    monkeypatch.setattr(prog.db, "status_of", lambda job_id: "cancelled")
    with pytest.raises(prog.JobCancelled):
        t.start("transcribe:source")
    assert [s.status for s in t.p.stages] == ["done"]


def test_a_cancelled_job_is_not_resurrected_by_the_worker(monkeypatch):
    """`revoke` bisa meleset (pesannya sudah diambil worker); status DB adalah
    penjaga terakhir supaya job batal tidak kembali jadi 'running'."""
    from videoflow import worker

    t = make_tracker(monkeypatch, status="cancelled")
    assert worker._begin(t) is False
    assert t.saved == [], "tidak ada progres yang ditulis untuk job yang sudah dibatalkan"


def test_finished_worker_cannot_overwrite_a_cancellation(monkeypatch):
    t = make_tracker(monkeypatch, status="cancelled")
    t.status("running")
    t.status("done")
    assert t.states == [], "render yang telanjur jalan tidak mengubah status"

    ok = make_tracker(monkeypatch, status="queued")
    ok.status("running")
    assert ok.states == ["running"]


def test_asr_progress_moves_the_bar_inside_the_long_stage(tracker):
    """Whisper bisa memakan satu jam untuk satu tahap. Tanpa sub-progres di dalam
    tahap itu, bar persentase diam padahal pekerjaannya jalan — dan itu terbaca
    sebagai macet oleh pengguna."""
    tracker.declare("download", "probe", "transcribe:source", "export")
    tracker.start("download")
    tracker.done("download", "source.mp4")
    tracker.start("transcribe:source")
    assert tracker.p.percent == pytest.approx(25.0)

    tracker.asr_progress(60.0, 240.0)

    assert tracker.p.percent == pytest.approx(31.2, abs=0.1), "25% tahap selesai + 25% dari tahap ini"
    assert _lines(tracker)[-1] == ("transcribe:source", "info", "transkrip 01:00 / 04:00 (25%)")


def test_asr_progress_says_nothing_without_a_duration(tracker):
    tracker.declare("transcribe:source")
    tracker.start("transcribe:source")
    tracker.asr_progress(10.0, 0.0)
    assert tracker.p.percent == 0.0
    assert len(tracker.p.log) == 1, "sumber tanpa durasi tidak boleh mengarang persentase"


def test_a_cancel_reaches_the_worker_inside_the_transcript_loop(monkeypatch):
    """Sudah dibatalkan tidak boleh baru dipatuhi satu jam kemudian. Transkrip
    adalah tahap terpanjang, jadi pembatalan diperiksa di dalam loop cue-nya."""
    t = make_tracker(monkeypatch, status="cancelled")
    try:
        t.declare("transcribe:source")
        t.active = "transcribe:source"
        with pytest.raises(prog.JobCancelled):
            t.asr_progress(600.0, 2400.0)
        assert _lines(t)[-1] == ("transcribe:source", "skip", "dibatalkan di tengah transkrip · 10:00")
    finally:
        t.close()
