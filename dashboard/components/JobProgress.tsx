"use client";

import { useEffect, useRef } from "react";

import type { Progress } from "../lib/api";

/** Jam UTC -> jam lokal singkat, cukup untuk melihat urutan kejadian. */
function clock(ts: string): string {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? "--:--:--" : d.toLocaleTimeString([], { hour12: false });
}

export default function JobProgress({
  progress,
  label,
  status,
  job,
}: {
  progress: Progress;
  label: string;
  status?: string | null;
  job?: string | null;
}) {
  const log = progress.log ?? [];
  const box = useRef<HTMLPreElement>(null);

  // Log tumbuh ke bawah; tanpa ini panel diam di atas saat job sudah jauh jalan.
  useEffect(() => {
    const el = box.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [log.length]);

  return (
    <div className="card" style={{ marginTop: 18 }}>
      <div className="prog">
        <div>
          <span className="pct">{progress.percent.toFixed(0)}%</span>
          <span className="note" style={{ margin: 0 }}> {label}</span>
        </div>
        <span className="note" style={{ margin: 0 }}>
          {/* Panel ini cuma memantau satu job; id-nya harus terbaca supaya baris
              di daftar antrean bisa dicocokkan. */}
          {job ? `${job.slice(0, 8)} · ` : ""}
          {progress.stages.filter((s) => s.status === "done" || s.status === "skipped").length}/
          {progress.stages.length} tahap
        </span>
      </div>
      <div className="bar">
        <i style={{ width: `${progress.percent}%` }} />
      </div>

      <ul className="stages">
        {progress.stages.map((s) => (
          <li key={s.name}>
            <span>{s.name}</span>
            <span className={s.status}>
              {s.status}
              {s.detail ? ` — ${s.detail}` : ""}
            </span>
          </li>
        ))}
      </ul>

      <p className="sect">Log aktivitas</p>
      <pre ref={box} className="log">
        {/* SSE mengirim snapshot DB lebih dulu dan isinya kosong selama job masih
            antre, jadi status-lah yang membuat baris ini bermakna. */}
        {log.length === 0 && (
          <span className="info">
            {status === "queued" ? "job antre, menunggu worker…" : "menunggu…"}
          </span>
        )}
        {log.map((l, i) => (
          <span key={i} className={l.level}>
            {clock(l.ts)} {l.stage.padEnd(18, " ")} {l.text}
            {"\n"}
          </span>
        ))}
      </pre>
    </div>
  );
}
