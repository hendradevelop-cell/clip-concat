"""Klien Concat 0.2.8 lewat MCP stdio.

Concat build desktop tidak punya server TCP — transpornya hanya
`concat --mcp [auto|window|headless]`. Jadi klien ini men-spawn satu proses
headless per worker lalu bicara JSON-RPC 2.0 newline-delimited melalui
stdin/stdout anak tersebut.

Empat hal yang bentuknya berbeda dari asumsi dokumen web Concat:
id selalu diambil dari `createdId` pada balasan atau dari `timeline_read`
(bukan ditebak), path proyek adalah `location/name` karena `project_create`
tidak mengembalikannya, framing hanya menerima "fit"|"fill", dan render
diikuti dengan `export_status` (state "running"/"done"/"cancelled"/"failed").
"""

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("videoflow.concat")

PROTOCOL_VERSION = "2024-11-05"


class ConcatError(RuntimeError):
    def __init__(self, message: str, *, tool: Optional[str] = None, code: Optional[int] = None):
        super().__init__(f"{tool}: {message}" if tool else message)
        self.message, self.tool, self.code = message, tool, code


def find_binary(explicit: str = "") -> str:
    for c in (
        explicit,
        os.environ.get("VF_CONCAT_BIN", ""),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Concat\concat.exe"),
        r"C:\Program Files\Concat\concat.exe",
    ):
        if c and os.path.isfile(c):
            return c
    for c in ("concat", "concat.exe"):
        w = shutil.which(c)
        if w:
            return w
    raise ConcatError("concat tidak ditemukan — set VF_CONCAT_BIN ke path concat.exe")


