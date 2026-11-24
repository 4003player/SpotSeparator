#!/usr/bin/env python3
"""
Spot Separator Server — MuPDF separation renderer for Mad Artist Graphics.

Endpoints:
  GET  /health        → {"ok": true}
  POST /spots         → multipart: file=<pdf> → {"separations": [...], "layers": [...]}
  POST /separate      → multipart: file=<pdf>, spots=<name,...>, dpi=<n>,
                              marks=<on|off>  → ZIP of plates

Outputs in ZIP:
  <stem>_4CP.tif        — DeviceCMYK composite (spots excluded, overprint preserved)
  <stem>_<Spot>.tif     — grayscale plate, black = ink (spot set to SPOT behavior)
  <stem>_<Spot>_vector.pdf — vector path PDF with /Separation colorspace (when the
                              spot paints stroked vector ops in the page content
                              stream; raster-only spots get no vector file)

Engine: PyMuPDF low-level mupdf bindings (fz_separations API).
"""
import io
import os
import re
import zlib
import tempfile
import zipfile

import numpy as np
import pymupdf
import pymupdf.mupdf as m
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from PIL import Image

MAX_UPLOAD_MB = 200
MAX_PIXELS = 130_000_000  # ~130MP safety cap (e.g. 150dpi on 40x60in is 9000x13500=121MP)

app = FastAPI(title="Spot Separator")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # pages are served from github.io; tighten later if desired
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- helpers

def open_doc(data: bytes) -> pymupdf.Document:
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"PDF larger than {MAX_UPLOAD_MB} MB")
    try:
        return pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        raise HTTPException(400, f"Could not open PDF: {e}")


def page_separations(doc, pno=0):
    """Return separation names for a page."""
    page = doc[pno]
    pp = page._pdf_page()
    seps = m.pdf_page_separations(pp)
    n = m.fz_count_separations(seps)
    return [m.fz_separation_name(seps, i) for i in range(n)]


def find_marks_ocg(doc):
    """Return list of xrefs of OCGs that look like marks/bleeds layers."""
    out = []
    for x in range(1, doc.xref_length()):
        if doc.xref_get_key(x, "Type")[1] == "/OCG":
            name = doc.xref_get_key(x, "Name")[1] or ""
            if "mark" in name.lower() or "bleed" in name.lower():
                out.append(x)
    return out


def turn_off_marks(doc):
    """Rewrite OCProperties D config so marks/bleeds OCGs are off for rendering.
    Best-effort: any structural surprise (indirect objects, arrays, odd catalogs)
    is skipped — marks stay on rather than failing the whole separation."""
    try:
        marks = find_marks_ocg(doc)
        if not marks:
            return
        ocgs = [x for x in range(1, doc.xref_length())
                if doc.xref_get_key(x, "Type")[1] == "/OCG"]
        on = [x for x in ocgs if x not in marks]
        onrefs = " ".join(f"{x} 0 R" for x in on)
        offrefs = " ".join(f"{x} 0 R" for x in marks)
        allrefs = " ".join(f"{x} 0 R" for x in ocgs)
        doc.xref_set_key(
            1, "OCProperties",
            f"<</D<</OFF[{offrefs}] /ON[{onrefs}] /RBGroups[]>>/OCGs[{allrefs}]>>",
        )
    except Exception:
        # Unusual catalog structure — leave marks visible rather than error out.
        return


def render_planes(doc, dpi, pno=0):
    """Render page with all spots as SPOT behavior. Returns (names, HxWxN array)."""
    page = doc[pno]
    pp = page._pdf_page()
    seps = m.pdf_page_separations(pp)
    n = m.fz_count_separations(seps)
    names = [m.fz_separation_name(seps, i) for i in range(n)]
    for i in range(n):
        m.fz_set_separation_behavior(seps, i, m.FZ_SEPARATION_SPOT)
    scale = dpi / 72.0
    W, H = page.mediabox.x1, page.mediabox.y1
    if W * scale * H * scale > MAX_PIXELS:
        raise HTTPException(413, "Render size too large for this server")
    ctm = m.fz_scale(scale, scale)
    cs = m.fz_device_cmyk()
    pix = m.pdf_new_pixmap_from_page_with_separations_and_usage(
        pp, ctm, cs, seps, 0, "View", m.FZ_MEDIA_BOX
    )
    w, h, nc = pix.w(), pix.h(), pix.n()
    mv = m.fz_pixmap_samples_memoryview(pix)
    arr = np.frombuffer(mv, dtype=np.uint8).reshape(h, w, nc).copy()
    del pix
    return names, arr


