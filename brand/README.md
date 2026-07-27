# Pharma OS brand assets

`logo.svg` is the source of truth. Everything else is generated:

```bash
.venv/bin/python brand/make_assets.py
```

| File | Used by |
|---|---|
| `logo.svg` | vector master — edit this, never the PNGs |
| `icon-16/32/48/192/512.png` | Streamlit tab icon, web |
| `favicon.ico` | browsers (multi-size) |
| `apple-touch-icon-180.png` | iOS home screen |
| `whatsapp-profile-640.png` | WhatsApp profile picture — `./run.sh brand` |
| `pdf-mark-96.png` | PDF letterhead |
| `pdf-watermark-600.png` | PDF background watermark (5% opacity) |

**Colour:** `#FF7A00`. Defined once in `logo.svg`, mirrored in
`dashboard/app.py` (`ORANGE`) and `api/pdfgen.py` (`ACCENT`).

## Where it appears

- **Dashboard** — browser tab icon, and the sign-in page
- **PDFs** — letterhead mark top-left of every page, watermark behind content,
  "Pharma OS" in the footer. Applies to the monthly report, customer receipts, and
  the purchase order a distributor receives.
- **WhatsApp** — profile picture and display name, pushed with `./run.sh brand`

## Two implementation notes

**The notch is a real cut-out, not a white rectangle.** A white overlay looks correct
only on a white page; it shows as a white bar the moment the mark sits on a dark
WhatsApp bubble or in dark mode. `logo.svg` uses a single path with the gap cut out,
and `make_assets.py` punches transparency rather than painting white.

**Every use is guarded.** `pdfgen.py` checks the file exists and wraps rendering in
try/except. A missing or corrupt asset must never be the reason a pharmacist cannot
produce a purchase order.

## Changing the logo

1. Edit `logo.svg`
2. Mirror the geometry constants at the top of `make_assets.py` (`HEADS`, `BODY`,
   `NOTCH`) — they are duplicated on purpose so asset generation needs no SVG
   rasteriser, which would drag in cairo/pango system libraries
3. `.venv/bin/python brand/make_assets.py`
4. `./run.sh brand` to update WhatsApp
5. Restart the dashboard for the new tab icon
