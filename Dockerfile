# ── Spot Separator Server — MuPDF separation renderer ──
FROM python:3.12-slim

# PyMuPDF wheels ship the MuPDF C library — no system packages needed
RUN pip install --no-cache-dir pymupdf fastapi uvicorn python-multipart pillow numpy

WORKDIR /app
COPY server.py .

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
