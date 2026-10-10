"""A smoke test of the window: tutorial, samples, every mode. Runs without a display."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication, QMainWindow

import vextrace as vt


def pump(app: QApplication, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def wait_for_trace(app: QApplication, win) -> None:
    deadline = time.time() + 60
    while time.time() < deadline and (
        win._worker is not None or win._pending or win._refine or win._debounce.isActive()
    ):
        pump(app, 0.05)


def test_window_tour_samples_and_modes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    results: dict = {}

    def drive(_app=None) -> int:
        app = QApplication.instance()
        win = next(w for w in app.topLevelWidgets() if isinstance(w, QMainWindow))
        pump(app, 0.8)
        results["tour_started"] = win._tour is not None
        while win._tour is not None:
            win._tour._advance()
            pump(app, 0.2)
        results["tour_saved"] = win._prefs.value("tutorial_done", False, type=bool)

        win.open_path(vt.SAMPLES_DIR / "mountains.png")
        for index, mode in enumerate(vt.MODES):
            if mode == vt.MODE_POTRACE and not vt.potrace_available():
                continue
            win._on_mode_changed(index)
            wait_for_trace(app, win)
            results[mode] = win.svg is not None and win.save_button.isEnabled()
        win.go_home()
        results["home"] = win.pages.currentWidget() is win.home
        win.close()
        return 0

    monkeypatch.setattr(QApplication, "exec", drive)
    assert vt.run_gui(None) == 0
    assert results.pop("tour_started") and results.pop("tour_saved")
    assert results.pop("home")
    assert all(results.values()), results
