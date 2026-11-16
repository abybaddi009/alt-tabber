"""Switcher hover must not steal the selection from a stationary cursor.

Opening the switcher with the pointer already over (say) the 6th tile used
to select that tile, because Qt delivers ``enterEvent`` to whatever renders
under the cursor. A single Option+Tab then no longer landed on the 2nd
window. Hover must still work once the user actually moves the mouse.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint

from alttabber.core import switcher as switcher_mod
from alttabber.core.switcher import SwitcherOverlay, _WindowEntry


class _FakeCursor:
    pos_value = QPoint(100, 100)

    @classmethod
    def pos(cls) -> QPoint:
        return QPoint(cls.pos_value)


def _overlay(qapp, monkeypatch, n: int = 8) -> SwitcherOverlay:
    monkeypatch.setattr(switcher_mod, "QCursor", _FakeCursor)
    _FakeCursor.pos_value = QPoint(100, 100)
    ov = SwitcherOverlay()
    entries = [
        _WindowEntry(window_id=i, title=f"w{i}", app_name="app",
                     icon_path=None, bundle_id="com.app")
        for i in range(n)
    ]
    ov.set_windows(entries, select_index=1)
    ov.show_overlay()
    qapp.processEvents()
    return ov


def test_stationary_cursor_does_not_steal_selection(qapp, monkeypatch):
    ov = _overlay(qapp, monkeypatch)
    ov._container.tile(5).enterEvent(None)  # tile rendered under cursor
    assert ov._index == 1
    ov.hide()


def test_hover_works_after_mouse_moves(qapp, monkeypatch):
    ov = _overlay(qapp, monkeypatch)
    _FakeCursor.pos_value = QPoint(140, 100)
    ov._container.tile(5).enterEvent(None)
    assert ov._index == 5
    _FakeCursor.pos_value = QPoint(260, 100)
    ov._container.tile(6).enterEvent(None)
    assert ov._index == 6
    ov.hide()


def test_preview_rebuild_does_not_steal_keyboard_selection(qapp, monkeypatch):
    ov = _overlay(qapp, monkeypatch)
    _FakeCursor.pos_value = QPoint(140, 100)
    ov._container.tile(5).enterEvent(None)
    assert ov._index == 5
    ov.cycle()  # keyboard moves on to 6; cursor still parked over 5
    ov.update_tile_previews({})  # previews arrive -> tiles rebuilt
    ov._container.tile(5).enterEvent(None)
    assert ov._index == 6
    ov.hide()


def test_small_move_within_parked_tile_selects_it(qapp, monkeypatch):
    ov = _overlay(qapp, monkeypatch)
    tile = ov._container.tile(5)
    centre = tile.mapToGlobal(tile.rect().center())
    _FakeCursor.pos_value = QPoint(centre)
    ov._disarm_hover()  # as if the switcher rendered with the pointer here
    ov._poll_cursor()
    assert ov._index == 1, "stationary pointer must not select"
    _FakeCursor.pos_value = centre + QPoint(2, 1)  # nudge, same tile
    ov._poll_cursor()
    assert ov._index == 5
    assert not ov._hover_poll.isActive()
    ov.hide()


def test_move_outside_tiles_arms_without_selecting(qapp, monkeypatch):
    ov = _overlay(qapp, monkeypatch)
    _FakeCursor.pos_value = QPoint(-5000, -5000)
    ov._poll_cursor()
    assert ov._index == 1
    assert ov._hover_origin is None
    ov.hide()
