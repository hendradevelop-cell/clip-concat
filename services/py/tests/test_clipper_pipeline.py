"""Integrasi pipeline Clipper terhadap Concat palsu.

Fakenya menyadap lapisan wrapper `Concat`, jadi yang diuji urutan dan isi
panggilan MCP-nya — bukan transpornya (itu tugas scripts/mcp_check.py yang
bicara dengan binary sungguhan). Whisper + yt-dlp di-stub; ffmpeg dipakai
betulan.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from videoflow.concat import Concat, ConcatError
from videoflow.pipelines import clipper
from videoflow.services import media
from videoflow.types import Cue, Word


class FakeConcat:
    """Meniru perilaku lapisan `Concat` dan mencatat setiap panggilan."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.tools = {"concat_version": {}}
        self.closed: list[str] = []
        self.fail_export = False
        self._media = 0
        self._clips = 0
        self._status_seen: dict[str, int] = {}

    def _rec(self, tool, **args):
        self.calls.append((tool, args))

    def version(self):
        self._rec("concat_version")
        return {"concat": "0.2.8-fake", "apiVersion": "0.2",
                "capabilities": ["mcp"], "mcp": {"mode": "headless"}}

    def project_create(self, location, name, video):
        self._rec("project_create", location=str(location), name=name, video=video)
        return str(Path(location) / name)

    def media_import(self, path, file):
        self._rec("media_import", path=path, file=str(file))
        self._media += 1
        return f"m{self._media}"

    def video_track_id(self, path):
        self._rec("timeline_read", path=path)
        return "T1"

    def clip_add(self, path, media_id, track_id, start=0.0):
        self._rec("clip_add", path=path, mediaId=media_id, trackId=track_id, start=start)
        self._clips += 1
        return f"c{self._clips}"

    def clip_frame(self, path, clip_id, framing="fill"):
        self._rec("clip_frame", path=path, clipId=clip_id, framing=framing)

    def captions_add(self, path, cues, style, offset_y):
        self._rec("captions_add", path=path, captions=cues, style=style, offsetY=offset_y)

    def export_start(self, path, output, **opts):
        self._rec("export_start", path=path, output=str(output), **opts)
        return "j1"

    def export_status(self, job):
        self._rec("export_status", job=job)
        n = self._status_seen[job] = self._status_seen.get(job, 0) + 1
        if self.fail_export:
            return {"job": job, "state": "failed", "error": "compositor hangus"}
        if n == 1:
            return {"job": job, "state": "running", "frame": 30, "total": 60}
        return {"job": job, "state": "done", "frame": 60, "total": 60}

    def wait_export(self, job, timeout, poll_s=5.0, on_progress=None):
        while True:
            status = self.export_status(job)
            if on_progress:
                on_progress(status)
            state = status["state"]
            if state == "done":
                return status
            if state != "running":
                raise ConcatError(f"render berakhir {state}", tool="export_status")

    def project_close(self, path, save=False):
        self._rec("project_close", path=path, save=save)
        self.closed.append(path)

    def close(self):
        self._rec("__process_close__")

    # --- kemudahan assertion ---

    def tools_used(self):
        return [t for t, _ in self.calls]

    def args_of(self, tool):
        return [a for t, a in self.calls if t == tool]


class FakeTracker:
    def __init__(self, job_id):
        self.job_id = job_id
        self.events: list[tuple[str, str, str | None]] = []

    def declare(self, *names):
        self.events += [("declare", n, None) for n in names]

    def start(self, name, detail=None):
        self.events.append(("start", name, detail))

    def done(self, name, detail=None):
        self.events.append(("done", name, detail))

    def skip(self, name, detail=None):
        self.events.append(("skip", name, detail))

    def note(self, text, level="info"):
        self.events.append(("note", text, level))

    def status(self, s):
        self.events.append(("status", s, None))

    def export_progress(self, st):
        self.events.append(("export_progress", st.get("state", ""), None))

    def asr_progress(self, done_s, total_s):
        self.events.append(("asr_progress", f"{done_s}/{total_s}", None))


class FakeRedis:
    def __init__(self):
        self.locks = []

    def set(self, key, val, nx=False, ex=None):
        self.locks.append(key)
        return True

    def eval(self, *a):
        return 1


JOB = "job-test"
PAYLOAD = {
    "youtube_url": "https://youtu.be/abc", "clip_count": 2, "clip_min_s": 5,
    "clip_max_s": 8, "template_id": "bold-yellow-1", "aspect": "9:16", "strategy": "even",
}


def _db_row():
    return {
        "id": "bold-yellow-1", "name": "Bold Kuning", "kind": "clipper", "offset_y": 0.5,
        "style": {"fontFamily": "Inter", "fontSize": 0.085, "fontWeight": 800,
                  "color": "#FFFFFF", "strokeColor": "#000000", "strokeWidth": 0.006},
    }


