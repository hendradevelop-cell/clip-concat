"use client";

import { useEffect, useRef, useState } from "react";
import CaptionPreview from "./CaptionPreview";
import JobProgress from "./JobProgress";
import JobQueue from "./JobQueue";
import {
  API,
  CAPTION_PRESETS,
  DEFAULT_CAPTION,
  DEFAULT_FONT_PX,
  DEFAULT_FONT_SIZE,
  FONTS,
  FRAME_W,
  type CaptionOpts,
  type CaptionPreset,
  type ClipperPayload,
  type Progress,
  type Quality,
  type SourceAnalysis,
  cancelJob,
  createAnalyzeJob,
  createClipperJob,
  fetchJob,
  fetchTemplates,
  fontToPx,
  frameHeight,
  offsetToPx,
  pxToFont,
  pxToOffset,
  watchJob,
} from "../lib/api";

type Template = { id: string; name: string };
type Source = Omit<ClipperPayload, "caption" | "ai_prompt">;

const DEFAULTS: Source = {
  youtube_url: "",
  clip_count: 3,
  clip_min_s: 30,
  clip_max_s: 90,
  template_id: "",
  aspect: "9:16",
  quality: "1080p",
  strategy: "auto",
};

const QUALITIES: Quality[] = ["480p", "720p", "1080p"];

const LAST_JOB = "videoflow:lastClipperJob";

/** Judul panel progres; "queued" harus dibedakan dari "running" karena antrean
 * panjang pernah terlihat seperti macet. */
const JOB_LABEL: Record<string, string> = {
  queued: "mengantre",
  running: "sedang dikerjakan",
  done: "selesai",
  failed: "gagal",
  cancelled: "dibatalkan",
};

const PRESET_LABEL: Record<CaptionPreset, string> = {  default: "default (2 kata, tebal)",
  formal: "formal (kalimat utuh)",
  karaoke: "karaoke (1 kata per baris)",
};

function alphaOf(hex8: string): number {
  return hex8.length >= 9 ? Math.round((parseInt(hex8.slice(7, 9), 16) / 255) * 100) : 100;
}

function withAlpha(hex6: string, percent: number): string {
  const a = Math.max(0, Math.min(255, Math.round((percent / 100) * 255)));
  return `${hex6}${a.toString(16).padStart(2, "0").toUpperCase()}`;
}

