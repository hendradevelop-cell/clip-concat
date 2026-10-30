# videoflow

Pipeline otomasi video di atas **Concat** (editor Rust + GPU compositor, AGPL-3.0).

Status: **shared layer + Kategori 1 (Clipper) selesai dan terbukti render** terhadap
Concat 0.2.8 asli. Kategori 2 (News) & 3 (Avatar) masih tahap desain.

```
Next.js dashboard  ->  FastAPI  ->  Celery worker  ->  concat --mcp headless
                                        |                (proses anak, JSON-RPC
                                        |                 lewat stdin/stdout)
                                        +-> yt-dlp / ffmpeg / faster-whisper
```

Pembagian kerja yang disengaja:

| Tugas | Eksekutor | Alasan |
|---|---|---|
| Unduh YouTube | `yt-dlp` | — |
| Potong klip | `ffmpeg` (re-encode) | akurat & murah |
| Transkripsi | `faster-whisper` | 42 tool MCP Concat tidak punya ASR; `captions_add` hanya menaruh teks yang sudah ada |
| Framing 9:16, style subtitle, render | **Concat** | GPU compositor, H.264/HEVC/AV1 |

## 1. Transpor: MCP stdio, bukan TCP

Build desktop Concat 0.2.8 **tidak** punya server TCP dan tidak punya
`concat-cli`. Yang ada hanya:

```
concat [--project <DIR> | --mcp [auto|window|headless] | --version | --smoke | --whisper-cpu]
```

`videoflow/concat.py` karenanya men-spawn `concat --mcp headless` sebagai proses
anak dan bicara JSON-RPC 2.0 newline-delimited melalui stdin/stdout-nya. Konsekuensi:

* **worker dan api harus di mesin yang sama dengan concat binary** — tidak bisa
  dikontainerkan terpisah (lihat `docker-compose.yml`);
* `concat_version` melaporkan `mcp.writesUnder` (pada Windows: home user), jadi
  `VF_DATA_DIR` harus di bawahnya;
* `--mcp=headless` **ditolak**, nilainya harus argumen terpisah; mode `window`
  akan menempel ke jendela Concat yang terbuka dan mode `auto` bisa memunculkan
  dialog — karena itu default proyek ini `headless`;
* satu proses headless dipakai satu job (N klip berurutan), lalu ditutup.

Cek terhadap binary yang terpasang:

```bash
cd services/py
python -m videoflow.scripts.mcp_check                      # versi + 42 tool
python -m videoflow.scripts.mcp_check                      # PROBE_OUT=tools.json = dump skema
MCP_RENDER=1 python -m videoflow.scripts.mcp_check          # satu render vertikal betulan
```

## 2. Peta tool yang dipakai pipeline

| Langkah | Tool | Yang penting |
|---|---|---|
| cek build | `concat_version` | `capabilities` = `["events","mcp"]`, `mcp.writesUnder` |
| buka proyek | `project_create location,name,video` | balasan **tidak** memuat path; foldernya `location/name` |
| masuk bin | `media_import path,file` | id baru ada di `createdId`; menolak file tanpa stream video/audio |
| cari lane | `timeline_read path` | lane gambar = `timeline.tracks[0].id` (`T1`) |
| taruh klip | `clip_add path,mediaId,trackId,start` | `trackId` wajib; id klip dari `createdId` |
| isi frame | `clip_frame path,clipId,framing` | hanya `"fit"` (bar) atau `"fill"` (crop) |
| subtitle | `captions_add path,captions[],style,offsetY` | satu panggilan untuk semua cue; `offsetY` positif ke bawah, default `0.35` |
| render | `export_start path,output,crf,preset,codec,width,height` | satu render pada satu waktu |
| kawal | `export_status job` | `state`: `running`/`done`/`cancelled`/`failed`, plus `frame`/`total` |
| beres | `project_close path,save` | `save:false` karena folder proyek dibuang setelah render |

`export_wait` juga ada (memblokir sampai 600 dtk); proyek ini sengaja memakai
`export_status` yang di-poll supaya progress bar ikut bergerak.

## 3. Jalankan stack

```bash
# infra saja (api + worker tidak ada di compose, lihat bagian 1)
docker compose up -d postgres redis
psql "$VF_DATABASE_URL" -f services/py/schema.sql

cd services/py
python -m venv .venv && . .venv/bin/activate
pip install -e ".[captions,dev]"
cp .env.example .env         # sesuaikan VF_CONCAT_BIN, VF_DATA_DIR, VF_WHISPER_DEVICE

python -m videoflow.scripts.seed_template --json ../examples/style-bold-yellow.json

uvicorn videoflow.api.server:app --reload --port 8000
celery -A videoflow.worker:celery_app worker -Q videoflow --concurrency=1 -l info

cd dashboard && npm install && npm run dev
```

