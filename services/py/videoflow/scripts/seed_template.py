"""Seed satu baris template clipper ke DB.

Dua gaya:
  --from-concat <name>  : pakai template yang sudah disimpan di UI Concat
                          (lihat `template_list`) -> pipeline nanti memanggil
                          `template_instantiate`, DB tidak menyimpan gaya.
  --json <file>         : style didefinisikan sebagai JSON addTextClip style.

Contoh --json style.json (nama field = milik `captions_add` Concat 0.2.8,
ukuran dalam pecahan frame, bukan piksel):
{
  "id": "bold-yellow-1",
  "name": "Bold Kuning Bawah",
  "offsetY": 0.34,
  "style": {
    "fontFamily": "Inter", "fontSize": 0.085, "fontWeight": 800,
    "color": "#FFFFFF", "strokeColor": "#000000", "strokeWidth": 0.006
  }
}
"""

import argparse
import json
import sys
from pathlib import Path

from psycopg.types.json import Json

from videoflow import db


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="clipper", choices=["clipper", "news", "avatar"])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--json", type=Path)
    src.add_argument("--from-concat", dest="concat_name")
    args = ap.parse_args()

    if args.json:
        row = json.loads(args.json.read_text())
        payload = {
            "id": row["id"],
            "name": row["name"],
            "kind": args.kind,
            "offsetY": row.get("offsetY", 0.35),
            "style": row["style"],
        }
    else:
        payload = {
            "id": f"concat-{args.concat_name}",
            "name": args.concat_name,
            "kind": args.kind,
            "offsetY": 0.35,
            # Style hidup di dalam template Concat, jadi DB tidak menyimpan
            # gaya apa pun. Pipeline memakai template.instantiate.
            "style": {},
        }

    with db.tx() as cur:
        cur.execute(
            """INSERT INTO templates (id, name, kind, style, offset_y)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE
                 SET name = EXCLUDED.name, kind = EXCLUDED.kind,
                     style = EXCLUDED.style, offset_y = EXCLUDED.offset_y""",
            (payload["id"], payload["name"], payload["kind"], Json(payload["style"]), payload["offsetY"]),
        )
    print(f"template {payload['id']} tersimpan (kind={payload['kind']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
