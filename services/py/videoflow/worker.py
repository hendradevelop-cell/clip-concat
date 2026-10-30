from celery import Celery

from . import db
from .config import get_settings
from .progress import JobCancelled, Tracker

s = get_settings()

celery_app = Celery("videoflow", broker=s.redis_url, backend=s.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # visibility_timeout HARUS lebih besar dari render terpanjang, kalau tidak
    # Celery mendeliver ulang task yang masih jalan dan job dobel render.
    broker_transport_options={"visibility_timeout": 7200},
    task_time_limit=7200,
    task_soft_time_limit=6900,
    task_default_queue="videoflow",
)


def _begin(tracker: Tracker) -> bool:
    """False kalau job dibatalkan saat masih antre.

    `revoke` hanya menghapus pesan yang masih ada di broker; kalau pesannya
    sudah terlanjur diambil worker, status 'cancelled' di DB-lah yang menahan
    supaya tidak ditimpa kembali jadi 'running'.
    """
    if db.status_of(tracker.job_id) == "cancelled":
        return False
    tracker.status("running")
    return True


@celery_app.task(name="videoflow.clipper", bind=True, max_retries=0)
def clipper_task(self, job_id: str, payload: dict) -> list[dict] | None:
    import redis

    from .pipelines.clipper import run

    tracker = Tracker(job_id)
    if not _begin(tracker):
        tracker.close()
        return None
    try:
        out = run(job_id, payload, tracker, redis.Redis.from_url(s.redis_url))
        tracker.status("done")
        return out
    except JobCancelled:
        tracker.note("job dibatalkan", "skip")
        return None
    except Exception as e:
        tracker.fail(f"{type(e).__name__}: {e}")
        raise
    finally:
        tracker.close()


@celery_app.task(name="videoflow.analyze", bind=True, max_retries=0)
def analyze_task(self, job_id: str, payload: dict) -> dict | None:
    """Tidak butuh lock export: tidak pernah memanggil Concat."""
    from .pipelines.analyze import run

    tracker = Tracker(job_id)
    if not _begin(tracker):
        tracker.close()
        return None
    try:
        result = run(job_id, payload, tracker)
        db.save_result(job_id, result)
        tracker.status("done")
        return result
    except JobCancelled:
        tracker.note("job dibatalkan", "skip")
        return None
    except Exception as e:
        tracker.fail(f"{type(e).__name__}: {e}")
        raise
    finally:
        tracker.close()
