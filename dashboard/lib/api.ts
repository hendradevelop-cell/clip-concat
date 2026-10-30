// uvicorn bind di 127.0.0.1 (IPv4 saja); "localhost" di browser bisa resolve ke
// ::1 lebih dulu dan connection refused karena tidak ada yang listen di IPv6.
export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export type Stage = { name: string; status: string; detail?: string | null };
export type LogLine = { ts: string; stage: string; level: "info" | "done" | "skip" | "error"; text: string };
export type Progress = { stages: Stage[]; percent: number; log?: LogLine[] };

export type CaptionPreset = "default" | "formal" | "karaoke";
export type Quality = "480p" | "720p" | "1080p";

/** Cermin FRAME_SIZES di types.py; "480p" dihitung dari sisi pendek frame. */
export const FRAME_SIZES: Record<"9:16" | "16:9", Record<Quality, [number, number]>> = {
  "9:16": { "480p": [480, 854], "720p": [720, 1280], "1080p": [1080, 1920] },
  "16:9": { "480p": [854, 480], "720p": [1280, 720], "1080p": [1920, 1080] },
};

/** Concat mau pecahan tinggi frame, bukan piksel. UI menampilkan piksel dari
 * resolusi terpilih: fontSize = px/tinggi, offsetY = px/(tinggi/2) karena
 * offset diukur dari tengah. Ganti resolusi = angka piksel ikut berubah supaya
 * tampilan relatif tetap sama. */
export const FRAME_W: Record<"9:16" | "16:9", Record<Quality, number>> = {
  "9:16": { "480p": 480, "720p": 720, "1080p": 1080 },
  "16:9": { "480p": 854, "720p": 1280, "1080p": 1920 },
};
export function frameHeight(aspect: "9:16" | "16:9", quality: Quality): number {
  const [, h] = FRAME_SIZES[aspect][quality];
  return h;
}
export const pxToFont = (px: number, h: number) => px / h;
export const fontToPx = (f: number, h: number) => Math.round(f * h);
export const pxToOffset = (px: number, h: number) => px / (h / 2);
export const offsetToPx = (f: number, h: number) => Math.round(f * h * 0.5);

/** Huruf mulai dari 10 piksel pada frame 1080p (1920). Yang dikirim ke Concat
 * tetap pecahan tinggi frame, jadi di resolusi lebih kecil slider-nya ikut
 * turun — angka 10 ini sekaligus jadi batas bawah kontrol ukuran huruf. */
export const DEFAULT_FONT_PX = 10;
export const DEFAULT_FONT_SIZE = DEFAULT_FONT_PX / 1920;

export type CaptionOpts = {
  enabled: boolean;
  preset: CaptionPreset;
  fontFamily: string;
  fontWeight: number;
  fontSize: number;
  offsetY: number;
  maxWords: number;
  color: string;
  strokeColor: string;
  strokeWidth: number;
  shadow: boolean;
  background: string;
  maxWidth: number;
};

/** Cermin `CAPTION_PRESETS` di services/py/videoflow/types.py. UI mengirim
 * semua field secara eksplisit, jadi tabel ini hanya mengisi kontrol saat
 * preset diganti. Ubah keduanya bersamaan. */
export const CAPTION_PRESETS: Record<CaptionPreset, Omit<CaptionOpts, "enabled" | "preset">> = {
  default: {
    fontFamily: "Montserrat", fontWeight: 800, fontSize: DEFAULT_FONT_SIZE, offsetY: 0.34, maxWords: 2,
    color: "#FFFFFF", strokeColor: "#000000", strokeWidth: 0.0005, shadow: true,
    background: "", maxWidth: 0.9,
  },
  formal: {
    fontFamily: "Inter", fontWeight: 500, fontSize: 0.062, offsetY: 0.4, maxWords: 6,
    color: "#FFFFFF", strokeColor: "#000000", strokeWidth: 0.003, shadow: false,
    background: "", maxWidth: 0.94,
  },
  karaoke: {
    fontFamily: "Montserrat", fontWeight: 900, fontSize: 0.11, offsetY: 0.3, maxWords: 1,
    color: "#FFE81A", strokeColor: "#000000", strokeWidth: 0.008, shadow: true,
    background: "#000000B3", maxWidth: 0.95,
  },
};

