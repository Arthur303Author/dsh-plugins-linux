#!/usr/bin/env python3
"""P0 probe — harmless group.

Nothing in this file moves the pointer, presses a key, opens a window, or
changes focus. It is safe to run while the desktop is in use.

Measures three things the rewrite's shape depends on:

  1. cold-start cost of the sidecar shapes (bare python / +Pillow / +AT-SPI /
     full screenshot-and-encode) -- decides spawn-per-call versus a resident process
  2. in-process capture latency (full grab, budgeted crop + PNG + base64,
     fingerprint)
  3. AT-SPI coverage of the applications that are already running -- decides
     whether the element layer is worth building
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import atspi  # noqa: E402  (local module, shadows nothing in gi.repository)

OUT_DIR = Path(__file__).parent / "out"
OUT_DIR.mkdir(exist_ok=True)
PYTHON = sys.executable
BUDGET = 640_000

FULL_GRAB_SNIPPET = (
    "import base64,io;"
    "from PIL import ImageGrab;"
    "im=ImageGrab.grab();"
    "w,h=im.size;"
    "s=(640000/(w*h))**0.5;"
    "im=im.convert('RGB').resize((max(1,int(w*s)),max(1,int(h*s))));"
    "b=io.BytesIO(); im.save(b,'PNG');"
    "print(len(base64.b64encode(b.getvalue())))"
)


def summarize(samples_ms: list[float]) -> dict:
    ordered = sorted(samples_ms)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.9) - 1))
    return {
        "n": len(ordered),
        "min": round(ordered[0], 1),
        "median": round(statistics.median(ordered), 1),
        "p90": round(ordered[index], 1),
        "max": round(ordered[-1], 1),
    }


def spawn_once(code: str) -> tuple[float, str]:
    started = time.perf_counter()
    proc = subprocess.run([PYTHON, "-c", code], capture_output=True, text=True, timeout=120)
    elapsed = (time.perf_counter() - started) * 1000.0
    return elapsed, (proc.stdout or proc.stderr or "").strip()[-120:]


def cold_start(n: int = 12) -> dict:
    cases = {
        "bare": "pass",
        "pillow": "from PIL import ImageGrab",
        "atspi": ("import gi; gi.require_version('Atspi','2.0');"
                  " from gi.repository import Atspi; Atspi.init()"),
        "full_grab": FULL_GRAB_SNIPPET,
    }
    out = {}
    for name, code in cases.items():
        samples = []
        note = ""
        for _ in range(n):
            elapsed, note = spawn_once(code)
            samples.append(elapsed)
        out[name] = {**summarize(samples), "lastOutput": note}
    return out


def capture_latency(n_grab: int = 30, n_crop: int = 12, n_fingerprint: int = 12) -> dict:
    from PIL import ImageGrab

    out: dict = {}

    samples = []
    size = None
    for _ in range(n_grab):
        started = time.perf_counter()
        image = ImageGrab.grab()
        samples.append((time.perf_counter() - started) * 1000.0)
        size = image.size
    out["full_grab"] = {**summarize(samples), "size": f"{size[0]}x{size[1]}"}

    samples = []
    payload_kb = 0
    for _ in range(n_crop):
        started = time.perf_counter()
        image = ImageGrab.grab().convert("RGB")
        width, height = image.size
        crop = image.crop((100, 100, 1100, 740))          # 1000x640 = 640,000 px exactly
        scale = (BUDGET / float(crop.size[0] * crop.size[1])) ** 0.5
        if scale < 1.0:
            crop = crop.resize((max(1, int(crop.size[0] * scale)),
                                max(1, int(crop.size[1] * scale))))
        import base64
        import io
        buffer = io.BytesIO()
        crop.save(buffer, "PNG")
        payload = base64.b64encode(buffer.getvalue())
        payload_kb = round(len(payload) / 1024.0, 1)
        samples.append((time.perf_counter() - started) * 1000.0)
    out["crop_640k_png_base64"] = {**summarize(samples), "payloadKB": payload_kb}

    samples = []
    for _ in range(n_fingerprint):
        started = time.perf_counter()
        ImageGrab.grab().convert("L").resize((64, 40)).tobytes()
        samples.append((time.perf_counter() - started) * 1000.0)
    out["fingerprint_64x40"] = summarize(samples)
    return out


def atspi_coverage(max_depth: int = 8, limit: int = 300) -> list[dict]:
    atspi.init()
    results = []
    for app in atspi.applications():
        if app["childCount"] <= 0:
            results.append({"app": app["name"], "found": True, "nodeCount": 0,
                            "actionableCount": 0, "note": "registered with no children"})
            continue
        try:
            results.append(atspi.measure(app["name"], limit=limit, rounds=3,
                                         interval=0.22, max_depth=max_depth))
        except Exception as exc:  # a broken app must not abort the sweep
            results.append({"app": app["name"], "found": True,
                            "error": f"{type(exc).__name__}: {exc}"})
    return results


def main() -> int:
    report: dict = {"startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"), "python": PYTHON}

    print("=" * 68)
    print("1. sidecar cold start (spawn python + do the work, ms)")
    print("=" * 68)
    report["coldStart"] = cold_start()
    for name, stats in report["coldStart"].items():
        print(f"  {name:11} median={stats['median']:7.1f}  p90={stats['p90']:7.1f}  "
              f"min={stats['min']:7.1f}  max={stats['max']:7.1f}   {stats['lastOutput'][:40]}")

    print()
    print("=" * 68)
    print("2. capture latency in-process (ms)")
    print("=" * 68)
    report["capture"] = capture_latency()
    for name, stats in report["capture"].items():
        extra = " ".join(f"{k}={v}" for k, v in stats.items()
                         if k not in ("n", "min", "median", "p90", "max"))
        print(f"  {name:22} median={stats['median']:7.1f}  p90={stats['p90']:7.1f}  "
              f"max={stats['max']:7.1f}  {extra}")

    print()
    print("=" * 68)
    print("3. AT-SPI coverage of running applications (a11y currently OFF)")
    print("=" * 68)
    report["atspi"] = atspi_coverage()
    for entry in report["atspi"]:
        if entry.get("nodeCount", 0) == 0 and "note" not in entry:
            print(f"  {entry['app']:26} (no data)")
            continue
        print(f"  {entry['app']:26} nodes={entry.get('nodeCount', 0):5} "
              f"named={entry.get('namedCount', 0):5} actionable={entry.get('actionableCount', 0):4} "
              f"stable={entry.get('stable')} rounds={entry.get('rounds')} "
              f"{entry.get('elapsedMs', '')}ms {entry.get('note', '')}")
        for item in (entry.get("actionable") or [])[:6]:
            print(f"        - {item['role']:16} {item['name'][:34]!r:36} {item['actions']}")

    path = OUT_DIR / "safe.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"raw data -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
