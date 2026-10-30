"""Pra-pemrosesan di sisi Python: unduh, probe, potong, transkripsi.

Keputusan desain: pemotongan dilakukan dengan ffmpeg re-encode (bukan
stream-copy yang tidak akurat, dan bukan splitClips di Concat). Hemat
puluhan menit render Concat per job dengan durasi tetap presisi.
"""

import hashlib
import logging
import re
import subprocess
from collections import Counter
from functools import lru_cache
from pathlib import Path

from ..types import ClipSpec, ClipWindow, Cue, Word

log = logging.getLogger("videoflow.media")


def run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} gagal ({p.returncode}): {p.stderr[-2000:]}")
    return p


def video_id(url: str) -> str:
    """Kunci cache transkrip. Id YouTube kalau URL-nya memang YouTube; selain
    itu hash URL, supaya sumber dari situs lain tetap dapat cache yang sama."""
    m = re.search(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else hashlib.sha1(url.encode()).hexdigest()[:16]


def download_youtube(url: str, dest: Path, cookie_file: str | None = None) -> Path:
    import yt_dlp

    out = dest / "source.%(ext)s"
    if out.with_suffix(".mp4").exists():
        return out.with_suffix(".mp4")
    opts = {
        "format": "bv*[height<=2160][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
        "merge_output_format": "mp4",
        "outtmpl": str(out),
        "noplaylist": True,
        "quiet": True,
        "retries": 3,
        # Seekable + fragment-download menghindari file berserakan bila
        # koneksi terputus di tengah HLS/DASH.
        "concurrent_fragment_downloads": 4,
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
        return path.with_suffix(".mp4") if path.suffix != ".mp4" else path


def probe(path: Path) -> dict:
    import json

    out = run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        timeout=120,
    )
    data = json.loads(out.stdout)
    video = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError(f"tidak ada stream video: {path}")
    rate = video.get("avg_frame_rate") or "30/1"
    num, _, den = rate.partition("/")
    return {
        "duration": float(data["format"]["duration"]),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps_num": int(num or 30),
        "fps_den": int(den or 1) or 1,
        "has_audio": any(s.get("codec_type") == "audio" for s in data["streams"]),
    }


def cut(src: Path, spec: ClipSpec, dest: Path) -> Path:
    """Potong satu klip dengan durasi akurat.

    `-c copy` TIDAK dipakai: stream copy memotong pada keyframe terdekat, jadi
    durasi bisa meleset seukuran satu GOP (terukur 3.02s untuk permintaan 2.0s
    pada testsrc). Re-encode veryfast tetap jauh lebih murah daripada render
    penuh di Concat, dan membuat timestamp subtitle tetap valid.
    """
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmp = dest.with_suffix(".part.mp4")
    run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{spec.start:.3f}", "-i", str(src), "-t", f"{spec.dur:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "160k", "-avoid_negative_ts", "make_zero", str(tmp),
        ],
        timeout=1800,
    )
    tmp.replace(dest)
    return dest


def plan_clips(total: float, n: int, min_s: float, max_s: float, strategy: str) -> list[ClipSpec]:
    """Tentukan titik potong. `auto` menyisihkan 5% awal (intro) dan
    5% akhir (outro/CTA) supaya klip tidak dibuka dengan logo channel."""
    lo, hi = (0.05 * total, 0.95 * total) if strategy == "auto" else (0.0, total)
    if hi - lo < min_s:
        lo, hi = 0.0, total
    span = hi - lo
    dur = max(min_s, min(max_s, span / n))
    dur = min(dur, span)
    step = (span - dur) / (n - 1) if n > 1 else 0.0
    return [ClipSpec(index=i, start=round(lo + i * step, 3), dur=round(dur, 3)) for i in range(n)]


@lru_cache(maxsize=1)
def _asr(model: str, device: str):
    from faster_whisper import WhisperModel

    # ctranslate2 menolak float16 di CPU (ValueError), dan int8 justru yang
    # paling cepat di sana; float16 hanya untuk GPU.
    compute_type = "float16" if device == "cuda" else "int8"
    return WhisperModel(model, device=device, compute_type=compute_type)


