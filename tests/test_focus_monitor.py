"""FocusMonitor updates MRU trackers from external window activations.

The switcher and palette only learn about activations that pass through
Alt-Tabber's own slots (palette selection, Alt+Tab commit). Clicks in
another app, native Cmd-Tab, or Dock clicks bypass those slots entirely,
so MRU ordering drifts from true focus recency. FocusMonitor polls the
frontmost window/app and touches the shared trackers so ordering stays
current.

These tests pin the poller's contract: touch on change, dedupe on
no-change, skip while paused (overlay visible), and graceful handling of
a provider that can't resolve a frontmost window/app id.
"""

from __future__ import annotations

from alttabber.backends.base import WindowInfo, WindowProvider


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeWindowProvider(WindowProvider):
    """Provider whose frontmost window/app are settable per test."""

    def __init__(self) -> None:
        self.frontmost_wid: int | None = None
        self.frontmost_bid: str | None = None

    def list_windows(self) -> list[WindowInfo]:
        return []

    def activate_window(self, window_id: int) -> bool:
        return False

    def activate_app(self, bundle_id: str) -> bool:
        return False

    def frontmost_bundle_id(self) -> str | None:
        return self.frontmost_bid

    def frontmost_window_id(self) -> int | None:
        return self.frontmost_wid


class _RecordingMRU:
    """App-level MRU that records every touch instead of persisting."""

    def __init__(self) -> None:
        self.touched: list[str] = []

    def touch(self, key: str) -> None:
        self.touched.append(key)


