import json
import time
import uuid
from pathlib import Path
from typing import AsyncIterator

import redis
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import db
from ..config import get_settings
from ..concat import Concat
from ..types import AnalyzeInput, ClipperInput
from ..worker import celery_app

app = FastAPI(title="videoflow", version="0.1")

# Dashboard (origin :3000) memanggil API ini dari browser tanpa proxy, jadi
# tanpa header CORS responsnya diblokir browser dan dashboard melihatnya sebagai
# "backend tidak menjawab".
_origins = [o.strip() for o in get_settings().cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["content-type"],
    max_age=600,
)

# Men-spawn concat.exe itu mahal (proses + konteks GPU), jadi hasil health
# dipakai ulang sebentar.
HEALTH_TTL_S = 20.0
_health_cache: tuple[float, dict] | None = None


@app.get("/api/v1/concat/health")
def concat_health() -> dict:
    """Spawn satu concat headless, panggil `concat_version`, lalu matikan.

    Dijeda beberapa detik supaya setiap reload dashboard tidak membuat
    proses + konteks GPU baru.
    """
    global _health_cache
    now = time.monotonic()
    if _health_cache and now - _health_cache[0] < HEALTH_TTL_S:
        return _health_cache[1]

    s = get_settings()
    try:
        with Concat(s.concat_bin, s.concat_mode) as c:
            info = c.version()
            result = {
                "ok": True,
                "version": info,
                "capabilities": sorted(info.get("capabilities") or []),
                "tools": len(c.tools),
            }
    except Exception as e:
        # Concat hanya satu instance: selama job lain memegang lock render,
        # health check ini akan gagal dan itu bukan kerusakan — dashboard tidak
        # boleh memaknainya sebagai "tidak bisa mengirim job".
        result = {"ok": False, "busy": _render_busy(), "error": f"{type(e).__name__}: {e}"[:800]}
    _health_cache = (now, result)
    return result


def _render_busy() -> bool:
    """Apakah Concat sedang dipakai.

    Dua sinyal: lock `videoflow:export` (fase render) dan job clipper berstatus
    `running` — pipeline membuka sesi Concat sejak awal job dan menahannya
    sampai selesai, jadi health check akan gagal walaupun belum sampai export.
    """
    try:
        if redis.Redis.from_url(get_settings().redis_url).exists("videoflow:export"):
            return True
    except Exception:
        pass
    with db.tx() as cur:
        cur.execute("SELECT 1 FROM jobs WHERE status = 'running' AND category = 'clipper' LIMIT 1")
        return cur.fetchone() is not None


@app.get("/api/v1/templates")
def templates(kind: str | None = None) -> list[dict]:
    return db.list_templates(kind)


class ClipperJob(BaseModel):
    job_id: str
    status: str


def _enqueue(task: str, job_id: str, payload: dict) -> None:
    """Catat id Celery di baris job.

    Tanpa id ini tombol Batal cuma bisa mengubah status DB, sementara pesannya
    tetap antre di broker dan worker tetap mengerjakannya.
    """
    db.insert_job(job_id, task.rsplit(".", 1)[-1], payload)
    res = celery_app.send_task(task, args=[job_id, payload])
    db.set_task_id(job_id, res.id)


@app.post("/api/v1/jobs/clipper", status_code=202, response_model=ClipperJob)
def create_clipper(inp: ClipperInput) -> ClipperJob:
    job_id = str(uuid.uuid4())
    if not any(t["id"] == inp.template_id for t in db.list_templates("clipper")):
        raise HTTPException(422, f"template {inp.template_id} bukan template clipper / tidak ada")
    _enqueue("videoflow.clipper", job_id, inp.model_dump(mode="json"))
    return ClipperJob(job_id=job_id, status="queued")


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    row = db.get_job(job_id)
    if not row:
        raise HTTPException(404, "job tidak ada")
    row["outputs"] = db.outputs(job_id)
    return row


# Status yang sudah final: tidak ada lagi yang bisa dibatalkan.
CLOSED = ("done", "failed", "cancelled")


