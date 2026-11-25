from __future__ import annotations

import json
import os
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon, QPainter, QPixmap, QColor, QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from . import backend as B
from .ui import APP_NAME, MainWindow


def app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#3ddc84"))
    p.setPen(QColor("#1b5e20"))
    p.drawRoundedRect(4, 4, 56, 56, 12, 12)
    f = QFont("Segoe UI", 26)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#0d3b1e"))
    p.drawText(pm.rect(), 0x84, "</>")  # AlignCenter
    p.end()
    return QIcon(pm)


def _selftest(win: MainWindow, spec: dict):
    """Automated smoke test: connect, list, wait for thumbnails, screenshot."""
    if spec.get("out"):  # the windowed exe has no console: write results to a file
        sys.stdout = sys.stderr = open(spec["out"], "w", encoding="utf-8", buffering=1)
    yes = QMessageBox.StandardButton.Yes
    QMessageBox.question = staticmethod(lambda *a, **k: yes)
    QMessageBox.warning = staticmethod(lambda *a, **k: (print("WARNING:", a[2] if len(a) > 2 else a), yes)[1])
    QMessageBox.critical = staticmethod(lambda *a, **k: (print("CRITICAL:", a[2] if len(a) > 2 else a), yes)[1])
    prof = B.Profile(name="selftest", host=spec["host"], port=spec["port"], username=spec["user"],
                     mode=spec.get("mode", "sftp"), su_command=spec.get("su", "su -c"),
                     start_dir=spec["start"])
    win.set_view(spec.get("view", "icons"))
    win.start_connect(prof, spec.get("password", ""), "")
    state = {"t": 0, "stage": 0}

    def poll():
        state["t"] += 1
        if state["t"] > spec.get("timeout", 120) * 4:
            print("SELFTEST TIMEOUT")
            win.grab().save(spec["shot"])
            QApplication.quit()
            return
        if spec.get("mirror"):  # start the screen mirror right after connecting
            if state["stage"] == 0 and win.model.rowCount():
                win.start_mirror()
                state["stage"], state["t2"] = 1, state["t"]
            elif state["stage"] == 1 and state["t"] > state["t2"] + 60:
                from . import mirror as M
                print("MIRROR running:", [p.poll() is None for p in win.mirror_procs])
                print(M.log_tail(20))
                for p in win.mirror_procs:
                    p.terminate()
                print("SELFTEST DONE")
                QApplication.quit()
                return
            QTimer.singleShot(250, poll)
            return
        if state["stage"] == 0 and win.model.rowCount() and not win.thumbs.queue \
                and not win.status_thumbs.text() and state["t"] > 8:
            state["stage"] = 1
            for i, e in enumerate(win.model.entries):
                print("ENTRY", e.kind, e.name, e.size, e.perm_string, e.owner, e.group, e.link_target,
                      "THUMB" if e.path in win.model.thumbs else "", win.thumb_info.get(e.path, ""))
            sel = spec.get("select")
            for r in range(win.proxy.rowCount()):
                idx = win.proxy.index(r, 0)
                if win.proxy.data(idx) == sel:
                    win.active_view().setCurrentIndex(idx)
            state["t2"] = state["t"]
        if state["stage"] == 1 and state["t"] > state["t2"] + 6:
            rows = [(win.props.table.item(i, 0).text(), win.props.table.item(i, 1).text())
                    for i in range(win.props.table.rowCount())]
            print("PROPS", rows)
            win.grab().save(spec["shot"])
            print("SELFTEST DONE")
            QApplication.quit()
            return
        QTimer.singleShot(250, poll)
    QTimer.singleShot(500, poll)


def setup_logging():
    import logging
    path = os.path.join(B.app_dir(), "androunderthehood.log")
    logging.basicConfig(filename=path, filemode="w", level=logging.DEBUG, encoding="utf-8", force=True,
                        format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
    # native crashes (Qt, codecs…) leave a trace too
    try:
        import faulthandler
        global _crash_file
        _crash_file = open(os.path.join(B.app_dir(), "crash.log"), "a", encoding="utf-8")
        faulthandler.enable(_crash_file)
    except Exception:  # noqa: BLE001
        pass

    def hook(etype, value, tb):
        import traceback
        text = "".join(traceback.format_exception(etype, value, tb))
        logging.getLogger("androunderthehood").error("unhandled error:\n%s", text)
        try:
            from PySide6.QtWidgets import QApplication
            if QApplication.instance():
                QMessageBox.critical(None, "AndroUnderTheHood – unexpected error",
                                     f"{etype.__name__}: {value}\n\nDetails are in {path}")
        except Exception:  # noqa: BLE001
            pass
    sys.excepthook = hook
    logging.getLogger("paramiko").setLevel(logging.INFO)
    logging.getLogger("androunderthehood").info("AndroUnderTheHood starting")


def main():
    B.migrate_old_name()
    setup_logging()
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AndroUnderTheHood.App")
        except Exception:  # noqa: BLE001
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    from . import theme
    theme.apply(app)
    app.setWindowIcon(app_icon())
    win = MainWindow()
    win.plugins.load_all()
    win.show()
    spec_path = os.environ.get("ANDROUNDERTHEHOOD_SELFTEST")
    if spec_path:
        with open(spec_path, "r", encoding="utf-8") as f:
            _selftest(win, json.load(f))
    else:
        QTimer.singleShot(200, win.connect_dialog)
    sys.exit(app.exec())