`--concurrency=1` itu bukan preferensi: Concat menjalankan satu render pada satu
waktu, dan `_ExportLock` (Redis) menahan stage render supaya tidak pernah ada dua.

Di Windows, pool bawaan (prefork/spawn) mati dengan `PermissionError: [WinError 5]`
pada semlock, jadi pakai `--pool=solo`:

```bash
celery -A videoflow.worker:celery_app worker -Q videoflow --pool=solo -l info
```

### Windows: `start.bat` / `stop.bat`

Dua file di root repository adalah pintasan untuk tumpukan di atas — tidak perlu
mengaktifkan venv atau menghafal urutan start:

```
start.bat   # Docker Desktop + postgres/redis, lalu API, worker, dashboard
stop.bat    # uvicorn + worker + node + concat.exe anak, lalu (opsional) container
```

`start.bat` memakai `services\py\.venv\Scripts\python.exe` langsung (tanpa
`activate`), memulai Docker Desktop kalau daemon-nya belum jalan, dan membuka satu
jendela `cmd /k` per proses supaya log tetap terbaca; sebelum memulai tiap proses ia
cek dulu apakah port-nya sudah didengar (atau apakah worker sudah hidup) — dua
uvicorn pada port yang sama mati diam-diam, dan dua worker pada satu antrean membuat
job diambil proses yang mana saja. Worker dijalankan dengan `--pool=solo` dan
`--logfile=worker.log`, API dengan `--host 127.0.0.1` — uvicorn hanya bind IPv4, dan
`localhost` di browser bisa resolve ke `::1` lebih dulu sehingga connection refused.

Catatan mesin: `faster-whisper large-v3` pada CPU tanpa AVX2 (mis. Celeron) tidak
realistis — pakai GPU, atau turunkan `VF_WHISPER_MODEL` ke `small`/`distil-large-v3`.

## 4. Submit job Clipper

```bash
curl -sX POST localhost:8000/api/v1/jobs/clipper \
  -H 'content-type: application/json' \
  -d '{
    "youtube_url": "https://www.youtube.com/watch?v=XXXXXXX",
    "clip_count": 3,
    "clip_min_s": 30, "clip_max_s": 90,
    "template_id": "bold-yellow-1",
    "aspect": "9:16",
    "quality": "1080p",
    "strategy": "auto"
  }'

# progres real-time (SSE, sumber: Redis channel per job)
curl -N localhost:8000/api/v1/jobs/<job_id>/events

# hasil
curl -s localhost:8000/api/v1/jobs/<job_id> | jq '.outputs[].path'
```

`strategy: "auto"` menyisihkan 5% awal + 5% akhir video supaya klip tidak dibuka
dengan intro/overlay channel.

### Progres & log aktivitas

`Tracker` (`videoflow/progress.py`) adalah satu-satunya penulis progres. Setiap
perubahan: tulis `jobs.progress` (jsonb) **dan** publish ke Redis channel
`videoflow:job:<id>`; endpoint SSE cuma meneruskan itu. Bentuknya:

```json
{ "stages": [{"name": "download", "status": "done", "detail": "source.mp4"}],
  "percent": 50.0,
  "log": [{"ts": "2026-10-27T12:33:53+00:00", "stage": "download",
           "level": "done", "text": "done: source.mp4"}] }
```

`level` ∈ `info | done | skip | error`. Isinya jejak waktu nyata dari worker, bukan
perkiraan dari sisi UI — termasuk `render frame 45/90 (50%)` yang berasal dari
polling `export_status` tiap 5 detik. Log dipangkas dari belakang (`LOG_MAX=300`)
karena payload dikirim utuh setiap flush.

Whisper bisa memakan puluhan menit tanpa satu pun kejadian baru, jadi ada thread
detak (`BEAT_S=45`) yang menulis `masih berjalan · 12:34` kalau sebuah tahap sudah
`running` dan tidak ada flush lain selama itu. Detak tidak mengubah `percent` —
persentase hanya boleh bergerak oleh pekerjaan yang benar-benar selesai.
Di dalam transkrip sendiri faster-whisper memberi batas waktu tiap segmen, jadi
`Tracker.asr_progress` menulis `transkrip 12:30 / 25:32 (49%)` tiap 20 cue dan
menggerakkan bar persentase; tanpa itu job 25 menit terbaca macet di 50% satu jam
lamanya. `Tracker.close()` dipanggil di `finally` task Celery; worker hidup
berjam-jam dan satu Tracker dibuat per job.