@app.post("/api/v1/jobs/{job_id}/cancel", response_model=ClipperJob)
def cancel_job(job_id: str) -> ClipperJob:
    """Batalkan job yang masih antre atau sedang berjalan.

    Dua jalur, dan keduanya diperlukan: `revoke` membuang pesan yang masih ada
    di broker (job antre tidak pernah dimulai), sedangkan status 'cancelled' di
    DB membuat worker yang sudah terlanjur mengerjakan job berhenti di batas
    tahap berikutnya. Render Concat sengaja tidak dipotong di tengah — hasilnya
    file rusak dan lock export tertinggal.
    """
    row = db.get_job(job_id)
    if not row:
        raise HTTPException(404, "job tidak ada")
    if row["status"] in CLOSED:
        raise HTTPException(409, f"job sudah {row['status']}, tidak bisa dibatalkan")
    db.set_status(job_id, "cancelled")
    if row.get("task_id"):
        celery_app.control.revoke(row["task_id"])
    return ClipperJob(job_id=job_id, status="cancelled")


@app.post("/api/v1/analyze", status_code=202, response_model=ClipperJob)
def create_analyze(inp: AnalyzeInput) -> ClipperJob:
    """Unduh + transkrip penuh, hasilnya di `GET /api/v1/jobs/{id}` (`result`).

    Butuh waktu sebanding dengan durasi video (Whisper lokal), jadi dipanggil
    lewat Celery dan dipantau lewat SSE yang sama dengan job Clipper.
    """
    job_id = str(uuid.uuid4())
    _enqueue("videoflow.analyze", job_id, inp.model_dump(mode="json"))
    return ClipperJob(job_id=job_id, status="queued")


@app.get("/api/v1/jobs")
def list_jobs(limit: int = 30) -> list[dict]:
    """Ringkasan untuk panel antrean.

    `progress` sengaja tidak ikut dibuang utuh di sini: satu job bisa punya
    300 baris log, dan daftar 30 job akan jadi beberapa MB. Yang dibutuhkan UI
    cuma persen, tahap yang sedang jalan, dan jumlah berkas hasil.
    """
    with db.tx() as cur:
        cur.execute(
            "SELECT j.id, j.category, j.status, j.error, j.created_at, j.updated_at, "
            "       j.input->>'youtube_url' AS url, "
            "       j.input->>'quality' AS quality, "
            "       (j.progress->>'percent')::real AS percent, "
            # Yang dicari tahap yang PALING BELAKANG masih running: `probe` sengaja
            # dibiarkan terbuka selama transkrip sumber, jadi ambil yang pertama
            # membuat antrean melaporkan "probe" padahal Whisper yang sedang jalan.
            "       (SELECT s->>'name' "
            "          FROM jsonb_array_elements(j.progress->'stages') WITH ORDINALITY t(s, ord) "
            "         WHERE s->>'status' = 'running' ORDER BY t.ord DESC LIMIT 1) AS stage, "
            "       (SELECT count(*) FROM job_outputs o WHERE o.job_id = j.id) AS outputs "
            "FROM jobs j ORDER BY j.created_at DESC LIMIT %s", (limit,),
        )
        return cur.fetchall()


@app.get("/api/v1/jobs/{job_id}/events")
async def events(job_id: str) -> StreamingResponse:
    """SSE: subscribe Redis channel yang ditulis Tracker._flush()."""
    s = get_settings()

    async def stream() -> AsyncIterator[bytes]:
        r = aioredis.from_url(s.redis_url)
        pubsub = r.pubsub()
        await pubsub.subscribe(f"videoflow:job:{job_id}")
        try:
            row = db.get_job(job_id)
            if row:
                yield f"data: {json.dumps(row['progress'], default=str)}\n\n"
            async for msg in pubsub.listen():
                if msg["type"] == "message":
                    yield f"data: {msg['data'].decode()}\n\n"
                    try:
                        payload = json.loads(msg["data"])
                        if payload.get("percent", 0) >= 100:
                            break
                    except json.JSONDecodeError:
                        pass
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()
            await r.aclose()

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v1/download")
def download(path: str):
    from fastapi.responses import FileResponse

    s = get_settings()
    p = Path(path).resolve()
    if not p.is_file() or s.out.resolve() not in p.parents:
        raise HTTPException(403, "path di luar folder output")
    return FileResponse(p, filename=p.name, media_type="video/mp4")