@pytest.fixture
def server(monkeypatch):
    """Satu FakeConcat per test; clipper.run() membangun kliennya lewat ini."""
    f = FakeConcat()
    monkeypatch.setattr(clipper, "Concat", lambda *a, **kw: f)
    return f


@pytest.fixture
def settings(tmp_path):
    for d in ("inbox", "work", "out", "cues"):
        (tmp_path / d).mkdir(exist_ok=True)
    return SimpleNamespace(
        data_dir=tmp_path, inbox=tmp_path / "inbox", work=tmp_path / "work", out=tmp_path / "out",
        cues=tmp_path / "cues",
        concat_bin="", concat_mode="headless",
        whisper_model="tiny", whisper_device="cpu", whisper_lang="id",
        export_crf=23, export_preset="ultrafast", export_codec="h264",
        render_timeout_s=60, export_lock_ttl_s=5,
    )


@pytest.fixture(scope="module")
def source_video(tmp_path_factory) -> Path:
    """Dirender sekali per modul. 640x360/12 fps sengaja kecil: render ffmpeg
    nyata adalah bagian mahal dari test ini (1080p30x30s membuat satu file
    test butuh puluhan menit)."""
    p = tmp_path_factory.mktemp("clipper") / "src.mp4"
    media.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=12:duration=20",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(p),
    ])
    return p


@pytest.fixture
def wired(monkeypatch, settings, source_video, server):
    """Stub unduh + transkripsi + DB. `server` diminta di sini supaya setiap
    test yang memakai wired otomatis dapat FakeConcat — tanpa itu clipper
    men-spawn concat.exe betulan dan render uji habis waktu 60s."""
    monkeypatch.setattr(clipper, "get_settings", lambda: settings)
    monkeypatch.setattr(media, "download_youtube",
                        lambda url, dest, ck=None: _copy(source_video, dest))

    transcribed: list[Path] = []

    def fake_transcribe(path, lang, model, device, on_progress=None):
        transcribed.append(Path(path))
        cues = [
            Cue(start=0.5, dur=1.5, text="kalimat satu", words=[
                Word(start=0.5, dur=0.7, text="kalimat"), Word(start=1.2, dur=0.8, text="satu")]),
            Cue(start=2.5, dur=1.4, text="kalimat dua", words=[
                Word(start=2.5, dur=0.7, text="kalimat"), Word(start=3.2, dur=0.7, text="dua")]),
        ]
        # Whisper melapor detik audio per segmen; pipeline harus meneruskan
        # callback-nya, tanpa itu bar progres diam sepanjang transkripsi.
        if on_progress:
            for c in cues:
                on_progress(c.start + c.dur, 4.0)
        return cues

    monkeypatch.setattr(clipper.media, "transcribe", fake_transcribe)
    monkeypatch.setattr(clipper, "get_template", lambda tid: _db_row())
    outputs: list[tuple] = []
    monkeypatch.setattr(clipper, "add_output", lambda *a: outputs.append(a))
    return SimpleNamespace(settings=settings, outputs=outputs, src=source_video,
                           transcribed=transcribed)


def _copy(src, dest_dir):
    p = dest_dir / "source.mp4"
    p.write_bytes(src.read_bytes())
    return p


def _run(wired, clip_count=None, **over) -> tuple[list[dict], FakeTracker]:
    payload = dict(PAYLOAD, clip_count=clip_count or PAYLOAD["clip_count"], **over)
    tracker = FakeTracker(JOB)
    return clipper.run(JOB, payload, tracker, FakeRedis()), tracker


def _final(wired, tag="clip00") -> Path:
    """Path hasil render, sama seperti dihitung clipper.run()."""
    return Path(wired.settings.out) / JOB / f"{tag}.mp4"


def test_call_order_per_clip(wired, server):
    _run(wired, clip_count=1)
    used = [t for t in server.tools_used() if t != "concat_version"]
    assert used == [
        "project_create", "media_import", "timeline_read", "clip_add", "clip_frame",
        "captions_add", "export_start", "export_status", "export_status",
        "project_close", "__process_close__",
    ]


def test_frame_uses_clip_id_from_clip_add(wired, server):
    """Regresi: dulu clipId ditebak dari struktur EditorView."""
    _run(wired, clip_count=2)
    assert [a["clipId"] for a in server.args_of("clip_frame")] == ["c1", "c2"]
    assert [a["mediaId"] for a in server.args_of("clip_add")] == ["m1", "m2"]
    assert all(a["framing"] == "fill" for a in server.args_of("clip_frame"))


def test_every_clip_gets_its_own_project(wired, server):
    _run(wired, clip_count=3)
    creates = server.args_of("project_create")
    assert len(creates) == len(server.args_of("media_import")) == len(
        server.args_of("export_start")) == 3
    assert len({c["name"] for c in creates}) == 3, "nama proyek harus unik per klip"
    assert creates[0]["video"] == {"width": 1080, "height": 1920, "rateNum": 30, "rateDen": 1}


