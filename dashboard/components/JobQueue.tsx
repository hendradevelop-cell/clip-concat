"use client";

import { useCallback, useEffect, useState } from "react";

import { type JobSummary, cancelJob, listJobs } from "../lib/api";

const OPEN = new Set(["queued", "running"]);

/** Panel antrean: job yang sedang berjalan tetap terlihat setelah halaman
 * di-refresh, dan tiap baris bisa dipantau atau dibatalkan dari sini. */
export default function JobQueue({
  activeId,
  onWatch,
}: {
  activeId: string | null;
  onWatch: (id: string) => void;
}) {
  const [rows, setRows] = useState<JobSummary[]>([]);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(() => {
    listJobs(20)
      .then((r) => {
        setRows(r);
        setErr(null);
      })
      .catch((e) => setErr(`Antrean: ${(e as Error).message}`));
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  async function stop(id: string) {
    setErr(null);
    try {
      await cancelJob(id);
    } catch (e) {
      setErr((e as Error).message);
    }
    refresh();
  }

  const open = rows.filter((r) => OPEN.has(r.status)).length;

  return (
    <div className="card" style={{ marginTop: 18 }}>
      <div className="prog">
        <p className="sect" style={{ margin: 0 }}>
          Antrean proses
        </p>
        <span className="note" style={{ margin: 0 }}>
          {open} aktif · {rows.length} terakhir
        </span>
      </div>
      {err && <p className="warn">{err}</p>}
      {rows.length === 0 && !err && <p className="note">Belum ada job.</p>}

      <ul className="q">
        {rows.map((j) => (
          <li key={j.id} className={j.id === activeId ? "qrow on" : "qrow"}>
            <button
              type="button"
              className="qid"
              onClick={() => onWatch(j.id)}
              title="Pantau job ini di panel progres"
            >
              {j.id.slice(0, 8)}
            </button>
            <span className="qsrc">
              {j.category === "analyze" ? "analisis" : "klip"} · {videoId(j.url)}
              {j.quality ? ` · ${j.quality}` : ""}
            </span>
            <span className={j.status}>
              {j.status === "done" ? "selesai" : j.status}
              {j.stage ? ` · ${j.stage}` : ""}
              {j.outputs ? ` · ${j.outputs} berkas` : ""}
            </span>
            <span className="qbar">
              <i style={{ width: `${j.percent}%` }} />
            </span>
            <span className="qpct">{j.percent.toFixed(0)}%</span>
            {OPEN.has(j.status) ? (
              <button type="button" className="danger" onClick={() => stop(j.id)}>
                Batal
              </button>
            ) : (
              <span className="qtime">{clock(j.created_at)}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Baris log dan daftar antrean sama-sama menampilkan jam lokal. */
function clock(ts: string): string {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? "--:--" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** URL panjang tidak muat di baris; id videonya cukup untuk mengenali sumber. */
function videoId(url?: string | null): string {
  if (!url) return "—";
  const m = url.match(/[?&]v=([\w-]{6,})/) ?? url.match(/youtu\.be\/([\w-]{6,})/);
  return m ? m[1] : url.slice(-11);
}
