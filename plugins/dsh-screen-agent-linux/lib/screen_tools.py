#!/usr/bin/env python3
"""dsh-screen-agent-linux sidecar: one JSON request in, one JSON response out.

Protocol: a single JSON object on stdin, a single JSON object on stdout.
Every failure becomes `{"ok": false, "error": "..."}` with exit code 0, so the
caller never has to interpret a stack trace.

Actions
  capture   full-desktop screenshot, returned inline as base64 PNG   (read-only)
  zoom      crop a region, from a NATIVE-resolution grab             (read-only)
  windows   list managed top-level windows in stacking order         (read-only)
  wait      wait for the screen to change, or to stop changing       (read-only)
  move      move the pointer, pressing nothing
  click     press a mouse button, optionally moving there first
  key       send key combinations to the focused window
  type      send key combinations and/or text

Coordinates are normalized (0..1) everywhere: fractions survive every image
resize, pixels do not.

Linux facts that shaped this file (all measured; see tools/probe/REPORT.md):

  * Xlib's default error handler TERMINATES the process on any protocol error --
    one window closing mid-walk would kill the call. `linux/x11.py` installs its
    own handler before the first request.
  * A reparenting WM (Mutter) wraps every client in a frame window that never
    appears in _NET_CLIENT_LIST, so hit-testing descends level by level and then
    matches the client list (`Display.client_at`).
  * Non-ASCII text CANNOT be typed with synthesized key events: the events
    arrive with the correct keysym and the widget still inserts nothing, and
    disabling the input method changes nothing. Text goes through the toolkit's
    own EditableText interface instead, where `length` counts BYTES.
  * X11 has no PrintWindow equivalent, so a covered window is simply not in the
    screenshot; callers must raise it first.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    from linux import keymap  # noqa: E402
    from linux import x11  # noqa: E402
    X11_IMPORT_ERROR = None
except Exception as exc:  # a missing libX11 must be a readable error, not a traceback
    x11 = None
    keymap = None
    X11_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

# Mirrors the host half. The provider's own per-image visual budget is ~800x800.
IMAGE_PIXEL_BUDGET = 640_000
# A crop smaller than this is upscaled into a blur; refuse instead of pretending.
MIN_REGION_PX = 8

# Screen-stability fingerprint: 64x40 cells sampled as RGB.
FINGERPRINT_SIZE = (64, 40)
# One channel must move by more than this for a cell to count as changed.
NOISE_FLOOR = 12
# Fraction of cells that must move for the screen to count as changed.
CHANGE_THRESHOLD = 0.02

MAX_TEXT_CHARS = 20_000
MAX_CLICKS = 10
MAX_HOLD_MS = 5_000

BUTTONS = {"left": 1, "middle": 2, "right": 3}


# --------------------------------------------------------------------------
# Protected windows — "do not act on the thing that is driving you"
#
# The agent is driven from a browser window on this very desktop, so clicking
# into it can cut the session that issued the click. The reference
# implementation learned this the hard way and so does this one: the guard runs
# before any real input, on both the hit point and the focused window.
# --------------------------------------------------------------------------

DEFAULT_PROTECT_MARKER = "DeepSeek Harness"


def protect_markers() -> list[str]:
    raw = os.environ.get("DSH_SCREEN_AGENT_PROTECT")
    if raw is None:
        return [DEFAULT_PROTECT_MARKER.lower()]
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def is_protected_title(title: str) -> bool:
    text = (title or "").lower()
    if not text:
        return False
    return any(marker in text for marker in protect_markers())


def window_label(display, window: int) -> str:
    if not window:
        return "(none)"
    return f"{display.window_title(window) or '(no title)'} [{hex(window)}]"


def assert_window_allowed(display, window: int) -> None:
    """Refuse to act on a window whose title matches a protected marker."""
    if not window:
        return
    title = display.window_title(window)
    if is_protected_title(title):
        raise PermissionError(
            f"refusing to act on {window_label(display, window)!r}: it matches a protected "
            f"marker ({', '.join(protect_markers())}). This agent is driven from a window on "
            f"this desktop, so acting on it can end the session. Override with "
            f"DSH_SCREEN_AGENT_PROTECT if that is really intended.",
        )


def assert_point_allowed(display, x: int, y: int) -> int:
    """Check the point AFTER any move and BEFORE the press.

    Moving the pointer can change what is under it (a hover popup, a raise), so
    a check made before the move does not cover the moment that matters.
    """
    window = display.client_at(x, y)
    assert_window_allowed(display, window)
    return window


def assert_focus_allowed(display) -> int:
    """Key events go to the focused window; that window must be an allowed one."""
    window = display.active_window()
    assert_window_allowed(display, window)
    return window


# --------------------------------------------------------------------------
# Audit log
#
# Every action that touches the desktop leaves a line. The log records what was
# aimed at, not the pixels, so it stays a record of intent rather than a second
# copy of whatever was on screen.
# --------------------------------------------------------------------------


def audit(record: dict) -> None:
    try:
        base = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
        directory = os.path.join(base, "screen-agent")
        os.makedirs(directory, exist_ok=True)
        entry = {"ts": round(time.time(), 3), **record}
        with open(os.path.join(directory, "audit.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # auditing must never break the action it is recording


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


def resample_filter():
    from PIL import Image

    resampling = getattr(Image, "Resampling", None)
    return getattr(resampling, "LANCZOS", None) or getattr(Image, "LANCZOS", None) or Image.BILINEAR


def fit_within(image, max_pixels):
    """Downscale to the pixel budget. Returns (image, scale); 1.0 means untouched."""
    budget = IMAGE_PIXEL_BUDGET if max_pixels is None else int(max_pixels)
    width, height = image.size
    if budget <= 0 or width * height <= budget:
        return image, 1.0
    scale = (budget / float(width * height)) ** 0.5
    # Truncate, do not round: rounding both sides up can push the product back
    # over the budget (measured: 1386x797 -> 1055x607 = 640,385 > 640,000).
    resized = image.resize(
        (max(1, int(width * scale)), max(1, int(height * scale))),
        resample_filter(),
    )
    return resized, scale


def emit_image(image, req, result):
    """Attach the PNG to `result` inline (base64) or on disk when asked."""
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    if req.get("inline"):
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        payload = buffer.getvalue()
        result["bytes"] = len(payload)
        result["pngBase64"] = base64.b64encode(payload).decode("ascii")
        return result

    out = req.get("out")
    if not out:
        raise ValueError("out is required when inline is not set")
    image.save(out, "PNG")
    result["out"] = out
    result["bytes"] = None
    return result


def grab_desktop():
    """The whole X virtual desktop as an RGB image."""
    from PIL import ImageGrab

    try:
        # No all_screens argument: that is a Windows-only knob. On X11 the grab
        # already covers the root window, which spans every monitor.
        image = ImageGrab.grab()
    except Exception as exc:
        raise RuntimeError(f"screen capture failed: {exc}") from exc
    return image if image.mode == "RGB" else image.convert("RGB")


# --------------------------------------------------------------------------
# Parameter validation
#
# Values are validated before anything is captured or pressed, so a malformed
# request cannot produce a partial side effect. The error strings are written
# for the model to read and act on, not for a log.
# --------------------------------------------------------------------------


def _number(req, key):
    value = req.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number between 0 and 1 (got {value!r})")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{key} must be a finite number (got {value!r})")
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{key} must be between 0 and 1 (got {value})")
    return number


def _pair(req):
    """Both halves or neither.

    A half-supplied coordinate must never degrade into "press where the cursor
    happens to be" -- that is firing at a place nobody named.
    """
    has_x = req.get("nx") is not None
    has_y = req.get("ny") is not None
    if has_x != has_y:
        given, missing = ("nx", "ny") if has_x else ("ny", "nx")
        raise ValueError(
            f"{given} was given without {missing}; supply both to aim at a point, "
            f"or neither to act where the cursor already is",
        )
    if not has_x:
        return None
    return _number(req, "nx"), _number(req, "ny")


def _integer(req, key, default, low, high):
    value = req.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer (got {value!r})")
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high} (got {value})")
    return value


def normalize_region(req, width, height):
    """Validate nx0/ny0/nx1/ny1 and turn them into a pixel box."""
    nx0 = _number(req, "nx0")
    ny0 = _number(req, "ny0")
    nx1 = _number(req, "nx1")
    ny1 = _number(req, "ny1")
    if nx0 >= nx1:
        raise ValueError(f"nx0 must be smaller than nx1 (got {nx0} and {nx1})")
    if ny0 >= ny1:
        raise ValueError(f"ny0 must be smaller than ny1 (got {ny0} and {ny1})")

    left = max(0, min(width - 1, int(round(nx0 * width))))
    top = max(0, min(height - 1, int(round(ny0 * height))))
    right = max(left + 1, min(width, int(round(nx1 * width))))
    bottom = max(top + 1, min(height, int(round(ny1 * height))))
    return (left, top, right, bottom)


def desktop_size(display):
    geometry = display.geometry(display.root)
    if geometry is None:
        raise RuntimeError("cannot read the root window geometry")
    return geometry[2], geometry[3]


def to_pixels(nx, ny, width, height):
    """Normalized fraction -> integer pixel inside the desktop."""
    return (
        max(0, min(width - 1, int(round(nx * (width - 1))))),
        max(0, min(height - 1, int(round(ny * (height - 1))))),
    )


# --------------------------------------------------------------------------
# Synthesized input
# --------------------------------------------------------------------------


def press_combo(display, combo: str) -> str:
    """Press one combination: modifiers down, key down/up, modifiers up."""
    modifiers, key_name = keymap.parse_combo(combo)

    key_keysym = display.keysym(key_name)
    if key_keysym == 0:
        key_keysym = display.keysym(key_name.capitalize())
    if key_keysym == 0:
        raise ValueError(f"unknown key {key_name!r} in {combo!r}; {keymap.USAGE}")
    key_code = display.keycode(key_keysym)
    if key_code == 0:
        raise ValueError(
            f"the key {key_name!r} in {combo!r} is not on the current keyboard layout",
        )

    modifier_codes = []
    for name in modifiers:
        code = display.keycode(display.keysym(name))
        if code == 0:
            raise ValueError(f"modifier {name!r} is not on the current keyboard layout")
        modifier_codes.append(code)

    for code in modifier_codes:
        display.inject_key(code, True)
    display.inject_key(key_code, True)
    display.inject_key(key_code, False)
    for code in reversed(modifier_codes):
        display.inject_key(code, False)
    display.sync()
    return combo


def type_ascii(display, text: str) -> int:
    """Type characters that exist on the current layout, one key event each."""
    typed = 0
    shift_code = display.keycode(display.keysym("Shift_L"))
    for char in text:
        # Every printable Latin-1 character has a keysym equal to its own code
        # point, but XStringToKeysym only understands NAMES ("space", "exclam").
        # Asking it for a literal " " or "!" returns 0 -- which made spaces and
        # every symbol untypeable.
        code_point = ord(char)
        keysym = code_point if 0x20 <= code_point <= 0x7E else display.keysym(char)
        if keysym == 0:
            raise ValueError(f"{char!r} is not typeable through the keyboard")
        code = display.keycode(keysym)
        if code == 0:
            raise ValueError(f"{char!r} is not on the current keyboard layout")
        need_shift = False
        from ctypes import c_int, c_ubyte

        for level in range(4):
            current = int(x11._x11.XKeycodeToKeysym(display._dpy, c_ubyte(code), c_int(level)))
            if current == keysym:
                need_shift = level in (1, 3)
                break
        if need_shift:
            display.inject_key(shift_code, True)
        display.inject_key(code, True)
        display.inject_key(code, False)
        if need_shift:
            display.inject_key(shift_code, False)
        display.sync()
        time.sleep(0.012)
        typed += 1
    return typed


def type_text(display, text: str) -> dict:
    """Route text to whichever mechanism can actually deliver it.

    ASCII goes through the keyboard. Anything else goes through the toolkit's
    EditableText interface, because synthesized key events cannot deliver
    non-ASCII at all on X11 -- measured, with the input method ruled out -- and
    a silent partial delivery would be worse than an explicit failure.
    """
    if text == "":
        return {"method": "none", "characters": 0}

    if text.isascii():
        return {"method": "xtest", "characters": type_ascii(display, text)}

    try:
        from linux import atspi
        atspi.init()
    except Exception as exc:
        raise RuntimeError(
            f"non-ASCII text needs the AT-SPI text interface, which is unavailable "
            f"({type(exc).__name__}: {exc}); ASCII text still works through the keyboard",
        ) from exc

    outcome = atspi.insert_text_into_focus(text)
    if not outcome.get("ok"):
        raise RuntimeError(
            f"non-ASCII text could not be delivered: {outcome.get('error')}. "
            f"Synthesized key events cannot type {text!r} on X11, so the text has to go "
            f"through an element that exposes EditableText; make sure the target text "
            f"field has keyboard focus first",
        )
    return {
        "method": "atspi-editable",
        "characters": len(text),
        "target": f"{outcome.get('application')}:{outcome.get('role')}",
    }


# --------------------------------------------------------------------------
# Read-only actions
# --------------------------------------------------------------------------


def do_capture(req):
    """Screenshot the whole desktop."""
    started = time.time()
    image = grab_desktop()
    desktop_width, desktop_height = image.size
    scaled, scale = fit_within(image, req.get("maxPixels"))
    result = {
        "ok": True,
        "desktopWidth": desktop_width,
        "desktopHeight": desktop_height,
        "imageWidth": scaled.size[0],
        "imageHeight": scaled.size[1],
        "scale": round(scale, 6),
        "lossless": scale == 1.0,
        "elapsedMs": int((time.time() - started) * 1000),
    }
    return emit_image(scaled, req, result)


def do_zoom(req):
    """Crop a normalized region out of a native-resolution grab."""
    started = time.time()
    image = grab_desktop()
    desktop_width, desktop_height = image.size
    box = normalize_region(req, desktop_width, desktop_height)
    crop = image.crop(box)
    if crop.size[0] < MIN_REGION_PX or crop.size[1] < MIN_REGION_PX:
        raise ValueError(
            f"the requested region is only {crop.size[0]}x{crop.size[1]} px; "
            f"at least {MIN_REGION_PX}x{MIN_REGION_PX} is required, because anything "
            f"smaller is upscaled into a blur rather than showing more detail",
        )
    scaled, scale = fit_within(crop, req.get("maxPixels"))
    result = {
        "ok": True,
        "desktopWidth": desktop_width,
        "desktopHeight": desktop_height,
        "nx0": _number(req, "nx0"),
        "ny0": _number(req, "ny0"),
        "nx1": _number(req, "nx1"),
        "ny1": _number(req, "ny1"),
        "cropWidth": crop.size[0],
        "cropHeight": crop.size[1],
        "imageWidth": scaled.size[0],
        "imageHeight": scaled.size[1],
        "lossless": scale == 1.0,
        "scale": round(scale, 6),
        "elapsedMs": int((time.time() - started) * 1000),
    }
    return emit_image(scaled, req, result)


def do_windows(req):
    """List managed top-level windows, top of the stack first."""
    with x11.Display() as display:
        active = display.active_window()
        windows = x11.list_windows(display)
        desktop_width, desktop_height = desktop_size(display)

    if not windows:
        return {"ok": True, "count": 0, "desktopWidth": desktop_width,
                "desktopHeight": desktop_height, "lines": ["(no visible top-level windows)"]}

    lines = []
    for index, window in enumerate(windows):
        flags = []
        if window["id"] == hex(active):
            flags.append("FOREGROUND")
        if "HIDDEN" in (window.get("states") or []):
            flags.append("minimized")
        if is_protected_title(window["title"]):
            flags.append("PROTECTED")
        suffix = f' [{" ".join(flags)}]' if flags else ""
        title = window["title"] or "(no title)"
        if len(title) > 90:
            title = title[:87] + "..."
        lines.append(
            f'[{index}] "{title}" {window["width"]}x{window["height"]} '
            f'at ({window["x"]},{window["y"]}) class={window["class"] or "?"} '
            f'pid={window["pid"]}{suffix}',
        )

    return {
        "ok": True,
        "count": len(windows),
        "desktopWidth": desktop_width,
        "desktopHeight": desktop_height,
        "lines": lines,
        "windows": windows,
    }


def _fingerprint():
    """64x40 RGB digest of the whole desktop.

    RGB, not greyscale: a greyscale comparison with a noise floor called a real
    repaint "no change" when the two colours happened to share a luminance
    (measured: two backgrounds differing by 76% of their pixels read as 0.02%).
    Greyscale is fine for tolerating ambient motion, not for judging change.
    """
    from PIL import ImageGrab

    image = ImageGrab.grab().convert("RGB").resize(FINGERPRINT_SIZE)
    return image.tobytes()


def _fingerprint_delta(before: bytes, after: bytes) -> float:
    """Fraction of cells that changed beyond the noise floor, in any channel."""
    if len(before) != len(after):
        return 1.0
    cells = len(before) // 3
    changed = 0
    for index in range(0, len(before), 3):
        if (abs(before[index] - after[index]) > NOISE_FLOOR
                or abs(before[index + 1] - after[index + 1]) > NOISE_FLOOR
                or abs(before[index + 2] - after[index + 2]) > NOISE_FLOOR):
            changed += 1
    return changed / max(1, cells)


def settle_check(timeout_ms: int, interval_ms: int = 120) -> dict:
    """Wait until two consecutive samples agree. The baseline is taken HERE.

    Shared by the explicit wait action and by actions that ask to settle, so the
    baseline is established in the same process as the thing that caused the
    change. Two separate calls cannot do that: by the time the second call's
    process has started, a fast repaint is already part of its baseline and the
    change is invisible. Measured: a window resize that moves 4.8% of the
    fingerprint read as no change at all when the wait ran as its own call.
    """
    started = time.time()
    previous = _fingerprint()
    stable_polls = 0
    polls = 0
    while (time.time() - started) * 1000.0 < timeout_ms:
        time.sleep(interval_ms / 1000.0)
        polls += 1
        current = _fingerprint()
        ratio = _fingerprint_delta(previous, current)
        if ratio <= CHANGE_THRESHOLD:
            stable_polls += 1
            if stable_polls >= 2:
                return {"settled": True, "polls": polls, "changeRatio": round(ratio, 4),
                        "elapsedMs": int((time.time() - started) * 1000)}
        else:
            stable_polls = 0
        previous = current
    return {"settled": False, "polls": polls,
            "elapsedMs": int((time.time() - started) * 1000)}


# How long an input action waits for the screen to settle when asked to.
SETTLE_TIMEOUT_MS = 1_500


def settle_from(req):
    """The optional settle step for an input action, or None when not asked."""
    if req.get("settle") is not True:
        return None
    return settle_check(SETTLE_TIMEOUT_MS)


def settle_result_fields(outcome: dict | None) -> dict:
    if outcome is None:
        return {}
    return {"settled": outcome["settled"], "settleRatio": outcome.get("changeRatio"),
            "settleMs": outcome["elapsedMs"]}


def do_wait(req):
    """Wait until the screen changes, or until it stops changing.

    A fixed sleep is a guess; this is a measurement. Exact equality is useless
    on a real desktop -- a clock or a blinking caret keeps pixels moving forever
    -- so the comparison is the *proportion* of changed cells.
    """
    mode = str(req.get("for") or "change").lower()
    if mode not in ("change", "stable"):
        raise ValueError("for must be 'change' or 'stable'")
    timeout_ms = _integer(req, "timeoutMs", 10_000, 0, 120_000)
    interval_ms = _integer(req, "intervalMs", 250, 20, 5_000)

    if mode == "stable":
        outcome = settle_check(timeout_ms, interval_ms)
        return {"ok": True, "mode": mode, "timeoutMs": timeout_ms, **outcome}

    started = time.time()
    baseline = _fingerprint()
    polls = 0

    while (time.time() - started) * 1000.0 < timeout_ms:
        time.sleep(interval_ms / 1000.0)
        polls += 1
        ratio = _fingerprint_delta(baseline, _fingerprint())
        if ratio >= CHANGE_THRESHOLD:
            return {"ok": True, "settled": True, "mode": mode, "changeRatio": round(ratio, 4),
                    "polls": polls, "elapsedMs": int((time.time() - started) * 1000)}

    return {"ok": True, "settled": False, "mode": mode, "polls": polls,
            "elapsedMs": int((time.time() - started) * 1000)}


# --------------------------------------------------------------------------
# Input actions
# --------------------------------------------------------------------------


def do_move(req):
    """Place the cursor without pressing anything.

    Split from clicking on purpose: a click that moves the cursor can change what
    is under it (a popup menu repositions), so "measure, then press" only holds
    if the cursor is already parked when the measurement is taken.
    """
    point = _pair(req)
    if point is None:
        raise ValueError("move needs both nx and ny; use it to park the cursor at a point")
    started = time.time()
    with x11.Display() as display:
        width, height = desktop_size(display)
        x, y = to_pixels(point[0], point[1], width, height)
        display.inject_motion(x, y)
        display.sync()
        time.sleep(0.08)
        cursor_x, cursor_y, _ = display.pointer()
        window = display.client_at(cursor_x, cursor_y)
        title = display.window_title(window)
        protected = is_protected_title(title)
        audit({"action": "move", "nx": point[0], "ny": point[1], "cursor": [cursor_x, cursor_y],
               "window": window_label(display, window), "protected": protected})
        return {
            "ok": True,
            "cursorX": cursor_x,
            "cursorY": cursor_y,
            "desktopWidth": width,
            "desktopHeight": height,
            "windowUnderCursor": window_label(display, window),
            "windowProtected": protected,
            "elapsedMs": int((time.time() - started) * 1000),
        }


def do_click(req):
    """Press a mouse button, optionally moving the cursor there first."""
    button_name = str(req.get("button") or "left").lower()
    if button_name not in BUTTONS:
        raise ValueError(f"button must be one of {sorted(BUTTONS)} (got {button_name!r})")
    clicks = _integer(req, "clicks", 1, 1, MAX_CLICKS)
    hold_ms = _integer(req, "holdMs", 0, 0, MAX_HOLD_MS)
    point = _pair(req)
    move = req.get("move", True) is not False

    started = time.time()
    with x11.Display() as display:
        width, height = desktop_size(display)
        cursor_before = display.pointer()[:2]

        if point is not None and move:
            x, y = to_pixels(point[0], point[1], width, height)
            display.inject_motion(x, y)
            display.sync()
            time.sleep(0.08)
        cursor_x, cursor_y, _ = display.pointer()

        # The guard runs AFTER the move and BEFORE the press.
        window = assert_point_allowed(display, cursor_x, cursor_y)

        button = BUTTONS[button_name]
        for _ in range(clicks):
            display.inject_button(button, True)
            if hold_ms:
                time.sleep(hold_ms / 1000.0)
            display.inject_button(button, False)
            display.sync()
            if clicks > 1:
                time.sleep(0.05)
        time.sleep(0.05)

        window_title = display.window_title(window)
        settle = settle_from(req)
        audit({"action": "click", "button": button_name, "clicks": clicks, "move": move,
               "nx": point[0] if point else None, "ny": point[1] if point else None,
               "cursorBefore": list(cursor_before), "cursorAfter": [cursor_x, cursor_y],
               "window": window_label(display, window)})
        return {
            **settle_result_fields(settle),
            "ok": True,
            "button": button_name,
            "clicks": clicks,
            "cursorX": cursor_x,
            "cursorY": cursor_y,
            "movedCursor": point is not None and move and cursor_before != (cursor_x, cursor_y),
            "inPlace": point is None,
            "windowUnderCursor": window_label(display, window),
            "windowTitle": window_title,
            "elapsedMs": int((time.time() - started) * 1000),
        }


def do_key(req):
    """Send key combinations to the focused window."""
    keys = req.get("keys")
    if not isinstance(keys, list) or len(keys) == 0:
        raise ValueError('keys must be a non-empty array of combinations, e.g. ["ctrl+z", "esc"]')
    if len(keys) > 20:
        raise ValueError(f"at most 20 combinations per call (got {len(keys)})")
    # Pure validation first: it touches nothing, so a malformed request is
    # reported as malformed even when the focused window happens to be protected.
    for combo in keys:
        keymap.parse_combo(combo)

    started = time.time()
    with x11.Display() as display:
        focused = assert_focus_allowed(display)
        pressed = []
        for combo in keys:
            pressed.append(press_combo(display, combo))
            time.sleep(0.02)

        settle = settle_from(req)
        audit({"action": "key", "keys": pressed, "focusedWindow": window_label(display, focused)})
        return {
            **settle_result_fields(settle),
            "ok": True,
            "keysPressed": len(pressed),
            "keys": pressed,
            "focusedWindow": window_label(display, focused),
            "elapsedMs": int((time.time() - started) * 1000),
        }


def do_type(req):
    """Send key combinations and/or text."""
    text = req.get("text")
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise ValueError(f"text must be a string (got {type(text).__name__})")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"text must be at most {MAX_TEXT_CHARS} characters (got {len(text)})")

    keys = req.get("keys")
    if keys is None:
        keys = []
    if not isinstance(keys, list) or not all(isinstance(item, str) for item in keys):
        raise ValueError('keys must be an array of combinations, e.g. ["ctrl+a"]')
    for combo in keys:
        keymap.parse_combo(combo)
    enter = req.get("enter", False) is True

    if text == "" and len(keys) == 0 and not enter:
        raise ValueError("nothing to send: supply text, keys, or enter")

    started = time.time()
    with x11.Display() as display:
        focused = assert_focus_allowed(display)

        pressed = []
        for combo in keys:
            pressed.append(press_combo(display, combo))
            time.sleep(0.02)

        delivery = type_text(display, text) if text else {"method": "none", "characters": 0}

        if enter:
            press_combo(display, "enter")

        settle = settle_from(req)
        audit({"action": "type", "characters": delivery.get("characters", 0),
               "method": delivery.get("method"), "keys": pressed, "enter": enter,
               "focusedWindow": window_label(display, focused)})
        return {
            **settle_result_fields(settle),
            "ok": True,
            "characters": delivery.get("characters", 0),
            "delivery": delivery.get("method"),
            "deliveryTarget": delivery.get("target"),
            "keysPressed": len(pressed),
            "enter": enter,
            "focusedWindow": window_label(display, focused),
            "elapsedMs": int((time.time() - started) * 1000),
        }




# --------------------------------------------------------------------------
# Window and element actions
#
# These are the three that make the accessibility tree usable: read a window's
# elements, drive one through its own interface, and capture a single window.
#
# Measured, and the reason `screen_act` is worth having: an AT-SPI action does
# NOT move the pointer and does NOT steal focus -- the window does not even have
# to be raised. (The Windows reference implementation found the opposite there:
# every pattern raised the target to the foreground, and it had to give focus
# back afterwards.) So on Linux this is a genuine background operation.
# --------------------------------------------------------------------------


def resolve_window_spec(display, spec):
    """Resolve an index, a title substring, or a hex window id to one window."""
    if isinstance(spec, bool) or spec is None:
        raise ValueError(
            "window is required: pass an index from screen_windows, a title substring, "
            "or a hex window id like 0x03400003",
        )
    windows = x11.list_windows(display)

    if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().isdigit()):
        index = int(spec)
        if not 0 <= index < len(windows):
            upper = max(0, len(windows) - 1)
            raise ValueError(
                f"window index {index} is out of range; screen_windows currently lists "
                f"{len(windows)} window(s), so use 0..{upper} or a title substring",
            )
        return windows[index]

    text = str(spec).strip()
    if text.lower().startswith("0x"):
        try:
            handle = int(text, 16)
        except ValueError as exc:
            raise ValueError(f"{spec!r} is not a valid window id") from exc
        for window in windows:
            if int(window["id"], 16) == handle:
                return window
        raise ValueError(f"no visible window has id {text} (it may have closed)")

    if not text:
        raise ValueError("window must not be empty: pass an index, a title substring, or a hex window id")

    needle = text.lower()
    matches = [w for w in windows if needle in (w["title"] or "").lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        available = "; ".join(
            f'[{i}] {(w["title"] or "(no title)")[:40]}' for i, w in enumerate(windows[:8])
        )
        raise ValueError(f"no visible window title contains {text!r}. Currently open: {available}")
    sample = "; ".join(f'[{w["stackIndex"]}] {(w["title"] or "")[:40]}' for w in matches[:6])
    raise ValueError(
        f"{len(matches)} windows match {text!r} ({sample}); use a longer substring or an index",
    )


def atspi_for_window(display, window_id):
    """The AT-SPI application that owns a window, matched by process id.

    AT-SPI groups elements by APPLICATION, and an application's name is its
    program name -- never its window title. Matching a title here silently finds
    nothing, which is exactly the bug the probe hit first.
    """
    from linux import atspi

    pid = display.window_pid(window_id)
    if not pid:
        return None, None
    atspi.init()
    app, _apps = atspi.resolve_app(pid=pid)
    return app, pid


def stable_snapshot(app, limit, max_depth=14, rounds=3, interval=0.22):
    """Sample until two consecutive rounds agree, then probe actions once.

    An accessibility tree is built lazily, so a single read under-reports whole
    applications. Probing the Action interface is a D-Bus round trip PER element,
    so it happens once, on the settled tree.
    """
    from linux import atspi

    previous = None
    stable = False
    used = 0
    for round_index in range(1, rounds + 1):
        used = round_index
        cheap = atspi.snapshot_app(app, limit=limit, max_depth=max_depth, include_actions=False)
        current = atspi.fingerprint(cheap)
        if previous is not None and current == previous:
            stable = True
            break
        previous = current
        if round_index < rounds:
            time.sleep(interval)
    entries = atspi.snapshot_app(app, limit=limit, max_depth=max_depth, include_actions=True)
    return entries, stable, used


def do_elements(req):
    """List a window's elements with the actions each one advertises."""
    started = time.time()
    with x11.Display() as display:
        info = resolve_window_spec(display, req.get("window"))
        window_id = int(info["id"], 16)
        # Reading a protected window's tree is refused too: the tree is only
        # useful for acting on that window, which is exactly what the guard
        # exists to prevent. Keep the three element tools consistent.
        assert_window_allowed(display, window_id)
        desktop_w, desktop_h = desktop_size(display)
        app, pid = atspi_for_window(display, window_id)

    if app is None:
        # A missing tree is an ordinary outcome, not an infrastructure failure:
        # custom-drawn UIs have none by design. Reporting it as a RESULT lets the
        # caller fall back to pixels instead of meeting a tool error.
        text = (
            f'"{info["title"]}" (pid {pid}) exposes no accessibility tree, so no elements can be '
            f'listed for it. Custom-drawn UIs and applications started without the accessibility '
            f'bridge expose nothing at all; use screen_look/screen_zoom and coordinates for those.'
        )
        return {"ok": True, "window": info["title"], "application": None, "pid": pid,
                "count": 0, "actionable": 0, "stable": False, "rounds": 0,
                "lines": [], "text": text,
                "elapsedMs": int((time.time() - started) * 1000)}

    limit = _integer(req, "limit", 200, 1, 2000)
    entries, stable, rounds = stable_snapshot(app, limit)
    needle = req.get("filter")
    if isinstance(needle, str) and needle:
        entries = [e for e in entries if needle.lower() in (e.get("name") or "").lower()]

    lines = []
    for entry in entries:
        role = entry.get("role") or "?"
        name = (entry.get("name") or "")[:60]
        parts = [f'{role:14} "{name}"']
        if entry.get("id"):
            parts.append(f'aid={entry["id"]}')
        actions = entry.get("actions") or []
        if actions:
            parts.append(f'[{",".join(actions)}]')
        notable = [s for s in (entry.get("states") or [])
                   if s in ("enabled", "focused", "checked", "expanded", "selected", "editable")]
        if notable:
            parts.append("{" + ",".join(notable) + "}")
        rect = entry.get("rect")
        if rect:
            cx = (rect[0] + rect[2] / 2.0) / max(1, desktop_w - 1)
            cy = (rect[1] + rect[3] / 2.0) / max(1, desktop_h - 1)
            parts.append(f'({round(cx, 4)},{round(cy, 4)})')
        lines.append("  ".join(parts))

    actionable = sum(1 for e in entries if e.get("actions"))
    header = (
        f'{len(entries)} element(s) from "{info["title"]}" ({app["name"]}, pid {pid}); '
        f'{actionable} expose an action. '
        + ("The tree settled." if stable else
           f"The tree did NOT settle in {rounds} rounds, so this list may be incomplete.")
    )
    footer = (
        "Coordinates are fractions of the desktop, ready for screen_click. "
        "Prefer driving an element by name/role with screen_act over clicking its coordinates."
    )
    return {
        "ok": True,
        "window": info["title"],
        "application": app["name"],
        "pid": pid,
        "count": len(entries),
        "actionable": actionable,
        "stable": stable,
        "rounds": rounds,
        "lines": lines,
        "text": "\n".join([header, *lines, footer]),
        "elapsedMs": int((time.time() - started) * 1000),
    }


