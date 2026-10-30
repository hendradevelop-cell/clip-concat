"""Cek klien MCP against build Concat yang terpasang.

    python -m videoflow.scripts.mcp_check                     # versi + daftar tool
    PROBE_SCHEMAS=captions_add,export_start python -m videoflow.scripts.mcp_check
    MCP_RENDER=1 python -m videoflow.scripts.mcp_check        # render satu klip

`MCP_RENDER=1` menjalankan satu render Clipper utuh lewat ClipperInput palsu
sumbernya sebuah video testsrc yang dibuat ffmpeg di `VF_DATA_DIR`, jadi tidak
butuh Postgres, Redis, atau koneksi internet.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ..config import get_settings
from ..concat import Concat, ConcatError, find_binary
from ..types import ASPECT_PRESETS, Cue, Template


def sh(cmd: list[str]) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"{cmd[0]} gagal: {p.stderr[-1500:]}")


def dump_tools(c: Concat) -> None:
    tools = c.raw("tools/list", {}).get("result", {}).get("tools", [])
    if out := os.environ.get("PROBE_OUT"):
        Path(out).write_text(json.dumps(tools, indent=2), encoding="utf-8")
        print(f"\nseluruh skema ditulis ke {out}")
    for t in tools:
        if t["name"] in [n for n in os.environ.get("PROBE_SCHEMAS", "").split(",") if n]:
            print(f"\n--- {t['name']} ---")
            print((t.get("description") or "")[:600])
            print(json.dumps(t.get("inputSchema"), indent=2)[:2500])


def render_check(c: Concat) -> None:
    """Satu render vertikal: clip_add + fill + captions_add + export."""
    s = get_settings()
    work = Path(tempfile.mkdtemp(prefix="vf-mcpcheck-", dir=str(s.data_dir)))
    src = work / "src.mp4"
    sh([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src),
    ])

    video = ASPECT_PRESETS["9:16"]
    path = c.project_create(work, "mcpcheck", video)
    try:
        mid = c.media_import(path, src)
        cid = c.clip_add(path, mid, c.video_track_id(path))
        c.clip_frame(path, cid, "fill")
        c.captions_add(
            path,
            [{"start": 0.2, "end": 1.8, "text": "kalimat pertama"},
             {"start": 1.8, "end": 3.4, "text": "kalimat kedua"}],
            Template.model_validate({
                "id": "t", "name": "t", "kind": "clipper",
                "style": {"fontFamily": "Inter", "fontSize": 0.085, "fontWeight": 800},
            }).style.model_dump(exclude_none=True),
            0.35,
        )
        out = work / "final.mp4"
        job = c.export_start(path, out, crf=23, preset="veryfast", codec="h264", **video)
        status = c.wait_export(job, 900)
        print(f"\nrender {job}: {status.get('state')} -> {out} ({out.stat().st_size} byte)")
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=width,height,codec_name,duration", "-of", "json", str(out)],
            capture_output=True, text=True,
        )
        print(p.stdout.strip() or p.stderr.strip())
    finally:
        try:
            c.project_close(path, save=False)
        except ConcatError as e:
            print(f"project_close gagal: {e}")


def main() -> int:
    s = get_settings()
    with Concat(s.concat_bin, s.concat_mode) as c:
        info = c.version()
        print(f"binary     : {c.binary}")
        print(f"concat     : {info.get('concat')}  api {info.get('apiVersion')}")
        print(f"capab.     : {sorted(info.get('capabilities') or [])}")
        print(f"mcp        : {info.get('mcp')}")
        print(f"tools      : {len(c.tools)}")
        print("           : " + ", ".join(sorted(c.tools)))
        dump_tools(c)
        if os.environ.get("MCP_RENDER"):
            render_check(c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
