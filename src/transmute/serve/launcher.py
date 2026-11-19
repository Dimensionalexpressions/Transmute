"""Start/stop serve mode from inside the desktop app (Qt-free on purpose).

The GUI dialog is a few labels around this class; the class is testable
headless (it's the same uvicorn-in-a-thread the tests already run).
Modes: 'off' | 'local' (127.0.0.1, no token - being here IS the trust)
| 'lan' (0.0.0.0 + generated access token, link shown for the phone).
"""
from __future__ import annotations

import secrets
import socket
import threading
import time
from typing import Any


def local_ip() -> str:
    """The IPv4 this machine uses to reach its gateway - what a phone on
    the home Wi-Fi would actually type. No packets leave: UDP connect just
    consults the routing table."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class ServeLauncher:
    def __init__(self, state_factory: Any = None):
        # injectable for tests; production uses the real ServeState
        self._state_factory = state_factory
        self._srv = None
        self._thread: threading.Thread | None = None
        self._st = None
        self.mode = "off"
        self.error = ""
        self.token: str | None = None
        self.url = ""

    @property
    def running(self) -> bool:
        return bool(self._srv and self._thread and self._thread.is_alive()
                    and self.mode != "off")

    def start(self, mode: str, port: int = 8765,
              max_upload_mb: float = 2048.0) -> dict[str, Any]:
        if mode == self.mode and self.running:
            return self.status()
        self.stop()
        if mode not in ("local", "lan"):
            raise ValueError(f"unknown serve mode {mode!r}")
        import uvicorn
        from .app import create_app
        host = "127.0.0.1" if mode == "local" else "0.0.0.0"
        token = secrets.token_urlsafe(18) if mode == "lan" else None
        if self._state_factory is not None:
            st = self._state_factory(max_upload_mb=max_upload_mb,
                                     auth_token=token)
        else:
            from .core import ServeState
            st = ServeState(max_upload_mb=max_upload_mb, auth_token=token)
        cfg = uvicorn.Config(create_app(st), host=host, port=port,
                             log_level="warning")
        srv = uvicorn.Server(cfg)
        err: list[str] = []

        def _run() -> None:
            try:
                srv.run()
            except BaseException as exc:  # noqa: BLE001 - uvicorn reports a
                # failed bind by dying with SystemExit(1): that tells us
                # 'could not bind', anything else deserves its real words
                if isinstance(exc, SystemExit):
                    err.append("")
                else:
                    err.append(str(exc) or exc.__class__.__name__)

        thread = threading.Thread(target=_run, daemon=True,
                                  name="transmute-serve")
        thread.start()
        deadline = time.time() + 20
        while not srv.started and time.time() < deadline:
            if not thread.is_alive():
                break
            time.sleep(0.02)
        if not srv.started:
            thread.join(timeout=2)
            self.error = (err[0] if err and err[0] else
                          f"port {port} is busy or Transmute could not "
                          "bind it")
            st.stop()
            return self.status()
        real = port or srv.servers[0].sockets[0].getsockname()[1]
        shown = local_ip() if mode == "lan" else "127.0.0.1"
        self._srv, self._thread, self._st = srv, thread, st
        self.mode, self.error, self.token = mode, None, token
        self.url = f"http://{shown}:{real}/" + (f"?token={token}"
                                                if token else "")
        return self.status()

    def stop(self) -> None:
        srv, thread, st = self._srv, self._thread, self._st
        self._srv = self._thread = self._st = None
        self.mode, self.url, self.token = "off", "", None
        if srv is not None:
            srv.should_exit = True
            if thread is not None:
                thread.join(timeout=8)
        if st is not None:
            st.stop()  # idempotent; stops the worker even if uvicorn lingered

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode, "running": self.running, "url": self.url,
                "token": self.token, "error": self.error or ""}
