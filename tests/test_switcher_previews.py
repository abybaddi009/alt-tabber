"""Switcher previews must not block showing the overlay or pressing Tab.

Previously the window_previews theme captured every tile's screenshot
before show, and the default theme captured the selected window's preview
synchronously on open and on every cycle. Both now go through
``PreviewLoader`` and fill in once the capture lands.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage

from alttabber.backends.base import AppInfo, AppProvider, WindowInfo, WindowProvider
from alttabber.core.mru import MRUTracker, WindowMRUTracker
from alttabber.core.switcher import SwitcherController


def _png_bytes() -> bytes:
    img = QImage(8, 8, QImage.Format_RGB32)
    img.fill(QColor("blue"))
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


class _SlowWindowProvider(WindowProvider):
    def __init__(self, windows: list[WindowInfo], delay: float) -> None:
        self._windows = windows
        self.delay = delay
        self.png = _png_bytes()

    def list_windows(self) -> list[WindowInfo]:
        return list(self._windows)

    def list_app_windows(self, bundle_id: str) -> list[WindowInfo]:
        return [w for w in self._windows if w.bundle_id == bundle_id]

    def activate_window(self, window_id: int) -> bool:
        return True

    def activate_app(self, bundle_id: str) -> bool:
        return True

    def capture_preview(self, window_id: int) -> bytes | None:
        time.sleep(self.delay)
        return self.png


class _AppProvider(AppProvider):
    def list_apps(self) -> list[AppInfo]:
        return []

    def launch(self, bundle_path: str) -> bool:
        return True

    def icon_for_bundle_id(self, bundle_id: str) -> str | None:
        return None


def _windows() -> list[WindowInfo]:
    return [
        WindowInfo(app_name=f"App{i}", window_title=f"Win{i}", window_id=i, bundle_id=f"b{i}")
        for i in range(1, 5)
    ]


def _controller(theme: str, delay: float = 0.1) -> SwitcherController:
    config = SimpleNamespace(
        ui_size="medium",
        screen_preference="active",
        switcher=SimpleNamespace(
            show_previews=True, theme=theme, expand_windows=True
        ),
    )
    return SwitcherController(
        _SlowWindowProvider(_windows(), delay),
        _AppProvider(),
        MRUTracker(),
        mode="apps",
        config=config,
        window_mru=WindowMRUTracker(),
    )


def _pump_until(qapp, cond, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return False


def test_window_previews_theme_shows_before_captures_finish(qapp):
    ctl = _controller("window_previews")
    # Warm up: the first native window shown in a process costs ~300 ms of
    # one-time Qt/AppKit setup unrelated to captures.
    ctl.on_trigger()
    ctl.on_cancel()
    t0 = time.monotonic()
    ctl.on_trigger()
    # 4 windows x 100 ms would be >= 400 ms if captured inline.
    assert time.monotonic() - t0 < 0.3, "on_trigger blocked on tile captures"
    assert ctl.overlay.isVisible()

    assert _pump_until(qapp, lambda: len(ctl.overlay._tile_previews) == 4)
    ctl.on_cancel()


def test_default_theme_cycle_does_not_block_on_capture(qapp):
    ctl = _controller("default")
    ctl.on_trigger()
    t0 = time.monotonic()
    ctl.on_cycle(reverse=False)
    assert time.monotonic() - t0 < 0.05, "Tab press blocked on preview capture"

    selected = ctl.overlay.current_entry().window_id
    assert _pump_until(qapp, lambda: selected in ctl._selection_previews)
    assert ctl._selection_previews[selected] is not None
    ctl.on_cancel()
