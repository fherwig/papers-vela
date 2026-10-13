# pdfjs-dist 6.3.289 (legacy build) — vendored 2026-10-06

Copied from the session-dashboard kit (0.11.3, `web/vendor/pdfjs/`), which vendored it on
2026-09-03 from

    https://registry.npmjs.org/pdfjs-dist/-/pdfjs-dist-6.3.289.tgz
    sha256  06f25e887adc6489f04c9fcb14198c77e4e5623a59a0bba5c4cea5838a4f1241
    Apache-2.0 (LICENSE beside this file)

Same files, same reasons: vendored so the viewer works with no network (no CDN), the legacy
build for older engines, `standard_fonts/` so PDFs that don't embed their fonts still render,
and `cmaps/` (CJK, 1.6 MB) deliberately left out — copy it in and set `cMapUrl` if a CJK PDF
ever renders with missing glyphs.

| file | sha256 |
|---|---|
| `pdf.min.mjs` | `f401927e692efc7735e0cd528c490d0dd31b7f0972c122b7040df805be45cce4` |
| `pdf.worker.min.mjs` | `a33cfe728c584fdba4fcc1fd54bcdc2f9f2f13889ddbb5b2bd1d0f8cbe49b84e` |
