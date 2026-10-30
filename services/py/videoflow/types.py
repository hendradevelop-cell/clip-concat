from typing import Any, Literal, Optional

from pydantic import AliasChoices, BaseModel, Field, model_validator

VERTICAL = {"width": 1080, "height": 1920, "rateNum": 30, "rateDen": 1}
HORIZONTAL = {"width": 1920, "height": 1080, "rateNum": 30, "rateDen": 1}

ASPECT_PRESETS: dict[str, dict[str, Any]] = {"9:16": VERTICAL, "16:9": HORIZONTAL}

# "480p/720p/1080p" dihitung dari sisi pendek: 9:16 @720p = 720x1280,
# 16:9 @720p = 1280x720. Semua angka genap supaya libx264 tidak memaksa scale ulang.
FRAME_SIZES: dict[str, dict[str, dict[str, int]]] = {
    "9:16": {"480p": (480, 854), "720p": (720, 1280), "1080p": (1080, 1920)},
    "16:9": {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080)},
}


def frame_size(aspect: str, quality: str) -> dict[str, Any]:
    w, h = FRAME_SIZES[aspect][quality]
    return {"width": w, "height": h, "rateNum": 30, "rateDen": 1}


class VideoSettings(BaseModel):
    model_config = {"extra": "allow"}
    width: int = 1080
    height: int = 1920
    rateNum: int = 30
    rateDen: int = 1


class CaptionStyle(BaseModel):
    """Style untuk `captions_add` Concat 0.2.8.

    Satu style dipakai bersama semua cue; `content` tidak ada di sini karena
    teksnya datang dari tiap cue. Ukuran bukan piksel: `fontSize` adalah
    tinggi huruf kapital sebagai pecahan tinggi frame, `strokeWidth` ikut
    satuan yang sama.
    """

    model_config = {"extra": "allow"}
    fontFamily: Optional[str] = None
    fontSize: Optional[float] = None
    fontWeight: Optional[float] = None
    color: Optional[str] = None
    strokeColor: Optional[str] = None
    strokeWidth: Optional[float] = None
    shadow: Optional[bool] = None
    background: Optional[str] = None
    backgroundPaddingX: Optional[float] = None
    backgroundPaddingY: Optional[float] = None
    backgroundRadius: Optional[float] = None
    align: Optional[Literal["left", "center", "right"]] = None
    italic: Optional[bool] = None
    lineHeight: Optional[float] = None
    tracking: Optional[float] = None
    opacity: Optional[float] = None
    maxWidth: Optional[float] = None
    maxHeight: Optional[float] = None


# Concat tidak punya tool daftar font, dan keluarga yang tidak dikenal diam-diam
# jatuh ke face default (terbukti lewat videoflow.scripts.font_check: frame-nya
# identik byte per byte). Hanya keluarga di bawah yang benar-benar dirender --
# enam dibundel concat.exe (dilihat dari string di binary), sisanya font Windows.
# Poppins, Open Sans, Nunito Sans, Sora, Rubik, Archivo dan Cabinet Grotesk
# TIDAK resolve; pasang manual di Settings > Fonts kalau mau dipakai.
CAPTION_FONTS = (
    "Montserrat", "Bebas Neue", "Anton", "Archivo Black", "Space Grotesk",
    "Inter", "Lobster", "Pacifico", "Arial", "Impact", "Georgia",
    "Times New Roman", "Segoe UI", "Comic Sans MS",
)

CAPTION_PRESETS: dict[str, dict[str, Any]] = {
    # default: potongan pendek, tebal -- mulai dari huruf 10 piksel pada frame
    # 1080p (10/1920) supaya pengguna menaikkan sendiri dari angka kecil.
    # Garis tepi ikut dikecilkan: stroke 0.006 di atas glyph 10px jadi blob hitam.
    "default": {
        "fontFamily": "Montserrat", "fontWeight": 800, "fontSize": 10 / 1920,
        "offsetY": 0.34, "maxWords": 2, "color": "#FFFFFF", "strokeColor": "#000000",
        "strokeWidth": 0.0005, "shadow": True, "background": "", "maxWidth": 0.9,
    },
    # formal: kalimat panjang seperti subtitle berita, lebih kecil dan tenang.
    "formal": {
        "fontFamily": "Inter", "fontWeight": 500, "fontSize": 0.062,
        "offsetY": 0.4, "maxWords": 6, "color": "#FFFFFF", "strokeColor": "#000000",
        "strokeWidth": 0.003, "shadow": False, "background": "", "maxWidth": 0.94,
    },
    # karaoke: satu kata per baris dengan batas waktu per kata (word_timestamps).
    # Concat cuma punya satu style untuk semua cue, jadi highlight dua warna
    # dalam satu baris tidak mungkin -- yang ini gaya "one word at a time".
    "karaoke": {
        "fontFamily": "Montserrat", "fontWeight": 900, "fontSize": 0.11,
        "offsetY": 0.3, "maxWords": 1, "color": "#FFE81A", "strokeColor": "#000000",
        "strokeWidth": 0.008, "shadow": True, "background": "#000000B3", "maxWidth": 0.95,
    },
}


