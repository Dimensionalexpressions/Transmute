"""Serve mode (Phase B): HTTP front door onto the SAME core.

Real uvicorn on 127.0.0.1 with an ephemeral port, real HTTP via stdlib
urllib (no extra test deps), a fake capability report for picker truth,
and the REAL ImageEngine behind the queue - so upload -> verify-before-
replace -> download is exercised end to end without ffmpeg.
"""
from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("PIL")

from transmute.paths import AppPaths                        # noqa: E402
from transmute.probe.capability import CapabilityReport, MatrixRow  # noqa: E402
from transmute.serve.core import ServeState, safe_name       # noqa: E402

pytestmark = pytest.mark.unit


def _png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (24, 16), (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


def _report() -> CapabilityReport:
    matrix = [
        MatrixRow(category="image", target="webp", path="pillow",
                  available=True, verified=True, recommended=True),
        MatrixRow(category="image", target="pdf (page)", path="Pillow/PDF",
                  available=True, verified=True),
        MatrixRow(category="image", target="avif", path="pillow",
                  available=False, note="AVIF plugin missing"),
    ]
    return CapabilityReport(schema=1, probe_key="k", generated_at=time.time(),
                            app_version="t", machine={}, engines={},
                            encoders=[], filters={}, gpu_nodes={},
                            image_backends={}, pdf_backends={}, caveats=[],
                            matrix=matrix)


class _Server:
    def __init__(self, tmp_path: Path, **kw):
        from transmute.serve.app import create_app
        st = ServeState(paths=AppPaths(tmp_path), report_provider=_report, **kw)
        self.state = st
        cfg = uvicorn.Config(create_app(st), host="127.0.0.1", port=0,
                             log_level="error")
        self.srv = uvicorn.Server(cfg)
        threading.Thread(target=self.srv.run, daemon=True).start()
        deadline = time.time() + 15
        while not self.srv.started and time.time() < deadline:
            time.sleep(0.02)
        assert self.srv.started, "uvicorn did not start"
        assert st._report_ready.wait(15), "report warm-up never finished"
        self.base = f"http://127.0.0.1:{self.srv.servers[0].sockets[0].getsockname()[1]}"

    def close(self):
        self.srv.should_exit = True
        self.state.stop()

    # ---- tiny client -----------------------------------------------------
    def get(self, path: str) -> tuple[int, bytes]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"content-type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read() or b"{}")
            except ValueError:
                body = {}
            return e.code, body

    def upload(self, filename: str, data: bytes) -> tuple[int, dict]:
        b = uuid.uuid4().hex
        head = (f'--{b}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\nContent-Type: image/png\r\n\r\n').encode()
        body = head + data + f"\r\n--{b}--\r\n".encode()
        req = urllib.request.Request(
            self.base + "/api/upload", data=body,
            headers={"content-type": f"multipart/form-data; boundary={b}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read() or b"{}")
            except ValueError:
                body = {}
            return e.code, body

    def wait_done(self, job_id: str, want: str = "replaced",
                  timeout: float = 60.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for k in self.state.jobs():
                if k["job_id"] == job_id and k["state"] == want:
                    return k
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} never reached {want}: "
                             f"{self.state.jobs()}")


@pytest.fixture()
def server(tmp_path):
    s = _Server(tmp_path)
    yield s
    s.close()


# ------------------------------------------------------------------ basics
def test_page_and_health(server):
    code, body = server.get("/")
    assert code == 200 and b"Transmute" in body
    code, body = server.get("/healthz")
    assert code == 200 and json.loads(body)["ok"] is True


def test_info_carries_probe_truth_including_refusals(server):
    from transmute import __version__
    info = json.loads(server.get("/api/info")[1])
    assert info["version"] == __version__
    targets = {t["target"]: t for t in info["categories"]["image"]["targets"]}
    assert targets["webp"]["available"] is True
    # F3 on the web too: refused target stays listed WITH its machine reason
    assert targets["avif"]["available"] is False
    assert "AVIF plugin missing" in targets["avif"]["reason"]
    # 'pdf (verify renders)' style diagnostics never become web targets
    assert all("verify renders" not in t for t in targets)


# ------------------------------------------------------------- full circle
def test_upload_convert_verify_download(server):
    code, up = server.upload("photo.png", _png())
    assert code == 200 and up["category"] == "image"
    code, res = server.post_json("/api/convert",
                                 {"upload_id": up["upload_id"],
                                  "target": "webp", "quality": 60})
    assert code == 200
    job = server.wait_done(res["job_id"])
    assert job["download"] is True
    code, blob = server.get(f"/download/{res['job_id']}")
    assert code == 200 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP"


def test_pdf_target_end_to_end(server):
    code, up = server.upload("scan.png", _png())
    code, res = server.post_json("/api/convert",
                                 {"upload_id": up["upload_id"],
                                  "target": "pdf (page)"})
    assert code == 200
    server.wait_done(res["job_id"])
    code, blob = server.get(f"/download/{res['job_id']}")
    assert code == 200 and blob[:5] == b"%PDF-"


def test_refused_target_still_refuses_after_upload(server):
    code, up = server.upload("photo.png", _png())
    code, body = server.post_json("/api/convert",
                                  {"upload_id": up["upload_id"],
                                   "target": "no-such-target"})
    assert code == 400  # suffix_for never raises; serve gates on the report
    # a target the probe REFUSED stays refused server-side, with its reason
    code, body = server.post_json("/api/convert",
                                  {"upload_id": up["upload_id"],
                                   "target": "avif"})
    assert code == 400 and "AVIF" in body.get("detail", "")


def test_error_paths(server):
    assert server.post_json("/api/convert", {"upload_id": "deadbeef",
                                             "target": "webp"})[0] == 404
    assert server.post_json("/api/convert", {"upload_id": "x"})[0] == 400
    assert server.get("/download/nosuchjob")[0] == 404
    # a known-but-not-finished job must not leak a download: the
    # authoritative guard is core.output_for() - only 'replaced' + on disk
    from transmute.runner.journal import JournalRecord
    fake = JournalRecord(job_id="fakejob", src="s", dst="d", category="image",
                         state="running")
    server.state.journal.records["fakejob"] = fake
    assert server.get("/download/fakejob")[0] == 404
    del server.state.journal.records["fakejob"]


def test_upload_size_limit_streams_413(tmp_path):
    s = _Server(tmp_path, max_upload_mb=0.005)  # 5 kB cap
    try:
        code, body = s.upload("big.png", b"0" * 40_000)
        assert code == 413
        assert "max-upload-mb" in body.get("detail", "")
        # nothing left behind: no .part, no upload record
        assert list(s.state.uploads_dir.glob("*.part")) == []
        assert s.state.uploads == {}
    finally:
        s.close()


def test_hostile_filenames_are_never_paths():
    assert safe_name("../../../../etc/passwd") == "passwd"
    assert safe_name(r"C:\Windows\System32\evil.exe") == "evil.exe"
    assert safe_name("....") == "file"
    assert safe_name("") == "file"
    assert "/" not in safe_name("a/b/c.png") and "\\" not in safe_name("a\\b")


def test_upload_name_sanitized_server_side(server):
    code, up = server.upload("..%2F..%2Fshadow.png".replace("%2F", "/"),
                             _png())
    assert code == 200 and up["name"] == "shadow.png"
    assert server.state.uploads[up["upload_id"]].path.parent.name \
        == up["upload_id"]  # inside its own uploads/<id>/ dir


def test_unknown_category_rejected_cleanly(server):
    code, up = server.upload("data.xyz", b"not a known type")
    assert code == 200 and up["category"] is None
    code, body = server.post_json("/api/convert",
                                  {"upload_id": up["upload_id"],
                                   "target": "webp"})
    assert code == 400 and "recognise" in body.get("detail", "")


def test_first_benchmark_is_warmed_not_faked(tmp_path):
    # the benchmark must never block an HTTP request or be pretended away:
    # while it runs, info() says 'warming'; the picker only appears once the
    # machine has actually measured itself (F1)
    gate = threading.Event()

    def slow():
        gate.wait(10)
        return _report()

    st = ServeState(paths=AppPaths(tmp_path), report_provider=slow)
    try:
        info = st.info()
        assert info.get("warming") is True and info["categories"] == {}
        gate.set()
        assert st._report_ready.wait(15)
        for _ in range(100):
            info = st.info()
            if not info.get("warming"):
                break
            time.sleep(0.02)
        assert "image" in info["categories"]
    finally:
        st.stop()
