"""The Serve dialog's face: radios drive the launcher, words tell the truth.

The launcher itself is tested headless in test_serve.py; this only checks
the widget layer (stub launcher, no network, offscreen platform).
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.unit


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class _StubLauncher:
    def __init__(self):
        self.mode = "off"
        self.url = ""
        self.token = None
        self.error = ""
        self.calls: list[str] = []

    @property
    def running(self) -> bool:
        return self.mode != "off"

    def start(self, mode, port=8765, max_upload_mb=2048.0, token=None):
        self.calls.append(mode)
        self.mode = mode
        self.token = token or ("tok123tok123" if mode == "lan" else None)
        self.url = "http://127.0.0.1:8765/" + (f"?token={self.token}"
                                               if self.token else "")
        return self.status()

    def stop(self):
        self.calls.append("off")
        self.mode, self.url, self.token = "off", "", None

    def status(self):
        return {"mode": self.mode, "running": self.running, "url": self.url,
                "token": self.token, "error": self.error}


def test_dialog_drives_launcher_exactly_once(qapp):
    from transmute.gui.serve_dialog import ServeDialog
    ln = _StubLauncher()
    dlg = ServeDialog(ln)
    qapp.processEvents()
    assert ln.calls == []                    # opening changes nothing
    dlg.rad_lan.setChecked(True)
    qapp.processEvents()                     # debounced single decision
    assert ln.calls == ["lan"]
    assert "token=" in dlg.url_edit.text()
    assert "Serving on your network" in dlg.lbl.text()
    dlg.rad_off.setChecked(True)
    qapp.processEvents()
    assert ln.calls == ["lan", "off"]
    assert dlg.lbl.text() == "Not serving."
    assert dlg.url_edit.text() == ""


def test_dialog_reopens_showing_the_running_truth(qapp):
    from transmute.gui.serve_dialog import ServeDialog
    ln = _StubLauncher()
    ln.start("lan")
    dlg = ServeDialog(ln)
    qapp.processEvents()
    assert dlg.rad_lan.isChecked()           # state, not assumptions
    assert ln.calls == ["lan"]               # reopen did not restart it
    assert "token=" in dlg.url_edit.text()


def test_closing_the_app_closes_the_door(qapp, tmp_path, monkeypatch):
    from test_gui_controller import _report
    from transmute.paths import AppPaths
    from transmute.serve import core as serve_core
    from transmute.serve.core import ServeState

    class _QuietState(ServeState):
        # same lifecycle, isolated roots, fake matrix: this test proves the
        # WINDOW closes the door, not that serve works (test_serve.py does)
        def __post_init__(self):
            self.paths = AppPaths(tmp_path / "door")
            self.report_provider = _report
            super().__post_init__()

    monkeypatch.setattr(serve_core, "ServeState", _QuietState)
    from transmute.gui.app import MainWindow
    from transmute.gui.controller import ConverterController
    ctrl = ConverterController(report_provider=_report)
    ctrl._report = _report()
    win = MainWindow(controller=ctrl)
    try:
        assert win.serve.status()["mode"] == "off"      # quiet until asked
        st = win.serve.start("local", port=0)
        assert st["running"], st["error"]
        import urllib.request
        base = st["url"].split("?")[0]
        with urllib.request.urlopen(base + "healthz", timeout=5) as r:
            assert r.status == 200
        win.close()
        assert win.serve.status()["mode"] == "off"      # window closed = door
    finally:
        win.serve.stop()                                # idempotent


def test_password_field_only_governs_the_home_network(qapp):
    from transmute.gui.serve_dialog import ServeDialog
    ln = _StubLauncher()
    dlg = ServeDialog(ln)
    qapp.processEvents()
    assert not dlg.pw_edit.isEnabled()          # nothing to protect when off
    dlg.pw_edit.setText("hunter2 secret")
    dlg.rad_lan.setChecked(True)
    qapp.processEvents()
    assert ln.calls == ["lan"]
    assert ln.token == "hunter2 secret"         # the operator's word wins
    assert dlg.pw_edit.isEnabled()
    dlg.rad_off.setChecked(True)
    qapp.processEvents()
    assert ln.calls == ["lan", "off"]


def test_typed_password_replaces_a_running_generated_one(qapp):
    from transmute.gui.serve_dialog import ServeDialog
    ln = _StubLauncher()
    dlg = ServeDialog(ln)
    qapp.processEvents()
    dlg.rad_lan.setChecked(True)
    qapp.processEvents()
    assert ln.token == "tok123tok123"           # generated: box left empty
    dlg.pw_edit.setText("hunter2")
    dlg.pw_edit.editingFinished.emit()          # Enter applies
    qapp.processEvents()
    assert ln.calls == ["lan", "off", "lan"]    # the old key dies with it
    assert ln.token == "hunter2"


def test_empty_password_box_leaves_a_running_server_alone(qapp):
    from transmute.gui.serve_dialog import ServeDialog
    ln = _StubLauncher()
    ln.start("lan")
    dlg = ServeDialog(ln)
    qapp.processEvents()
    dlg.pw_edit.editingFinished.emit()          # focus-out on an empty box
    qapp.processEvents()
    assert ln.calls == ["lan"]                  # no accidental restart:
    assert ln.token == "tok123tok123"           # the link stays valid