def tif_bytes(plane: np.ndarray, dpi) -> bytes:
    """Grayscale TIFF where black = ink. Input plane: 255 = full ink."""
    img = Image.fromarray(255 - plane, mode="L")
    buf = io.BytesIO()
    img.save(buf, format="TIFF", dpi=(dpi, dpi))
    return buf.getvalue()


def cmyk_tif_bytes(cmyk: np.ndarray, dpi) -> bytes:
    img = Image.fromarray(cmyk, mode="CMYK")
    buf = io.BytesIO()
    img.save(buf, format="TIFF", dpi=(dpi, dpi))
    return buf.getvalue()


# -------------------------------------------------- vector spot extraction

LINE_RE = re.compile(rb"^([\d.\-eE]+)\s+([\d.\-eE]+)\s+m\s*$")


def extract_spot_vector_ops(doc, page, spot_name):
    """Find the resource CS that resolves to Separation/<spot> and pull the
    stroked path section from the page content stream.

    Returns (ops_bytes, cs_name, c1_cmyk) or None.
    """
    # locate the CS ref whose object is Separation/<spot>
    res_obj = doc.xref_object(page.xref, compressed=True)
    csm = re.search(r"/ColorSpace<<(.+?)>>", res_obj)
    if not csm:
        return None
    target_ref = None
    cs_alias = None
    c1 = None
    for tok in re.finditer(r"/(\S+)\s+(\d+)\s+0\s+R", csm.group(1)):
        alias, ref = tok.group(1), int(tok.group(2))
        obj = doc.xref_object(ref, compressed=True)
        msep = re.search(r"/Separation\s*/([^\s/\[\]<>]+)", obj)
        if msep and msep.group(1) == spot_name:
            target_ref = ref
            cs_alias = alias
            c1m = re.search(r"/C1\s*\[([\d\s.]+)\]", obj)
            if c1m:
                c1 = [float(v) for v in c1m.group(1).split()]
            break
    if target_ref is None:
        return None

    ct = page.read_contents()
    lines = ct.split(b"\n")
    marker = ("/%s CS" % cs_alias).encode()
    i = 0
    out_sections = []
    while i < len(lines):
        l = lines[i].strip()
        if l.startswith(marker) and b"SCN" in l:
            # capture until S/s/f close (or Q at depth 0)
            sec = []
            depth = 0
            j = i + 1
            painted = False
            while j < len(lines):
                tl = lines[j].strip()
                if tl in (b"S", b"s"):
                    painted = True
                    sec.append(lines[j])
                    j += 1
                    break
                if tl in (b"f", b"f*", b"B", b"B*"):
                    break  # fill — not a stroke spot path; skip section
                if tl == b"q" or tl.startswith(b"q "):
                    depth += 1
                if tl == b"Q":
                    if depth == 0:
                        break
                    depth -= 1
                sec.append(lines[j])
                j += 1
            if painted:
                out_sections.append(b"\n".join(sec) + b"\n")
            i = j
        else:
            i += 1
    if not out_sections:
        return None
    return b"\n".join(out_sections), cs_alias, c1


