"""macOS HotkeyProvider: Carbon hotkeys for chords, an event tap for modifiers.

Chords are registered with Carbon ``RegisterEventHotKey`` (see ``_carbon``),
not matched on ``keyDown`` in an event tap. When a password field or other
secure text field has focus, macOS Secure Event Input filters ``keyDown`` out
of every event tap, which used to make the switcher and palette hotkeys dead
there. Carbon hotkeys are unaffected, and ``flagsChanged`` events still reach
taps, so the switcher keeps its hold-to-cycle behavior. This is the same split
AltTab (lwouis/alt-tab-macos) uses.

Two registration modes:

* ``register(chord, callback)`` -- a *discrete* hotkey: the callback fires
  once per keypress of the chord (e.g. Super+A launcher).
* ``register_switcher(chord, handler)`` -- a *hold-to-cycle* hotkey
  (Alt+Tab style): ``on_trigger`` fires on the initial chord press, then
  ``on_cycle`` fires for each further press of the chord key or Tab while the
  modifier is held, ``on_commit`` fires when the modifier is released, and
  ``on_cancel`` on Esc.

During a switcher session, Tab and Esc with the session's modifiers are
registered as extra Carbon hotkeys, so cycling and cancelling also work under
secure input. Holding a key down to auto-repeat the cycle only works when
secure input is off: Carbon reports one press per physical key-down, and the
repeats come from the tap's ``keyDown`` events.

All state lives on the main thread. Carbon delivers its handlers there; the
listen-only tap runs on a background CFRunLoop and forwards modifier changes
and auto-repeats through Qt signals.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable

from PySide6.QtCore import QObject, Signal

from ...backends.base import HotkeyProvider, SwitcherHandler
from .chord import Hotkey, parse_chord, COMMAND, ALTERNATE, CONTROL, SHIFT

_TAB_KEYCODE = 48
_ESC_KEYCODE = 53

# CGEventFlags we treat as "known modifiers".
_KNOWN_MODS = COMMAND | ALTERNATE | CONTROL | SHIFT

# Carbon modifier masks (duplicated from ``_carbon`` so this module and its
# tests don't need to load Carbon to compute them).
_CARBON_MODS = {
    COMMAND: 0x0100,
    SHIFT: 0x0200,
    ALTERNATE: 0x0800,
    CONTROL: 0x1000,
}

# Event types macOS sends to a tap callback when it disables the tap.
_TAP_DISABLED_BY_TIMEOUT = 0xFFFFFFFE
_TAP_DISABLED_BY_USER_INPUT = 0xFFFFFFFF


_DEBUG = os.environ.get("ALTTABBER_DEBUG_HOTKEYS") == "1"


def _log(msg: str) -> None:
    if _DEBUG:
        print(f"[hotkeys {time.monotonic():.3f}] {msg}", file=sys.stderr, flush=True)


def _secure_input_enabled() -> bool:
    from .overlay import secure_input_enabled

    return secure_input_enabled()


def carbon_modifiers(cg_flags: int) -> int:
    """Convert CGEventFlags modifier bits to Carbon modifier bits."""
    out = 0
    for cg_bit, carbon_bit in _CARBON_MODS.items():
        if cg_flags & cg_bit:
            out |= carbon_bit
    return out


def _hid_modifier_flags() -> int | None:
    """Modifiers physically held right now, or None if unavailable."""
    try:
        from Quartz import (  # type: ignore
            CGEventSourceFlagsState,
            kCGEventSourceStateHIDSystemState,
        )

        return int(CGEventSourceFlagsState(kCGEventSourceStateHIDSystemState))
    except Exception:
        return None


class _TapBridge(QObject):
    """Marshals tap-thread events to the main thread."""

    flags_changed = Signal(int)  # CGEventFlags
    key_repeat = Signal(int, int)  # keycode, CGEventFlags


# Actions a Carbon hotkey id can map to.
_DISCRETE = "discrete"
_SWITCHER = "switcher"
_SESSION_CYCLE = "session_cycle"
_SESSION_CANCEL = "session_cancel"


class MacHotkeyProvider(HotkeyProvider):
    def __init__(
        self,
        carbon_factory: Callable[..., object] | None = None,
        modifier_state: Callable[[], int | None] = _hid_modifier_flags,
        use_tap: bool = True,
    ) -> None:
        self._carbon_factory = carbon_factory
        self._modifier_state = modifier_state
        self._use_tap = use_tap
        self._carbon = None

        self._callbacks: dict[str, Callable[[], None]] = {}
        self._switchers: dict[str, tuple[Hotkey, SwitcherHandler]] = {}

        # Carbon hotkey id -> (action, payload), and id -> (keycode, mods).
        self._next_id = 1
        self._actions: dict[int, tuple[str, object]] = {}
        self._keys: dict[int, tuple[int, int]] = {}
        self._chord_ids: dict[str, list[int]] = {}
        self._session_ids: list[int] = []

        # Active switcher session: (Hotkey, handler), or None.
        self._active: tuple[Hotkey, SwitcherHandler] | None = None

        self._bridge = _TapBridge()
        self._bridge.flags_changed.connect(self._on_flags_changed)
        self._bridge.key_repeat.connect(self._on_key_repeat)

        self._thread: threading.Thread | None = None
        self._rl = None
        self._port = None
        self._started = False

    # ---- registration ----

    def register(self, chord: str, callback) -> None:
        hk = parse_chord(chord)
        self.unregister(hk.chord)
        self._callbacks[hk.chord] = callback
        self._chord_ids[hk.chord] = [
            self._add_key(hk.keycode, hk.flags, (_DISCRETE, hk.chord))
        ]

    def register_switcher(self, chord: str, handler: SwitcherHandler) -> None:
        hk = parse_chord(chord)
        self.unregister(hk.chord)
        self._switchers[hk.chord] = (hk, handler)
        ids = [self._add_key(hk.keycode, hk.flags, (_SWITCHER, (hk.chord, False)))]
        if not hk.flags & SHIFT:
            # Carbon matches modifiers exactly, so Shift+chord (start on the
            # last entry) needs its own registration.
            ids.append(
                self._add_key(hk.keycode, hk.flags | SHIFT, (_SWITCHER, (hk.chord, True)))
            )
        self._chord_ids[hk.chord] = ids

    def unregister(self, chord: str) -> None:
        self._callbacks.pop(chord, None)
        self._switchers.pop(chord, None)
        for ident in self._chord_ids.pop(chord, []):
            self._remove_key(ident)
        if self._active is not None and self._active[0].chord == chord:
            self._end_session()

    def _add_key(self, keycode: int, cg_flags: int, action: tuple[str, object]) -> int:
        ident = self._next_id
        self._next_id += 1
        self._actions[ident] = action
        self._keys[ident] = (keycode, carbon_modifiers(cg_flags))
        if self._carbon is not None:
            status = self._carbon.register(ident, *self._keys[ident])
            _log(f"register id={ident} key={self._keys[ident]} {action} status={status}")
        return ident

    def _remove_key(self, ident: int) -> None:
        self._actions.pop(ident, None)
        self._keys.pop(ident, None)
        if self._carbon is not None:
            self._carbon.unregister(ident)

    # ---- lifecycle ----

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self._carbon is None:
            factory = self._carbon_factory
            if factory is None:
                from ._carbon import CarbonHotkeys

                factory = CarbonHotkeys
            try:
                self._carbon = factory(self._on_carbon_press)
            except Exception as e:
                _log(f"carbon init failed: {e!r}")
                self._carbon = None
        if self._carbon is not None:
            for ident, (keycode, mods) in self._keys.items():
                status = self._carbon.register(ident, keycode, mods)
                _log(f"register id={ident} key={(keycode, mods)} {self._actions[ident]} status={status}")
        if self._use_tap:
            self._thread = threading.Thread(target=self._run_tap, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._end_session()
        if self._carbon is not None:
            try:
                self._carbon.close()
            except Exception:
                pass
            self._carbon = None
        try:
            from CoreFoundation import CFRunLoopStop  # type: ignore

            if self._rl is not None:
                CFRunLoopStop(self._rl)
        except Exception:
            pass

    # ---- main-thread event handling ----

    def _on_carbon_press(self, ident: int) -> None:
        action = self._actions.get(ident)
        _log(f"carbon press id={ident} {action} active={self._active is not None}")
        if action is None:
            return
        kind, payload = action
        if kind == _DISCRETE:
            # A discrete chord (e.g. the launcher) during a switcher session
            # takes over; the app hides the switcher overlay itself.
            self._end_session()
            self._call(self._callbacks.get(payload))
        elif kind == _SWITCHER:
            chord, reverse = payload
            if self._active is not None:
                self._call(self._active[1].on_cycle, reverse)
            elif chord in self._switchers:
                self._begin_session(self._switchers[chord], reverse)
        elif kind == _SESSION_CYCLE and self._active is not None:
            self._call(self._active[1].on_cycle, payload)
        elif kind == _SESSION_CANCEL and self._active is not None:
            handler = self._active[1]
            self._end_session()
            self._call(handler.on_cancel)

    def _begin_session(self, binding: tuple[Hotkey, SwitcherHandler], reverse: bool) -> None:
        hk, handler = binding
        self._active = binding
        base = hk.flags & ~SHIFT
        if hk.keycode != _TAB_KEYCODE:
            self._session_ids.append(self._add_key(_TAB_KEYCODE, base, (_SESSION_CYCLE, False)))
            self._session_ids.append(
                self._add_key(_TAB_KEYCODE, base | SHIFT, (_SESSION_CYCLE, True))
            )
        self._session_ids.append(self._add_key(_ESC_KEYCODE, base, (_SESSION_CANCEL, None)))
        self._session_ids.append(
            self._add_key(_ESC_KEYCODE, base | SHIFT, (_SESSION_CANCEL, None))
        )
        self._call(handler.on_trigger, reverse)
        # The modifier release may already have happened (a quick tap, or a
        # Carbon event delivered late behind main-thread work). The tap's
        # flagsChanged for it would then have arrived while no session was
        # active, so check the live state rather than waiting forever.
        state = self._modifier_state()
        _log(f"trigger {hk.chord} reverse={reverse} hid_flags={state if state is None else hex(state)}")
        if self._active is binding and not self._modifiers_held(hk, state):
            _log("modifiers already released -> commit")
            self._commit()

    def _end_session(self) -> None:
        self._active = None
        for ident in self._session_ids:
            self._remove_key(ident)
        self._session_ids = []

    def _commit(self) -> None:
        if self._active is None:
            return
        handler = self._active[1]
        self._end_session()
        self._call(handler.on_commit)

    @staticmethod
    def _modifiers_held(hk: Hotkey, flags: int | None) -> bool:
        if flags is None:
            return True  # unknown: wait for the tap
        required = hk.flags & ~SHIFT
        return flags & required == required

    def _on_flags_changed(self, flags: int) -> None:
        if _DEBUG:
            _log(f"flags {hex(flags)} active={self._active is not None} secure_input={_secure_input_enabled()}")
        if self._active is not None and not self._modifiers_held(self._active[0], flags):
            self._commit()

    def _on_key_repeat(self, keycode: int, flags: int) -> None:
        if self._active is None:
            return
        hk = self._active[0]
        if keycode in (hk.keycode, _TAB_KEYCODE) and self._modifiers_held(hk, flags):
            self._call(self._active[1].on_cycle, bool(flags & SHIFT))

    @staticmethod
    def _call(fn, *args) -> None:
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as e:
            _log(f"handler {getattr(fn, '__qualname__', fn)} raised {e!r}")

    # ---- listen-only event tap (background thread) ----

    def _run_tap(self) -> None:
        try:
            from CoreFoundation import (  # type: ignore
                CFMachPortCreateRunLoopSource,
                CFRunLoopGetCurrent,
                CFRunLoopAddSource,
                kCFRunLoopCommonModes,
                CFRunLoopRun,
            )
            from Quartz import (  # type: ignore
                CGEventGetFlags,
                CGEventGetIntegerValueField,
                CGEventMaskBit,
                CGEventTapCreate,
                CGEventTapEnable,
                kCGEventFlagsChanged,
                kCGEventKeyDown,
                kCGEventTapOptionListenOnly,
                kCGHeadInsertEventTap,
                kCGKeyboardEventAutorepeat,
                kCGKeyboardEventKeycode,
                kCGSessionEventTap,
            )
        except ImportError:
            return

        bridge = self._bridge

        def callback(proxy, event_type, event, refcon):
            try:
                if event_type in (_TAP_DISABLED_BY_TIMEOUT, _TAP_DISABLED_BY_USER_INPUT):
                    _log(f"tap disabled ({hex(event_type)}), re-enabling")
                    if self._port is not None:
                        CGEventTapEnable(self._port, True)
                    return event
                if event_type == kCGEventFlagsChanged:
                    bridge.flags_changed.emit(int(CGEventGetFlags(event)))
                elif event_type == kCGEventKeyDown:
                    if _DEBUG and CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode) == _TAB_KEYCODE:
                        _log("tap saw Tab keyDown")
                    if CGEventGetIntegerValueField(event, kCGKeyboardEventAutorepeat):
                        bridge.key_repeat.emit(
                            int(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)),
                            int(CGEventGetFlags(event)),
                        )
            except Exception:
                pass
            return event

        self._port = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            kCGEventTapOptionListenOnly,
            CGEventMaskBit(kCGEventFlagsChanged) | CGEventMaskBit(kCGEventKeyDown),
            callback,
            None,
        )
        _log(f"tap created={self._port is not None}")
        if self._port is None:
            # Missing Input Monitoring / Accessibility permission. Chords still
            # fire via Carbon, but switcher commit-on-release can't work.
            return
        self._rl = CFRunLoopGetCurrent()
        source = CFMachPortCreateRunLoopSource(None, self._port, 0)
        CFRunLoopAddSource(self._rl, source, kCFRunLoopCommonModes)
        CFRunLoopRun()