Dashboard (`components/JobProgress.tsx`) menampilkan persen besar, bar, daftar
tahap, dan konsol log yang auto-scroll. ID job terakhir disimpan di
`localStorage` (`videoflow:lastClipperJob`) dan dipantau ulang saat halaman
dibuka, jadi refresh atau tab baru tidak kehilangan job yang masih antre.

### Membatalkan job

`POST /api/v1/jobs/{id}/cancel` menempuh dua jalur, dan keduanya diperlukan:
`celery_app.control.revoke(task_id)` membuang pesan yang masih antre di broker,
sedangkan status `cancelled` di DB menahan worker yang sudah terlanjur mengerjakan
jobnya. `id` Celery itu dicatat di kolom `jobs.task_id` saat enqueue — tanpa itu
tombol Batal cuma mengubah angka di dashboard sementara render tetap jalan.

Pembatalan berlaku pada **titik teraman terdekat**, bukan di tengah pekerjaan yang
menyisakan rusak: `Tracker.asr_progress` memeriksanya tiap 20 cue (generator
Whisper berhenti di antara segmen tanpa menulis apa pun), `Tracker.start()` di batas
tahap. Hanya render Concat tidak pernah dipotong — file output-nya akan rusak dan
lock export tertinggal — jadi untuk job yang sedang di tahap `export`, Batal baru
terlihat setelah render itu selesai. Job yang sudah `cancelled` tidak bisa dihidupkan
kembali: `Tracker.status()` menolak menulis `done` atas nama worker lama.

Daftar antrean diambil dari `GET /api/v1/jobs?limit=` — ringkasan per baris
(`percent`, nama tahap yang sedang `running`, jumlah output) tanpa 300 baris log,
karena payload penuh 20 job akan membebani polling 5 detik di dashboard
(`components/JobQueue.tsx`).

Catatan operasi: Celery memuat modul saat worker start. Restart worker ketika
ada job nyata sedang berjalan meninggalkan baris `running` yatim di `jobs` —
sumber dan transkrip tetap ter-cache, jadi cukup kirim ulang job yang sama.

### Kualitas video

`quality` memilih resolusi render — hanya `480p`, `720p`, `1080p` (UI-nya tombol
segmental, bukan dropdown). Label dihitung dari **sisi pendek** frame:

| rasio | 480p | 720p | 1080p |
| --- | --- | --- | --- |
| `9:16` | 480×854 | 720×1280 | 1080×1920 |
| `16:9` | 854×480 | 1280×720 | 1920×1080 |

Ukurannya dipakai dua kali: `project_create` (frame proyek) dan `export_start`
(besar file), lewat satu pemanggil `frame_size()` supaya tidak bisa beda.
1080p adalah standar TikTok/Reels; 480p berguna untuk cek cepat komposisi karena
render Concat memakan waktu sekitar durasi video.

### Knob subtitle

Semua kontrol UI dikirim sebagai `caption`; field yang tidak disebut diisi dari
preset di `videoflow/types.py` (`CAPTION_PRESETS`), jadi `{"preset":"karaoke"}`
sudah cukup untuk satu kata per baris:

```bash
  -d '{ ..., "caption": { "preset": "default", "maxWords": 2, "offsetY": 0.34 } }'
```

`fontSize`/`strokeWidth` bukan piksel: keduanya pecahan tinggi frame (cap height),
`offsetY` diukur dari tengah frame — positif ke bawah. `maxWords` memecah transkrip
pada batas waktu kata (`word_timestamps` Whisper), jadi barisnya tidak dibagi rata.
Karaoke bukan highlight dua warna: `captions_add` cuma punya satu style untuk semua
cue, jadi "karaoke" di sini berarti satu kata per baris.

Preset `default` mulai dari huruf 10 piksel pada frame 1080p (`fontSize: 10/1920`)
dengan garis tepi 0.0005; `fontSize` divalidasi 0.005–0.16, jadi bawahnya memang
sengaja sekecil itu dan sisanya diatur dari dashboard.

Dashboard menampilkan knob ini dalam **piksel** (mis. 10 px, +326 px dari tengah)
dan mengonversinya ke pecahan terhadap tinggi frame resolusi terpilih — helper yang
sama di `dashboard/lib/api.ts` (`pxToFont`/`pxToOffset`). Efeknya: ganti dari 1080p
ke 720p mengubah angka pikselnya tapi subtitle tetap terlihat sama besarnya relatif
terhadap frame, dan karena Concat yang menyimpan pecahan, hasil render ikut
menyesuaikan resolusi tanpa perlu knob baru.

### Font yang benar-benar dirender

Concat tidak punya tool daftar font, dan `fontFamily` yang tidak dikenal **diam-diam**
jatuh ke face default (frame-nya identik byte per byte). Verifikasi lewat
`python -m videoflow.scripts.font_check` — PNG per keluarga ditulis ke
`<data>/fontcheck/`. Yang tersedia:

