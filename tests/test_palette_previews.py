"""Palette previews must load in the background, after the palette shows.

Previously ``show_palette`` captured every window's screenshot inline before
``show()``, so open latency scaled with window count (and stalled entirely
when a capture hung). ``_PreviewLoader`` now captures on a worker thread and
streams results back; the overview grid picks them up as they arrive.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage, QPixmap

from alttabber.core.palette import _OverviewGrid, _PreviewLoader
from alttabber.core.switcher import _WindowEntry


def _png_bytes() -> bytes:
    img = QImage(8, 8, QImage.Format_RGB32)
    img.fill(QColor("red"))
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


class _SlowProvider:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls: list[int] = []
        self.png = _png_bytes()

    def capture_preview(self, window_id: int) -> bytes | None:
        self.calls.append(window_id)
        time.sleep(self.delay)
        return self.png


def _pump_until(qapp, cond, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return False


def test_start_returns_immediately_and_streams_results(qapp):
    provider = _SlowProvider(delay=0.1)
    loader = _PreviewLoader()
    got: list[tuple[int, int]] = []
    loader.preview_ready.connect(lambda gen, wid, data: got.append((gen, wid)))

    t0 = time.monotonic()
    gen = loader.start(provider, [1, 2, 3])
    assert time.monotonic() - t0 < 0.05, "start() blocked on capture"

    assert _pump_until(qapp, lambda: len(got) == 3)
    assert got == [(gen, 1), (gen, 2), (gen, 3)]


def test_cancel_stops_worker_before_remaining_windows(qapp):
    provider = _SlowProvider(delay=0.1)
    loader = _PreviewLoader()
    got: list[int] = []
    loader.preview_ready.connect(lambda gen, wid, data: got.append(wid))

    loader.start(provider, [1, 2, 3, 4, 5])
    assert _pump_until(qapp, lambda: len(provider.calls) >= 1)
    loader.cancel()
    time.sleep(0.3)
    qapp.processEvents()

    assert len(provider.calls) <= 2, "worker kept capturing after cancel"
    assert got == [], "result from a cancelled generation was delivered"


def test_grid_picks_up_previews_that_arrive_after_entries(qapp):
    grid = _OverviewGrid(scale=1.0)
    grid.resize(800, 600)
    previews: dict[int, QPixmap] = {}
    entry = _WindowEntry(
        window_id=1, title="one", app_name="app", icon_path=None, bundle_id="b"
    )
    grid.set_entries([entry], previews)
    tile_before = grid._container.tile(0)

    pix = QPixmap.fromImage(QImage.fromData(_png_bytes()))
    previews[1] = pix
    grid.set_previews(previews)

    tile_after = grid._container.tile(0)
    assert tile_after is not tile_before, "tile not rebuilt with new preview"
    assert tile_after._signature[2] == id(pix)
