"""KATEGORI 1 — Clipper.

Satu video YouTube -> N klip vertikal 9:16 dengan audio asli + subtitle
template. Satu job = N project Concat, dieksekusi berurutan dalam satu proses
concat headless (Concat hanya menjalankan satu render pada satu waktu).
"""

import json
import logging
import shutil
from pathlib import Path

from ..config import get_settings
from ..concat import Concat, ConcatError
from ..db import add_output, get_template
from ..progress import Tracker
from ..services import media
from ..types import ClipperInput, ClipSpec, Cue, Template, frame_size, resolve_template
from . import analyze

log = logging.getLogger("videoflow.clipper")


def _cue_rows(cues: list[Cue]) -> list[dict]:
    """`captions_add` mau `start`/`end` absolut pada timeline; klipnya sudah
    dipotong, jadi timeline klip dimulai dari 0. Dibulatkan karena start+dur
    membawa error float (3.2+0.7 = 3.9000000000000004)."""
    return [{"start": q.start, "end": round(q.start + q.dur, 3), "text": q.text} for q in cues]


def _render_one(
    c: Concat, tracker: Tracker, src: Path, cues: list[Cue], tpl: Template,
    size: dict, project_name: str, out: Path,
) -> None:
    s = get_settings()
    proj_dir = s.work / tracker.job_id
    proj_dir.mkdir(parents=True, exist_ok=True)
    # project_create menolak folder yang sudah berisi proyek, dan folder itu
    # bisa tertinggal kalau job sebelumnya mati di tengah jalan.
    shutil.rmtree(proj_dir / project_name, ignore_errors=True)

    path = c.project_create(proj_dir, project_name, size)
    try:
        mid = c.media_import(path, src)
        cid = c.clip_add(path, mid, c.video_track_id(path))
        c.clip_frame(path, cid, "fill")
        if cues:
            c.captions_add(path, _cue_rows(cues), tpl.style.model_dump(exclude_none=True), tpl.offsetY)

        job = c.export_start(
            path, out, crf=s.export_crf, preset=s.export_preset, codec=s.export_codec, **size,
        )
        c.wait_export(job, s.render_timeout_s, on_progress=tracker.export_progress)
    finally:
        try:
            c.project_close(path, save=False)
        except ConcatError as e:
            log.warning("project_close gagal: %s", e)
        shutil.rmtree(proj_dir / project_name, ignore_errors=True)


class _ExportLock:
    """Lock Redis agar dua worker tidak pernah memanggil export bareng."""

    def __init__(self, redis, key: str, ttl: int):
        self.r, self.key, self.ttl = redis, f"videoflow:{key}", ttl
        self.token: str | None = None

    def __enter__(self):
        import secrets
        import time

        self.token = secrets.token_hex(8)
        deadline = time.monotonic() + self.ttl
        while not self.r.set(self.key, self.token, nx=True, ex=self.ttl):
            if time.monotonic() > deadline:
                raise TimeoutError("menunggu lock render Concat habis waktu")
            time.sleep(2)
        return self

    def __exit__(self, *exc):
        if self.token:
            self.r.eval(
                "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end",
                1, self.key, self.token,
            )


