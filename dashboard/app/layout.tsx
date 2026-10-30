import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "videoflow", description: "Clipper" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="id">
      <head>
        {/* Hanya untuk pratinjau di browser; render Concat memakai font yang
            ada di mesin concat, bukan yang diunduh di sini. Keluarga di bawah
            sama dengan yang dibundel Concat (lihat CAPTION_FONTS di types.py). */}
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Anton&family=Archivo+Black&family=Bebas+Neue&family=Inter:wght@500;700&family=Montserrat:wght@500;700;800;900&family=Space+Grotesk:wght@500;700&display=swap"
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
