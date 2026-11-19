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
    def _hdr(self, tok: str | None = None,
             cookie: str | None = None) -> dict[str, str]:
        h = {}
        if tok is not None:
            h["X-Token"] = tok
        if cookie is not None:
            h["Cookie"] = cookie
        return h

    def get(self, path: str, tok: str | None = None,
            cookie: str | None = None) -> tuple[int, bytes]:
        req = urllib.request.Request(self.base + path,
                                     headers=self._hdr(tok, cookie))
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def post_json(self, path: str, payload: dict,
                  tok: str | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={**self._hdr(tok),
                     "content-type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read() or b"{}")
            except ValueError:
                body = {}
            return e.code, body

    def upload(self, filename: str, data: bytes,
               tok: str | None = None) -> tuple[int, dict]:
        b = uuid.uuid4().hex
        head = (f'--{b}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\nContent-Type: image/png\r\n\r\n').encode()
        body = head + data + f"\r\n--{b}--\r\n".encode()
        req = urllib.request.Request(
            self.base + "/api/upload", data=body,
            headers={**self._hdr(tok),
                     "content-type": f"multipart/form-data; boundary={b}"},
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


# ------------------------------------------------------------- token lock
def test_token_locks_every_door_but_the_page(tmp_path):
    srv = _Server(tmp_path / "tok", auth_token="sekret")
    try:
        assert srv.get("/")[0] == 200              # page shell: public, asks
        assert srv.get("/healthz")[0] == 401       # even a ping costs a key
        assert srv.get("/api/info")[0] == 401
        assert srv.get("/api/info", tok="wrong")[0] == 401
        code, body = srv.get("/api/info", tok="sekret")
        assert code == 200 and "image" in json.loads(body)["categories"]
        assert srv.upload("t.png", _png())[0] == 401   # uploads locked too
        code, up = srv.upload("t.png", _png(), tok="sekret")
        assert code == 200
        assert srv.get("/api/jobs", cookie="trtoken=sekret")[0] == 200
        # cookies are the download path: a plain <a href> sends no headers,
        # so the browser must be able to authenticate with the cookie alone
        code, res = srv.post_json(
            "/api/convert",
            {"upload_id": up["upload_id"], "target": "webp"}, tok="sekret")
        job = res["job_id"]
        srv.wait_done(job)
        assert srv.get(f"/download/{job}")[0] == 401
        got = srv.get(f"/download/{job}", cookie="trtoken=sekret")
        assert got[0] == 200 and got[1][:4] == b"RIFF"
    finally:
        srv.close()


def test_token_rules_are_one_rule():
    from transmute.serve.app import resolve_token
    assert resolve_token("127.0.0.1", None) is None   # this machine IS trust
    assert resolve_token("localhost", None) is None
    tok = resolve_token("0.0.0.0", None)
    assert tok and len(tok) >= 16
    assert resolve_token("0.0.0.0", None) != tok      # fresh key per start
    assert resolve_token("0.0.0.0", "mine") == "mine"  # --token pins it
    assert resolve_token("127.0.0.1", "mine") == "mine"


# ------------------------------------------------------------- launcher
def test_launcher_lifecycle_switch_and_stop(tmp_path):
    from urllib.parse import urlparse
    from transmute.serve.launcher import ServeLauncher
    n = [0]

    def factory(**kw):
        n[0] += 1
        return ServeState(paths=AppPaths(tmp_path / f"l{n[0]}"),
                          report_provider=_report, **kw)

    ln = ServeLauncher(state_factory=factory)
    try:
        st = ln.start("local", port=0)
        assert st["running"] and st["mode"] == "local" and not st["token"]
        # a server converts OTHER people's files: it must run under the
        # throttle unless TRANSMUTE_GOVERNOR says otherwise, and the
        # governor has to reach the queue that will actually spawn engines
        gov_ref = ln._st.governor
        assert gov_ref is not None and ln._st.queue.governor is gov_ref
        assert st["url"].startswith("http://127.0.0.1:")
        base = st["url"].split("?")[0]
        with urllib.request.urlopen(base + "healthz", timeout=5) as r:
            assert r.status == 200                  # localhost: no key
        st = ln.start("lan", port=0)                # switch adds the lock
        assert st["token"] and "?token=" in st["url"]
        host = urlparse(st["url"]).hostname or "127.0.0.1"
        port = urlparse(st["url"]).port
        url = f"http://127.0.0.1:{port}/api/info"
        req = urllib.request.Request(url, headers={"X-Token": "nope"})
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("wrong token was accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        req = urllib.request.Request(url, headers={"X-Token": st["token"]})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 200
        assert host  # lan url names the address a phone would use
        ln.stop()
        assert ln.status()["mode"] == "off"
        assert gov_ref._thread is None          # no monitor thread outlives
        assert ln._gov is None                  # a stopped server
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz",
                                   timeout=2)
            raise AssertionError("port still open after stop")
        except (urllib.error.HTTPError, OSError):
            pass                                    # door closed
    finally:
        ln.stop()


def test_launcher_reports_a_busy_port(tmp_path):
    from transmute.serve.launcher import ServeLauncher
    n = [0]

    def factory(**kw):
        n[0] += 1
        return ServeState(paths=AppPaths(tmp_path / f"p{n[0]}"),
                          report_provider=_report, **kw)

    a = ServeLauncher(state_factory=factory)
    b = ServeLauncher(state_factory=factory)
    st = a.start("local", port=18965)
    assert st["running"], st["error"]
    try:
        st2 = b.start("local", port=18965)
        assert not st2["running"] and st2["error"]  # spoken, not swallowed
        assert b.status()["mode"] == "off"
    finally:
        a.stop()
        b.stop()


# ------------------------------------------------------------- batch
def test_batch_part_success_names_every_refusal(tmp_path):
    srv = _Server(tmp_path / "batch")
    try:
        ids = []
        for nm in ("a.png", "b.png"):
            code, up = srv.upload(nm, _png())
            assert code == 200
            ids.append(up["upload_id"])
        code, up = srv.upload("ghost.png", _png())
        assert code == 200
        items = [{"upload_id": ids[0], "target": "webp"},
                 {"upload_id": ids[1], "target": "webp"},
                 {"upload_id": up["upload_id"], "target": "no-such-target"},
                 {"upload_id": "missing123", "target": "webp"}]
        code, res = srv.post_json("/api/convert_batch", {"items": items})
        assert code == 200
        assert len(res["jobs"]) == 2                      # good files run
        refused = {r["name"]: r["reason"] for r in res["refused"]}
        assert "unknown target" in refused.pop("ghost.png")   # machine's words
        assert "upload not found" in refused.pop("missing123")
        assert refused == {}                              # nothing else lied
        # every refusal carries the upload_id that was sent: names are not
        # unique, so the page can only attribute "not queued" by the id
        assert all(r.get("upload_id") for r in res["refused"])
        got_ids = {r["upload_id"] for r in res["refused"]}
        assert {up["upload_id"], "missing123"} == got_ids
        for job in res["jobs"]:
            srv.wait_done(job)
            c, blob = srv.get(f"/download/{job}")
            assert c == 200 and blob[:4] == b"RIFF"       # both really made
        assert srv.post_json("/api/convert_batch", {"items": []})[0] == 400
    finally:
        srv.close()


def test_worker_loop_reports_queue_error_and_survives(tmp_path):
    """A runner error must kill THAT burst, never the worker thread: the
    fix appends worker_error to the event log and keeps serving - without
    it, one escaping exception silently stops every later upload from
    converting, with nothing the user could ever see."""
    from types import SimpleNamespace
    from transmute.jsonl import JsonlWriter
    from transmute.serve.core import ServeState as _SS

    core = _SS.__new__(_SS)                          # loop deps only
    core._stop, core._wake = threading.Event(), threading.Event()
    ev = JsonlWriter(tmp_path / "events.jsonl")
    calls = {"n": 0}

    def run(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("engine exploded")
        return SimpleNamespace(processed=0)

    core.queue = SimpleNamespace(run=run, events=ev, stop=lambda: None)
    core._wake.set()
    th = threading.Thread(target=core._worker_loop, daemon=True)
    th.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            lines = [json.loads(l) for l in
                     (tmp_path / "events.jsonl").read_text().splitlines()]
        except FileNotFoundError:                # thread has not written yet
            lines = []
        if any(l.get("event") == "worker_error" for l in lines):
            break
        time.sleep(0.05)
    else:
        pytest.fail("worker_error never reached the event log")
    # thread survived the burst: wake again, second call returns cleanly
    core._wake.set()
    time.sleep(0.2)
    assert th.is_alive()
    core._stop.set()
    core._wake.set()
    th.join(5)
    assert not th.is_alive()


def test_duplicate_names_refusal_attaches_to_the_right_upload(tmp_path):
    """Two uploads may share a display name (safe_name never dedups). The
    refusal must identify WHICH upload was refused - the page used to map
    by name and painted 'not queued' on the innocent twin."""
    srv = _Server(tmp_path / "dupname")
    try:
        c1, u1 = srv.upload("IMG.jpg", _png())
        c2, u2 = srv.upload("IMG.jpg", _png())
        assert u1["upload_id"] != u2["upload_id"]
        code, res = srv.post_json("/api/convert_batch", {"items": [
            {"upload_id": u1["upload_id"], "target": "webp"},
            {"upload_id": u2["upload_id"], "target": "no-such-target"}]})
        assert code == 200 and len(res["jobs"]) == 1
        refused = res["refused"]
        assert len(refused) == 1
        assert refused[0]["upload_id"] == u2["upload_id"]   # the guilty twin
        for job in res["jobs"]:
            srv.wait_done(job)
    finally:
        srv.close()


# ------------------------------------------------- clear finished
def test_clear_finished_forgets_rows_and_frees_outputs(server):
    code, up = server.upload("a.png", _png())
    assert code == 200
    code, res = server.post_json(
        "/api/convert", {"upload_id": up["upload_id"], "target": "webp"})
    assert code == 200
    job = res["job_id"]
    server.wait_done(job)
    dst = Path(server.state.journal.records[job].dst)
    assert dst.is_file()
    # a queued row is NOT finished: it must survive a neighbour's clear
    from transmute.runner.journal import Journal, JournalRecord
    server.state.journal.append(JournalRecord(
        job_id="stay-queued", src="/x/keep.png",
        dst=str(server.state.outputs_dir / "keep.webp"), category="image"))
    code, got = server.post_json("/api/clear_finished", {})
    assert code == 200 and got["cleared"] == 1
    assert not dst.exists()                    # output space is freed
    assert server.get(f"/download/{job}")[0] == 404
    assert [k["state"] for k in server.state.jobs()] == ["queued"]
    # the forgetting survives a serve restart: tombstone lines replay gone
    j2 = Journal(server.state.journal.path)
    assert [r.job_id for r in j2.records.values()] == ["stay-queued"]
    # and an already-clean list answers honestly, not with an error
    assert server.post_json("/api/clear_finished", {})[1] == {"cleared": 0}


def test_clear_finished_needs_the_password_too(tmp_path):
    srv = _Server(tmp_path / "cf", auth_token="sekret")
    try:
        assert srv.post_json("/api/clear_finished", {})[0] == 401
        code, got = srv.post_json("/api/clear_finished", {}, tok="sekret")
        assert code == 200 and got == {"cleared": 0}
    finally:
        srv.close()


def test_launcher_operator_password_instead_of_a_generated_one(tmp_path):
    from urllib.parse import urlparse
    from transmute.serve.launcher import ServeLauncher

    def factory(**kw):
        return ServeState(paths=AppPaths(tmp_path / "pw"),
                          report_provider=_report, **kw)

    ln = ServeLauncher(state_factory=factory)
    try:
        st = ln.start("lan", port=0, token="hunter2 here")
        assert st["token"] == "hunter2 here"
        # the shown link carries it URL-quoted, so it still just works
        assert "hunter2%20here" in st["url"]
        port = urlparse(st["url"]).port
        url = f"http://127.0.0.1:{port}/api/info"
        req = urllib.request.Request(url, headers={"X-Token": "hunter2"})
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("a prefix of the password was accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401             # all of it or nothing
        req = urllib.request.Request(url, headers={"X-Token": "hunter2 here"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 200
    finally:
        ln.stop()
