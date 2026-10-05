#!/usr/bin/env python3
"""P0 probe — coordinate alignment and the occlusion question.

Two things worth measuring before writing a single line of the plugin:

  1. Do the three coordinate systems agree? X11 geometry, the screenshot's pixel
     grid, and AT-SPI element rectangles all claim to describe the same screen.
     If they disagree, every synthesized click lands somewhere else.

  2. What does a window crop actually contain when the window is covered? On
     Windows the reference implementation reads a window's own surface
     (PrintWindow) and occlusion is irrelevant. X11 has no equivalent, so the
     honest answer has to be measured rather than assumed: crop the same
     rectangle before and after covering the window and compare.

Only screenshots and one focus request; the pointer is not moved.
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
from probe_inject import LOG_PATH, PYTHON, WINDOW_TITLE  # noqa: E402

from PIL import ImageGrab  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
OUT_DIR.mkdir(exist_ok=True)
NOISE_FLOOR = 12


def start_window(pos: str, size: str, tag: str = "A", bg: str = "#2f2f35") -> subprocess.Popen:
    env = dict(os.environ)
    env["PROBE_LOG"] = str(LOG_PATH)
    env["PROBE_POS"] = pos
    env["PROBE_SIZE"] = size
    env["PROBE_TAG"] = tag
    env["PROBE_BG"] = bg
    return subprocess.Popen([PYTHON, str(Path(__file__).parent / "probe_app.py")], env=env)


def window_for_pid(display: x11.Display, pid: int, timeout: float = 15.0):
    started = time.time()
    while time.time() - started < timeout:
        for window in display.client_list_stacking():
            if display.window_title(window) == WINDOW_TITLE and display.window_pid(window) == pid:
                return window
        time.sleep(0.1)
    return None


def crop_rect(rect) -> "object":
    image = ImageGrab.grab().convert("RGB")
    x, y, width, height = rect
    return image.crop((x, y, x + width, y + height))


def diff_ratio(first, second) -> float:
    """Fraction of pixels that differ beyond the noise floor."""
    if first.size != second.size:
        return 1.0
    left = first.convert("L").tobytes()
    right = second.convert("L").tobytes()
    changed = 0
    for a, b in zip(left, right):
        if abs(a - b) > NOISE_FLOOR:
            changed += 1
    return round(changed / max(1, len(left)), 4)


def main() -> int:
    report: dict = {"startedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}
    display = x11.Display()
    original_active = display.active_window()

    a = start_window("40,1217", "480,300")
    b = None
    try:
        window_a = window_for_pid(display, a.pid)
        if window_a is None:
            print("FATAL: window A never appeared")
            return 1
        time.sleep(1.0)
        rect_a = display.geometry(window_a)
        report["windowA"] = {"id": hex(window_a), "pid": a.pid, "rect": list(rect_a)}
        print(f"window A {hex(window_a)} pid={a.pid} rect={rect_a}")

        visible = crop_rect(rect_a)
        visible.save(OUT_DIR / "crop_A_visible.png")
        print(f"  cropped visible window -> crop_A_visible.png {visible.size}")

        # ---- coordinate alignment: X11 geometry vs AT-SPI rectangles --------
        atspi.init()
        app_a = [x for x in atspi.applications() if x["pid"] == a.pid]
        if app_a:
            entries = atspi.snapshot_app(app_a[0], limit=100)
            frames = [e for e in entries if e.get("rect")]
            report["atspiRects"] = [{"role": e["role"], "name": e["name"], "rect": e["rect"]}
                                    for e in frames[:6]]
            print("  AT-SPI rectangles in the same window:")
            for entry in frames[:6]:
                dx = entry["rect"][0] - rect_a[0]
                dy = entry["rect"][1] - rect_a[1]
                print(f"    {entry['role']:12} {entry['name'][:22]!r:24} rect={entry['rect']} "
                      f"(offset from X11 geometry: {dx:+d},{dy:+d})")
            report["alignment"] = {
                "x11": list(rect_a),
                "atspi": report["atspiRects"],
            }
        else:
            print("  AT-SPI: application for pid not found")

        # ---- occlusion -----------------------------------------------------
        b = start_window("40,1217", "480,300", tag="B", bg="#5a3018")
        window_b = window_for_pid(display, b.pid)
        if window_b is None:
            print("WARN: window B never appeared; skipping occlusion")
        else:
            time.sleep(1.0)
            stacking = display.client_list_stacking()
            b_above_a = stacking.index(window_b) > stacking.index(window_a)
            occluded = crop_rect(rect_a)
            occluded.save(OUT_DIR / "crop_A_occluded.png")
            ratio = diff_ratio(visible, occluded)
            report["occlusion"] = {
                "windowB": {"id": hex(window_b), "pid": b.pid,
                            "rect": list(display.geometry(window_b))},
                "bIsAboveA": b_above_a,
                "diffRatio": ratio,
                "verdict": ("a covered window is NOT visible in a screenshot" if ratio > 0.05
                            else "crop looked unchanged even while covered"),
            }
            print(f"\nwindow B {hex(window_b)} stacked above A: {b_above_a}")
            print(f"  same rectangle re-cropped while covered -> diff={ratio:.2%} "
                  f"=> {report['occlusion']['verdict']}")

            # Raising A again should bring the original content back.
            x11.request_focus(display, window_a, timeout=3.0)
            time.sleep(0.6)
            raised = crop_rect(rect_a)
            raised.save(OUT_DIR / "crop_A_raised.png")
            back = diff_ratio(visible, raised)
            report["occlusion"]["diffAfterRaise"] = back
            print(f"  after raising A again -> diff vs original={back:.2%} "
                  f"({'recovers' if back < 0.05 else 'does not fully recover'})")

    finally:
        if b is not None:
            b.terminate()
            try:
                b.wait(timeout=5)
            except subprocess.TimeoutExpired:
                b.kill()
        a.terminate()
        try:
            a.wait(timeout=5)
        except subprocess.TimeoutExpired:
            a.kill()
        if original_active:
            x11.request_focus(display, original_active, timeout=2.0)
        print(f"\nrestored focus to {hex(original_active)}")
        display.close()

    path = OUT_DIR / "coords.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"raw data -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
