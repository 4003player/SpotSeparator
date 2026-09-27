# Spot Separator

Extracts spot color channels (OPW, Dieline, etc.) from PDF files as spot-tagged PDFs using Ghostscript. Built for Konami Yu-Gi-Oh card production — separates the OPW white plate for Caldera/Vutek workflow.

## How to Use

1. Go to **Issues** → **New Issue** → **[Separate] Spot Color Separation**
2. Drag and drop your PDF file(s) into the issue body
3. Check the boxes for which spot colors to extract (OPW is checked by default)
4. Submit the issue
5. Wait ~1 minute — GitHub Actions runs Ghostscript to separate the channels
6. A comment appears with a download link
7. Click the link → scroll to **Artifacts** → download `spot-separations`
8. The issue auto-closes

## Output Format

- One PDF per input file, per selected spot color
- Multipage PDFs stay multipage (e.g., a 7-page input → 7-page OPW output)
- Each page uses `/Separation /OPW /DeviceGray` color space — Caldera recognizes it as a named spot
- Page dimensions match the original PDF
- 150 DPI grayscale

## Example

**Input:** `BETB-EN098-40.pdf` (6 MB, CMYK + OPW + Dieline)  
**Output:** `BETB-EN098-40_OPW.pdf` (500 KB, spot-tagged grayscale)

Feed the OPW PDF to the Imposer alongside the original CMYK PDF, and Caldera gets a matched pair for the Vutek's CMYK + White channels.

## Workflow

```
Konami PDFs ──→ SpotSeparator ──→ Individual OPW PDFs (spot-tagged)
                                        ↓
CMYK originals ──→ Imposer ──→ Sheet_Print.pdf  (CMYK)
OPW PDFs ────────→ Imposer ──→ Sheet_OPW.pdf    (spot white)
                                        ↓
                                  Caldera → Vutek
```

## File Limits

- GitHub issue attachments: 25 MB per file
- Artifacts retained for 7 days, then auto-deleted
- No job data persisted after download

## Local Usage

If you have Ghostscript installed locally:

```bash
python scripts/separate.py input.pdf --spots OPW --dpi 150 --outdir ./output --gs gs
```

## Requirements (handled by GitHub Actions)

- Ghostscript (tiffsep device)
- Python 3.11+
- Pillow