class CaptionOpts(BaseModel):
    """Semua knob subtitle yang dikirim dashboard.

    Field yang tidak dikirim klien diisi dari `CAPTION_PRESETS[preset]`, jadi
    panggilan API cukup `{"preset": "karaoke"}` sementara UI mengirim
    semuanya secara eksplisit.
    """

    enabled: bool = True
    preset: Literal["default", "formal", "karaoke"] = "default"
    fontFamily: Optional[str] = None
    fontWeight: Optional[float] = None
    # Bawah 0.005 = 10 piksel pada frame 1080p; di atasnya slider dashboard
    # tidak punya alasan menurunkan huruf sampai hilang.
    fontSize: Optional[float] = Field(default=None, ge=0.005, le=0.16)
    offsetY: Optional[float] = Field(default=None, ge=-0.48, le=0.48)
    maxWords: Optional[int] = Field(default=None, ge=1, le=8)
    color: Optional[str] = None
    strokeColor: Optional[str] = None
    strokeWidth: Optional[float] = Field(default=None, ge=0, le=0.05)
    shadow: Optional[bool] = None
    background: Optional[str] = None
    maxWidth: Optional[float] = Field(default=None, ge=0.2, le=1.0)

    @model_validator(mode="after")
    def _fill_from_preset(self) -> "CaptionOpts":
        for key, value in CAPTION_PRESETS[self.preset].items():
            if key not in self.model_fields_set:
                setattr(self, key, value)
        return self

    def style(self) -> CaptionStyle:
        return CaptionStyle(
            fontFamily=self.fontFamily, fontWeight=self.fontWeight, fontSize=self.fontSize,
            color=self.color, strokeColor=self.strokeColor, strokeWidth=self.strokeWidth,
            shadow=self.shadow, background=self.background, maxWidth=self.maxWidth,
            align="center",
        )


def resolve_template(tpl: "Template", cap: CaptionOpts) -> "Template":
    """Template jadi basis, kontrol UI menang di atasnya."""
    style = tpl.style.model_dump(exclude_none=True) | cap.style().model_dump(exclude_none=True)
    return tpl.model_copy(update={"style": CaptionStyle(**style), "offsetY": cap.offsetY})



class Template(BaseModel):
    """Baris tabel templates. Kolom `offset_y` dipetakan ke `offsetY`.

    `offsetY` diukur dari tengah frame sebagai pecahan setengah tinggi:
    positif ke bawah, jadi 0.35 artinya subtitle duduk di bagian bawah.
    """

    model_config = {"populate_by_name": True}

    id: str
    name: str
    kind: Literal["clipper", "news", "avatar"]
    style: CaptionStyle
    offsetY: float = Field(default=0.35, validation_alias=AliasChoices("offsetY", "offset_y"))


class ClipSpec(BaseModel):
    index: int
    start: float
    dur: float

    @property
    def end(self) -> float:
        return self.start + self.dur


class Word(BaseModel):
    start: float
    dur: float
    text: str


class Cue(BaseModel):
    start: float
    dur: float
    text: str
    words: list[Word] = Field(default_factory=list)


class ClipResult(BaseModel):
    index: int
    file: str
    start: float
    dur: float
    cues: int = 0
    output: Optional[str] = None


class ClipperInput(BaseModel):
    youtube_url: str = Field(pattern=r"^https?://")
    clip_count: int = Field(default=3, ge=1, le=12)
    clip_min_s: float = Field(default=30.0, ge=5)
    clip_max_s: float = Field(default=90.0, le=180)
    template_id: str
    aspect: Literal["9:16", "16:9"] = "9:16"
    quality: Literal["480p", "720p", "1080p"] = "1080p"
    strategy: Literal["even", "auto", "prompt"] = "auto"
    cookie_file: Optional[str] = None
    caption: CaptionOpts = Field(default_factory=CaptionOpts)
    ai_prompt: Optional[str] = Field(default=None, max_length=600)

    @model_validator(mode="after")
    def _prompt_strategy_needs_text(self) -> "ClipperInput":
        if self.strategy == "prompt" and not (self.ai_prompt or "").strip():
            raise ValueError("strategy=prompt butuh ai_prompt berisi kata kunci")
        return self


class AnalyzeInput(BaseModel):
    """`POST /api/v1/analyze`: unduh + transkrip penuh, tanpa render Concat."""

    youtube_url: str = Field(pattern=r"^https?://")
    clip_min_s: float = Field(default=30.0, ge=5)
    clip_max_s: float = Field(default=90.0, le=180)
    cookie_file: Optional[str] = None
    top_windows: int = Field(default=6, ge=1, le=20)


class ClipWindow(BaseModel):
    """Satu kandidat potongan hasil penilaian transkrip."""

    start: float
    dur: float
    score: float
    text: str


class Analysis(BaseModel):
    """Isi kolom `jobs.result` untuk kategori analyze."""

    duration: float
    transcript: str
    topics: list[str] = Field(default_factory=list)
    windows: list[ClipWindow] = Field(default_factory=list)
    prompts: list[str] = Field(default_factory=list)


class Stage(BaseModel):
    name: str
    status: Literal["pending", "running", "done", "skipped", "failed"]
    detail: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class LogLine(BaseModel):
    """Satu baris log aktivitas job.

    Ditulis `Tracker` setiap kali ada yang berubah, jadi isinya adalah jejak
    waktu nyata (kapan stage mulai, apa yang dilewati karena cached, berapa
    frame render terakhir), bukan perkiraan dari sisi UI.
    """

    ts: str
    stage: str
    level: Literal["info", "done", "skip", "error"]
    text: str


class Progress(BaseModel):
    stages: list[Stage] = Field(default_factory=list)
    percent: float = 0.0
    export: dict[str, Any] = Field(default_factory=dict)
    log: list[LogLine] = Field(default_factory=list)