def test_export_keeps_aspect_and_settings(wired, server):
    _run(wired, clip_count=1)
    export = server.args_of("export_start")[0]
    assert (export["width"], export["height"]) == (1080, 1920)
    assert export["rateNum"] == 30
    assert export["codec"] == "h264" and export["crf"] == 23 and export["preset"] == "ultrafast"


def test_quality_picks_the_output_resolution(wired, server):
    """Resolusinya dipakai dua kali: ukuran frame proyek dan parameter export.
    Kalau keduanya beda, Concat men-scaled ulang hasil render diam-diam."""
    _run(wired, clip_count=1, quality="720p")
    assert server.args_of("project_create")[0]["video"] == {
        "width": 720, "height": 1280, "rateNum": 30, "rateDen": 1,
    }
    export = server.args_of("export_start")[0]
    assert (export["width"], export["height"]) == (720, 1280)


def test_480p_fast_test_render_is_registered_with_its_real_size(wired):
    _, _ = _run(wired, clip_count=1, quality="480p")
    meta = wired.outputs[0][3]
    assert (meta["width"], meta["height"]) == (480, 854)


def test_captions_become_one_captions_add_per_clip(wired, server):
    """Regresi: style lama memakai family/size/weight yang tidak ada di Concat."""
    _run(wired, clip_count=1)
    call = server.args_of("captions_add")[0]
    assert call["captions"] == [
        {"start": 0.5, "end": 2.0, "text": "kalimat satu"},
        {"start": 2.5, "end": 3.9, "text": "kalimat dua"},
    ]
    assert call["style"] == {
        # fontSize adalah pecahan tinggi frame: 10/1920 = huruf 10 piksel di 1080p.
        "fontFamily": "Montserrat", "fontSize": 10 / 1920, "fontWeight": 800,
        "color": "#FFFFFF", "strokeColor": "#000000", "strokeWidth": 0.0005,
        "shadow": True, "background": "", "maxWidth": 0.9, "align": "center",
    }


def test_transcript_reports_progress_while_it_runs(wired):
    """Bar harus bergerak di dalam tahap transkripsi, bukan hanya saat tahap
    selesai -- tanpa ini 20 menit Whisper terlihat seperti macet di 50%."""
    _, tracker = _run(wired, clip_count=1)
    seen = [n for ev, n, _ in tracker.events if ev == "asr_progress"]
    assert len(seen) == 2 and all(n.endswith("/4.0") for n in seen), seen
    times = [float(n.split("/")[0]) for n in seen]
    assert times == sorted(times), "detik audio harus maju, tidak pernah mundur"


def test_caption_controls_override_the_template(wired, server):
    """Regresi posisi: dulu style/offset hanya dari baris templates."""
    _run(wired, clip_count=1, caption={"fontFamily": "Anton", "color": "#FF0000",
                                       "offsetY": 0.1, "background": "#000000B3"})
    call = server.args_of("captions_add")[0]
    assert call["offsetY"] == 0.1, "offsetY template (0.5) harus kalah dari kontrol UI"
    assert call["style"]["fontFamily"] == "Anton"
    assert call["style"]["color"] == "#FF0000"
    assert call["style"]["background"] == "#000000B3"


def test_max_words_splits_lines_on_word_times(wired, server):
    _run(wired, clip_count=1, caption={"maxWords": 1})
    assert server.args_of("captions_add")[0]["captions"] == [
        {"start": 0.5, "end": 1.2, "text": "kalimat"},
        {"start": 1.2, "end": 2.0, "text": "satu"},
        {"start": 2.5, "end": 3.2, "text": "kalimat"},
        {"start": 3.2, "end": 3.9, "text": "dua"},
    ]


def test_preset_karaoke_implies_one_word_rows(wired, server):
    """Preset mengisi field yang tidak dikirim, jadi API cukup kirim namanya."""
    _run(wired, clip_count=1, caption={"preset": "karaoke"})
    call = server.args_of("captions_add")[0]
    assert call["offsetY"] == 0.3
    assert call["style"]["background"] == "#000000B3"
    assert call["style"]["fontSize"] == 0.11
    assert len(call["captions"]) == 4, "karaoke = satu kata per baris"


def test_captions_off_skips_whisper_and_the_concat_call(wired, server):
    """Subtitle mati tidak boleh membayar transkripsi CPU yang mahal."""
    _, tracker = _run(wired, clip_count=1, caption={"enabled": False})
    assert not server.args_of("captions_add")
    assert wired.transcribed == []
    assert ("skip", "captions:clip00", "subtitle mati") in tracker.events
    assert server.args_of("export_start"), "rendernya tetap jalan"


