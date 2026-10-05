#!/usr/bin/env python3
"""P3 acceptance test: the accessibility element layer and window capture.

Same discipline as the P2 test: everything happens inside a GTK window this
script starts itself, and that window's own event log is the ground truth.

The protected-window guard is exercised with the PROBE window's own title as the
marker, never the real one, so a guard failure costs a throwaway window rather
than the browser driving this session.

Usage:  python3 sidecar_p3_test.py
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
LIB = PLUGIN / "lib"
PROBE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(LIB))

from linux import x11  # noqa: E402

SIDECAR = LIB / "screen_tools.py"
PYTHON = sys.executable
LOG_PATH = Path("/tmp/dsh-probe/app.log")
WINDOW_TITLE = "DSH Probe Target"

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def kill_stray_probes() -> None:
    """Kill leftover probe windows without pkill.

    `pkill -f probe_app` also matches the shell running it, because the pattern
    appears in that shell's own command line -- which kills the test instead.
    """
    me = os.getpid()
    listing = subprocess.run(["pgrep", "-af", "python3"], capture_output=True, text=True).stdout
    for line in listing.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) != me:
            if "probe_app" in parts[1] and "screen_tools" not in parts[1] and "sidecar_" not in parts[1]:
                try:
                    os.kill(int(parts[0]), signal.SIGTERM)
                except Exception:
                    pass


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


def main() -> int:
    kill_stray_probes()
    time.sleep(0.8)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["PROBE_LOG"] = str(LOG_PATH)

    app = subprocess.Popen([PYTHON, str(PROBE_DIR / "probe_app.py")], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           start_new_session=True)
    display = x11.Display()
    original_active = display.active_window()
    original_pointer = display.pointer()[:2]

    try:
        window = None
        deadline = time.time() + 15
        while time.time() < deadline and window is None:
            for candidate in display.client_list_stacking():
                if display.window_title(candidate) == WINDOW_TITLE:
                    window = candidate
                    break
            time.sleep(0.1)
        if window is None:
            print("FATAL: probe window never appeared")
            return 1
        time.sleep(0.9)
        geometry = display.geometry(window)
        print(f"probe window {hex(window)} at {geometry}")

        # ---- elements --------------------------------------------------
        print("\nelements")
        result = call({"action": "elements", "window": WINDOW_TITLE})
        lines = result.get("lines") or []
        record("elements lists the window's tree",
               result.get("ok") is True and result.get("count", 0) > 0,
               f"count={result.get('count')} actionable={result.get('actionable')} "
               f"stable={result.get('stable')}")
        record("elements reports the button with its action",
               any("PROBE_BUTTON" in line and "click" in line for line in lines),
               next((line.strip() for line in lines if "PROBE_BUTTON" in line), "(not found)"))
        record("elements reports the editable text field",
               any(line.strip().startswith("text") and "editable" in line for line in lines),
               next((line.strip() for line in lines if line.strip().startswith("text")), "(not found)"))

        # ---- act: invoke ----------------------------------------------
        print("\nact (invoke)")
        pointer_before = display.pointer()[:2]
        focused_before = display.active_window()
        before = len(records())
        result = call({"action": "act", "window": WINDOW_TITLE, "elementAction": "invoke",
                       "name": "PROBE_BUTTON", "role": "push button"})
        time.sleep(0.5)
        pointer_after = display.pointer()[:2]
        focused_after = display.active_window()
        clicked = any(r.get("kind") == "BUTTON_CLICKED" for r in records()[before:])
        record("act invoke activates the button", result.get("actionOk") is True and clicked,
               f"actionOk={result.get('actionOk')} observed={clicked}")
        record("act invoke moves neither pointer nor focus",
               pointer_before == pointer_after and focused_before == focused_after,
               f"pointer {pointer_before}->{pointer_after} focus "
               f"{hex(focused_before)}->{hex(focused_after)}")

        # ---- act: set_value with non-ASCII ----------------------------
        print("\nact (set_value)")
        before = len(records())
        result = call({"action": "act", "window": WINDOW_TITLE, "elementAction": "set_value",
                       "role": "text", "value": "设置的中文值"})
        time.sleep(0.5)
        texts = "".join(r.get("text", "") for r in records()[before:]
                        if r.get("kind") == "TEXT_CHANGED")
        record("act set_value writes non-ASCII text",
               result.get("actionOk") is True and "设置的中文值" in texts,
               f"observed={texts!r}")

        # ---- act: describe --------------------------------------------
        print("\nact (describe)")
        result = call({"action": "act", "window": WINDOW_TITLE, "elementAction": "describe",
                       "name": "PROBE_CHECK", "role": "check box"})
        record("act describe reports the element's own actions",
               result.get("actionOk") is True and bool(result.get("actions")),
               f"actions={result.get('actions')} states={result.get('states')}")

        # ---- act: a missing element is a result, not a crash ----------
        print("\nact (missing element)")
        result = call({"action": "act", "window": WINDOW_TITLE, "elementAction": "invoke",
                       "name": "NO_SUCH_BUTTON", "role": "push button"})
        record("a missing element returns a domain result, not a failure envelope",
               result.get("ok") is True and result.get("actionOk") is False
               and "element not found" in str(result.get("error", "")),
               f"ok={result.get('ok')} actionOk={result.get('actionOk')}")

        result = call({"action": "act", "window": WINDOW_TITLE, "elementAction": "invoke"})
        record("act without an identifier is refused before touching anything",
               result.get("ok") is False and "identify the element" in str(result.get("error", "")),
               str(result.get("error", ""))[:70])

        # ---- window capture -------------------------------------------
        print("\nwindow (focus + capture)")
        result = call({"action": "window", "window": WINDOW_TITLE, "inline": True})
        record("window captures the window rectangle",
               result.get("ok") is True
               and result.get("cropWidth") == geometry[2]
               and result.get("cropHeight") == geometry[3],
               f"crop={result.get('cropWidth')}x{result.get('cropHeight')} "
               f"lossless={result.get('lossless')} method={result.get('captureMethod')}")

        # ---- window capture with an in-window click and typing --------
        print("\nwindow (click + type inside)")
        before = len(records())
        result = call({"action": "window", "window": WINDOW_TITLE, "inline": True,
                       "click": True, "nx": 0.5, "ny": 0.11, "text": "win ",
                       "capture": True})
        time.sleep(0.5)
        seen = records()[before:]
        # A click ON the button is consumed by the button widget and never
        # bubbles to the window's own handler, so activation -- not the
        # window-level press -- is the ground truth for this one.
        activated = [r for r in seen if r.get("kind") == "BUTTON_CLICKED"]
        typed = [r.get("keyval") for r in seen if r.get("kind") == "KEY_PRESS"]
        record("window can click and type inside itself before capturing",
               result.get("ok") is True and bool(activated) and typed[:3] == ["w", "i", "n"],
               f"activated={len(activated)} typed={typed}")

        # ---- protected window -----------------------------------------
        print("\nprotected-window guard")
        guard = {"DSH_SCREEN_AGENT_PROTECT": WINDOW_TITLE}
        result = call({"action": "elements", "window": WINDOW_TITLE}, guard)
        record("elements does not leak a protected window's tree",
               result.get("ok") is False and "protected" in str(result.get("error", "")),
               str(result.get("error", ""))[:70])
        result = call({"action": "act", "window": WINDOW_TITLE, "elementAction": "invoke",
                       "name": "PROBE_BUTTON", "role": "push button"}, guard)
        record("act refuses to drive a protected window",
               result.get("ok") is False and "protected" in str(result.get("error", "")),
               str(result.get("error", ""))[:70])
        result = call({"action": "window", "window": WINDOW_TITLE}, guard)
        record("window refuses to capture a protected window",
               result.get("ok") is False and "protected" in str(result.get("error", "")),
               str(result.get("error", ""))[:70])

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
