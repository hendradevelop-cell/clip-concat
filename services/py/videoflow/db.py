import json
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from .config import get_settings

_POOL: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _POOL
    if _POOL is None:
        _POOL = ConnectionPool(
            get_settings().database_url, kwargs={"row_factory": dict_row}, min_size=1, max_size=8, open=True
        )
    return _POOL


@contextmanager
def tx() -> Iterator[psycopg.Cursor]:
    with pool().connection() as conn:
        with conn.cursor() as cur:
            yield cur
            conn.commit()


def insert_job(job_id: str, category: str, payload: dict[str, Any]) -> None:
    with tx() as cur:
        cur.execute(
            "INSERT INTO jobs (id, category, status, input, progress) "
            "VALUES (%s, %s, 'queued', %s, %s)",
            (job_id, category, Json(payload), Json({"stages": [], "percent": 0})),
        )


def set_status(job_id: str, status: str, error: str | None = None) -> None:
    with tx() as cur:
        cur.execute("UPDATE jobs SET status = %s, error = %s WHERE id = %s", (status, error, job_id))


def status_of(job_id: str) -> str | None:
    """Dipanggil worker di tiap batas tahap untuk melihat permintaan batal."""
    with tx() as cur:
        cur.execute("SELECT status FROM jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        return row["status"] if row else None


def set_task_id(job_id: str, task_id: str) -> None:
    """Id Celery disimpan supaya job bisa dibatalkan lewat `control.revoke`."""
    with tx() as cur:
        cur.execute("UPDATE jobs SET task_id = %s WHERE id = %s", (task_id, job_id))


def save_progress(job_id: str, progress: dict) -> None:
    with tx() as cur:
        cur.execute("UPDATE jobs SET progress = %s WHERE id = %s", (Json(progress), job_id))


def save_result(job_id: str, result: dict[str, Any]) -> None:
    """Hasil analisis (kategori `analyze`) disimpan di baris job, bukan di
    job_outputs, karena tidak ada file yang dihasilkan."""
    with tx() as cur:
        cur.execute("UPDATE jobs SET result = %s WHERE id = %s", (Json(result), job_id))


def get_job(job_id: str) -> dict | None:
    with tx() as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


def add_output(job_id: str, path: str, kind: str, meta: dict[str, Any]) -> None:
    with tx() as cur:
        cur.execute(
            "INSERT INTO job_outputs (job_id, path, kind, duration_s, w, h, meta) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (job_id, path) DO NOTHING",
            (job_id, path, kind, meta.get("duration"), meta.get("width"), meta.get("height"), Json(meta)),
        )


def outputs(job_id: str) -> list[dict]:
    with tx() as cur:
        cur.execute("SELECT * FROM job_outputs WHERE job_id = %s ORDER BY id", (job_id,))
        return cur.fetchall()


def get_template(template_id: str) -> dict:
    with tx() as cur:
        cur.execute("SELECT * FROM templates WHERE id = %s", (template_id,))
        row = cur.fetchone()
    if not row:
        raise KeyError(f"template {template_id!r} tidak ada di tabel templates")
    return row


def list_templates(kind: str | None = None) -> list[dict]:
    with tx() as cur:
        if kind:
            cur.execute("SELECT * FROM templates WHERE kind = %s ORDER BY id", (kind,))
        else:
            cur.execute("SELECT * FROM templates ORDER BY kind, id")
        return cur.fetchall()


def dumps(v: Any) -> str:
    return json.dumps(v, default=str)