export default function ClipperForm() {
  const [form, setForm] = useState<Source>(DEFAULTS);
  const [cap, setCap] = useState<CaptionOpts>(DEFAULT_CAPTION);
  const [prompt, setPrompt] = useState("");
  const [preview, setPreview] = useState("harga saham naik tajam hari ini karena berita");
  const [templates, setTemplates] = useState<Template[]>([]);
  const [apiOk, setApiOk] = useState<boolean | null>(null);
  const [concatOk, setConcatOk] = useState<boolean | null>(null);
  const [concatVersion, setConcatVersion] = useState<string | null>(null);
  const [concatDetail, setConcatDetail] = useState<string | null>(null);
  const [concatBusy, setConcatBusy] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState<string | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [outputs, setOutputs] = useState<{ path: string }[]>([]);
  const [analysis, setAnalysis] = useState<SourceAnalysis | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeJob, setAnalyzeJob] = useState<string | null>(null);
  const unwatch = useRef<(() => void) | null>(null);
  const stopAnalyze = useRef(false);

  useEffect(() => {
    fetchTemplates("clipper")
      .then((rows) => {
        setTemplates(rows as Template[]);
        setForm((f) => ({ ...f, template_id: f.template_id || rows[0]?.id || "" }));
      })
      .catch((e) => setError(`Template: ${e.message}`));

    fetch(`${API}/api/v1/concat/health`)
      .then(async (r) => {
        const h = await r.json().catch(() => null);
        setApiOk(Boolean(h));
        setConcatOk(h ? Boolean(h.ok) : null);
        setConcatBusy(Boolean(h?.busy));
        setConcatVersion(h?.version?.concat ?? null);
        setConcatDetail(h?.ok ? null : (h?.error ?? null));
      })
      .catch(() => {
        setApiOk(false);
        setConcatOk(null);
      });

    return () => unwatch.current?.();
  }, []);

  // Job yang sedang antre atau berjalan harus tetap kelihatan setelah halaman
  // di-reload; tanpa ini pengguna mengira tombolnya tidak bekerja.
  useEffect(() => {
    const id = localStorage.getItem(LAST_JOB);
    if (id) follow(id).catch(() => localStorage.removeItem(LAST_JOB));
  }, []);

  useEffect(() => {
    if (jobId) localStorage.setItem(LAST_JOB, jobId);
  }, [jobId]);

  /** Ambil status dari DB lalu pasang SSE kalau job masih hidup. */
  async function follow(id: string) {
    setJobId(id);
    const job = await fetchJob(id);
    setJobStatus(job.status);
    setProgress(job.progress);
    setOutputs(job.outputs ?? []);
    setError(job.status === "failed" ? (job.error ?? "job gagal") : null);
    if (job.status === "queued" || job.status === "running") attach(id);
  }

  function attach(id: string) {
    unwatch.current?.();
    unwatch.current = watchJob(id, (p) => {
      setProgress(p);
      if (p.percent >= 100) loadOutputs(id);
    });
  }

  // EventSource langsung menutup dirinya sendiri saat koneksi putus, dan
  // setelah itu panel diam di snapshot terakhir padahal worker masih jalan —
  // job yang sedang Whisper 50 menit terlihat seperti macet di 0%. Poll tipis
  // ini menjaga panel tetap jujur dan sekaligus menarik status `cancelled` dari
  // DB, karena pembatalan tidak lewat jalur SSE.
  useEffect(() => {
    if (!jobId || !jobStatus || !["queued", "running"].includes(jobStatus)) return;
    const t = setInterval(() => {
      fetchJob(jobId)
        .then((job) => {
          setJobStatus(job.status);
          setProgress(job.progress);
          setOutputs(job.outputs ?? []);
          if (job.status === "failed") setError(job.error ?? "job gagal");
        })
        .catch(() => {});
    }, 10_000);
    return () => clearInterval(t);
  }, [jobId, jobStatus]);

  /** Batal = hapus dari antrean broker + hentikan di batas tahap berikutnya. */
  async function stop() {
    if (!jobId) return;
    setError(null);
    try {
      await cancelJob(jobId);
      await follow(jobId);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const set = <K extends keyof Source>(k: K, v: Source[K]) => setForm((f) => ({ ...f, [k]: v }));
  const setC = <K extends keyof CaptionOpts>(k: K, v: CaptionOpts[K]) =>
    setCap((c) => ({ ...c, [k]: v }));

  /** Mengganti preset menimpa semua knob, sama seperti di backend. */
  const pickPreset = (p: CaptionPreset) => setCap((c) => ({ ...c, preset: p, ...CAPTION_PRESETS[p] }));

  const custom = (Object.keys(CAPTION_PRESETS[cap.preset]) as (keyof typeof CAPTION_PRESETS["default"])[]).some(
    (k) => cap[k] !== CAPTION_PRESETS[cap.preset][k]
  );

  // Semua knob piksel diukur terhadap tinggi frame output yang akan dirender.
  const h = frameHeight(form.aspect, form.quality);

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setError(null);
    setProgress(null);
    setOutputs([]);
    try {
      const id = await createClipperJob({ ...form, caption: cap, ai_prompt: prompt.trim() || null });
      setJobId(id);
      setJobStatus("queued");
      // Panel muncul segera; tanpa ini ada jeda kosong sampai worker mengambil job.
      setProgress({
        stages: [],
        percent: 0,
        log: [{ ts: new Date().toISOString(), stage: "job", level: "info",
                text: `job ${id.slice(0, 8)} dikirim, menunggu worker…` }],
      });
      attach(id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function loadOutputs(id: string) {
    const res = await fetch(`${API}/api/v1/jobs/${id}`);
    const job = await res.json();
    setOutputs(job.outputs ?? []);
    setJobStatus(job.status);
    if (job.status === "failed") setError(job.error ?? "job gagal");
  }

  /** Analisis sumber bisa makan waktu beberapa menit (unduh + Whisper), jadi
   * hasilnya diambil dengan polling — SSE akan menutup stream lebih dulu. */
  async function analyzeSource() {
    stopAnalyze.current = false;
    setError(null);
    setAnalysis(null);
    setProgress(null);
    setAnalyzing(true);
    try {
      const id = await createAnalyzeJob(form.youtube_url, form.clip_min_s, form.clip_max_s);
      setAnalyzeJob(id);
      for (;;) {
        await new Promise((r) => setTimeout(r, 4000));
        const job = await fetchJob(id);
        setProgress(job.progress);
        if (job.status === "failed") throw new Error(job.error ?? "analisis gagal");
        if (job.result) {
          setAnalysis(job.result);
          break;
        }
        if (stopAnalyze.current) break;
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setAnalyzing(false);
    }
  }

  const addTerm = (t: string) => {
    setPrompt((p) => (p.toLowerCase().includes(t.toLowerCase()) ? p : `${p} ${t}`.trim()));
    set("strategy", "prompt");
  };

  return (
    <div className="shell">
      <div>
        {apiOk === false && (
          <div className="err">
            Browser tidak bisa membaca respons dari <code>{API}</code>. Cek API hidup
            (<code>uvicorn videoflow.api.server:app --port 8000</code>), worker jalan
            (<code>celery -A videoflow.worker:celery_app worker -Q videoflow --pool=solo</code>),
            dan origin dashboard ini ada di <code>VF_CORS_ORIGINS</code>.
          </div>
        )}
        {apiOk === true && concatOk === false && concatBusy && (
          <div className="warnbox">
            Concat sedang dipakai render lain (satu instance), jadi statusnya belum bisa
            diperiksa. Kirim job tetap bisa — ia masuk antrean dan menunggu lock render
            lepas.
          </div>
        )}
        {apiOk === true && concatOk === false && !concatBusy && (
          <div className="err">
            Concat tidak bisa di-spawn dari mesin API. Periksa <code>VF_CONCAT_BIN</code>{" "}
            (atau <code>concat</code> di PATH) dan <code>VF_CONCAT_MODE</code>.
            {concatDetail && (
              <>
                <br />
                <code>{concatDetail}</code>
              </>
            )}
          </div>
        )}
        {apiOk === null && <p className="note">Cek koneksi backend…</p>}
        {apiOk === true && concatOk === null && <p className="note">Cek koneksi Concat…</p>}
        {concatOk === true && concatVersion && (
          <p className="note">Concat {concatVersion} siap (MCP headless).</p>
        )}

        <form className="card" onSubmit={submit}>
          <fieldset>
            <legend>Sumber</legend>
            <div className="grid">
              <div className="full">
                <label htmlFor="yt">Link YouTube</label>
                <input
                  id="yt"
                  type="url"
                  required
                  placeholder="https://www.youtube.com/watch?v=…"
                  value={form.youtube_url}
                  onChange={(e) => set("youtube_url", e.target.value)}
                />
              </div>
              <div>
                <label htmlFor="count">Jumlah klip</label>
                <input
                  id="count"
                  type="number"
                  min={1}
                  max={12}
                  value={form.clip_count}
                  onChange={(e) => set("clip_count", Number(e.target.value))}
                />
              </div>
              <div>
                <label htmlFor="aspect">Rasio</label>
                <select
                  id="aspect"
                  value={form.aspect}
                  onChange={(e) => set("aspect", e.target.value as ClipperPayload["aspect"])}
                >
                  <option value="9:16">9:16 vertikal</option>
                  <option value="16:9">16:9 horizontal</option>
                </select>
              </div>

              <div className="full">
                <label id="q-label">Kualitas video (resolusi output)</label>
                <div className="seg" role="group" aria-labelledby="q-label">
                  {QUALITIES.map((q) => (
                    <button
                      type="button"
                      key={q}
                      className={form.quality === q ? "on" : ""}
                      aria-pressed={form.quality === q}
                      onClick={() => set("quality", q)}
                    >
                      {q}
                    </button>
                  ))}
                </div>
                <p className="hint">
                  {FRAME_W[form.aspect][form.quality]}×{frameHeight(form.aspect, form.quality)} piksel.
                  1080p standar TikTok/Reels; 720p cukup untuk hemat kuota; 480p hanya untuk uji cepat.
                </p>
              </div>
              <div>
                <label htmlFor="min">Min. durasi (detik)</label>
                <input
                  id="min"
                  type="number"
                  min={5}
                  value={form.clip_min_s}
                  onChange={(e) => set("clip_min_s", Number(e.target.value))}
                />
              </div>
              <div>
                <label htmlFor="max">Maks. durasi (detik)</label>
                <input
                  id="max"
                  type="number"
                  min={10}
                  value={form.clip_max_s}
                  onChange={(e) => set("clip_max_s", Number(e.target.value))}
                />
              </div>
            </div>
          </fieldset>

          <fieldset>
            <legend>Subtitle</legend>
            <div className="grid">
              <div className="full">
                <label className="check">
                  <input
                    type="checkbox"
                    checked={cap.enabled}
                    onChange={(e) => setC("enabled", e.target.checked)}
                  />
                  Tampilkan subtitle
                </label>
                <p className="hint">Mati = transkripsi Whisper dilewati, jadi job jauh lebih cepat.</p>
              </div>

              <div>
                <label htmlFor="preset">Gaya</label>
                <select
                  id="preset"
                  value={cap.preset}
                  onChange={(e) => pickPreset(e.target.value as CaptionPreset)}
                >
                  {(Object.keys(PRESET_LABEL) as CaptionPreset[]).map((p) => (
                    <option key={p} value={p}>
                      {PRESET_LABEL[p]}
                    </option>
                  ))}
                </select>
                {custom && <p className="hint">kustom — berbeda dari preset</p>}
              </div>

              <div>
                <label htmlFor="tpl">Template dasar (Concat)</label>
                <select
                  id="tpl"
                  required
                  value={form.template_id}
                  onChange={(e) => set("template_id", e.target.value)}
                >
                  {templates.length === 0 && <option value="">(belum ada template)</option>}
                  {templates.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}
                    </option>
                  ))}
                </select>
                <p className="hint">Knob di bawah menimpa isi template.</p>
              </div>

              <div>
                <label htmlFor="font">Font</label>
                <select id="font" value={cap.fontFamily} onChange={(e) => setC("fontFamily", e.target.value)}>
                  {FONTS.map((f) => (
                    <option key={f} value={f}>
                      {f}
                    </option>
                  ))}
                </select>
                <p className="hint">
                  Hanya yang benar-benar dirender Concat. Poppins / Open Sans / Nunito Sans
                  belum dipasang di mesin ini — Concat menggantinya diam-diam dengan Arial.
                </p>
              </div>

              <div>
                <label htmlFor="words">Maks. kata di layar</label>
                <input
                  id="words"
                  type="number"
                  min={1}
                  max={8}
                  value={cap.maxWords}
                  onChange={(e) => setC("maxWords", Number(e.target.value))}
                />
              </div>

              <div className="full">
                <label htmlFor="size">Ukuran huruf (piksel)</label>
                <div className="slider">
                  <input
                    id="size"
                    type="range"
                    min={fontToPx(DEFAULT_FONT_SIZE, h)}
                    max={Math.round(0.16 * h)}
                    step={1}
                    value={fontToPx(cap.fontSize, h)}
                    onChange={(e) => setC("fontSize", pxToFont(Number(e.target.value), h))}
                  />
                  <input
                    type="number"
                    aria-label="Ukuran huruf manual"
                    min={fontToPx(DEFAULT_FONT_SIZE, h)}
                    max={Math.round(0.16 * h)}
                    step={1}
                    value={fontToPx(cap.fontSize, h)}
                    onChange={(e) => setC("fontSize", pxToFont(Number(e.target.value), h))}
                  />
                </div>
                <p className="hint">
                  Tinggi huruf kapital dalam piksel pada output {form.quality} (tinggi frame {h} piksel).
                  Default {DEFAULT_FONT_PX} piksel di 1080p — naikkan kalau hasilnya terlalu kecil.
                </p>
              </div>

              <div className="full">
                <label htmlFor="pos">Posisi vertikal (piksel)</label>
                <div className="slider">
                  <input
                    id="pos"
                    type="range"
                    min={-Math.round(0.48 * h * 0.5)}
                    max={Math.round(0.48 * h * 0.5)}
                    step={1}
                    value={offsetToPx(cap.offsetY, h)}
                    onChange={(e) => setC("offsetY", pxToOffset(Number(e.target.value), h))}
                  />
                  <input
                    type="number"
                    aria-label="Posisi vertikal manual"
                    min={-Math.round(0.48 * h * 0.5)}
                    max={Math.round(0.48 * h * 0.5)}
                    step={1}
                    value={offsetToPx(cap.offsetY, h)}
                    onChange={(e) => setC("offsetY", pxToOffset(Number(e.target.value), h))}
                  />
                </div>
                <p className="hint">
                  Jarak dari tengah frame dalam piksel; 0 = tengah, positif ke bawah. Garis putus di
                  pratinjau = area aman UI aplikasi.
                </p>
              </div>

              <div>
                <label htmlFor="cmain">Warna utama</label>
                <input
                  id="cmain"
                  type="color"
                  value={cap.color}
                  onChange={(e) => setC("color", e.target.value.toUpperCase())}
                />
              </div>
              <div>
                <label htmlFor="csec">Warna garis tepi</label>
                <input
                  id="csec"
                  type="color"
                  value={cap.strokeColor}
                  onChange={(e) => setC("strokeColor", e.target.value.toUpperCase())}
                />
              </div>

              <div className="full">
                <label className="check">
                  <input
                    type="checkbox"
                    checked={cap.background !== ""}
                    onChange={(e) => setC("background", e.target.checked ? "#000000B3" : "")}
                  />
                  Plat latar belakang
                </label>
                {cap.background !== "" && (
                  <div className="slider" style={{ marginTop: 8 }}>
                    <input
                      type="color"
                      aria-label="Warna plat"
                      value={`#${cap.background.slice(1, 7)}`}
                      onChange={(e) =>
                        setC("background", withAlpha(e.target.value.toUpperCase(), alphaOf(cap.background)))
                      }
                    />
                    <input
                      type="number"
                      aria-label="Kekasaran plat persen"
                      min={0}
                      max={100}
                      value={alphaOf(cap.background)}
                      onChange={(e) =>
                        setC("background", withAlpha(cap.background.slice(0, 7), Number(e.target.value)))
                      }
                    />
                  </div>
                )}
              </div>

              <div>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={cap.shadow}
                    onChange={(e) => setC("shadow", e.target.checked)}
                  />
                  Bayangan
                </label>
              </div>
              <div>
                <label htmlFor="stroke">Tebal garis tepi</label>
                <input
                  id="stroke"
                  type="number"
                  min={0}
                  max={0.03}
                  step={0.0001}
                  value={cap.strokeWidth}
                  onChange={(e) => setC("strokeWidth", Number(e.target.value))}
                />
              </div>
            </div>
          </fieldset>

          <fieldset>
            <legend>Potongan</legend>
            <div className="grid">
              <div>
                <label htmlFor="strategy">Pemilihan titik potong</label>
                <select
                  id="strategy"
                  value={form.strategy}
                  onChange={(e) => set("strategy", e.target.value as ClipperPayload["strategy"])}
                >
                  <option value="auto">auto (sisihkan intro/outro)</option>
                  <option value="even">even (dari awal video)</option>
                  <option value="prompt" disabled={!prompt.trim()}>
                    prompt (ikuti instruksi AI)
                  </option>
                </select>
              </div>
            </div>
            <div style={{ marginTop: 14 }}>
              <label htmlFor="ai">Instruksi AI untuk pemotongan</label>
              <textarea
                id="ai"
                maxLength={600}
                placeholder="contoh: ambil bagian yang menyebut angka dan kesimpulan, hindari basa-basi pembuka"
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
              />
              <p className="hint">
                Ditulis bebas; pipeline mencocokkan kata-katanya dengan transkrip untuk
                memilih jendela potongan. Klik <em>Analisis sumber</em> untuk melihat isinya dulu.
              </p>
              <div className="row">
                <button type="button" className="ghost" onClick={analyzeSource} disabled={analyzing || !form.youtube_url}>
                  {analyzing ? "Menganalisis…" : "Analisis sumber"}
                </button>
                {analyzing && (
                  <button type="button" className="ghost" onClick={() => (stopAnalyze.current = true)}>
                    Hentikan pemantauan
                  </button>
                )}
                {analyzeJob && <span className="note">analisis {analyzeJob.slice(0, 8)}</span>}
              </div>

              {analysis && (
                <>
                  <p className="hint">
                    Topik paling sering muncul di {Math.round(analysis.duration)} detik transkrip —
                    klik untuk menambahkannya ke instruksi:
                  </p>
                  <div className="chips">
                    {analysis.topics.map((t) => (
                      <button type="button" key={t} className="chip" onClick={() => addTerm(t)}>
                        {t}
                      </button>
                    ))}
                  </div>
                  <p className="hint">Prompt siap pakai, dibuat dari jendela terbaik:</p>
                  <div className="chips">
                    {analysis.prompts.map((p) => (
                      <button
                        type="button"
                        key={p}
                        className="chip"
                        onClick={() => {
                          setPrompt(p);
                          set("strategy", "prompt");
                        }}
                      >
                        {p}
                      </button>
                    ))}
                  </div>
                </>
              )}
            </div>
          </fieldset>

          <div className="row">
            {/* concatOk sengaja tidak ikut mematikan tombol: Concat cuma satu
                instance, jadi "tidak bisa diperiksa" biasanya berarti sedang
                dipakai render lain — antrean justru untuk itu. */}
            <button type="submit" disabled={busy || apiOk === false}>
              {busy ? "Mengirim…" : "Buat klip"}
            </button>
            {jobId && (jobStatus === "queued" || jobStatus === "running") && (
              <button type="button" className="danger" onClick={stop}>
                Batalkan job
              </button>
            )}
            {jobId && (
              <button type="button" className="ghost" onClick={() => unwatch.current?.()}>
                Putus pemantauan
              </button>
            )}
            {jobId && <span className="note">job {jobId}</span>}
          </div>
        </form>

        {error && <div className="err">{error}</div>}

        {progress && (
          <JobProgress
            progress={progress}
            status={jobStatus}
            job={jobId}
            label={analyzing ? "analisis sumber" : (JOB_LABEL[jobStatus ?? ""] ?? "render klip")}
          />
        )}

        <JobQueue activeId={jobId} onWatch={(id) => void follow(id)} />

        {outputs.length > 0 && (
          <div className="card" style={{ marginTop: 18 }}>
            <h2 style={{ fontSize: 15, margin: "0 0 10px" }}>Hasil</h2>
            {/* Hanya tautan: pemain video di sini membuat halaman menarik
                setiap klip ke browser padahal yang dibutuhkan cuma unduhan. */}
            {outputs.map((o) => {
              const href = `${API}/api/v1/download?path=${encodeURIComponent(o.path)}`;
              return (
                <p key={o.path} className="note">
                  <a href={href}>unduh</a> · {o.path}
                </p>
              );
            })}
          </div>
        )}
      </div>

      <aside className="side">
        <CaptionPreview cap={cap} aspect={form.aspect} quality={form.quality} text={preview} onText={setPreview} />
        <p className="hint">
          Pratinjau browser: komposisi dan warna akurat, tapi painter Concat bisa berbeda beberapa
          persen, dan font harus ada di mesin concat.
        </p>
      </aside>
    </div>
  );
}
