"""Background focus monitor: keeps MRU trackers current with external activations.

The switcher and palette only record activations that pass through
Alt-Tabber's own slots (palette selection, Alt+Tab commit, the
``activate_window``/``activate_app`` signals). A window focused by any
other means -- a mouse click in another app, native Cmd-Tab, a Dock
click, a Spaces switch -- never touches the MRU trackers, so ordering
drifts from true focus recency until the next in-app activation.

:class:`FocusMonitor` polls the platform's frontmost window/app on a
``QTimer`` and touches the shared :class:`~alttabber.core.mru.MRUTracker`
and :class:`~alttabber.core.mru.WindowMRUTracker` when focus changes, so
the switcher and palette ordering reflects real usage.

Polling (rather than ``NSWorkspace`` notifications + per-app
``AXObserver``) reuses the two existing AX calls in
:meth:`~alttabber.platform.macos.windows.MacWindowProvider.frontmost_window_id`
and avoids the lifecycle cost of observing every running app. A 1--2 s
poll interval is imperceptible for MRU ordering.

The monitor is paused (via ``should_skip``) while the palette or a
switcher overlay is visible: :meth:`PaletteWindow.show_palette` calls
``activateWindow()`` on itself, which would otherwise record the palette
as the frontmost window and corrupt MRU. Pausing freezes the last-seen
state so the first poll after the overlay closes catches any focus change
that happened while it was up.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QTimer

from ..backends.base import WindowProvider
from .mru import MRUTracker, WindowMRUTracker


class FocusMonitor(QObject):
    """Polls the frontmost window/app and touches shared MRU trackers.

    Touches happen only on a *change* of frontmost window id or bundle id
    (dedupe), so a steady focus state produces no tracker writes and no
    redundant ``mru.json`` persistence. A ``None`` id (provider couldn't
    resolve it) is a valid distinct value: transitioning to ``None`` does
    not touch the corresponding tracker, so losing focus never records a
    bogus entry.
    """

    def __init__(
        self,
        window_provider: WindowProvider,
        mru: MRUTracker,
        wmru: WindowMRUTracker,
        should_skip: Callable[[], bool] | None = None,
        interval_ms: int = 1500,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._wp = window_provider
        self._mru = mru
        self._wmru = wmru
        self._should_skip = should_skip or (lambda: False)
        self._last_window_id: int | None = None
        self._last_bundle_id: str | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._poll)

    def start(self) -> None:
        """Begin periodic polling."""
        self._timer.start()

    def stop(self) -> None:
        """Stop polling."""
        self._timer.stop()

    def poll(self) -> None:
        """Run one poll cycle immediately (test seam)."""
        self._poll()

    def _poll(self) -> None:
        if self._should_skip():
            return
        wid = self._wp.frontmost_window_id()
        bid = self._wp.frontmost_bundle_id()
        # Dedupe per tracker: touch only the one whose key actually
        # changed, so a window switch within the same app doesn't
        # re-record the app, and an app switch keeping the same window
        # (rare but possible) doesn't re-record the window. Skipping
        # while paused (above) leaves _last_* frozen at the pre-pause
        # value, so the first poll after resume detects a change that
        # happened during the pause.
        if wid != self._last_window_id:
            self._last_window_id = wid
            if wid is not None:
                self._wmru.touch(wid)
        if bid != self._last_bundle_id:
            self._last_bundle_id = bid
            if bid is not None:
                self._mru.touch(bid)
