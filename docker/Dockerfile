# Transmute serve - convert files over HTTP from any device on your network.
#
# The GUI is deliberately NOT in this image: it is a desktop app (Qt), and a
# container has no desktop. Serve mode is the same conversion core behind a
# browser, which is exactly what a container is for.
#
# Engines are external system processes (project doctrine), so the image
# installs them from Debian apt: ffmpeg, ghostscript, qpdf, poppler,
# LibreOffice, pandoc. On first start Transmute PROBES this container and the
# web UI offers only what it actually found - the same zero-assumption rule
# as on a desktop. GPU encoders exist only with an NVIDIA container runtime
# (`runtime: nvidia` in compose); the probe inside the container will simply
# report CPU encoders and say so - nothing faked, nothing hidden.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ffmpeg ghostscript qpdf poppler-utils pandoc \
      libreoffice-writer libreoffice-calc libreoffice-impress \
      fonts-dejavu-core fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
# [serve] extra only: FastAPI/uvicorn, no PySide6 - a container has no screen.
RUN pip install --no-cache-dir ".[serve]"

# One directory holds everything Transmute owns inside the container
# (uploads, outputs, journals, config). Bind-mount it and state survives
# container rebuilds; serve's journal decides what "finished" may forget.
ENV TRANSMUTE_ROOT=/data HOME=/data
RUN mkdir -p /data

EXPOSE 8765
ENTRYPOINT ["/app/docker/entrypoint.sh"]
