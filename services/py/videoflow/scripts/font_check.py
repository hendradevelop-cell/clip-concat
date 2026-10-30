"""Buktikan font mana yang benar-benar dipakai Concat.

Concat tidak punya tool daftar font, dan `fontFamily` yang tidak dikenal bisa
diam-diam fallback ke Cabinet Grotesk. Jadi tiap kandidat dirender sungguhan
lalu frame-nya diekstrak untuk dibandingkan.

    python -m videoflow.scripts.font_check
    VF_FONTS="Montserrat,Arial" python -m videoflow.scripts.font_check

Hasil: <data_dir>/fontcheck/<Family>.png
"""

import os
import shutil
import sys
from pathlib import Path

from ..config import get_settings
from ..concat import Concat
from ..services import media
from ..types import ASPECT_PRESETS

STYLE_BASE = {
    "fontSize": 0.09, "fontWeight": 800, "color": "#FFFFFF", "strokeColor": "#000000",
    "strokeWidth": 0.004, "shadow": False, "background": "", "align": "center", "maxWidth": 0.95,
}
CAPTION_TEXT = "HOKI ZEQ 34 gjpy"  # huruf besar+kecil, angka, descender
RATE = "ultrafast"


def main() -> int:
    s = get_settings()
    out_dir = s.data_dir / "fontcheck"
    out_dir.mkdir(parents=True, exist_ok=True)
    src = out_dir / "sample.mp4"
    if not src.exists():
        media.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=0x2b6cb0:s=1080x1920:r=30:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(src),
        ], timeout=600)

    families = [f.strip() for f in os.environ.get(
        "VF_FONTS", "Montserrat,Poppins,Open Sans,Bebas Neue,Nunito Sans,Arial,Cabinet Grotesk"
    ).split(",") if f.strip()]

    pngs: list[Path] = []
    with Concat(s.concat_bin, s.concat_mode) as c:
        print("concat", c.version().get("concat"))
        for fam in families:
            name = fam.replace(" ", "")
            proj_dir = out_dir / "proj"
            proj_dir.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(proj_dir / name, ignore_errors=True)
            path = c.project_create(proj_dir, name, ASPECT_PRESETS["9:16"])
            try:
                mid = c.media_import(path, src)
                cid = c.clip_add(path, mid, c.video_track_id(path))
                c.clip_frame(path, cid, "fill")
                c.captions_add(path, [{"start": 0.2, "end": 1.9, "text": CAPTION_TEXT}],
                               {**STYLE_BASE, "fontFamily": fam}, 0.0)
                mp4 = out_dir / f"{name}.mp4"
                mp4.unlink(missing_ok=True)
                job = c.export_start(path, mp4, crf=28, preset=RATE, codec="h264",
                                     **ASPECT_PRESETS["9:16"])
                c.wait_export(job, 900)
            finally:
                c.project_close(path, save=False)
                shutil.rmtree(proj_dir / name, ignore_errors=True)

            png = out_dir / f"{name}.png"
            media.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                       "-ss", "1.0", "-i", str(mp4), "-frames:v", "1", str(png)], timeout=300)
            print(f"{fam:16} -> {png} ({png.stat().st_size} byte)")
            pngs.append(png)

    print("\bPNG:", " ".join(str(p) for p in pngs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