def transcribe(
    path: Path,
    lang: str,
    model: str,
    device: str,
    on_progress=None,
) -> list[Cue]:
    """Transkripsi lokal. Concat punya `captions_add`, tapi tool itu hanya
    menaruh teks yang sudah ada — tidak ada tool ASR di 42 tool MCP-nya.
    Jadi cue selalu dari sini, lalu dikirim sebagai satu `captions_add`.

    `word_timestamps` selalu aktif: batas kata diperlukan supaya `maxWords`
    dan mode karaoke tidak perlu menebak durasi tiap kata.

    `beam_size=1` (greedy) dipilih karena default faster-whisper adalah 5, dan
    di CPU 4 core beam 5 memakan ±8x durasi audio — satu video 6 menit lebih
    dari 50 menit. Greedy 2-3x lebih cepat dengan penurunan akurasi yang masih
    terbaca untuk subtitle.

    `on_progress(selesai_s, total_s)` dipanggil mengikuti maju waktu audio:
    generator Whisper sudah mengeluarkan batas waktunya sendiri, jadi tidak ada
    alasan bar progres diam satu jam lamanya.
    """
    segs, info = _asr(model, device).transcribe(
        str(path), language=lang, vad_filter=True, word_timestamps=True, beam_size=1
    )
    # Ambangnya waktu audio, bukan jumlah cue: satu klip 40 detik hanya punya
    # belasan segmen, jadi ambang "tiap 20 cue" tidak pernah tersentuh dan
    # pembatalan pun baru terbaca di tahap berikutnya.
    step = max(5.0, info.duration / 60)
    cues: list[Cue] = []
    mark = 0.0
    for s in segs:
        text = s.text.strip()
        if not text:
            continue
        words = [
            Word(start=round(w.start, 3), dur=round(max(0.05, w.end - w.start), 3), text=w.word.strip())
            for w in (s.words or [])
            if w.word and w.word.strip()
        ]
        cues.append(Cue(start=round(s.start, 3), dur=round(max(0.4, s.end - s.start), 3), text=text, words=words))
        if on_progress and s.end >= mark:
            on_progress(s.end, info.duration)
            mark = s.end + step
    return cues


def _fallback_words(cue: Cue) -> list[Word]:
    """Cue tanpa informasi kata (hasil transkripsi lama) dibagi rata waktunya."""
    parts = cue.text.split()
    if not parts:
        return []
    per = cue.dur / len(parts)
    return [Word(start=round(cue.start + i * per, 3), dur=round(per, 3), text=t) for i, t in enumerate(parts)]


def group_cues(cues: list[Cue], max_words: int) -> list[Cue]:
    """Potong transkrip jadi baris subtitle <= `max_words` kata.

    Concat menampilkan satu cue utuh apa adanya, jadi tanpa ini satu kalimat
    panjang menutupi seluruh frame. Batas tiap baris diambil dari waktu kata
    pertama dan akhir kata terakhir, bukan dibagi rata.
    """
    rows: list[Cue] = []
    for cue in cues:
        units = cue.words or _fallback_words(cue)
        for i in range(0, len(units), max_words):
            chunk = units[i : i + max_words]
            start = chunk[0].start
            end = chunk[-1].start + chunk[-1].dur
            rows.append(Cue(
                start=round(start, 3), dur=round(max(0.35, end - start), 3),
                text=" ".join(w.text for w in chunk),
            ))
    return rows


# --- penilaian isi transkrip -------------------------------------------------

# Transkrip bahasa Indonesia penuh kata pengisi; tanpanya peringkat jendela
# didominasi "yang/saya/itu" dan prompt user tenggelam.
STOPWORDS = frozenset("""
ada agar akan antara anda apa atau baik bisa dan dengan deh dia dll doi
harus jika justru kah kalau kami kamu kita mereka masih namun nya on oh oke
pada paling pun saya sebab sebagai sangat semua sih saja seperti tapi tentang
tentu itu ini untuk yang di ke dari dalam luar lagi banyak sedikit juga karena
kok lah dong nan yuk sudah
jadi hal biasa menurut banget bilang bikin cuma emang gitu gini kayak memang
malah saat sendiri sekali soal terus
the a an and or but so to of in is are was were be been it this that you your
we they he she i me my for with as at do does did not no yes
""".split())

