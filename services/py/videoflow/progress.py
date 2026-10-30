"""Pelacak progres job: tulis ke DB + publish ke Redis channel untuk SSE."""

import threading
import time
from datetime import datetime, timezone

import redis

from . import db
from .config import get_settings
from .types import LogLine, Progress, Stage

# Log disimpan di satu kolom jsonb dan dikirim utuh lewat SSE tiap kali berubah,
# jadi dipangkas dari belakang: yang penting adalah apa yang baru terjadi.
LOG_MAX = 300

# Whisper bisa berjalan puluhan menit tanpa satu pun kejadian baru. Tanpa detak
# ini panel terlihat mati padahal tidak.
BEAT_S = 45.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


class JobCancelled(Exception):
    """Dilempar worker begitu melihat job dibatalkan, pada titik teraman terakhir.

    Transkrip berhenti di antara cue (`asr_progress`), tahap biasa di batas tahap
    (`start`). Render Concat sengaja tidak pernah dipotong di tengah — hasilnya
    file rusak dan lock export tertinggal — jadi itu satu-satunya tempat tombol
    Batal masih harus menunggu tahapnya selesai.
    """


class Tracker:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.p = Progress()
        self.active = "job"
        s = get_settings()
        self.r = redis.Redis.from_url(s.redis_url)
        self.channel = f"videoflow:job:{job_id}"
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._last_flush = time.monotonic()
        self._stage_t0 = 0.0
        self._beat = threading.Thread(target=self._beat_loop, daemon=True)
        self._beat.start()

    def close(self) -> None:
        """Hentikan thread detak. Worker Celery hidup berjam-jam dan membuat
        satu Tracker per job, jadi thread-nya harus ikut mati bersama job."""
        self._stop.set()

    def _flush(self) -> None:
        with self._lock:
            payload = self.p.model_dump(mode="json")
            db.save_progress(self.job_id, payload)
            self.r.publish(self.channel, db.dumps(payload))
            self._last_flush = time.monotonic()

    def _beat_loop(self) -> None:
        while not self._stop.wait(BEAT_S):
            with self._lock:
                if self._stop.is_set():
                    return
                quiet = time.monotonic() - self._last_flush
                st = next((s for s in self.p.stages if s.name == self.active), None)
                if not st or st.status != "running" or quiet < BEAT_S:
                    continue
                self._log(st.name, "info", f"masih berjalan · {_clock(time.monotonic() - self._stage_t0)}")
                self._flush()

    def _log(self, stage: str, level: str, text: str) -> None:
        self.p.log.append(LogLine(ts=_now(), stage=stage, level=level, text=text))
        del self.p.log[:-LOG_MAX]

    def note(self, text: str, level: str = "info") -> None:
        """Baris log bebas untuk hal yang bukan pergantian stage (isi sumber,
        jumlah cue, alasan sebuah keputusan)."""
        with self._lock:
            self._log(self.active, level, text)
            self._flush()

    def _get(self, name: str) -> Stage:
        for s in self.p.stages:
            if s.name == name:
                return s
        st = Stage(name=name, status="pending")
        self.p.stages.append(st)
        return st

    def declare(self, *names: str) -> None:
        with self._lock:
            for n in names:
                self._get(n)
            self._flush()

    def start(self, name: str, detail: str | None = None) -> None:
        with self._lock:
            if db.status_of(self.job_id) == "cancelled":
                self._log(name, "skip", f"dibatalkan sebelum mulai: {name}")
                self._flush()
                raise JobCancelled(name)
            st = self._get(name)
            st.status, st.detail, st.started_at = "running", detail, _now()
            self.active = name
            self._stage_t0 = time.monotonic()
            self._log(name, "info", f"mulai{': ' + detail if detail else ''}")
            self._flush()

    def _finish(self, name: str, status: str, detail: str | None) -> None:
        with self._lock:
            st = self._get(name)
            st.status, st.detail, st.finished_at = status, detail or st.detail, _now()
            finished = sum(1 for s in self.p.stages if s.status in ("done", "skipped"))
            self.p.percent = round(100 * finished / max(1, len(self.p.stages)), 1)
            self._log(name, "skip" if status == "skipped" else "done",
                      f"{status}: {detail or st.detail or 'selesai'}")
            self._flush()

    def done(self, name: str, detail: str | None = None) -> None:
        self.active = name
        self._finish(name, "done", detail)

    def skip(self, name: str, detail: str = "artifact sudah ada") -> None:
        self._finish(name, "skipped", detail)

    def fail(self, err: str) -> None:
        with self._lock:
            st = self._get(self.active)
            st.status, st.detail, st.finished_at = "failed", err[:2000], _now()
            self._log(self.active, "error", err[:2000])
            db.set_status(self.job_id, "failed", err[:4000])
            self._flush()

    def status(self, s: str) -> None:
        # Job yang sudah dibatalkan tidak boleh berubah jadi 'done' hanya karena
        # worker lama menyelesaikan tahap yang sedang ia kerjakan.
        if s != "cancelled" and db.status_of(self.job_id) == "cancelled":
            return
        db.set_status(self.job_id, s)

    def asr_progress(self, done_s: float, total_s: float) -> None:
        """Progres transkrip dalam satu tahap.

        Whisper adalah tahap terpanjang (±2x durasi audio di CPU) dan tidak punya
        persentase sendiri, jadi tanpanya job 25 menit terbaca macet di 50% selama
        satu jam penuh.

        Callback ini juga tempat tombol Batal diperiksa saat ASR berjalan:
        generator faster-whisper berhenti di antara cue tanpa merusak apa pun,
        jadi pembatalan tidak perlu menunggu satu jam sampai tahapnya selesai.
        """
        if total_s <= 0:
            return
        with self._lock:
            if db.status_of(self.job_id) == "cancelled":
                self._log(self.active, "skip", f"dibatalkan di tengah transkrip · {_clock(done_s)}")
                self._flush()
                raise JobCancelled(self.active)
            frac = min(1.0, done_s / total_s)
            share = 100 / max(1, len(self.p.stages))
            finished = sum(1 for s in self.p.stages if s.status in ("done", "skipped"))
            self.p.percent = round(min(99.5, finished * share + share * frac), 1)
            self._log(
                self.active, "info",
                f"transkrip {_clock(done_s)} / {_clock(total_s)} ({frac * 100:.0f}%)",
            )
            self._flush()

    def export_progress(self, st: dict) -> None:
        """Callback untuk Concat `export.progress` (frame, total, stage)."""
        with self._lock:
            self.p.export = st
            frame, total = st.get("frame"), st.get("total")
            if frame and total:
                share = 100 / max(1, len(self.p.stages))
                finished = sum(1 for s in self.p.stages if s.status in ("done", "skipped"))
                self.p.percent = round(min(99.5, finished * share + share * frame / total), 1)
                self._log(self.active, "info", f"render frame {frame}/{total} ({100 * frame / total:.0f}%)")
            self._flush()