def test_prompt_strategy_cuts_where_the_words_are(wired, server):
    """Strategi prompt: titik potong dari isi, bukan pembagian rata durasi."""
    results, tracker = _run(wired, clip_count=2, strategy="prompt", ai_prompt="kalimat satu")
    assert len(results) == 1, "hanya satu jendela yang memuat kata kunci"
    assert results[0]["start"] == 0.5
    assert ("start", "transcribe:source", None) in tracker.events


def test_prompt_strategy_reuses_the_source_transcript(wired, server):
    """Whisper sekali untuk sumber utuh, bukan per klip — dan cache-nya dipakai
    ulang saat job yang sama dijalankan lagi."""
    _run(wired, clip_count=1, strategy="prompt", ai_prompt="kalimat satu")
    assert [p.name for p in wired.transcribed] == ["source.mp4"]
    assert server.args_of("captions_add")[0]["captions"] == [
        {"start": 0.0, "end": 1.5, "text": "kalimat satu"},
        {"start": 2.0, "end": 3.4, "text": "kalimat dua"},
    ], "waktu cue digeser supaya 0 = awal klip"

    _run(wired, clip_count=1, strategy="prompt", ai_prompt="kalimat satu")
    assert len(wired.transcribed) == 1


def test_prompt_without_a_match_fails_before_render(wired, server):
    with pytest.raises(RuntimeError, match="tidak cocok"):
        _run(wired, clip_count=1, strategy="prompt", ai_prompt="kucing tisu")
    assert not server.args_of("export_start"), "jangan render apa pun kalau promptnya kosong melompong"


def test_stage_names_are_per_clip_and_never_regress(wired):
    """Regresi: dulu stage 'cut' klip 2 menimpa status klip 1 sehingga bar mundur."""
    _, tracker = _run(wired, clip_count=3)
    declared = [n for ev, n, _ in tracker.events if ev == "declare"]
    assert len(declared) == len(set(declared)), "nama stage tidak boleh dobel"
    started = [n for ev, n, _ in tracker.events if ev == "start"]
    assert len(started) == len(set(started)), "stage tidak boleh di-start dua kali"
    for tag in ("clip00", "clip01", "clip02"):
        assert f"cut:{tag}" in declared and f"export:{tag}" in declared


def test_export_progress_reaches_tracker(wired, server):
    _, tracker = _run(wired, clip_count=1)
    assert [n for ev, n, _ in tracker.events if ev == "export_progress"] == ["running", "done"]


def test_output_rows_registered(wired):
    results, _ = _run(wired)
    assert len(results) == 2 == len(wired.outputs)
    assert {o[2] for o in wired.outputs} == {"clip"}
    assert all(o[3]["width"] == 1080 and o[3]["height"] == 1920 for o in wired.outputs)


def test_rerun_skips_finished_work(wired, server):
    _run(wired, clip_count=1)
    assert len(server.args_of("export_start")) == 1

    final = _final(wired)
    assert final.parent.is_dir(), "clipper harus membuat folder output per job"
    final.write_bytes(b"cached")  # tandai render sudah selesai

    _, tracker = _run(wired, clip_count=1)
    skipped = {n for ev, n, _ in tracker.events if ev == "skip"}
    assert {"cut:clip00", "captions:clip00", "export:clip00"} <= skipped
    assert len(server.args_of("export_start")) == 1


def test_project_scratch_is_removed_after_render(wired, server):
    """Folder proyek cuma tempat kerja; kalau ditinggalkan, project_create
    berikutnya menolak folder yang sudah berisi proyek."""
    _run(wired, clip_count=1)
    create = server.args_of("project_create")[0]
    assert not Path(create["location"], create["name"]).exists()


def test_process_and_project_closed_even_when_render_fails(wired, server):
    server.fail_export = True
    with pytest.raises(ConcatError, match="failed"):
        _run(wired, clip_count=1)
    assert "__process_close__" in server.tools_used()
    assert server.closed, "proyek harus ditutup walau rendernya gagal"


def test_source_without_audio_is_rejected(wired, server, monkeypatch):
    """Clipper bergantung audio asli; sumber tanpa audio harus gagal cepat."""
    silent = Path(wired.settings.inbox) / "silent-src.mp4"
    media.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "testsrc=size=640x360:rate=12:duration=8", "-an", str(silent),
    ])
    monkeypatch.setattr(media, "download_youtube", lambda url, dest, ck=None: _copy(silent, dest))

    with pytest.raises(RuntimeError, match="audio"):
        clipper.run("job-silent", dict(PAYLOAD, clip_count=1, clip_min_s=5, clip_max_s=6),
                    FakeTracker("job-silent"), FakeRedis())
    assert not server.args_of("export_start"), "harus gagal sebelum render"
    assert not server.args_of("project_create"), "Concat tidak usah di-spawn"