ELEMENT_ACTIONS = (
    "invoke", "set_value", "insert_text", "toggle", "select",
    "expand", "collapse", "focus", "describe",
)


def do_act(req):
    """Drive one element through its own accessibility interface."""
    action = str(req.get("elementAction") or req.get("element_action") or "invoke").lower()
    if action not in ELEMENT_ACTIONS:
        raise ValueError(
            f"elementAction must be one of {list(ELEMENT_ACTIONS)} (got {action!r})",
        )
    name = req.get("name")
    role = req.get("role")
    if name is not None and not isinstance(name, str):
        raise ValueError("name must be a string")
    if role is not None and not isinstance(role, str):
        raise ValueError("role must be a string")
    occurrence = _integer(req, "occurrence", 1, 1, 50)
    value = req.get("value")
    if value is not None and not isinstance(value, str):
        raise ValueError("value must be a string")
    if action in ("set_value", "insert_text") and value is None:
        raise ValueError(f"{action} needs a value")
    if name is None and role is None and not req.get("automationId"):
        raise ValueError(
            "identify the element: pass name, role, or automationId (read them from screen_elements)",
        )

    started = time.time()
    with x11.Display() as display:
        info = resolve_window_spec(display, req.get("window"))
        window_id = int(info["id"], 16)
        assert_window_allowed(display, window_id)
        previous = display.active_window()
        app, pid = atspi_for_window(display, window_id)

    if app is None:
        return {
            "ok": True,
            "actionOk": False,
            "elementAction": action,
            "window": info["title"],
            "error": f'"{info["title"]}" (pid {pid}) exposes no accessibility tree, so no element can '
                     f'be driven in it. Use screen_look/screen_zoom and screen_click instead.',
            "elapsedMs": int((time.time() - started) * 1000),
        }

    from linux import atspi

    outcome = atspi.perform(
        app_name=None if pid else app["name"], action=action, role=role, name=name,
        occurrence=occurrence, value=value, pid=pid,
        action_name=req.get("actionName"),
    )

    keep_focus = req.get("keepFocus", True) is not False
    restored = False
    if keep_focus and previous and previous != window_id:
        with x11.Display() as display:
            restored = x11.request_focus(display, previous, timeout=2.0).get("ok", False)

    audit({"action": "act", "elementAction": action, "name": name, "role": role,
           "window": info["title"], "ok": outcome.get("ok"), "value": value})
    return {
        # The envelope means "the sidecar ran". Whether the element action itself
        # succeeded is a domain result and lives in `actionOk`, so a missing
        # element reads as a result the model can act on rather than as a crash.
        "ok": True,
        "actionOk": bool(outcome.get("ok")),
        "elementAction": action,
        "element": {"name": name, "role": role, "path": outcome.get("path")},
        # `describe` and the action paths return different detail; carry both
        # through so the caller can present whatever the element actually said.
        "resolved": {"role": outcome.get("role"), "name": outcome.get("name"),
                     "id": outcome.get("id")},
        "available": outcome.get("available"),
        # The action path reports `available`, the describe path reports
        # `actions`; one field for callers either way.
        "actions": outcome.get("actions") or outcome.get("available"),
        "value": value,
        "statesAfter": outcome.get("statesAfter"),
        "states": outcome.get("states"),
        "rect": outcome.get("rect"),
        "window": info["title"],
        "focusRestored": restored,
        "error": outcome.get("error"),
        "elapsedMs": int((time.time() - started) * 1000),
    }