| sumber | keluarga |
| --- | --- |
| dibundel concat.exe | Montserrat, Bebas Neue, Anton, Archivo Black, Space Grotesk, Inter |
| font Windows | Arial, Impact, Georgia, Times New Roman, Segoe UI, Comic Sans MS |
| **tidak** resolve | Poppins, Open Sans, Nunito Sans, Sora, Rubik, Archivo, Cabinet Grotesk |

Rekomendasi untuk Reels/TikTok: **Montserrat 800** (default) untuk semua-caps padat,
**Bebas Neue** atau **Anton** kalau mau potongan yang berteriak, **Inter 500** untuk
gaya berita yang tenang. Poppins/Open Sans/Nunito Sans baru bisa dipakai kalau
pasang TTF-nya lewat *Settings > Fonts* (install untuk user ini saja) — setelah itu
masukkan namanya ke `CAPTION_FONTS` dan `FONTS` di dashboard.

## 4b. Analisis sumber (sebelum memotong)

`POST /api/v1/analyze` mengunduh + mentranskrip video penuh tanpa merender apa pun,
lalu menyimpannya di kolom `jobs.result`:

```bash
curl -sX POST localhost:8000/api/v1/analyze -H 'content-type: application/json' \
  -d '{ "youtube_url": "https://www.youtube.com/watch?v=XXXXXXX" }'

curl -s localhost:8000/api/v1/jobs/<job_id> | jq '.result | {topics, prompts}'
```

Isinya `topics` (kata paling sering, setelah stopword Indonesia dibuang), `windows`
(jendela terbaik + skornya) dan `prompts` (kalimat siap tempel). Dashboard
menampilkannya sebagai chip; mengklik satu prompt mengatur `strategy: "prompt"`.

Dengan `strategy: "prompt"` + `ai_prompt`, Clipper mentranskrip sumber **sekali**,
memilih jendela yang membuka dengan kata kunci itu (`media.rank_windows`), lalu
memotong cue dari transkrip yang sama — Whisper per klip tidak dijalankan lagi.
Kalau tidak ada satu pun yang cocok, job gagal sebelum render (lebih murah daripada
memotong bagian yang salah).

## 5. Retry & idempotensi

Setiap stage menulis artifact ke `VF_DATA_DIR` lalu cek keberadaannya sebelum bekerja:

```
<data>/inbox/<job_id>/source.mp4         download
<data>/cues/<vid>.<model>.<lang>.json     transkrip sumber (analyze / strategy=prompt)
<data>/inbox/<job_id>/clip00.mp4         cut (ffmpeg)
<data>/inbox/<job_id>/clip00.cues.json   transkripsi
<data>/out/<job_id>/clip00.mp4           hasil render Concat
```

Transkrip sumber sengaja TIDAK ikut di folder `<job_id>`: Whisper di CPU 4 core
(`small`, `int8`) butuh ±8x durasi audio, jadi satu video 6 menit pernah memakan
54 menit. Dikunci oleh `media.video_id(url)` + model + bahasa supaya `analyze`
dan job Clipper berikutnya pada video yang sama membayar ASR hanya sekali, dan
transkrip dari model lain tidak pernah terbaca sebagai hasil model sekarang.
Ganti `VF_WHISPER_MODEL` = cache lama tidak dipakai, bukan salah dipakai.

Folder proyek Concat (`<data>/work/<job_id>/<nama>`) cuma tempat kerja: dibersihkan
sebelum dan sesudah render, karena `project_create` menolak folder yang sudah berisi
proyek. Job yang gagal di tengah render tinggal dikirim ulang — dia mulai dari stage
terakhir yang output-nya belum ada. `job_outputs` punya `UNIQUE (job_id, path)` dengan
`ON CONFLICT DO NOTHING` supaya insert ulang tidak dobel.

## 6. Test

```bash
cd services/py && pytest -q
```

Menguji `probe`/`cut`/`plan_clips` terhadap ffmpeg betulan, transpor MCP terhadap
peer stdio palsu (framing newline & `Content-Length`, `structuredContent` vs
`content[].text`, `isError`, code JSON-RPC, notifikasi yang menyusup sebelum balasan,
proses anak yang mati), dan urutan panggilan pipeline terhadap `Concat` palsu —
termasuk regresi nama stage per klip dan penutupan proses saat render gagal.

## Catatan lisensi

Concat AGPL-3.0. Memakainya sebagai tool internal, atau menjual **file video**
hasil render, tidak memicu kewajiban rilis source. Kewajiban itu muncul jika kamu
mendistribusikan perangkat lunak turunan dari Concat ke pihak lain.
