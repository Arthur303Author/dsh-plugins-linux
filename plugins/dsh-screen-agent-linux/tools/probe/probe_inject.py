#!/usr/bin/env python3
"""P0 probe — injection group.

This file DOES take over the pointer and keyboard for a few seconds. It is built
so that it can only ever act inside a window it started itself:

  * every injection step re-verifies that the focused window is the probe window
    (an injection with the wrong focus would type into whatever the user has open)
  * a click is verified against the window under the target point before it is sent
  * the original pointer position and focused window are restored on the way out
  * no step ever touches a window the probe did not create

Ground truth is probe_app.py's own event log: an injected event either shows up
there or it did not happen.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import atspi  # noqa: E402
import x11  # noqa: E402

HERE = Path(__file__).parent
OUT_DIR = HERE / "out"
OUT_DIR.mkdir(exist_ok=True)
LOG_PATH = Path("/tmp/dsh-probe/app.log")
PYTHON = sys.executable
WINDOW_TITLE = "DSH Probe Target"
UNICODE_BASE = 0x01000000


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def log_records() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    out = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def wait_for_window(display: x11.Display, title: str, timeout: float = 15.0):
    started = time.time()
    while time.time() - started < timeout:
        for window in display.client_list_stacking():
            if display.window_title(window) == title:
                return window
        time.sleep(0.1)
    return None


def focus_gate(display: x11.Display, window: int, step: str) -> dict:
    """Guarantee that the probe window owns the keyboard before any injection."""
    attempt = x11.request_focus(display, window, timeout=3.0)
    active = display.active_window()
    same = active == window or (
        active != 0 and display.top_level_of(active) == display.top_level_of(window)
    )
    return {
        "step": step,
        "focusRequest": attempt,
        "activeWindow": hex(active),
        "ok": bool(same),
    }


def keycode_for(display: x11.Display, char: str) -> tuple[int, bool] | None:
    """(keycode, needsShift) for a character that is already on the keymap."""
    keysym = display.keysym(char)
    if keysym == 0:
        return None
    keycode = display.keycode(keysym)
    if keycode == 0:
        return None
    for index in range(4):
        from ctypes import c_ubyte, c_int

        current = x11._x11.XKeycodeToKeysym(display._dpy, c_ubyte(keycode), c_int(index))
        if int(current) == keysym:
            return keycode, index in (1, 3)
    return None


def type_text(display: x11.Display, text: str) -> dict:
    """Type text: mapped keycodes when possible, a remapped scratch keycode for
    characters X has no key for (CJK, emoji).

    X11 has no equivalent of KEYEVENTF_UNICODE: a character that is not on the
    keymap must be given one temporarily. The scratch keycode is restored after.
    """
    shift_keycode = display.keycode(display.keysym("Shift_L"))
    scratch = display.free_keycode()
    remapped: list[str] = []
    scratch_original = None
    if scratch is not None:
        from ctypes import c_int, c_ubyte

        scratch_original = int(
            x11._x11.XKeycodeToKeysym(display._dpy, c_ubyte(scratch), c_int(0))
        )

    for char in text:
        direct = keycode_for(display, char)
        if direct is not None:
            keycode, needs_shift = direct
            if needs_shift:
                display.inject_key(shift_keycode, True)
            display.inject_key(keycode, True)
            display.inject_key(keycode, False)
            if needs_shift:
                display.inject_key(shift_keycode, False)
            display.sync()
            time.sleep(0.02)
            continue

        if scratch is None:
            return {"ok": False, "error": f"no free keycode to type {char!r}",
                    "typed": "".join(remapped)}
        display.map_keycode(scratch, UNICODE_BASE + ord(char))
        display.inject_key(scratch, True)
        display.inject_key(scratch, False)
        display.sync()
        time.sleep(0.05)          # the remap must land before the app reads the event
        remapped.append(char)

    if scratch is not None and scratch_original is not None:
        display.map_keycode(scratch, scratch_original)   # always restore the keymap

    return {"ok": True, "characters": len(text), "remapped": "".join(remapped),
            "scratchKeycode": scratch}


def click_at(display: x11.Display, x: int, y: int, expected_window: int,
             button: int = 1) -> dict:
    """Move, re-verify what is under the point, then press.

    The check happens AFTER the move and BEFORE the press on purpose: moving the
    pointer can change what is under it (a hover popup, a raise), so a check made
    before the move does not cover the moment that actually matters.
    """
    display.inject_motion(x, y)
    display.sync()
    time.sleep(0.15)
    under = display.client_at(x, y)
    if under != expected_window:
        return {"refused": True, "windowUnderPoint": hex(under),
                "expected": hex(expected_window)}
    display.inject_button(button, True)
    time.sleep(0.05)
    display.inject_button(button, False)
    display.sync()
    time.sleep(0.25)
    return {"x": x, "y": y, "windowUnderPoint": hex(under)}


# --------------------------------------------------------------------------
# Test sequence
# --------------------------------------------------------------------------


def main() -> int:
    atspi.init()
    report: dict = {"startedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}

    display = x11.Display()
    original_active = display.active_window()
    original_pointer = display.pointer()[:2]
    report["baseline"] = {
        "activeWindow": hex(original_active),
        "activeTitle": display.window_title(original_active)[:70],
        "pointer": list(original_pointer),
    }
    print(f"baseline: active={hex(original_active)} {display.window_title(original_active)[:50]!r} "
          f"pointer={original_pointer}")

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["PROBE_LOG"] = str(LOG_PATH)
    app = subprocess.Popen([PYTHON, str(HERE / "probe_app.py")], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    try:
        window = wait_for_window(display, WINDOW_TITLE, timeout=15)
        if window is None:
            report["error"] = "probe window never appeared"
            print("FATAL: probe window never appeared")
            return 1
        time.sleep(0.6)
        geometry = display.geometry(window) or (0, 0, 0, 0)
        info = x11.describe_window(display, window)
        report["window"] = info
        print(f"probe window: {hex(window)} {geometry[2]}x{geometry[3]}+{geometry[0]}+{geometry[1]} "
              f"pid={info['pid']}")

        # ---- 1. focus -----------------------------------------------------
        gate = focus_gate(display, window, "focus")
        report["focus"] = gate
        print(f"\n[1] focus: ok={gate['ok']} active={gate['activeWindow']} "
              f"({gate['focusRequest'].get('elapsedMs')}ms)")
        if not gate["ok"]:
            report["aborted"] = "could not take focus; refusing to inject anywhere"
            print("    refusing to inject: focus is not ours")
            return write_report(report)

        # ---- 2. keyboard (ASCII) -----------------------------------------
        typed = type_text(display, "abc")
        time.sleep(0.4)
        records = log_records()
        key_names = [r.get("keyval") for r in records if r.get("kind") == "KEY_PRESS"]
        text_value = "".join(r.get("text", "") for r in records if r.get("kind") == "TEXT_CHANGED")
        report["keyboard"] = {"inject": typed, "observedKeyPresses": key_names,
                              "ok": key_names[-3:] == ["a", "b", "c"] if len(key_names) >= 3 else False}
        print(f"\n[2] keyboard ascii: injected 'abc' -> observed {key_names} "
              f"=> {'PASS' if report['keyboard']['ok'] else 'FAIL'}")

        # ---- 3. unicode / CJK --------------------------------------------
        re_gate = focus_gate(display, window, "before-unicode")
        if re_gate["ok"]:
            typed_cjk = type_text(display, "你好")
            time.sleep(0.5)
            records = log_records()
            changed = [r.get("text", "") for r in records if r.get("kind") == "TEXT_CHANGED"]
            joined = "".join(changed)
            report["unicode"] = {"inject": typed_cjk, "entryText": joined[-40:],
                                 "ok": "你好" in joined}
            print(f"[3] unicode CJK: injected '你好' (remapped={typed_cjk.get('remapped')!r}) "
                  f"-> entry={joined[-20:]!r} => {'PASS' if report['unicode']['ok'] else 'FAIL'}")
        else:
            report["unicode"] = {"ok": False, "error": "focus lost before unicode test"}
            print("[3] unicode CJK: SKIPPED (focus lost)")

        # ---- 4. mouse -----------------------------------------------------
        gate = focus_gate(display, window, "before-click")
        if not gate["ok"]:
            report["mouse"] = {"ok": False, "error": "focus lost before mouse test"}
            print("[4] mouse: SKIPPED (focus lost)")
        else:
            geometry = display.geometry(window) or (0, 0, 0, 0)
            target_x = geometry[0] + geometry[2] // 2
            target_y = geometry[1] + geometry[3] // 2
            under = display.client_at(target_x, target_y)
            report["mouse"] = {"target": [target_x, target_y], "windowUnderPoint": hex(under),
                               "sameWindow": under == window}
            if under != window:
                report["mouse"]["ok"] = False
                report["mouse"]["error"] = "target point belongs to another window; click refused"
                print(f"[4] mouse: REFUSED - point belongs to {hex(under)}")
            else:
                before = len(log_records())
                click_result = click_at(display, target_x, target_y, window)
                time.sleep(0.4)
                records = log_records()[before:]
                presses = [r for r in records if r.get("kind") == "BUTTON_PRESS"]
                report["mouse"].update(click_result)
                report["mouse"]["observed"] = presses
                match = bool(presses) and abs(presses[0].get("x_root", -1) - target_x) <= 2 \
                    and abs(presses[0].get("y_root", -1) - target_y) <= 2
                report["mouse"]["ok"] = match
                print(f"[4] mouse: clicked ({target_x},{target_y}) -> observed "
                      f"{[(p.get('x_root'), p.get('y_root')) for p in presses]} "
                      f"=> {'PASS' if match else 'FAIL'}")

        # ---- 5. AT-SPI act (no pointer movement) --------------------------
        focus_gate(display, window, "before-act")
        pointer_before = display.pointer()[:2]
        before = len(log_records())
        act_result = atspi.perform(pid=app.pid, action="invoke", role="push button",
                                   name="PROBE_BUTTON")
        time.sleep(0.5)
        pointer_after = display.pointer()[:2]
        records = log_records()[before:]
        clicked = any(r.get("kind") == "BUTTON_CLICKED" for r in records)
        report["atspiAct"] = {
            "result": act_result,
            "observedButtonClicked": clicked,
            "pointerBefore": list(pointer_before),
            "pointerAfter": list(pointer_after),
            "pointerMoved": pointer_before != pointer_after,
            "ok": clicked and pointer_before == pointer_after,
        }
        print(f"\n[5] atspi act: {act_result.get('action') or act_result.get('error')} "
              f"-> BUTTON_CLICKED={clicked} pointerMoved={pointer_before != pointer_after} "
              f"=> {'PASS' if report['atspiAct']['ok'] else 'FAIL'}")

        # ---- 6. AT-SPI set_value -----------------------------------------
        before = len(log_records())
        set_result = atspi.perform(pid=app.pid, action="set_value", role="entry", value="via-set-value")
        if not set_result.get("ok"):
            set_result = atspi.perform(pid=app.pid, action="set_value", role="text", value="via-set-value")
        time.sleep(0.5)
        records = log_records()[before:]
        texts = [r.get("text", "") for r in records if r.get("kind") == "TEXT_CHANGED"]
        report["atspiSetValue"] = {"result": set_result, "observed": texts,
                                   "ok": any("via-set-value" in t for t in texts)}
        print(f"[6] atspi set_value: {set_result.get('ok')} -> TEXT_CHANGED={texts} "
              f"=> {'PASS' if report['atspiSetValue']['ok'] else 'FAIL'}")

        # ---- 7. element coverage of the probe window ---------------------
        coverage = atspi.measure(pid=app.pid, limit=200, rounds=3, interval=0.2, max_depth=10)
        report["coverage"] = coverage
        print(f"[7] probe window a11y: nodes={coverage.get('nodeCount')} "
              f"named={coverage.get('namedCount')} actionable={coverage.get('actionableCount')} "
              f"stable={coverage.get('stable')} rounds={coverage.get('rounds')}")
        for item in (coverage.get("actionable") or [])[:8]:
            print(f"        - {item['role']:14} {item['name'][:30]!r:32} {item['actions']}")

        return write_report(report)

    finally:
        # Give the desktop back in the reverse order it was taken: pointer,
        # then focus, and only then close the probe window. Closing first would
        # leave _NET_ACTIVE_WINDOW pointing at a window that no longer exists.
        display.inject_motion(original_pointer[0], original_pointer[1])
        display.sync()
        if original_active:
            x11.request_focus(display, original_active, timeout=2.0)
        app.terminate()
        try:
            app.wait(timeout=5)
        except subprocess.TimeoutExpired:
            app.kill()
        print(f"\nrestored pointer={original_pointer} focus={hex(original_active)}")
        print(f"x protocol errors swallowed: {len(x11.recent_errors())} {x11.recent_errors()[-3:]}")
        display.close()


def write_report(report: dict) -> int:
    path = OUT_DIR / "inject.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"raw data -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
