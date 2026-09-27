#!/usr/bin/env python3
"""
Spot Separator — Ghostscript tiffsep → spot-tagged PDF builder.

Usage:
    python separate.py input.pdf --spots OPW Dieline --dpi 150 --outdir ./output

Pipeline:
  1. Run GS tiffsep to extract all channels as grayscale TIFFs
  2. For each user-selected spot, collect per-page TIFFs
  3. Build a multipage PDF where each page uses /Separation color space
  4. Page dimensions match the original PDF
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow is required. Install with: pip install Pillow")
    sys.exit(1)


# ---------------------------------------------------------------------------
# TIFF reader via Pillow
# ---------------------------------------------------------------------------

def read_tiff_grayscale(path):
    """Read a grayscale TIFF via Pillow and return (width, height, raw_bytes, dpi_x, dpi_y)."""
    img = Image.open(path)
    if img.mode != "L":
        img = img.convert("L")
    dpi = img.info.get("dpi", (150, 150))
    raw = img.tobytes()
    return img.width, img.height, raw, float(dpi[0]), float(dpi[1])


# ---------------------------------------------------------------------------
# PDF writer — build spot-tagged PDF from scratch
# ---------------------------------------------------------------------------

class SpotPDFBuilder:
    """Build a PDF with /Separation color space pages."""

    def __init__(self):
        self.objects = []  # list of bytes, 1-indexed (obj 1 = self.objects[0])
        self.pages = []    # list of page obj numbers

    def _add_obj(self, content):
        """Add a PDF object, return its 1-based number."""
        self.objects.append(content)
        return len(self.objects)

    def add_page(self, width_pt, height_pt, image_bytes, img_w, img_h, spot_name):
        """Add a page with a spot-color image.
        
        GS tiffsep outputs grayscale where:
          0 = full ink (100% of the spot)
          255 = no ink (0% of the spot)
        This is the standard for separation TIFFs.
        We embed as-is with a tintTransform that maps:
          tint 1.0 → DeviceGray 0.0 (full ink = black on proof)
          tint 0.0 → DeviceGray 1.0 (no ink = white/paper)
        """
        # Deflate-compress the image data
        compressed = zlib.compress(image_bytes, 6)

        # Image XObject
        # The image samples are the raw grayscale bytes from tiffsep.
        # In the /Separation CS, each sample is interpreted as a tint value.
        # 0 in the TIFF = full ink, but PDF tint 0 = no ink, so we need
        # to invert. We do this with a /Decode [1 0] on the image, which
        # maps sample 0→tint 1.0 (full ink) and 255→tint 0.0 (no ink).
        img_obj = self._add_obj(
            f"<< /Type /XObject /Subtype /Image /Width {img_w} /Height {img_h} "
            f"/ColorSpace CSREF /BitsPerComponent 8 "
            f"/Decode [1 0] "
            f"/Filter /FlateDecode /Length {len(compressed)} >>\n"
            f"stream\n".encode() + compressed + b"\nendstream"
        )

        # Tint transform function: tint → gray  (1.0 → 0.0, 0.0 → 1.0)
        # This is for proofing/display: full spot tint renders as black
        func_body = b"{ 1 exch sub }"
        func_obj = self._add_obj(
            f"<< /FunctionType 4 /Domain [0 1] /Range [0 1] /Length {len(func_body)} >>\n"
            f"stream\n".encode() + func_body + b"\nendstream"
        )

        # Separation color space: [/Separation /name /DeviceGray tintTransform]
        cs_obj = self._add_obj(
            f"[/Separation /{spot_name} /DeviceGray {func_obj} 0 R]".encode()
        )

        # Patch the image object to reference the CS
        placeholder = "CSREF".encode()
        self.objects[img_obj - 1] = self.objects[img_obj - 1].replace(
            placeholder, f"{cs_obj} 0 R".encode()
        )

        # Page content stream: draw image scaled to page
        content = f"q {width_pt:.4f} 0 0 {height_pt:.4f} 0 0 cm /Im0 Do Q"
        content_bytes = content.encode()
        content_compressed = zlib.compress(content_bytes, 6)
        content_obj = self._add_obj(
            f"<< /Filter /FlateDecode /Length {len(content_compressed)} >>\n"
            f"stream\n".encode() + content_compressed + b"\nendstream"
        )

        # Resources
        resources_obj = self._add_obj(
            f"<< /XObject << /Im0 {img_obj} 0 R >> >>".encode()
        )

        # Page
        page_obj = self._add_obj(
            f"<< /Type /Page /Parent PAGESREF "
            f"/MediaBox [0 0 {width_pt:.4f} {height_pt:.4f}] "
            f"/Contents {content_obj} 0 R /Resources {resources_obj} 0 R >>".encode()
        )
        self.pages.append(page_obj)

    def save(self, path):
        """Write the complete PDF file."""
        # Pages object
        kids = " ".join(f"{p} 0 R" for p in self.pages)
        pages_obj = self._add_obj(
            f"<< /Type /Pages /Kids [{kids}] /Count {len(self.pages)} >>".encode()
        )

        # Catalog
        catalog_obj = self._add_obj(
            f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()
        )

        # Patch page parent references
        for i in range(len(self.objects)):
            if b"PAGESREF" in self.objects[i]:
                self.objects[i] = self.objects[i].replace(
                    b"PAGESREF", f"{pages_obj} 0 R".encode()
                )

        # Write PDF
        with open(path, "wb") as f:
            f.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
            offsets = []
            for i, obj in enumerate(self.objects):
                offsets.append(f.tell())
                f.write(f"{i + 1} 0 obj\n".encode())
                if isinstance(obj, str):
                    f.write(obj.encode())
                else:
                    f.write(obj)
                f.write(b"\nendobj\n")

            xref_pos = f.tell()
            f.write(b"xref\n")
            f.write(f"0 {len(self.objects) + 1}\n".encode())
            f.write(b"0000000000 65535 f \n")
            for off in offsets:
                f.write(f"{off:010d} 00000 n \n".encode())

            f.write(b"trailer\n")
            f.write(f"<< /Size {len(self.objects) + 1} /Root {catalog_obj} 0 R >>\n".encode())
            f.write(b"startxref\n")
            f.write(f"{xref_pos}\n".encode())
            f.write(b"%%EOF\n")


# ---------------------------------------------------------------------------
# Ghostscript spot-name discovery (without full separation)
# ---------------------------------------------------------------------------

def discover_spots(pdf_path, gs_path="gs"):
    """Run GS tiffsep in list-only mode to discover spot color names.
    Returns a list of spot names found (excludes CMYK process colors)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out_pattern = os.path.join(tmpdir, "probe_%04d.tif").replace("\\", "/")
        # Use low res just to discover channel names quickly
        cmd = [
            gs_path, "-sDEVICE=tiffsep", "-dNOPAUSE", "-dBATCH",
            "-dFirstPage=1", "-dLastPage=1", "-r36",
            f"-sOutputFile={out_pattern}", pdf_path
        ]
        subprocess.run(cmd, capture_output=True, timeout=120,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

        # Find spot files — GS names them as probe_0001(SpotName).tif
        spots = set()
        process = {"Cyan", "Magenta", "Yellow", "Black"}
        for f in os.listdir(tmpdir):
            m = re.search(r"\((.+?)\)\.tif$", f)
            if m and m.group(1) not in process:
                spots.add(m.group(1))
        return sorted(spots)


# ---------------------------------------------------------------------------
# Get original PDF page dimensions
# ---------------------------------------------------------------------------

def get_pdf_page_sizes(pdf_path, gs_path="gs"):
    """Use GS to get page sizes in points for each page."""
    cmd = [
        gs_path, "-sDEVICE=bbox", "-dNOPAUSE", "-dBATCH", "-dQUIET",
        pdf_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    # bbox device writes to stderr
    pages = []
    for line in result.stderr.splitlines():
        m = re.match(r"%%HiResBoundingBox:\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)", line)
        if m:
            x0, y0, x1, y1 = float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))
            pages.append((x1 - x0, y1 - y0))
    return pages


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def separate(pdf_path, spots, dpi, outdir, gs_path="gs"):
    """Full pipeline: GS tiffsep → spot-tagged PDFs."""
    pdf_path = os.path.abspath(pdf_path)
    base_name = Path(pdf_path).stem
    os.makedirs(outdir, exist_ok=True)

    # Get original page dimensions
    page_sizes = get_pdf_page_sizes(pdf_path, gs_path)
    if not page_sizes:
        print(f"WARNING: Could not determine page sizes for {pdf_path}, using defaults")
        page_sizes = [(612, 792)]  # Letter fallback

    # Run tiffsep
    with tempfile.TemporaryDirectory() as tmpdir:
        out_pattern = os.path.join(tmpdir, "sep_%04d.tif").replace("\\", "/")
        cmd = [
            gs_path, "-sDEVICE=tiffsep", "-dNOPAUSE", "-dBATCH",
            f"-r{dpi}", f"-sOutputFile={out_pattern}", pdf_path
        ]
        print(f"Running Ghostscript on {base_name}...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode != 0:
            print(f"ERROR: GS failed for {pdf_path}")
            print(result.stderr)
            return []

        # Count pages from output
        page_count = 0
        for f in os.listdir(tmpdir):
            m = re.match(r"sep_(\d+)\.tif$", f)  # composite files
            if m:
                page_count = max(page_count, int(m.group(1)))

        # Build a spot-tagged PDF for each selected spot
        outputs = []
        for spot in spots:
            builder = SpotPDFBuilder()
            found_any = False

            for page_num in range(1, page_count + 1):
                tiff_path = os.path.join(tmpdir, f"sep_{page_num:04d}({spot}).tif")
                if not os.path.exists(tiff_path):
                    print(f"  WARNING: No {spot} channel on page {page_num}")
                    continue

                # Get page dimensions (points)
                if page_num - 1 < len(page_sizes):
                    width_pt, height_pt = page_sizes[page_num - 1]
                else:
                    width_pt, height_pt = page_sizes[-1]

                # Read the TIFF
                img_w, img_h, img_data, _, _ = read_tiff_grayscale(tiff_path)
                builder.add_page(width_pt, height_pt, img_data, img_w, img_h, spot)
                found_any = True
                print(f"  Page {page_num}: {spot} ({img_w}x{img_h})")

            if found_any:
                out_path = os.path.join(outdir, f"{base_name}_{spot}.pdf")
                builder.save(out_path)
                outputs.append(out_path)
                print(f"  → {out_path}")

        # Clean up composite TIFFs (the big ones)
        return outputs


def main():
    parser = argparse.ArgumentParser(description="Separate spot colors from PDF")
    parser.add_argument("inputs", nargs="+", help="Input PDF files")
    parser.add_argument("--spots", nargs="+", required=True, help="Spot color names to extract")
    parser.add_argument("--dpi", type=int, default=150, help="Output resolution (default: 150)")
    parser.add_argument("--outdir", default="./output", help="Output directory")
    parser.add_argument("--gs", default="gs", help="Path to Ghostscript binary")
    parser.add_argument("--discover", action="store_true", help="Just list spot colors, don't separate")
    args = parser.parse_args()

    if args.discover:
        for pdf in args.inputs:
            spots = discover_spots(pdf, args.gs)
            print(f"{os.path.basename(pdf)}: {', '.join(spots) if spots else '(no spots found)'}")
        return

    all_outputs = []
    for pdf in args.inputs:
        outputs = separate(pdf, args.spots, args.dpi, args.outdir, args.gs)
        all_outputs.extend(outputs)

    print(f"\nDone. {len(all_outputs)} file(s) created in {args.outdir}")


if __name__ == "__main__":
    main()
