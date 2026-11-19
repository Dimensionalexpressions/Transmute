"""HTTP front door for serve mode (Phase B).

Security, stated plainly:
  * binds to 127.0.0.1 by DEFAULT - only this machine can reach it, and
    no login is asked for: being on this machine already means trusted;
  * anything reachable from OFF this machine (--lan, or --host elsewhere)
    requires an access token: a real one is generated at startup and
    printed once as a ready-to-open link. The page hands it back via
    X-Token header / cookie; wrong or missing => 401. --token pins your
    own. The token is the only lock - anyone holding the link can make
    this machine convert files, so treat the link like a Wi-Fi password;
  * uploads stream to disk with a size cap (default 2 GB, --max-upload-mb);
    filenames are treated as hostile input (see core.safe_name);
  * downloads only resolve job ids recorded in our own journal.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Template

from .core import ServeState, UploadTooLarge
from .page import PAGE_TEMPLATE

_CHUNK = 1 << 20

LOOPBACKS = ("127.0.0.1", "localhost", "::1")


def resolve_token(host: str, token: str | None) -> str | None:
    """One rule, no surprises: on-loopback = no login; anywhere the traffic
    can come from other machines = token, generated if not supplied."""
    import secrets
    if token:
        return token
    if host not in LOOPBACKS:
        return secrets.token_urlsafe(18)
    return None


def _cookie(request: Request, name: str) -> str | None:
    for part in (request.headers.get("cookie") or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def create_app(state: ServeState | None = None) -> FastAPI:
    st = state or ServeState()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        st.wake()  # resume anything the journal says was interrupted
        yield
        st.stop()

    app = FastAPI(title="Transmute serve", docs_url=None, redoc_url=None,
                  lifespan=lifespan)

    @app.middleware("http")
    async def auth_gate(request, call_next):
        # '/' stays public: it is only the empty page shell, which then
        # asks for the token before ANY data crosses this door.
        if (st.auth_token and request.url.path != "/"
                and not st.token_ok(request.headers.get("x-token")
                                    or _cookie(request, "trtoken"))):
            return JSONResponse({"detail": "access token required - "
                                           "open the link shown on the "
                                           "computer running Transmute"},
                                status_code=401)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return Template(PAGE_TEMPLATE).render(max_upload_mb=st.max_upload_mb)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        from .. import __version__
        return {"ok": True, "version": __version__}

    @app.get("/api/info")
    def info() -> dict[str, Any]:
        return st.info()

    @app.post("/api/upload")
    async def upload(request: Request) -> dict[str, Any]:
        form = await request.form()
        file = form.get("file")
        if file is None or not hasattr(file, "read"):
            raise HTTPException(400, "no file part named 'file'")
        name = getattr(file, "filename", None) or "file"
        writer = st.begin_upload(name)
        try:
            while True:
                if await request.is_disconnected():
                    writer.abort()
                    raise HTTPException(499, "client went away")
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                writer.write(chunk)
            return writer.finish().to_dict()
        except UploadTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a traceback body
            writer.abort()
            raise HTTPException(500, f"upload failed: {exc}") from exc

    @app.post("/api/convert")
    async def convert(body: dict[str, Any]) -> dict[str, str]:
        target = str(body.get("target") or "")
        if not target:
            raise HTTPException(400, "target is required")
        kw: dict[str, Any] = {}
        for key in ("encoder",):
            if body.get(key):
                kw[key] = str(body[key])
        for key in ("quality", "crf"):
            if body.get(key) is not None and str(body[key]).strip() != "":
                try:
                    kw[key] = int(body[key])
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{key} must be a whole number")
        try:
            job_id = st.convert(str(body.get("upload_id") or ""), target, **kw)
        except LookupError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"job_id": job_id}

    @app.get("/api/jobs")
    def jobs() -> list[dict[str, Any]]:
        return st.jobs()

    @app.get("/download/{job_id}")
    def download(job_id: str):
        from fastapi.responses import FileResponse
        try:
            dst = st.output_for(job_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc))
        return FileResponse(dst, filename=dst.name,
                            media_type="application/octet-stream")

    # queue-level controls the browser exposes (same code as the GUI uses)
    @app.post("/api/{action}")
    def queue_action(action: str) -> dict[str, int]:
        if action == "cancel":
            return {"cancelled": st.queue.cancel_all()}
        if action == "resume":
            return {"resumed": st.queue.resume_all()}
        raise HTTPException(404, "unknown action")

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="transmute.serve",
        description="Transmute serve: convert files over HTTP from another "
                    "device. Conversion, probing and verify-before-replace "
                    "run through the exact same core as the desktop app.")
    ap.add_argument("--host", default=None,
                    help="bind address (default 127.0.0.1; --lan => 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--lan", action="store_true",
                    help="bind 0.0.0.0 - reachable from your local network; "
                         "an access token is then generated and printed once")
    ap.add_argument("--token", default=None,
                    help="access token to require (generated automatically "
                         "for any non-localhost bind; none needed on 127.0.0.1)")
    ap.add_argument("--max-upload-mb", type=float, default=2048.0)
    args = ap.parse_args(argv)
    host = args.host or ("0.0.0.0" if args.lan else "127.0.0.1")

    import uvicorn
    token = resolve_token(host, args.token)
    st = ServeState(max_upload_mb=args.max_upload_mb, auth_token=token)
    scope = ("EVERY device on your local network"
             if host not in LOOPBACKS else "only this computer")
    # flush: this promise must be readable even when stdout is a log file
    print(f"Transmute serve http://{host}:{args.port}  "
          f"(reachable from {scope})", flush=True)
    if token:
        shown = host
        if host == "0.0.0.0":  # a phone cannot open '0.0.0.0'; name the real address
            from .launcher import local_ip
            shown = local_ip()
        print(f"  access token: {token}", flush=True)
        print(f"  open on another device: "
              f"http://{shown}:{args.port}/?token={token}", flush=True)
        print("  the token IS the lock - anyone with that link can use this "
              "machine to convert files", flush=True)
    uvicorn.run(create_app(st), host=host, port=args.port, log_level="warning")
    return 0