class _RecordingWindowMRU:
    """Per-window MRU that records every touch instead of persisting."""

    def __init__(self) -> None:
        self.touched: list[int] = []

    def touch(self, window_id: int) -> None:
        self.touched.append(window_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_monitor(skip_flag: list[bool] | None = None):
    """Build a FocusMonitor with controllable skip state.

    ``skip_flag`` is a one-element list so the closure can mutate it in
    place (Python bool can't be captured by reference in a closure that
    needs to reassign it); ``skip_flag[0]`` is read on every poll.
    """
    from alttabber.core.focus import FocusMonitor

    wp = _FakeWindowProvider()
    mru = _RecordingMRU()
    wmru = _RecordingWindowMRU()
    flag = skip_flag if skip_flag is not None else [False]
    mon = FocusMonitor(
        wp, mru, wmru, should_skip=lambda: flag[0], interval_ms=1500
    )
    return mon, wp, mru, wmru, flag


# ---------------------------------------------------------------------------
# Contract: base provider default
# ---------------------------------------------------------------------------


class _BareProvider(WindowProvider):
    """Concrete provider overriding only the abstract methods."""

    def list_windows(self) -> list[WindowInfo]:
        return []

    def activate_window(self, window_id: int) -> bool:
        return False

    def activate_app(self, bundle_id: str) -> bool:
        return False


def test_base_provider_frontmost_window_id_defaults_none():
    """A provider that doesn't override frontmost_window_id returns None.

    Guarantees the abstract base ships a safe default (None) so the
    poller no-ops on platforms/backends without window-level detection,
    rather than raising NotImplementedError.
    """
    assert _BareProvider().frontmost_window_id() is None


# ---------------------------------------------------------------------------
# Touch on change
# ---------------------------------------------------------------------------


def test_first_poll_touches_both_trackers(qapp):
    mon, wp, mru, wmru, _ = _make_monitor()
    wp.frontmost_wid = 101
    wp.frontmost_bid = "com.safari"

    mon.poll()

    assert wmru.touched == [101]
    assert mru.touched == ["com.safari"]


def test_window_change_touches_window_mru(qapp):
    mon, wp, mru, wmru, _ = _make_monitor()
    wp.frontmost_wid = 101
    wp.frontmost_bid = "com.safari"
    mon.poll()

    wp.frontmost_wid = 202  # user clicked another window
    mon.poll()

    assert wmru.touched == [101, 202]
    assert mru.touched == ["com.safari"]  # bundle unchanged -> not re-touched


def test_bundle_change_touches_app_mru(qapp):
    mon, wp, mru, wmru, _ = _make_monitor()
    wp.frontmost_wid = 101
    wp.frontmost_bid = "com.safari"
    mon.poll()

    wp.frontmost_bid = "com.notes"  # switched apps (e.g. via Dock)
    mon.poll()

    assert mru.touched == ["com.safari", "com.notes"]
    assert wmru.touched == [101]  # window id unchanged -> not re-touched


# ---------------------------------------------------------------------------
# Dedupe on no change
# ---------------------------------------------------------------------------


def test_unchanged_frontmost_does_not_touch(qapp):
    mon, wp, mru, wmru, _ = _make_monitor()
    wp.frontmost_wid = 101
    wp.frontmost_bid = "com.safari"
    mon.poll()

    mon.poll()  # same frontmost
    mon.poll()  # same again

    assert wmru.touched == [101]
    assert mru.touched == ["com.safari"]


# ---------------------------------------------------------------------------
# Pause / resume (overlay visibility)
# ---------------------------------------------------------------------------


def test_skip_when_paused_does_not_touch(qapp):
    mon, wp, mru, wmru, flag = _make_monitor()
    wp.frontmost_wid = 101
    wp.frontmost_bid = "com.safari"
    mon.poll()

    flag[0] = True  # palette/switcher overlay opened
    wp.frontmost_wid = 202
    wp.frontmost_bid = "com.notes"
    mon.poll()

    assert wmru.touched == [101]  # frozen at pre-pause state
    assert mru.touched == ["com.safari"]


def test_resume_after_pause_detects_change_during_pause(qapp):
    """Pausing freezes the last-seen state so the first poll after resume
    catches a focus change that happened while the overlay was up.

    Without this invariant, activating a window via the palette (which
    hides the overlay and shifts focus) would be missed: the poller would
    see the new frontmost, compare against a _last that had already
    advanced during the pause, and dedupe it away.
    """
    mon, wp, mru, wmru, flag = _make_monitor()
    wp.frontmost_wid = 101
    wp.frontmost_bid = "com.safari"
    mon.poll()

    flag[0] = True  # overlay opened
    wp.frontmost_wid = 202  # focus shifted while paused
    wp.frontmost_bid = "com.notes"
    mon.poll()  # skipped -- _last frozen at (101, com.safari)

    flag[0] = False  # overlay closed
    mon.poll()

    assert wmru.touched == [101, 202]
    assert mru.touched == ["com.safari", "com.notes"]


def test_resume_to_same_window_does_not_touch(qapp):
    """Cancelling an overlay (Esc) returns focus to the window that had it
    before -- already most-recent in MRU, so no touch is needed."""
    mon, wp, mru, wmru, flag = _make_monitor()
    wp.frontmost_wid = 101
    wp.frontmost_bid = "com.safari"
    mon.poll()

    flag[0] = True
    mon.poll()  # paused, no change to frontmost
    flag[0] = False
    mon.poll()  # same frontmost as _last -> dedupe

    assert wmru.touched == [101]
    assert mru.touched == ["com.safari"]


# ---------------------------------------------------------------------------
# None handling (provider can't resolve frontmost)
# ---------------------------------------------------------------------------


def test_none_window_id_skips_window_mru(qapp):
    """A backend without the private AX bridge (or no focused window) returns
    None for the window id; the poller must still track app-level MRU."""
    mon, wp, mru, wmru, _ = _make_monitor()
    wp.frontmost_wid = None
    wp.frontmost_bid = "com.safari"
    mon.poll()

    assert wmru.touched == []
    assert mru.touched == ["com.safari"]


def test_none_bundle_id_skips_app_mru(qapp):
    mon, wp, mru, wmru, _ = _make_monitor()
    wp.frontmost_wid = 101
    wp.frontmost_bid = None
    mon.poll()

    assert wmru.touched == [101]
    assert mru.touched == []


def test_both_none_does_not_touch(qapp):
    mon, wp, mru, wmru, _ = _make_monitor()
    wp.frontmost_wid = None
    wp.frontmost_bid = None
    mon.poll()

    assert wmru.touched == []
    assert mru.touched == []


def test_transition_to_none_does_not_touch(qapp):
    """Losing the focused window (e.g. frontmost app has no AX window)
    must not record a None touch that would corrupt the trackers."""
    mon, wp, mru, wmru, _ = _make_monitor()
    wp.frontmost_wid = 101
    wp.frontmost_bid = "com.safari"
    mon.poll()

    wp.frontmost_wid = None
    wp.frontmost_bid = None
    mon.poll()

    assert wmru.touched == [101]
    assert mru.touched == ["com.safari"]


# ---------------------------------------------------------------------------
# Timer lifecycle
# ---------------------------------------------------------------------------


def test_start_starts_timer(qapp):
    from PySide6.QtCore import QTimer

    mon, *_ = _make_monitor()
    assert not mon._timer.isActive()

    mon.start()
    assert mon._timer.isActive()

    mon.stop()
    assert not mon._timer.isActive()
