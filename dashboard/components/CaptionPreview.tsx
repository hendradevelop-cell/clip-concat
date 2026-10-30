"use client";

import { useEffect, useRef, useState } from "react";

import type { CaptionOpts, Quality } from "../lib/api";
import { fontToPx, frameHeight, offsetToPx } from "../lib/api";

/** Sama seperti group_cues() di Python: satu grup = maksimal `maxWords` kata,
 * dipecah dari batas spasi, jadi grup di sini sama dengan cue yang dikirim ke
 * Concat. */
function groups(text: string, maxWords: number): string[] {
  const words = text.trim().split(/\s+/).filter(Boolean);
  const out: string[] = [];
  for (let i = 0; i < words.length; i += maxWords) out.push(words.slice(i, i + maxWords).join(" "));
  return out;
}

export default function CaptionPreview({
  cap, aspect, quality, text, onText,
}: {
  cap: CaptionOpts; aspect: "9:16" | "16:9"; quality: Quality; text: string; onText: (v: string) => void;
}) {
  const rows = groups(text, cap.maxWords);
  // Satu cue tampil sendiri di video, bukan bertumpuk; pratinjau memutar grup
  // satu per satu supaya "maks. kata di layar" terlihat apa adanya.
  const [at, setAt] = useState(0);
  useEffect(() => {
    if (!cap.enabled || rows.length < 2) return;
    const t = setInterval(() => setAt((v) => v + 1), 1100);
    return () => clearInterval(t);
  }, [cap.enabled, rows.length]);

  const index = rows.length ? at % rows.length : 0;
  const wide = aspect === "16:9";
  const h = frameHeight(aspect, quality);
  const offPx = offsetToPx(cap.offsetY, h);

  // Satu kata yang lebih lebar dari kotak subtitle tidak bisa dipecah, jadi dia
  // keluar frame di video juga -- lebih baik diberitahu daripada baru kelihatan
  // setelah render.
  const capRef = useRef<HTMLDivElement>(null);
  const [tooWide, setTooWide] = useState(false);
  useEffect(() => {
    const el = capRef.current;
    setTooWide(!!el && el.scrollWidth > el.clientWidth + 1);
  }, [text, index, cap.fontSize, cap.maxWords, cap.maxWidth, cap.fontFamily, cap.fontWeight, cap.enabled]);
  const style = {
    "--fam": `"${cap.fontFamily}", sans-serif`,
    "--fw": cap.fontWeight,
    "--fs": cap.fontSize,
    "--oy": cap.offsetY,
    "--mw": cap.maxWidth,
    "--col": cap.color,
    "--scol": cap.strokeColor,
    "--sw": cap.strokeWidth,
    "--bg": cap.background || "transparent",
    "--sh": cap.shadow ? "0 calc(0.012 * 100cqh) calc(0.02 * 100cqh) rgba(0,0,0,.85)" : "none",
  } as React.CSSProperties;

  return (
    <div className={`phone ${wide ? "wide" : ""}`}>
      <div className={`prev ${wide ? "wide" : ""}`} style={style}>
        <div className="scene">
          <i className="guide top" />
          <i className="guide bottom" />
          {cap.enabled ? (
            <div className="cap" ref={capRef}>
              <span className="line">{rows[index] ?? ""}</span>
            </div>
          ) : (
            <div className="cap off">subtitle mati</div>
          )}
        </div>
        {!wide && (
          <>
            <div className="status">
              <b>9:41</b>
              <span className="batt" />
            </div>
            <i className="island" />
            <i className="home" />
          </>
        )}
      </div>

      <label htmlFor="pvtext" className="pvtext">
        Teks pratinjau
        <input
          id="pvtext"
          value={text}
          maxLength={120}
          placeholder="ketik contoh kalimat subtitle"
          onChange={(e) => onText(e.target.value)}
        />
      </label>
      <p className="note">
        grup {rows.length ? index + 1 : 0}/{rows.length} · maks {cap.maxWords} kata di layar ·{" "}
        {cap.fontFamily} {cap.fontWeight} · {fontToPx(cap.fontSize, h)}px, {offPx >= 0 ? "+" : ""}
        {offPx}px dari tengah · {quality} ({h}px tinggi frame)
      </p>
      {tooWide && (
        <p className="warn">Kata melebihi lebar kotak subtitle — turunkan ukuran huruf.</p>
      )}
    </div>
  );
}