_TOKEN = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if len(t) > 2 and t not in STOPWORDS]


def topic_weights(cues: list[Cue], limit: int = 40) -> Counter:
    """Kata paling sering muncul di seluruh transkrip, sebagai bobot skor."""
    c: Counter = Counter()
    for cue in cues:
        c.update(tokens(cue.text))
    return Counter(dict(c.most_common(limit)))


def prompt_weights(prompt: str) -> Counter:
    """Istilah dari instruksi AI. Dianggap permintaan keras: tiap kemunculan
    dihitung tiga kali supaya mengalahkan kepadatan topik umum."""
    c: Counter = Counter()
    for t in tokens(prompt or ""):
        c[t] += 3
    return c


def rank_windows(
    cues: list[Cue], terms: Counter, min_s: float, max_s: float, limit: int
) -> list[ClipWindow]:
    """Cari jendela `min_s..max_s` dengan skor kata tertinggi, tidak tumpang tindih.

    Titik potong selalu di awal cue, bukan di tengah kalimat, supaya klip tidak
    terpotong pada suku kata. Skor dibagi akar durasi agar segmen panjang tidak
    otomatis menang atas segmen pendek yang padat.
    """
    if not cues or not terms:
        return []
    ends = [c.start + c.dur for c in cues]
    total = ends[-1]
    candidates: list[ClipWindow] = []
    for i, cue in enumerate(cues):
        # Jendela harus DIMULAI dengan cue yang memuat kata kunci: klip yang
        # membuka dengan topik jauh lebih baik daripada menguburnya di detik 11,
        # dan tanpa aturan ini keduanya punya skor seri.
        first_hits = sum(terms[t] for t in tokens(cue.text) if t in terms)
        if first_hits <= 0:
            continue
        # Batas bawah min_s boleh dilewati sedikit: klip pendek lebih baik
        # daripada memotong kalimat di tengah.
        reach = min(total, cue.start + max_s)
        j = i
        while j < len(cues) and cues[j].start < reach:
            j += 1
        window_end = min(ends[j - 1], cue.start + max_s)
        dur = window_end - cue.start
        if dur < min_s * 0.6:
            continue
        body = " ".join(cues[k].text for k in range(i, j))
        hits = sum(terms[t] for t in tokens(body) if t in terms)
        if hits <= 0:
            continue
        candidates.append(ClipWindow(
            start=round(cue.start, 3), dur=round(dur, 3),
            score=round(hits / (dur ** 0.5), 4), text=body[:280],
        ))

    picked: list[ClipWindow] = []
    for cand in sorted(candidates, key=lambda w: (-w.score, w.start)):
        if all(cand.start + cand.dur <= p.start or cand.start >= p.start + p.dur for p in picked):
            picked.append(cand)
        if len(picked) == limit:
            break
    return sorted(picked, key=lambda w: w.start)


def cues_in_window(cues: list[Cue], start: float, dur: float) -> list[Cue]:
    """Cue milik satu jendela, waktu digeser supaya 0 = awal klip.

    Dipakai strategi `prompt`: transkrip sumber sudah ada, jadi tidak perlu
    Whisper ulang untuk tiap klip yang dipotong.
    """
    out: list[Cue] = []
    for cue in cues:
        mid = cue.start + cue.dur / 2
        if not (start <= mid < start + dur):
            continue
        s = max(0.0, cue.start - start)
        e = min(dur, cue.start + cue.dur - start)
        if e - s >= 0.2:
            out.append(Cue(
                start=round(s, 3), dur=round(e - s, 3), text=cue.text,
                words=[
                    Word(start=round(max(0.0, w.start - start), 3), dur=w.dur, text=w.text)
                    for w in cue.words if w.start - start < dur
                ],
            ))
    return out


def mmss(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"
