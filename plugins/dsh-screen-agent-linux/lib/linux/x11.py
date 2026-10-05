"""Minimal X11 / EWMH / XTest bindings through ctypes.

Zero third-party packages: libX11 and libXtst are already on any X11 desktop.

Naming contract for callers:
  * `get_*` / `list_*` / `find_*`  — read-only
  * `inject_*` / `focus_*`        — change desktop state (mouse, keyboard, focus)

Anything that injects is only ever called by the probe harness after an explicit
safety gate; this module itself holds no policy.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import time
from ctypes import POINTER, byref, c_char_p, c_int, c_long, c_ubyte, c_uint, c_ulong, c_void_p

# --------------------------------------------------------------------------
# Libraries
# --------------------------------------------------------------------------

_x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
_xtst = ctypes.CDLL(ctypes.util.find_library("Xtst") or "libXtst.so.6")

_x11.XOpenDisplay.restype = c_void_p
_x11.XOpenDisplay.argtypes = [c_char_p]
_x11.XCloseDisplay.argtypes = [c_void_p]
_x11.XDefaultRootWindow.restype = c_ulong
_x11.XDefaultRootWindow.argtypes = [c_void_p]
_x11.XDefaultScreen.argtypes = [c_void_p]
_x11.XFlush.argtypes = [c_void_p]
_x11.XSync.argtypes = [c_void_p, c_int]
_x11.XFree.argtypes = [c_void_p]

_x11.XInternAtom.restype = c_ulong
_x11.XInternAtom.argtypes = [c_void_p, c_char_p, c_int]

_x11.XGetWindowProperty.restype = c_int
_x11.XGetWindowProperty.argtypes = [
    c_void_p, c_ulong, c_ulong, c_long, c_long, c_int, c_ulong,
    POINTER(c_ulong), POINTER(c_int), POINTER(c_ulong), POINTER(c_ulong),
    POINTER(POINTER(c_ubyte)),
]

_x11.XQueryPointer.restype = c_int
_x11.XQueryPointer.argtypes = [
    c_void_p, c_ulong, POINTER(c_ulong), POINTER(c_ulong),
    POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_uint),
]

_x11.XGetGeometry.restype = c_int
_x11.XGetGeometry.argtypes = [
    c_void_p, c_ulong, POINTER(c_ulong), POINTER(c_int), POINTER(c_int),
    POINTER(c_uint), POINTER(c_uint), POINTER(c_uint), POINTER(c_uint),
]

_x11.XTranslateCoordinates.restype = c_int
_x11.XTranslateCoordinates.argtypes = [
    c_void_p, c_ulong, c_ulong, c_int, c_int,
    POINTER(c_int), POINTER(c_int), POINTER(c_ulong),
]

_x11.XQueryTree.restype = c_int
_x11.XQueryTree.argtypes = [
    c_void_p, c_ulong, POINTER(c_ulong), POINTER(c_ulong),
    POINTER(POINTER(c_ulong)), POINTER(c_uint),
]

_x11.XSendEvent.restype = c_int
_x11.XSendEvent.argtypes = [c_void_p, c_ulong, c_int, c_long, c_void_p]

_x11.XStringToKeysym.restype = c_ulong
_x11.XStringToKeysym.argtypes = [c_char_p]
_x11.XKeysymToKeycode.restype = c_ubyte
_x11.XKeysymToKeycode.argtypes = [c_void_p, c_ulong]
_x11.XKeysymToString.restype = c_char_p
_x11.XKeysymToString.argtypes = [c_ulong]
_x11.XKeycodeToKeysym.restype = c_ulong
_x11.XKeycodeToKeysym.argtypes = [c_void_p, c_ubyte, c_int]

_x11.XChangeKeyboardMapping.argtypes = [c_void_p, c_int, c_int, POINTER(c_ulong), c_int]

_xtst.XTestQueryExtension.restype = c_int
_xtst.XTestQueryExtension.argtypes = [
    c_void_p, POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_int),
]
_xtst.XTestFakeMotionEvent.restype = c_int
_xtst.XTestFakeMotionEvent.argtypes = [c_void_p, c_int, c_int, c_int, c_ulong]
_xtst.XTestFakeButtonEvent.restype = c_int
_xtst.XTestFakeButtonEvent.argtypes = [c_void_p, c_uint, c_int, c_ulong]
_xtst.XTestFakeKeyEvent.restype = c_int
_xtst.XTestFakeKeyEvent.argtypes = [c_void_p, c_uint, c_int, c_ulong]

# --------------------------------------------------------------------------
# X error handling
#
# Xlib's default error handler TERMINATES the process on any protocol error.
# On a live desktop that is fatal for anything that walks windows: a window that
# closes between enumeration and inspection -- or a WM that recycles an id --
# raises BadWindow and takes the whole tool down mid-call. Measured: X_QueryTree
# on a just-destroyed window killed the probe process outright.
#
# The handler is process-global, not per-connection, so it is installed once at
# import. The callback object must stay referenced for the life of the process:
# a garbage-collected libffi callback is a segfault.
# --------------------------------------------------------------------------


class _XErrorEvent(ctypes.Structure):
    _fields_ = [
        ("type", c_int),
        ("display", c_void_p),
        ("resourceid", c_ulong),
        ("serial", c_ulong),
        ("error_code", c_ubyte),
        ("request_code", c_ubyte),
        ("minor_code", c_ubyte),
    ]


_ERRORS: list[dict] = []


@ctypes.CFUNCTYPE(c_int, c_void_p, POINTER(_XErrorEvent))
def _on_x_error(_display, event):
    try:
        info = event.contents
        _ERRORS.append({
            "resource": hex(int(info.resourceid)),
            "errorCode": int(info.error_code),
            "requestCode": int(info.request_code),
        })
        del _ERRORS[:-64]        # diagnostics only; keep the tail
    except Exception:
        pass
    return 0                     # 0 = handled; Xlib does not abort


_x11.XSetErrorHandler.restype = c_void_p
_x11.XSetErrorHandler.argtypes = [c_void_p]
_x11.XSetErrorHandler(ctypes.cast(_on_x_error, c_void_p))


def recent_errors() -> list[dict]:
    """Protocol errors swallowed since start-up.

    They are ignored so one dead window cannot kill a call, but callers that
    care (a window that vanished mid-walk) can still see them.
    """
    return list(_ERRORS)


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------


class Display:
    """One X connection. Use as a context manager."""

    def __init__(self, name: str | None = None) -> None:
        self.name = name if name is not None else os.environ.get("DISPLAY")
        self._dpy = _x11.XOpenDisplay(self.name.encode() if self.name else None)
        if not self._dpy:
            raise RuntimeError(
                f"cannot open X display {self.name!r}; check DISPLAY and XAUTHORITY",
            )
        self.root = _x11.XDefaultRootWindow(self._dpy)
        self.screen = _x11.XDefaultScreen(self._dpy)
        self._atoms: dict[str, int] = {}

    def close(self) -> None:
        if self._dpy:
            _x11.XCloseDisplay(self._dpy)
            self._dpy = None

    def __enter__(self) -> "Display":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def flush(self) -> None:
        _x11.XFlush(self._dpy)

    def sync(self, discard: bool = False) -> None:
        _x11.XSync(self._dpy, 1 if discard else 0)

    def atom(self, name: str) -> int:
        cached = self._atoms.get(name)
        if cached is None:
            cached = int(_x11.XInternAtom(self._dpy, name.encode(), 0))
            self._atoms[name] = cached
        return cached

    # -- properties --------------------------------------------------------

    def get_property(self, window: int, name: str, req_type: int = 0):
        """Return (actual_type, format, values) or (0, 0, None)."""
        atom = self.atom(name)
        actual_type = c_ulong()
        actual_format = c_int()
        nitems = c_ulong()
        bytes_after = c_ulong()
        prop = POINTER(c_ubyte)()
        status = _x11.XGetWindowProperty(
            self._dpy, c_ulong(window), c_ulong(atom), 0, 1024, 0,
            c_ulong(req_type), byref(actual_type), byref(actual_format),
            byref(nitems), byref(bytes_after), byref(prop),
        )
        if status != 0 or not prop:
            return 0, 0, None
        try:
            if actual_format.value == 32:
                array = ctypes.cast(prop, POINTER(c_ulong * nitems.value)).contents
                values = [int(v) for v in array]
            elif actual_format.value == 8:
                raw = ctypes.string_at(prop, nitems.value)
                values = raw
            else:
                values = None
            return int(actual_type.value), int(actual_format.value), values
        finally:
            _x11.XFree(prop)

    def window_title(self, window: int) -> str:
        """_NET_WM_NAME (UTF-8) with a WM_NAME fallback."""
        _t, fmt, values = self.get_property(window, "_NET_WM_NAME")
        if fmt == 8 and values:
            return values.decode("utf-8", "replace")
        _t, fmt, values = self.get_property(window, "WM_NAME")
        if fmt == 8 and values:
            return values.decode("utf-8", "replace")
        if fmt == 32 and values:
            return ""  # WM_NAME as a compound-text list is not worth decoding
        return ""

    def window_class(self, window: int) -> tuple[str, str]:
        """WM_CLASS -> (instance, klass)."""
        _t, fmt, values = self.get_property(window, "WM_CLASS")
        if fmt != 8 or not values:
            return "", ""
        parts = values.split(b"\x00")
        instance = parts[0].decode("utf-8", "replace") if parts else ""
        klass = parts[1].decode("utf-8", "replace") if len(parts) > 1 else ""
        return instance, klass

    def window_pid(self, window: int) -> int | None:
        _t, fmt, values = self.get_property(window, "_NET_WM_PID")
        if fmt == 32 and values:
            return values[0]
        return None

    def window_type(self, window: int) -> list[str]:
        """_NET_WM_WINDOW_TYPE atoms, short names (e.g. 'NORMAL', 'DIALOG')."""
        _t, fmt, values = self.get_property(window, "_NET_WM_WINDOW_TYPE")
        if fmt != 32 or not values:
            return []
        out = []
        for value in values:
            name = _x11.XGetAtomName(self._dpy, c_ulong(value))
            if name:
                text = ctypes.string_at(name).decode("utf-8", "replace")
                _x11.XFree(name)
                out.append(text.split("_NET_WM_WINDOW_TYPE_")[-1])
        return out

    def window_states(self, window: int) -> list[str]:
        _t, fmt, values = self.get_property(window, "_NET_WM_STATE")
        if fmt != 32 or not values:
            return []
        out = []
        for value in values:
            name = _x11.XGetAtomName(self._dpy, c_ulong(value))
            if name:
                text = ctypes.string_at(name).decode("utf-8", "replace")
                _x11.XFree(name)
                out.append(text.split("_NET_WM_STATE_")[-1])
        return out

    # -- geometry / topology ----------------------------------------------

    def geometry(self, window: int):
        """(x, y, width, height) in root coordinates, or None."""
        root_ret = c_ulong()
        x = c_int()
        y = c_int()
        width = c_uint()
        height = c_uint()
        border = c_uint()
        depth = c_uint()
        if _x11.XGetGeometry(self._dpy, c_ulong(window), byref(root_ret), byref(x), byref(y),
                             byref(width), byref(height), byref(border), byref(depth)) == 0:
            return None
        dest_x = c_int()
        dest_y = c_int()
        child = c_ulong()
        if _x11.XTranslateCoordinates(self._dpy, c_ulong(window), c_ulong(self.root), 0, 0,
                                      byref(dest_x), byref(dest_y), byref(child)):
            return int(dest_x.value), int(dest_y.value), int(width.value), int(height.value)
        return int(x.value), int(y.value), int(width.value), int(height.value)

    def move_resize(self, window: int, x: int, y: int, width: int, height: int) -> None:
        """Reposition and/or resize a window.

        Not used by the tools themselves, but a window resize is the cheapest way
        to produce a genuinely large repaint when exercising change detection.
        """
        _x11.XMoveResizeWindow.argtypes = [c_void_p, c_ulong, c_int, c_int, c_uint, c_uint]
        _x11.XMoveResizeWindow(self._dpy, c_ulong(window), c_int(int(x)), c_int(int(y)),
                               c_uint(int(width)), c_uint(int(height)))
        self.flush()

    def children(self, window: int | None = None) -> list[int]:
        target = self.root if window is None else window
        root_ret = c_ulong()
        parent_ret = c_ulong()
        children = POINTER(c_ulong)()
        count = c_uint()
        if _x11.XQueryTree(self._dpy, c_ulong(target), byref(root_ret), byref(parent_ret),
                           byref(children), byref(count)) == 0:
            return []
        try:
            return [int(children[i]) for i in range(count.value)]
        finally:
            if children:
                _x11.XFree(children)

    def pointer(self):
        """(root_x, root_y, child_window_under_pointer)."""
        root_ret = c_ulong()
        child_ret = c_ulong()
        root_x = c_int()
        root_y = c_int()
        win_x = c_int()
        win_y = c_int()
        mask = c_uint()
        _x11.XQueryPointer(self._dpy, c_ulong(self.root), byref(root_ret), byref(child_ret),
                           byref(root_x), byref(root_y), byref(win_x), byref(win_y), byref(mask))
        return int(root_x.value), int(root_y.value), int(child_ret.value)

    def parent_of(self, window: int) -> int:
        root_ret = c_ulong()
        parent_ret = c_ulong()
        children = POINTER(c_ulong)()
        count = c_uint()
        if _x11.XQueryTree(self._dpy, c_ulong(window), byref(root_ret), byref(parent_ret),
                           byref(children), byref(count)) == 0:
            return 0
        if children:
            _x11.XFree(children)
        return int(parent_ret.value)

    def top_level_at(self, x: int, y: int) -> int:
        """The root's direct child containing (x, y): the frame, under a reparenting WM."""
        child = self.pointer_at(x, y)
        if child == 0:
            return 0
        return self.top_level_of(child)

    def client_at(self, x: int, y: int) -> int:
        """The MANAGED CLIENT window containing (x, y).

        This is NOT the window X reports under the pointer. A reparenting WM
        (Mutter and most others) decorates every client with its own frame
        window, so the window under the pointer is that frame -- an id that never
        appears in _NET_CLIENT_LIST. Anything comparing a hit point against a
        client id (a click-safety gate, "which application would receive this
        click") must resolve through here or every comparison fails. Measured: a
        click inside a window's own client area reported the frame's id instead,
        and the safety gate refused a legitimate click.
        """
        child = self.pointer_at(x, y)
        if child == 0:
            return 0
        clients = set(self.client_list())
        current = child
        for _ in range(16):
            if current in clients:
                return current
            parent = self.parent_of(current)
            if parent == 0 or parent == current or parent == self.root:
                break
            current = parent
        return self.top_level_of(child)

    def pointer_at(self, x: int, y: int) -> int:
        """The DEEPEST window containing the root-relative point (x, y).

        XTranslateCoordinates from root reports only root's DIRECT child -- the
        frame, under any reparenting WM -- so the descent has to be repeated one
        level at a time. Stopping at the first answer is exactly the bug that
        made a click inside a window report that window's frame instead of the
        window itself.
        """
        current = int(self.root)
        for _ in range(32):
            dest_x = c_int()
            dest_y = c_int()
            child = c_ulong()
            _x11.XTranslateCoordinates(self._dpy, c_ulong(self.root), c_ulong(current),
                                       c_int(x), c_int(y), byref(dest_x), byref(dest_y),
                                       byref(child))
            if child.value == 0:
                return current
            current = int(child.value)
        return current

    def top_level_of(self, window: int) -> int:
        """Climb to the highest ancestor that is a direct child of root."""
        current = window
        for _ in range(16):
            root_ret = c_ulong()
            parent_ret = c_ulong()
            children = POINTER(c_ulong)()
            count = c_uint()
            if _x11.XQueryTree(self._dpy, c_ulong(current), byref(root_ret), byref(parent_ret),
                               byref(children), byref(count)) == 0:
                return current
            if children:
                _x11.XFree(children)
            parent = int(parent_ret.value)
            if parent in (0, self.root) or parent == current:
                return current
            current = parent
        return current

    def active_window(self) -> int:
        _t, fmt, values = self.get_property(self.root, "_NET_ACTIVE_WINDOW")
        if fmt == 32 and values:
            return int(values[0])
        return 0

    def client_list(self) -> list[int]:
        """EWMH _NET_CLIENT_LIST: managed top-level windows in initial map order."""
        _t, fmt, values = self.get_property(self.root, "_NET_CLIENT_LIST")
        if fmt == 32 and values:
            return [int(v) for v in values]
        return []

    def client_list_stacking(self) -> list[int]:
        """EWMH _NET_CLIENT_LIST_STACKING: same windows in bottom-to-top order."""
        _t, fmt, values = self.get_property(self.root, "_NET_CLIENT_LIST_STACKING")
        if fmt == 32 and values:
            return [int(v) for v in values]
        return []

    # -- keys --------------------------------------------------------------

    def keysym(self, name: str) -> int:
        return int(_x11.XStringToKeysym(name.encode()))

    def keysym_name(self, keysym: int) -> str:
        value = _x11.XKeysymToString(c_ulong(keysym))
        return value.decode() if value else ""

    def keycode(self, keysym: int) -> int:
        return int(_x11.XKeysymToKeycode(self._dpy, c_ulong(keysym)))

    def map_keycode(self, keycode: int, keysym: int) -> None:
        """Point one keycode at one keysym (used to type characters X has no key for)."""
        table = (c_ulong * 1)(keysym)
        _x11.XChangeKeyboardMapping(self._dpy, c_int(keycode), c_int(1), table, c_int(1))
        self.sync()

    def free_keycode(self) -> int | None:
        """A keycode whose whole row is empty, or None when the table is full."""
        for keycode in range(8, 256):
            row = [_x11.XKeycodeToKeysym(self._dpy, c_ubyte(keycode), c_int(i)) for i in range(4)]
            if all(ks == 0 for ks in row):
                return keycode
        return None

    # -- input injection ---------------------------------------------------

    def x_test_available(self) -> bool:
        ev = c_int()
        er = c_int()
        major = c_int()
        minor = c_int()
        ok = _xtst.XTestQueryExtension(self._dpy, byref(ev), byref(er), byref(major), byref(minor))
        return bool(ok), (major.value, minor.value)

    def inject_motion(self, x: int, y: int, screen: int | None = None) -> None:
        _xtst.XTestFakeMotionEvent(self._dpy, c_int(self.screen if screen is None else screen),
                                   c_int(int(x)), c_int(int(y)), c_ulong(0))
        self.flush()

    def inject_button(self, button: int = 1, press: bool = True) -> None:
        _xtst.XTestFakeButtonEvent(self._dpy, c_uint(button), c_int(1 if press else 0), c_ulong(0))
        self.flush()

    def inject_key(self, keycode: int, press: bool = True) -> None:
        _xtst.XTestFakeKeyEvent(self._dpy, c_uint(keycode), c_int(1 if press else 0), c_ulong(0))
        self.flush()


