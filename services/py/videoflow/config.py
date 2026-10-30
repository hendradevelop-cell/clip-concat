from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VF_", env_file=".env", extra="ignore")

    data_dir: Path = Path.home() / "videoflow"
    database_url: str = "postgresql:///videoflow"
    redis_url: str = "redis://localhost:6379/0"

    # Concat bicara lewat MCP stdio, jadi binary-nya harus ada di mesin yang
    # sama dengan worker. Kosong = cari di VF_CONCAT_BIN/tempat umum/PATH.
    # concat.exe hanya mau menulis di bawah home-nya (mcp.writesUnder),
    # sehingga data_dir default ikut ke bawah home.
    concat_bin: str = ""
    concat_mode: str = "headless"

    whisper_model: str = "large-v3"
    whisper_device: str = "cuda"
    whisper_lang: str = "id"

    # Dashboard Next.js memanggil API ini dari origin berbeda, jadi browser
    # butuh header CORS. Pisahkan dengan koma kalau dev server-nya lebih dari satu.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    export_crf: int = 19
    export_preset: str = "medium"
    export_codec: str = "h264"

    render_timeout_s: int = 1800
    # Concat menjalankan satu render pada satu waktu, jadi kunci ini dipakai
    # sebagai Redis lock agar dua worker tidak pernah memanggil export bareng.
    export_lock_ttl_s: int = 2400

    @property
    def inbox(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def work(self) -> Path:
        return self.data_dir / "work"

    @property
    def cues(self) -> Path:
        """Transkrip sumber di sini, dikunci video id — bukan id job."""
        return self.data_dir / "cues"

    @property
    def out(self) -> Path:
        return self.data_dir / "out"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for p in (s.inbox, s.work, s.out, s.cues):
        p.mkdir(parents=True, exist_ok=True)
    return s
