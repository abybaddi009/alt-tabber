"""Background window-preview capture shared by the palette and switcher."""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from ..backends.base import WindowProvider


class PreviewLoader(QObject):
    """Captures window previews off the GUI thread, one window at a time.

    ``capture_preview`` blocks (up to the backend's per-window timeout), so
    running it inline before showing an overlay made open latency scale with
    the number of windows (and every switcher Tab press wait on a capture). Each :meth:`start` bumps a generation counter;
    a running worker stops at its next window once superseded, and results
    from a stale generation are dropped on arrival.
    """

    # (generation, window_id, encoded image bytes or None when the capture
    # failed). Emitted from the worker thread; Qt queues delivery onto this
    # object's (GUI) thread.
    preview_ready = Signal(int, int, object)

    def __init__(self) -> None:
        super().__init__()
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    def start(self, provider: WindowProvider, window_ids: list[int]) -> int:
        self._generation += 1
        gen = self._generation
        if window_ids:
            threading.Thread(
                target=self._run,
                args=(provider, list(window_ids), gen),
                name="palette-previews",
                daemon=True,
            ).start()
        return gen

    def cancel(self) -> None:
        self._generation += 1

    def _run(self, provider: WindowProvider, window_ids: list[int], gen: int) -> None:
        for wid in window_ids:
            if gen != self._generation:
                return
            try:
                data = provider.capture_preview(wid)
            except Exception:
                data = None
            if gen == self._generation:
                self.preview_ready.emit(gen, wid, bytes(data) if data else None)