# `XGetAtomName` lives on the same library; declare it after the class so the
# declarations stay in one block above.
_x11.XGetAtomName.restype = c_void_p
_x11.XGetAtomName.argtypes = [c_void_p, c_ulong]

# --------------------------------------------------------------------------
# EWMH focus
# --------------------------------------------------------------------------


class _ClientMessage(ctypes.Structure):
    _fields_ = [
        ("type", c_int),
        ("serial", c_ulong),
        ("send_event", c_int),
        ("display", c_void_p),
        ("window", c_ulong),
        ("message_type", c_ulong),
        ("format", c_int),
        ("data", c_long * 5),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [("type", c_int), ("xclient", _ClientMessage), ("pad", c_long * 24)]


def request_focus(display: Display, window: int, timeout: float = 3.0) -> dict:
    """Ask the WM to activate `window` via _NET_ACTIVE_WINDOW, then verify.

    The request is not synchronous: a WM is free to refuse (focus-stealing
    prevention) or to take a few frames. Callers must not assume success --
    returning `ok=False` is a real outcome, and injection must be skipped.
    """
    event = _XEvent()
    event.xclient.type = 33  # ClientMessage
    event.xclient.serial = 0
    event.xclient.send_event = 1
    event.xclient.display = display._dpy
    event.xclient.window = c_ulong(window)
    event.xclient.message_type = c_ulong(display.atom("_NET_ACTIVE_WINDOW"))
    event.xclient.format = 32
    event.xclient.data[0] = 2      # source indication: 2 = pager/tool
    event.xclient.data[1] = 0      # timestamp (0 = now)
    event.xclient.data[2] = 0      # requestor's currently active window

    mask = (1 << 20) | (1 << 19)   # SubstructureRedirectMask | SubstructureNotifyMask
    _x11.XSendEvent(display._dpy, c_ulong(display.root), c_int(0), c_long(mask),
                    ctypes.byref(event))
    display.flush()

    started = time.time()
    polls = 0
    while (time.time() - started) < timeout:
        polls += 1
        active = display.active_window()
        if active == window:
            return {"ok": True, "polls": polls, "elapsedMs": int((time.time() - started) * 1000)}
        # Some WMs activate the frame, not the client window; accept either.
        if active and display.top_level_of(active) == display.top_level_of(window):
            return {"ok": True, "polls": polls, "elapsedMs": int((time.time() - started) * 1000),
                    "viaFrame": True}
        time.sleep(0.05)
    return {"ok": False, "polls": polls, "elapsedMs": int((time.time() - started) * 1000),
            "activeNow": hex(display.active_window())}


def raise_window(display: Display, window: int) -> None:
    """XRaiseWindow, expressed as an EWMH-ish restack request through XConfigureWindow."""
    _x11.XRaiseWindow.argtypes = [c_void_p, c_ulong]
    _x11.XRaiseWindow(display._dpy, c_ulong(window))
    display.flush()


# --------------------------------------------------------------------------
# Convenience
# --------------------------------------------------------------------------


def describe_window(display: Display, window: int) -> dict:
    geometry = display.geometry(window) or (0, 0, 0, 0)
    instance, klass = display.window_class(window)
    return {
        "id": hex(window),
        "title": display.window_title(window),
        "instance": instance,
        "class": klass,
        "pid": display.window_pid(window),
        "types": display.window_type(window),
        "states": display.window_states(window),
        "x": geometry[0],
        "y": geometry[1],
        "width": geometry[2],
        "height": geometry[3],
    }


def list_windows(display: Display) -> list[dict]:
    """Every EWMH-managed top-level window, top of the stack first."""
    stacking = display.client_list_stacking()
    order = list(reversed(stacking)) if stacking else display.children()
    out = []
    for window in order:
        geometry = display.geometry(window)
        if geometry is None:
            continue
        width, height = geometry[2], geometry[3]
        if width <= 1 or height <= 1:      # unmapped bookkeeping windows
            continue
        info = describe_window(display, window)
        info["stackIndex"] = len(out)
        out.append(info)
    return out