/** Cermin `CAPTION_FONTS` di types.py — hasil render nyata lewat
 * `python -m videoflow.scripts.font_check`. Concat diam-diam mengganti font yang
 * tidak dikenal dengan face default, jadi daftar ini dibatasi ke yang benar-benar
 * tampil: enam dibundel di concat.exe (Montserrat, Bebas Neue, Anton, Archivo
 * Black, Space Grotesk, Inter) sisanya font Windows. Poppins, Open Sans dan
 * Nunito Sans belum ada — pasang dulu lewat Settings > Fonts kalau mau dipakai.
 * Enam yang pertama tersedia di Google Fonts, jadi preview bisa cocok 1:1. */
export const FONTS = [
  "Montserrat", "Bebas Neue", "Anton", "Archivo Black", "Space Grotesk", "Inter",
  "Arial", "Impact", "Georgia",
];

export const DEFAULT_CAPTION: CaptionOpts = { enabled: true, preset: "default", ...CAPTION_PRESETS.default };

export type ClipperPayload = {
  youtube_url: string;
  clip_count: number;
  clip_min_s: number;
  clip_max_s: number;
  template_id: string;
  aspect: "9:16" | "16:9";
  quality: Quality;
  strategy: "auto" | "even" | "prompt";
  caption: CaptionOpts;
  ai_prompt?: string | null;
};

export type SourceAnalysis = {
  duration: number;
  topics: string[];
  windows: { start: number; dur: number; text: string; score: number }[];
  prompts: string[];
};

export type Job = {
  status: string;
  error?: string | null;
  created_at?: string;
  progress: Progress;
  result?: SourceAnalysis | null;
  outputs?: { path: string }[];
};

export async function fetchJob(id: string): Promise<Job> {
  const res = await fetch(`${API}/api/v1/jobs/${id}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`gagal ambil job ${id}: ${res.status}`);
  return res.json();
}

/** Analisis sumber: unduh + transkrip penuh, tanpa render Concat. */
export async function createAnalyzeJob(youtube_url: string, clip_min_s: number, clip_max_s: number) {
  const res = await fetch(`${API}/api/v1/analyze`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ youtube_url, clip_min_s, clip_max_s }),
  });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return (await res.json()).job_id as string;
}

export async function createClipperJob(payload: ClipperPayload): Promise<string> {
  const res = await fetch(`${API}/api/v1/jobs/clipper`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`${res.status}: ${detail}`);
  }
  return (await res.json()).job_id;
}

export async function fetchTemplates(kind: string) {
  const res = await fetch(`${API}/api/v1/templates?kind=${kind}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`gagal ambil template: ${res.status}`);
  return res.json();
}

/** Subscribe SSE progres. onProgress dipanggil tiap stage berubah. */
export function watchJob(jobId: string, onProgress: (p: Progress) => void): () => void {
  const es = new EventSource(`${API}/api/v1/jobs/${jobId}/events`);
  es.onmessage = (e) => {
    const p = JSON.parse(e.data) as Progress;
    onProgress(p);
    if (p.percent >= 100) es.close();
  };
  es.onerror = () => es.close();
  return () => es.close();
}

/** Ringkasan satu baris panel antrean. `progress` sengaja tidak ikut: satu job
 * bisa punya 300 baris log dan daftarnya akan membengkak. */
export type JobSummary = {
  id: string;
  category: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled" | string;
  error?: string | null;
  created_at: string;
  url?: string | null;
  quality?: Quality | null;
  percent: number;
  stage?: string | null;
  outputs: number;
};

export async function listJobs(limit = 20): Promise<JobSummary[]> {
  const res = await fetch(`${API}/api/v1/jobs?limit=${limit}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`gagal ambil antrean: ${res.status}`);
  return res.json();
}

/** Batalkan job. Yang masih antre tidak pernah dimulai; yang sedang berjalan
 * berhenti di batas tahap berikutnya, jadi render tidak menghasilkan file rusak. */
export async function cancelJob(id: string): Promise<void> {
  const res = await fetch(`${API}/api/v1/jobs/${id}/cancel`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `gagal membatalkan ${id.slice(0, 8)}: ${res.status}`);
  }
}
