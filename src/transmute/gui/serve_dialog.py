"""'Serve' dialog: start/stop the HTTP front door from the desktop app.

All logic lives in serve.launcher (Qt-free, tested headless); this is only
its face. The wording is deliberately plain about what the link lets other
devices do - the token is the whole lock.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton,
    QVBoxLayout,
)


class ServeDialog(QDialog):
    def __init__(self, launcher, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Serving files")
        self.ln = launcher
        lay = QVBoxLayout(self)
        self.rad_off = QRadioButton("Not serving (off)")
        self.rad_local = QRadioButton("Only this computer (127.0.0.1, no login)")
        self.rad_lan = QRadioButton("My home network (phone / laptop can convert here)")
        for r in (self.rad_off, self.rad_local, self.rad_lan):
            r.toggled.connect(self._apply)
            lay.addWidget(r)
        prow = QHBoxLayout()
        prow.addWidget(QLabel("Password (home network):"))
        self.pw_edit = QLineEdit()
        self.pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw_edit.setPlaceholderText(
            "leave empty and Transmute makes one up, hidden in the link")
        self.pw_edit.setToolTip(
            "What other devices must enter before this machine converts\n"
            "anything for them. Anyone who knows it can use this computer.")
        self.pw_edit.editingFinished.connect(self._apply)  # Enter applies
        prow.addWidget(self.pw_edit, stretch=1)
        lay.addLayout(prow)
        row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setReadOnly(True)
        self.url_edit.setPlaceholderText("link stays empty while off")
        self.btn_copy = QPushButton("Copy link")
        self.btn_copy.clicked.connect(
            lambda: QApplication_clipboard(self.url_edit.text()))
        row.addWidget(self.url_edit, stretch=1)
        row.addWidget(self.btn_copy)
        lay.addLayout(row)
        self.lbl = QLabel()
        self.lbl.setWordWrap(True)
        lay.addWidget(self.lbl)
        note = QLabel(
            "Serving never touches the watch folders: it only converts what "
            "someone uploads, one at a time, results verify before download. "
            "On the home-network setting a password is required - set your "
            "own above or let Transmute generate one inside the link. Either "
            "way anyone holding the link or the password can make this "
            "machine convert, so share it like a Wi-Fi password. Stopping "
            "the app stops serving.")
        note.setWordWrap(True)
        note.setObjectName("muted")  # colored by theme.qss (QLabel#muted)
        lay.addWidget(note)
        {"off": self.rad_off, "local": self.rad_local,
         "lan": self.rad_lan}.get(launcher.mode, self.rad_off).setChecked(True)
        self._show(launcher.status())
        self._ready = True  # setChecked storms above must not start servers

    def _apply(self) -> None:
        # a password only means something when other machines can arrive
        self.pw_edit.setEnabled(self.rad_lan.isChecked())
        # toggling one radio off checks the next: two signals, one decision
        if not getattr(self, "_ready", False) or getattr(self, "_queued", False):
            return
        self._queued = True
        QTimer.singleShot(0, self._apply_now)

    def _apply_now(self) -> None:
        self._queued = False
        mode = ("lan" if self.rad_lan.isChecked() else
                "local" if self.rad_local.isChecked() else "off")
        pw = self.pw_edit.text().strip() if mode == "lan" else ""
        # a typed password replaces the running one; anything else already
        # running keeps its lock (a generated key is not something one
        # retypes, so an empty box means 'leave the running server alone')
        restart_new_pw = bool(pw) and pw != (self.ln.token or "")
        if mode == self.ln.mode and mode != "off" and not restart_new_pw:
            self._show(self.ln.status())
            return
        try:
            if mode == "off":
                self.ln.stop()
            else:
                if restart_new_pw and self.ln.running:
                    self.ln.stop()  # the running server holds the old key
                self._show(self.ln.start(mode, token=pw or None))
            self._show(self.ln.status())
        except Exception as exc:  # noqa: BLE001 - dialog must not explode
            self.lbl.setText(f"could not start: {exc}")
            self.rad_off.setChecked(True)


    def _show(self, st: dict) -> None:
        self.url_edit.setText(st.get("url") or "")
        if st.get("error"):
            self.lbl.setText(st["error"])
        elif st.get("mode") == "lan":
            self.lbl.setText("Serving on your network. Open the link on the "
                             "other device - it asks nothing else.")
        elif st.get("mode") == "local":
            self.lbl.setText("Serving this computer only (http://127.0.0.1).")
        else:
            self.lbl.setText("Not serving.")


def QApplication_clipboard(text: str) -> None:  # tiny indirection for tests
    if not text:
        return
    from PySide6.QtWidgets import QApplication
    cb = QApplication.clipboard()
    if cb is not None:
        cb.setText(text)
