"""MacHotkeyProvider state machine, driven through a fake Carbon backend.

The real backend is Carbon ``RegisterEventHotKey`` (works under Secure Event
Input); the fake records registrations and lets tests fire presses by id.
"""

from __future__ import annotations

import pytest

from alttabber.platform.macos.chord import ALTERNATE, SHIFT
from alttabber.platform.macos.hotkeys import MacHotkeyProvider, carbon_modifiers

OPTION = 0x0800
SHIFT_KEY = 0x0200
TAB, BACKTICK, ESC, A = 48, 50, 53, 0


class FakeCarbon:
    def __init__(self, on_press):
        self.on_press = on_press
        self.keys: dict[int, tuple[int, int]] = {}
        self.closed = False

    def register(self, ident, keycode, mods):
        self.keys[ident] = (keycode, mods)
        return 0

    def unregister(self, ident):
        self.keys.pop(ident, None)

    def close(self):
        self.keys.clear()
        self.closed = True

    def press(self, keycode, mods):
        matches = [i for i, k in self.keys.items() if k == (keycode, mods)]
        assert matches, f"no hotkey registered for {(keycode, mods)}"
        self.on_press(matches[0])


class RecordingHandler:
    def __init__(self):
        self.events: list[tuple] = []

    def on_trigger(self, reverse=False):
        self.events.append(("trigger", reverse))

    def on_cycle(self, reverse):
        self.events.append(("cycle", reverse))

    def on_commit(self):
        self.events.append(("commit",))

    def on_cancel(self):
        self.events.append(("cancel",))


@pytest.fixture
def env(qapp):
    held = {"flags": ALTERNATE}
    carbons: list[FakeCarbon] = []

    def factory(on_press):
        c = FakeCarbon(on_press)
        carbons.append(c)
        return c

    provider = MacHotkeyProvider(
        carbon_factory=factory, modifier_state=lambda: held["flags"], use_tap=False
    )
    yield provider, carbons, held
    provider.stop()


def test_carbon_modifier_conversion():
    assert carbon_modifiers(ALTERNATE) == OPTION
    assert carbon_modifiers(ALTERNATE | SHIFT) == OPTION | SHIFT_KEY


def test_registrations_made_before_start_are_applied_on_start(env):
    provider, carbons, _ = env
    provider.register_switcher("Alt+Tab", RecordingHandler())
    provider.register("Super+A", lambda: None)
    provider.start()
    keys = set(carbons[0].keys.values())
    assert keys == {(TAB, OPTION), (TAB, OPTION | SHIFT_KEY), (A, 0x0100)}


def test_hold_cycle_release(env):
    provider, carbons, held = env
    h = RecordingHandler()
    provider.register_switcher("Alt+Tab", h)
    provider.start()
    c = carbons[0]

    c.press(TAB, OPTION)
    c.press(TAB, OPTION)
    c.press(TAB, OPTION | SHIFT_KEY)
    provider._on_flags_changed(ALTERNATE | SHIFT)  # Shift released only
    assert h.events == [("trigger", False), ("cycle", False), ("cycle", True)]

    held["flags"] = 0
    provider._on_flags_changed(0)
    assert h.events[-1] == ("commit",)
    # Session-only keys (Esc) are gone again.
    assert (ESC, OPTION) not in c.keys.values()


def test_shift_chord_starts_reversed(env):
    provider, carbons, _ = env
    h = RecordingHandler()
    provider.register_switcher("Alt+Tab", h)
    provider.start()
    carbons[0].press(TAB, OPTION | SHIFT_KEY)
    assert h.events == [("trigger", True)]


def test_release_before_press_is_delivered_commits_immediately(env):
    provider, carbons, held = env
    h = RecordingHandler()
    provider.register_switcher("Alt+Tab", h)
    provider.start()
    held["flags"] = 0  # Option already up when Carbon delivers the press
    carbons[0].press(TAB, OPTION)
    assert h.events == [("trigger", False), ("commit",)]
    assert provider._active is None


def test_esc_cancels_session(env):
    provider, carbons, _ = env
    h = RecordingHandler()
    provider.register_switcher("Alt+Tab", h)
    provider.start()
    c = carbons[0]
    c.press(TAB, OPTION)
    c.press(ESC, OPTION)
    assert h.events == [("trigger", False), ("cancel",)]
    provider._on_flags_changed(0)  # later release must not commit
    assert h.events[-1] == ("cancel",)


def test_tab_cycles_a_backtick_session(env):
    provider, carbons, _ = env
    h = RecordingHandler()
    provider.register_switcher("Alt+`", h)
    provider.start()
    c = carbons[0]
    assert (TAB, OPTION) not in c.keys.values()  # not grabbed outside a session
    c.press(BACKTICK, OPTION)
    c.press(TAB, OPTION)
    c.press(TAB, OPTION | SHIFT_KEY)
    c.press(BACKTICK, OPTION)
    assert h.events == [
        ("trigger", False),
        ("cycle", False),
        ("cycle", True),
        ("cycle", False),
    ]
    provider._on_flags_changed(0)
    assert (TAB, OPTION) not in c.keys.values()


def test_other_switcher_chord_cycles_the_active_session(env):
    provider, carbons, _ = env
    apps, wins = RecordingHandler(), RecordingHandler()
    provider.register_switcher("Alt+Tab", apps)
    provider.register_switcher("Alt+`", wins)
    provider.start()
    c = carbons[0]
    c.press(BACKTICK, OPTION)
    c.press(TAB, OPTION)
    assert wins.events == [("trigger", False), ("cycle", False)]
    assert apps.events == []


def test_discrete_chord_fires_and_ends_switcher_session(env):
    provider, carbons, _ = env
    fired = []
    h = RecordingHandler()
    provider.register("Alt+A", lambda: fired.append(1))
    provider.register_switcher("Alt+Tab", h)
    provider.start()
    c = carbons[0]
    c.press(A, OPTION)
    assert fired == [1]
    c.press(TAB, OPTION)
    c.press(A, OPTION)
    assert fired == [1, 1]
    provider._on_flags_changed(0)
    assert h.events == [("trigger", False)]  # no commit after takeover


def test_key_repeat_cycles_only_during_session(env):
    provider, carbons, _ = env
    h = RecordingHandler()
    provider.register_switcher("Alt+Tab", h)
    provider.start()
    provider._on_key_repeat(TAB, ALTERNATE)
    assert h.events == []
    carbons[0].press(TAB, OPTION)
    provider._on_key_repeat(TAB, ALTERNATE)
    provider._on_key_repeat(TAB, ALTERNATE | SHIFT)
    provider._on_key_repeat(A, ALTERNATE)
    assert h.events == [("trigger", False), ("cycle", False), ("cycle", True)]


def test_unregister_and_rebind(env):
    provider, carbons, _ = env
    h = RecordingHandler()
    provider.register_switcher("Alt+Tab", h)
    provider.start()
    c = carbons[0]
    c.press(TAB, OPTION)
    provider.unregister("Alt+Tab")
    assert c.keys == {}
    assert provider._active is None
    provider.register_switcher("Alt+`", h)
    assert set(c.keys.values()) == {(BACKTICK, OPTION), (BACKTICK, OPTION | SHIFT_KEY)}


def test_stop_releases_carbon(env):
    provider, carbons, _ = env
    provider.register("Super+A", lambda: None)
    provider.start()
    provider.stop()
    assert carbons[0].closed