def build_spot_pdf(ops_bytes, spot_name, c1, W, H) -> bytes:
    """Minimal one-page PDF with the vector ops under /Separation/<spot>."""
    content = f"/{spot_name} CS\n1 SCN\n".encode("latin1") + ops_bytes + b"0 0 0 1 k\n"
    comp = zlib.compress(content, 6)
    c1 = c1 or [0, 1, 0, 0]
    c1s = " ".join(str(v) for v in c1)
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [6 0 R] /Count 1 >>",
        3: f"<< /FunctionType 2 /Domain [0 1] /C0 [0 0 0 0] /C1 [{c1s}] /N 1 >>".encode(),
        4: f"[/Separation /{spot_name} /DeviceCMYK 3 0 R]".encode(),
        5: b"<< /Filter /FlateDecode /Length " + str(len(comp)).encode() + b" >>\nstream\n" + comp + b"\nendstream",
        6: (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W:.4f} {H:.4f}] /Contents 5 0 R "
            f"/Resources << /ColorSpace << /{spot_name} 4 0 R >> >> >>").encode(),
    }
    pdf = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offs = {}
    for i in sorted(objs):
        offs[i] = len(pdf)
        pdf += f"{i} 0 obj\n".encode() + objs[i] + b"\nendobj\n"
    xo = len(pdf)
    mx = max(objs)
    pdf += f"xref\n0 {mx+1}\n0000000000 65535 f \n".encode()
    for i in range(1, mx + 1):
        pdf += f"{offs[i]:010d} 00000 n \n".encode()
    pdf += f"trailer\n<< /Size {mx+1} /Root 1 0 R >>\nstartxref\n{xo}\n%%EOF".encode()
    return pdf


# ---------------------------------------------------------------- routes

@app.get("/health")
def health():
    return {"ok": True, "engine": "mupdf", "version": pymupdf.__version__}


@app.post("/spots")
async def list_spots(file: UploadFile = File(...)):
    data = await file.read()
    doc = open_doc(data)
    try:
        names = []
        for pno in range(min(doc.page_count, 50)):
            names.extend(page_separations(doc, pno))
        # layers info for the front end
        layers = []
        for x in range(1, doc.xref_length()):
            if doc.xref_get_key(x, "Type")[1] == "/OCG":
                layers.append(doc.xref_get_key(x, "Name")[1])
        return JSONResponse({"separations": sorted(set(names)), "layers": layers,
                             "pages": doc.page_count})
    finally:
        doc.close()


@app.post("/separate")
async def separate(
    file: UploadFile = File(...),
    spots: str = Form(""),
    dpi: int = Form(150),
    marks: str = Form("off"),
    include_4cp: str = Form("on"),
):
    wanted = [s.strip() for s in spots.split(",") if s.strip()]
    if not wanted:
        raise HTTPException(400, "No spots selected")
    data = await file.read()
    doc = open_doc(data)
    stem = re.sub(r"[^\w\- ]", "_", os.path.splitext(file.filename or "job")[0])

    buf = io.BytesIO()
    zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
    report = []
    try:
        if marks != "on":
            turn_off_marks(doc)

        names, arr = render_planes(doc, dpi)
        found = set(names)

        if include_4cp != "off" and arr.shape[2] >= 4:
            zf.writestr(f"{stem}_4CP.tif", cmyk_tif_bytes(arr[:, :, :4], dpi))
            report.append("4CP")

        page = doc[0]
        W, H = page.mediabox.x1, page.mediabox.y1

        for spot in wanted:
            if spot not in found:
                continue
            si = names.index(spot)
            plane = arr[:, :, 4 + si]

            # vector PDF for stroke-painted spots — the deliverable for cut/die channels
            try:
                v = extract_spot_vector_ops(doc, page, spot)
                if v:
                    ops, alias, c1 = v
                    zf.writestr(f"{stem}_{spot}_vector.pdf",
                                build_spot_pdf(ops, spot, c1, W, H))
                    report.append(f"{spot}(vector)")
                    # raster plate only for non-vector spots (white ink, varnish, etc.)
                    continue
            except Exception:  # vector extraction is best-effort
                pass
            zf.writestr(f"{stem}_{spot}.tif", tif_bytes(plane, dpi))
            report.append(spot)

        zf.close()
        missing = [s for s in wanted if s not in found]
        if not report:
            raise HTTPException(422, f"None of the requested spots found. Available: {names}")
    finally:
        doc.close()

    buf.seek(0)
    headers = {
        "X-Separations": ",".join(report),
        "X-Missing": ",".join(missing) if missing else "",
        "Content-Disposition": f'attachment; filename="{stem}_separations.zip"',
    }
    return StreamingResponse(buf, media_type="application/zip", headers=headers)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