def run(job_id: str, payload: dict, tracker: Tracker, r) -> list[dict]:
    inp = ClipperInput.model_validate(payload)
    s = get_settings()
    size = frame_size(inp.aspect, inp.quality)
    tracker.note(
        f"target {size['width']}x{size['height']} @{inp.quality} ({inp.aspect}), "
        f"subtitle {'nyala ' + inp.caption.preset if inp.caption.enabled else 'mati'}"
    )
    tpl = resolve_template(Template.model_validate(get_template(inp.template_id)), inp.caption)

    inbox = s.inbox / job_id
    inbox.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    tracker.declare("download", "probe")

    tracker.start("download")
    src = media.download_youtube(inp.youtube_url, inbox, inp.cookie_file)
    tracker.done("download", src.name)

    tracker.start("probe")
    info = media.probe(src)
    if not info["has_audio"]:
        raise RuntimeError("sumber tidak punya audio — Clipper memakai audio asli YouTube")
    source_cues: list[Cue] | None = None
    if inp.strategy == "prompt":
        # Butuh transkrip seluruh sumber sebelum memotong: titik potongnya
        # ditentukan oleh isi, bukan oleh pembagian rata durasi.
        tracker.start("transcribe:source")
        source_cues = analyze.transcribe_source(
            src, analyze.source_cache(s, inp.youtube_url), s, tracker.asr_progress
        )
        windows = media.rank_windows(
            source_cues, media.prompt_weights(inp.ai_prompt or ""),
            inp.clip_min_s, inp.clip_max_s, inp.clip_count,
        )
        tracker.done("transcribe:source", f"{len(source_cues)} segmen -> {len(windows)} jendela")
        if not windows:
            raise RuntimeError(
                f"prompt {inp.ai_prompt!r} tidak cocok dengan transkrip — ganti kata kuncinya"
            )
        specs = [ClipSpec(index=i, start=w.start, dur=w.dur) for i, w in enumerate(windows)]
    else:
        specs = media.plan_clips(info["duration"], inp.clip_count, inp.clip_min_s, inp.clip_max_s, inp.strategy)
    # Nama stage memakai tag klip, supaya status klip 2 tidak menimpa
    # status klip 1 dan membuat progress bar mundur.
    tracker.declare(*[f"{phase}:clip{sp.index:02d}" for sp in specs
                      for phase in ("cut", "captions", "assemble", "export")])
    tracker.done("probe", f'{info["duration"]:.0f}s -> {len(specs)} klip @{specs[0].dur:.0f}s')

    # Baru di sini Concat dibutuhkan: men-spawn-nya lebih awal hanya membuat
    # proses menggantung selama video diunduh.
    c = Concat(s.concat_bin, s.concat_mode)
    try:
        ver = c.version()
        log.info("concat %s, mode %s, %d tool", ver.get("concat"),
                 (ver.get("mcp") or {}).get("mode"), len(c.tools))

        for spec in specs:
            tag = f"clip{spec.index:02d}"
            cut = inbox / f"{tag}.mp4"
            final = s.out / job_id / f"{tag}.mp4"
            final.parent.mkdir(parents=True, exist_ok=True)

            if cut.exists() and cut.stat().st_size > 0:
                tracker.skip(f"cut:{tag}", "cached")
            else:
                tracker.start(f"cut:{tag}")
                media.cut(src, spec, cut)
                tracker.done(f"cut:{tag}")

            # File cache menyimpan hasil ASR mentah (per kalimat, dengan batas
            # kata), jadi mengubah maxWords tidak perlu transkripsi ulang.
            if not inp.caption.enabled:
                sub: list[Cue] = []
                tracker.skip(f"captions:{tag}", "subtitle mati")
            elif source_cues is not None:
                # Strategi prompt sudah punya transkrip utuh di sini, jadi
                # Whisper per klip tidak perlu dijalankan lagi.
                raw = media.cues_in_window(source_cues, spec.start, spec.dur)
                tracker.skip(f"captions:{tag}", f"{len(raw)} segmen dari transkrip sumber")
                sub = media.group_cues(raw, inp.caption.maxWords)
            else:
                cues_file = cut.with_suffix(".cues.json")
                if cues_file.exists():
                    raw = [Cue.model_validate(x) for x in json.loads(cues_file.read_text())]
                    tracker.skip(f"captions:{tag}", "cached")
                else:
                    tracker.start(f"captions:{tag}")
                    raw = media.transcribe(
                        cut, s.whisper_lang, s.whisper_model, s.whisper_device, tracker.asr_progress
                    )
                    cues_file.write_text(json.dumps([q.model_dump() for q in raw]))
                    tracker.done(f"captions:{tag}", f"{len(raw)} segmen")
                sub = media.group_cues(raw, inp.caption.maxWords)

            if final.exists() and final.stat().st_size > 0:
                tracker.skip(f"export:{tag}", "sudah dirender")
            else:
                tracker.start(f"assemble:{tag}")
                with _ExportLock(r, "export", s.export_lock_ttl_s):
                    tracker.done(f"assemble:{tag}")
                    tracker.start(f"export:{tag}")
                    _render_one(c, tracker, cut, sub, tpl, size, f"{job_id}_{tag}", final)
                    tracker.done(f"export:{tag}", final.name)

            add_output(job_id, str(final), "clip", {
                "duration": spec.dur, **size, "source_start": spec.start,
            })
            results.append({
                "index": spec.index, "file": str(final), "start": spec.start,
                "dur": spec.dur, "cues": len(sub),
            })
    finally:
        c.close()
    return results