def do_window(req):
    """Focus one window, optionally click or type inside it, then capture it.

    X11 has no PrintWindow equivalent, so a window that is covered cannot be read
    from its own surface: the window is raised, the desktop is captured, and the
    window's rectangle is cropped out. That is a real capability gap against the
    Windows implementation, and it takes focus, which is why focus is handed back
    afterwards unless the caller says not to.
    """
    started = time.time()
    with x11.Display() as display:
        info = resolve_window_spec(display, req.get("window"))
        window_id = int(info["id"], 16)
        assert_window_allowed(display, window_id)
        previous = display.active_window()
        desktop_w, desktop_h = desktop_size(display)

        want_focus = req.get("focus", True) is not False
        focused = False
        if want_focus:
            focused = bool(x11.request_focus(display, window_id, timeout=3.0).get("ok"))
            time.sleep(0.15)

        click = req.get("click") is True
        keys = req.get("keys") or []
        text = req.get("text") or ""
        if not isinstance(keys, list) or not all(isinstance(item, str) for item in keys):
            raise ValueError('keys must be an array of combinations, e.g. ["ctrl+a"]')
        for combo in keys:
            keymap.parse_combo(combo)
        if text and not isinstance(text, str):
            raise ValueError("text must be a string")

        acted = {}
        if click or keys or text:
            if click or keys:
                assert_focus_allowed(display)
            point = _pair(req)
            if point is not None:
                geometry = display.geometry(window_id)
                x = geometry[0] + int(round(point[0] * (geometry[2] - 1)))
                y = geometry[1] + int(round(point[1] * (geometry[3] - 1)))
                display.inject_motion(x, y)
                display.sync()
                time.sleep(0.08)
                assert_point_allowed(display, x, y)
                if click:
                    display.inject_button(BUTTONS["left"], True)
                    display.inject_button(BUTTONS["left"], False)
                    display.sync()
                    acted["clickedAt"] = {"nx": point[0], "ny": point[1], "x": x, "y": y}
                time.sleep(0.12)
            pressed = []
            for combo in keys:
                pressed.append(press_combo(display, combo))
                time.sleep(0.02)
            if pressed:
                acted["keys"] = pressed
            if text:
                delivery = type_text(display, text)
                acted["text"] = delivery
            if req.get("enter") is True:
                press_combo(display, "enter")
                acted["enter"] = True

        # Capture while the window is still in front.
        image = grab_desktop()
        geometry = display.geometry(window_id)
        left = max(0, min(image.size[0] - 1, geometry[0]))
        top = max(0, min(image.size[1] - 1, geometry[1]))
        right = max(left + 1, min(image.size[0], geometry[0] + geometry[2]))
        bottom = max(top + 1, min(image.size[1], geometry[1] + geometry[3]))
        crop = image.crop((left, top, right, bottom))
        scaled, scale = fit_within(crop, req.get("maxPixels"))

        restored = False
        if want_focus and req.get("keepFocus", True) is not False and previous and previous != window_id:
            restored = bool(x11.request_focus(display, previous, timeout=2.0).get("ok"))

        title = info["title"]
        audit({"action": "window", "window": title, "focused": focused,
               "acted": acted, "restored": restored})

    result = {
        "ok": True,
        "window": title,
        "windowId": hex(window_id),
        "focused": focused,
        "captureMethod": "screen",
        "windowWidth": geometry[2],
        "windowHeight": geometry[3],
        "imageWidth": scaled.size[0],
        "imageHeight": scaled.size[1],
        "cropWidth": crop.size[0],
        "cropHeight": crop.size[1],
        "lossless": scale == 1.0,
        "scale": round(scale, 6),
        "focusRestored": restored,
        "acted": acted,
        "occlusionNote": (
            "X11 cannot read a covered window's own surface, so this is the desktop "
            "cropped to the window rectangle: the window was raised first, and anything "
            "that was covering it is not part of the result."
        ),
        "elapsedMs": int((time.time() - started) * 1000),
    }
    return emit_image(scaled, req, result)


ACTIONS = {
    "capture": do_capture,
    "zoom": do_zoom,
    "windows": do_windows,
    "wait": do_wait,
    "move": do_move,
    "click": do_click,
    "key": do_key,
    "type": do_type,
    "elements": do_elements,
    "act": do_act,
    "window": do_window,
}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        raw = sys.stdin.read()
        req = json.loads(raw) if raw.strip() else {}
        if not isinstance(req, dict):
            raise ValueError("the request must be a JSON object")
        action = req.get("action")
        handler = ACTIONS.get(action)
        if handler is None:
            raise ValueError(f"unknown action {action!r}; expected one of {sorted(ACTIONS)}")
        if x11 is None:
            raise RuntimeError(
                f"the X11 layer is unavailable ({X11_IMPORT_ERROR}); "
                f"this needs a running X display (DISPLAY and XAUTHORITY)",
            )
        result = handler(req)
    except Exception as exc:  # every failure must still produce JSON
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 0

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
