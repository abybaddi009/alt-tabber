"""ctypes binding for Carbon ``RegisterEventHotKey``.

Carbon hotkeys are delivered by the WindowServer hotkey system rather than
through event taps, so they keep working while Secure Event Input is on (a
focused password field or terminal password prompt), which filters
``keyDown`` out of every CGEventTap. They need no Accessibility permission.

PyObjC does not bridge this API, hence ctypes. Handlers run on the main
thread, dispatched by the main run loop (Qt's Cocoa event dispatcher pumps
it). A matched hotkey is consumed: the keystroke does not reach the
frontmost app.
"""

from __future__ import annotations

import ctypes
import struct
from typing import Callable

_carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode())[0]


_kEventClassKeyboard = _fourcc("keyb")
_kEventHotKeyPressed = 5
_kEventHotKeyReleased = 6
_kEventParamDirectObject = _fourcc("----")
_typeEventHotKeyID = _fourcc("hkid")
_SIGNATURE = _fourcc("ATbr")

# Carbon modifier masks (Events.h), distinct from CGEventFlags.
CMD_KEY = 0x0100
SHIFT_KEY = 0x0200
OPTION_KEY = 0x0800
CONTROL_KEY = 0x1000

# eventHotKeyExistsErr: this process already registered the same chord.
HOTKEY_EXISTS_ERR = -9878

_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

_carbon.GetEventDispatcherTarget.restype = ctypes.c_void_p
_carbon.GetEventDispatcherTarget.argtypes = []
_carbon.RegisterEventHotKey.argtypes = [
    ctypes.c_uint32,
    ctypes.c_uint32,
    _EventHotKeyID,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_void_p),
]
_carbon.RegisterEventHotKey.restype = ctypes.c_int32
_carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
_carbon.UnregisterEventHotKey.restype = ctypes.c_int32
_carbon.InstallEventHandler.argtypes = [
    ctypes.c_void_p,
    _HANDLER,
    ctypes.c_uint32,
    ctypes.POINTER(_EventTypeSpec),
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
]
_carbon.InstallEventHandler.restype = ctypes.c_int32
_carbon.RemoveEventHandler.argtypes = [ctypes.c_void_p]
_carbon.RemoveEventHandler.restype = ctypes.c_int32
_carbon.GetEventParameter.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.c_void_p,
    ctypes.c_void_p,
]
_carbon.GetEventParameter.restype = ctypes.c_int32
_carbon.GetEventKind.argtypes = [ctypes.c_void_p]
_carbon.GetEventKind.restype = ctypes.c_uint32


class CarbonHotkeys:
    """Registers hotkeys by integer id; reports presses (and releases) by id.

    Must be created and used on the main thread.
    """

    def __init__(
        self,
        on_press: Callable[[int], None],
        on_release: Callable[[int], None] | None = None,
    ) -> None:
        self._on_press = on_press
        self._on_release = on_release
        self._refs: dict[int, ctypes.c_void_p] = {}
        # Keep the ctypes callback referenced for the handler's lifetime, or
        # Carbon calls into freed memory.
        self._callback = _HANDLER(self._handle)
        specs = (_EventTypeSpec * 2)(
            _EventTypeSpec(_kEventClassKeyboard, _kEventHotKeyPressed),
            _EventTypeSpec(_kEventClassKeyboard, _kEventHotKeyReleased),
        )
        self._handler_ref = ctypes.c_void_p()
        status = _carbon.InstallEventHandler(
            _carbon.GetEventDispatcherTarget(),
            self._callback,
            2,
            specs,
            None,
            ctypes.byref(self._handler_ref),
        )
        if status != 0:
            raise OSError(f"InstallEventHandler failed: {status}")

    def _handle(self, _call, event, _user_data) -> int:
        try:
            hid = _EventHotKeyID()
            _carbon.GetEventParameter(
                event,
                _kEventParamDirectObject,
                _typeEventHotKeyID,
                None,
                ctypes.sizeof(hid),
                None,
                ctypes.byref(hid),
            )
            if hid.signature != _SIGNATURE:
                return 0
            if _carbon.GetEventKind(event) == _kEventHotKeyPressed:
                self._on_press(hid.id)
            elif self._on_release is not None:
                self._on_release(hid.id)
        except Exception:
            # Never let an exception unwind through the C callback.
            pass
        return 0  # noErr

    def register(self, ident: int, keycode: int, modifiers: int) -> int:
        """Register a hotkey; returns the Carbon status (0 on success)."""
        self.unregister(ident)
        ref = ctypes.c_void_p()
        status = _carbon.RegisterEventHotKey(
            keycode,
            modifiers,
            _EventHotKeyID(_SIGNATURE, ident),
            _carbon.GetEventDispatcherTarget(),
            0,
            ctypes.byref(ref),
        )
        if status == 0:
            self._refs[ident] = ref
        return status

    def unregister(self, ident: int) -> None:
        ref = self._refs.pop(ident, None)
        if ref is not None:
            _carbon.UnregisterEventHotKey(ref)

    def close(self) -> None:
        for ident in list(self._refs):
            self.unregister(ident)
        if self._handler_ref:
            _carbon.RemoveEventHandler(self._handler_ref)
            self._handler_ref = ctypes.c_void_p()
