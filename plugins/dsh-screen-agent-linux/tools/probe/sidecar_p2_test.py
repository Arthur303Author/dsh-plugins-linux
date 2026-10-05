#!/usr/bin/env python3
"""P2 acceptance test: drive the sidecar through its real protocol.

Everything happens inside a GTK window this script starts itself, and the
window's own event log is the ground truth -- an action either shows up there or
it did not happen.

The protected-window guard is verified with the PROBE window's own title as the
marker, not the real one: if the guard ever failed, the worst case is a click on
a throwaway window instead of on the browser driving this session.

Usage:  python3 sidecar_p2_test.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
LIB = PLUGIN / "lib"
PROBE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(LIB))

from linux import atspi, x11  # noqa: E402

SIDECAR = LIB / "screen_tools.py"
PYTHON = sys.executable
LOG_PATH = Path("/tmp/dsh-probe/app.log")
WINDOW_TITLE = "DSH Probe Target"

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def call(req: dict, env_extra: dict | None = None) -> dict:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [PYTHON, str(SIDECAR)], input=json.dumps(req), capture_output=True,
        text=True, env=env, timeout=120,
    )
    out = (proc.stdout or "").strip()
    if not out:
        return {"ok": False, "error": f"no output; stderr={proc.stderr[-300:]}"}
    return json.loads(out)


def records() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    out = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def wait_window(display, timeout: float = 15.0):
    started = time.time()
    while time.time() - started < timeout:
        for window in display.client_list_stacking():
            if display.window_title(window) == WINDOW_TITLE:
                return window
        time.sleep(0.1)
    return None


def main() -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["PROBE_LOG"] = str(LOG_PATH)

    app = subprocess.Popen([PYTHON, str(PROBE_DIR / "probe_app.py")], env=env)
    display = x11.Display()
    original_active = display.active_window()
    original_pointer = display.pointer()[:2]

    try:
        window = wait_window(display)
        if window is None:
            print("FATAL: probe window never appeared")
            return 1
        time.sleep(0.8)
        x11.request_focus(display, window, timeout=3.0)
        time.sleep(0.4)
        geometry = display.geometry(window)
        center_x = geometry[0] + geometry[2] // 2
        center_y = geometry[1] + geometry[3] // 2
        print(f"probe window {hex(window)} at {geometry}, center ({center_x},{center_y})")

        # ---- read-only ------------------------------------------------
        print("\nread-only actions")
        result = call({"action": "windows"})
        record("windows lists the probe window", result.get("ok") and any(
            WINDOW_TITLE in line for line in result.get("lines", [])),
            f"count={result.get('count')}")

        result = call({"action": "wait", "for": "change", "timeoutMs": 700, "intervalMs": 100})
        record("wait(change) times out cleanly on a static screen",
               result.get("ok") is True and result.get("settled") is False,
               f"polls={result.get('polls')}")

        # ---- pointer --------------------------------------------------
        print("\npointer")
        nx = center_x / (result.get("desktopWidth") or 2560)
        ny = center_y / (result.get("desktopHeight") or 1600)

        result = call({"action": "move", "nx": nx, "ny": ny})
        moved_ok = (result.get("ok")
                    and abs(result.get("cursorX", 0) - center_x) <= 2
                    and abs(result.get("cursorY", 0) - center_y) <= 2)
        record("move parks the cursor at the target", moved_ok,
               f"cursor=({result.get('cursorX')},{result.get('cursorY')})")

        result = call({"action": "click", "nx": nx, "ny": ny})
        time.sleep(0.3)
        presses = [r for r in records() if r.get("kind") == "BUTTON_PRESS"]
        hit = bool(presses) and abs(presses[-1].get("x_root", -1) - center_x) <= 2
        record("click reaches the window at the aimed point", result.get("ok") and hit,
               f"observed={[(p.get('x_root'), p.get('y_root')) for p in presses][-1:] }")

        result = call({"action": "click", "nx": nx})
        refused = result.get("ok") is False and "without" in str(result.get("error", ""))
        record("half a coordinate is refused, never degraded", refused,
               str(result.get("error", ""))[:70])

        # ---- keyboard -------------------------------------------------
        print("\nkeyboard")
        before = len(records())
        result = call({"action": "key", "keys": ["ctrl+a"]})
        time.sleep(0.3)
        observed = [r.get("keyval") for r in records()[before:] if r.get("kind") == "KEY_PRESS"]
        record("key sends a combination", result.get("ok") and len(observed) > 0,
               f"observed={observed}")

        before = len(records())
        result = call({"action": "type", "text": "abc"})
        time.sleep(0.3)
        observed = [r.get("keyval") for r in records()[before:] if r.get("kind") == "KEY_PRESS"]
        record("type delivers ASCII through the keyboard",
               result.get("ok") and observed[-3:] == ["a", "b", "c"],
               f"method={result.get('delivery')} observed={observed}")

        before = len(records())
        result = call({"action": "type", "text": "a b!c"})
        time.sleep(0.4)
        observed = [r.get("keyval") for r in records()[before:] if r.get("kind") == "KEY_PRESS"]
        record("type handles spaces and symbols",
               result.get("ok") is True and "space" in observed and len(observed) >= 5,
               f"observed={observed}")

        result = call({"action": "key", "keys": ["ctrl+nonsense"]})
        record("an unknown key is refused with a reason",
               result.get("ok") is False and "unknown key" in str(result.get("error", "")),
               str(result.get("error", ""))[:70])

        # ---- non-ASCII through the toolkit ----------------------------
        print("\nnon-ASCII text")
        atspi.init()
        found, meta = atspi.find(pid=app.pid, role="text")
        if found is None:
            record("focused text widget is reachable through AT-SPI", False, str(meta))
        else:
            _info, node, _path = found
            iface = node.get_component_iface()
            iface.grab_focus()
            time.sleep(0.4)
            before = len(records())
            result = call({"action": "type", "text": "你好世界"})
            time.sleep(0.4)
            texts = "".join(r.get("text", "") for r in records()[before:]
                            if r.get("kind") == "TEXT_CHANGED")
            record("type delivers non-ASCII through EditableText",
                   result.get("ok") and "你好世界" in texts,
                   f"method={result.get('delivery')} entry={texts!r}")

        # ---- protected windows ----------------------------------------
        print("\nprotected-window guard (marker pinned to the probe title)")
        guard = {"DSH_SCREEN_AGENT_PROTECT": WINDOW_TITLE}
        result = call({"action": "click", "nx": nx, "ny": ny}, guard)
        record("click into a protected window is refused",
               result.get("ok") is False and "protected" in str(result.get("error", "")),
               str(result.get("error", ""))[:70])

        x11.request_focus(display, window, timeout=2.0)
        time.sleep(0.3)
        result = call({"action": "key", "keys": ["esc"]}, guard)
        record("keystrokes into a protected focused window are refused",
               result.get("ok") is False and "protected" in str(result.get("error", "")),
               str(result.get("error", ""))[:70])

        result = call({"action": "type", "text": "x"}, guard)
        record("text into a protected focused window is refused",
               result.get("ok") is False and "protected" in str(result.get("error", "")),
               str(result.get("error", ""))[:70])

        # ---- change detection -----------------------------------------
        # The change must land AFTER wait has taken its baseline: a sidecar call
        # is a fresh process, so anything that already happened before the call
        # began is simply the new baseline. That asymmetry is also why input
        # actions offer `settle`, which takes the baseline in-process.
        #
        # A single label character is below what a 64x40 digest can see. That
        # granularity is deliberate -- it tolerates clocks and blinking carets --
        # and it is what "a window opened / a page loaded" needs.
        print("\nchange detection")
        import threading

        def bump():
            time.sleep(0.6)
            other = x11.Display()
            try:
                other.move_resize(window, geometry[0] + 240, max(0, geometry[1] - 200),
                                  max(80, geometry[2] - 160), max(60, geometry[3] - 120))
                other.sync()
            finally:
                other.close()

        worker = threading.Thread(target=bump)
        worker.start()
        result = call({"action": "wait", "for": "change", "timeoutMs": 5000,
                       "intervalMs": 100})
        worker.join()
        record("wait(change) detects a repaint that lands while it waits",
               result.get("ok") is True and result.get("settled") is True,
               f"ratio={result.get('changeRatio')} polls={result.get('polls')}")

        # ---- settle inside the action's own process -------------------
        print("\nsettle")
        desktop_w, desktop_h = display.geometry(display.root)[2:4]
        display.move_resize(window, geometry[0], geometry[1],
                            max(80, geometry[2] - 200), max(60, geometry[3] - 140))
        display.sync()
        time.sleep(0.6)
        resized = display.geometry(window)
        nx2 = (resized[0] + resized[2] // 2) / (desktop_w - 1)
        ny2 = (resized[1] + resized[3] // 2) / (desktop_h - 1)

        result = call({"action": "click", "nx": nx2, "ny": ny2, "settle": True})
        record("click with settle reports a settled verdict",
               result.get("ok") is True and "settled" in result,
               f"settled={result.get('settled')} settleMs={result.get('settleMs')}")

        result = call({"action": "key", "keys": ["esc"]})
        record("key without settle omits the verdict",
               result.get("ok") is True and "settled" not in result,
               f"keys={result.get('keys')}")

    finally:
        display.inject_motion(original_pointer[0], original_pointer[1])
        display.sync()
        if original_active:
            x11.request_focus(display, original_active, timeout=2.0)
        app.terminate()
        try:
            app.wait(timeout=5)
        except subprocess.TimeoutExpired:
            app.kill()
        display.close()

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILED: {name} — {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