class Concat:
    """Satu proses `concat --mcp headless` beserta handshake-nya."""

    def __init__(self, binary: str = "", mode: str = "headless", timeout: float = 60.0):
        self.binary = binary or find_binary()
        self.mode = mode
        self.timeout = timeout
        self._id = 0
        self._stderr: list[str] = []
        self._dead = False

        # Concat 0.2.8 menolak `--mcp=headless`; nilainya harus argumen terpisah.
        self.argv = [self.binary, "--mcp"] + ([mode] if mode else [])
        try:
            self._p = subprocess.Popen(
                self.argv,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1,
            )
        except OSError as e:
            raise ConcatError(f"gagal menjalankan {self.argv[0]}: {e}")
        threading.Thread(target=self._drain_stderr, daemon=True).start()

        init = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "videoflow", "version": "0.1"},
        })
        self.server_info: dict = init.get("result") or {}
        self._notify("notifications/initialized")
        listed = self._request("tools/list", {})
        self.tools: dict[str, dict] = {
            t["name"]: t for t in (listed.get("result") or {}).get("tools", [])
        }

    # --- transpor ---

    def _send(self, obj: dict) -> None:
        if self._p.poll() is not None:
            raise self._died()
        self._p.stdin.write(json.dumps(obj, separators=(",", ":")) + "\n")
        self._p.stdin.flush()

    def _recv(self, deadline: float) -> Optional[dict]:
        while time.monotonic() < deadline:
            if self._p.poll() is not None:
                raise self._died()
            line = self._p.stdout.readline()
            if not line:
                raise self._died()
            line = line.strip()
            if line.startswith(("Content-Length:", "Content-type:")):
                headers = {"Content-Length": line.split(":", 1)[1].strip()}
                while True:
                    h = self._p.stdout.readline()
                    if h in ("\r\n", "\n", ""):
                        break
                    if ":" in h:
                        k, v = h.split(":", 1)
                        headers[k.strip()] = v.strip()
                return json.loads(self._p.stdout.read(int(headers.get("Content-Length", 0))))
            if line:
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    log.warning("baris non-JSON dari concat: %s", line[:200])
        raise ConcatError("tidak ada balasan sebelum batas waktu tunggu")

    def _request(self, method: str, params: dict, timeout: Optional[float] = None) -> dict:
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            msg = self._recv(deadline)
            # notifikasi (progress, log) boleh muncul di depan balasan.
            if msg.get("id") == rid:
                return msg

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def raw(self, method: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
        """Balasan JSON-RPC mentah; dipakai probe untuk melihat bentuk asli."""
        return self._request(method, params or {}, timeout)

    def _drain_stderr(self) -> None:
        """stderr dibaca thread terpisah supaya readline() stdout tidak macet."""
        for line in self._p.stderr:
            self._stderr.append(line.rstrip())
            if len(self._stderr) > 2000:
                break

    def _died(self) -> ConcatError:
        tail = "\n".join(self._stderr[-15:])
        self._dead = True
        return ConcatError(
            f"proses concat berhenti (exit {self._p.returncode}). stderr:\n{tail[-1500:]}"
        )

    def stderr_tail(self, n: int = 15) -> str:
        return "\n".join(self._stderr[-n:])

    # --- tools ---

    def call(self, tool: str, timeout: Optional[float] = None, **args) -> dict:
        if tool not in self.tools:
            raise ConcatError(
                f"build ini tidak punya tool {tool} "
                f"({len(self.tools)} tool tersedia)", tool=tool
            )
        try:
            resp = self._request("tools/call", {"name": tool, "arguments": args}, timeout)
        except ConcatError as e:
            raise ConcatError(e.message, tool=tool) from e
        if "error" in resp:
            err = resp["error"]
            raise ConcatError(str(err.get("message", err)), tool=tool, code=err.get("code"))
        result = resp.get("result") or {}
        if result.get("isError"):
            raise ConcatError(self._text(result), tool=tool)
        payload = result.get("structuredContent")
        if payload is not None:
            return payload
        text = self._text(result)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}

    @staticmethod
    def _text(result: dict) -> str:
        return " ".join(
            c.get("text", "") for c in result.get("content") or [] if c.get("type") != "image"
        ).strip()

    def version(self) -> dict:
        return self.call("concat_version")

    def project_create(self, location: str | Path, name: str, video: dict) -> str:
        """Balasan tidak memuat path, dan dokumennya menegaskan folder = location/name."""
        self.call("project_create", location=str(location), name=name, video=video)
        return str(Path(location) / name)

    def media_import(self, path: str, file: str | Path) -> str:
        return self.call("media_import", path=path, file=str(file))["createdId"]

    def timeline(self, path: str) -> dict:
        return self.call("timeline_read", path=path)

    def video_track_id(self, path: str) -> str:
        """Lane paling atas adalah lane gambar; captions_add membuat lane sendiri."""
        tracks = self.timeline(path).get("timeline", {}).get("tracks") or []
        if not tracks:
            raise ConcatError("timeline_read tidak mengembalikan satu lane pun", tool="timeline_read")
        return tracks[0]["id"]

    def clip_add(self, path: str, media_id: str, track_id: str, start: float = 0.0) -> str:
        return self.call(
            "clip_add", path=path, mediaId=media_id, trackId=track_id, start=start
        )["createdId"]

    def clip_frame(self, path: str, clip_id: str, framing: str = "fill") -> None:
        self.call("clip_frame", path=path, clipId=clip_id, framing=framing)

    def captions_add(self, path: str, cues: list[dict], style: dict, offset_y: float) -> None:
        self.call("captions_add", path=path, captions=cues, style=style, offsetY=offset_y)

    def export_start(self, path: str, output: str | Path, **opts) -> str:
        return self.call("export_start", path=path, output=str(output), **opts)["job"]

    def export_status(self, job: str) -> dict:
        return self.call("export_status", job=job)

    def wait_export(
        self, job: str, timeout: float, poll_s: float = 5.0,
        on_progress: Optional[Callable[[dict], None]] = None,
    ) -> dict:
        """Dikuti lewat `export_status`, bukan `export_wait` yang memblokir:
        tiap poll ada angka frame terbaru, jadi progress bar bergerak terus."""
        deadline = time.monotonic() + timeout
        while True:
            status = self.export_status(job)
            if on_progress:
                on_progress(status)
            state = str(status.get("state", "")).lower()
            if state == "done":
                return status
            if state in ("cancelled", "failed"):
                raise ConcatError(
                    f"render berakhir {state}: {status.get('error') or status.get('note') or ''}",
                    tool="export_status",
                )
            if time.monotonic() + poll_s > deadline:
                raise ConcatError(f"render {job} melebihi {timeout:.0f}s", tool="export_status")
            time.sleep(poll_s)

    def project_close(self, path: str, save: bool = False) -> None:
        self.call("project_close", path=path, save=save)

    def close(self) -> None:
        if self._p.poll() is None:
            self._p.terminate()
        try:
            self._p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._p.kill()

    def __enter__(self) -> "Concat":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
