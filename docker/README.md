# Transmute in Docker (serve mode)

The container runs **`transmute serve`** — the same conversion core the GUI
uses, behind a small web page. It is deliberately **not** the desktop GUI:
a container has no desktop, and the GUI would just be a worse browser tab.
The engines (ffmpeg, ghostscript, qpdf, poppler-utils, pandoc, LibreOffice)
are baked into the image, so the probe inside reports a full bench — the
container tells you honestly what it can do, nothing faked.

## Run it

```bash
docker run -d --name transmute \
  -p 8765:8765 \
  -v ./transmute-data:/data \
  ghcr.io/dimensionalexpressions/transmute-serve:latest
docker logs transmute
```

The logs print the access link **with the token**:

```
open on another device: http://<your-lan-ip>:8765/?token=…
the token IS the lock - anyone with that link can use this machine to convert files
```

Open that link from any device on your LAN. Workspaces/conversions write
under `/data` (keep it in a volume).

To pick your own token instead: `-e TRANSMUTE_TOKEN=*** long random string>`.

## Docker compose

```bash
cd docker
docker compose up -d --build
docker compose logs transmute   # grab the token link
```

`compose.yml` maps the same port to `127.0.0.1` only — expose it on the LAN
boundary intentionally, never to the internet: the token protects the page,
but it is one string, not TLS + auth.

## GPU encoding inside the container

The image is CPU-first. To use an NVIDIA GPU (probe will then report NVENC
only if it *measures* it working, same honesty as everywhere else), uncomment
the `runtime: nvidia` line in `compose.yml` (requires the NVIDIA container
toolkit).

## Environment knobs

| Variable | Default | Effect |
|---|---|---|
| `TRANSMUTE_PORT` | `8765` | container listen port |
| `TRANSMUTE_TOKEN` | generated per start | access token; generated link printed at startup |
| `TRANSMUTE_MAX_UPLOAD_MB` | `2048` | upload size cap |
| `TRANSMUTE_GOVERNOR` | on | the resource governor always throttles unless set to `0` — an uploaded file should never own your machine |

## Building locally

```bash
docker build -f docker/Dockerfile -t transmute-serve:local .
```
