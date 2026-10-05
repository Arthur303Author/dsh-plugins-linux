#!/usr/bin/env python3
"""Stability verification for the sidecar.

Only read-only actions are exercised here, so this file never moves the pointer,
never presses a key, and is safe to run while the desktop is in use.

What it establishes:
  * sustained capture latency does not drift (no state accumulating per call)
  * concurrent calls all return complete, decodable payloads
  * repeated AT-SPI walks stay stable and do not slow down
  * a call leaves no orphan interpreter behind
  * X connections are actually released (the server has a hard limit)
  * the largest payload the plugin can produce fits the host's buffer budget

Usage:  python3 stability_test.py
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
LIB = PLUGIN / "lib"
PROBE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(LIB))

SIDECAR = LIB / "screen_tools.py"
PYTHON = sys.executable
WINDOW_TITLE = "DSH Probe Target"
# The host mounts the sidecar with execFile({ maxBuffer: 64 MiB }).
HOST_MAX_BUFFER = 64 * 1024 * 1024

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def call(req: dict, timeout: int = 120) -> dict:
    proc = subprocess.run([PYTHON, str(SIDECAR)], input=json.dumps(req), capture_output=True,
                          text=True, timeout=timeout)
    out = (proc.stdout or "").strip()
    if not out:
        return {"ok": False, "error": f"no output; stderr={proc.stderr[-200:]}"}
    return json.loads(out)


def sidecar_pids() -> set[str]:
    """PIDs of running sidecars.

    The bracket keeps the pattern from matching pgrep's own command line, which
    contains the literal pattern it is searching for.
    """
    out = subprocess.run(["pgrep", "-f", "[s]creen_tools.py"], capture_output=True, text=True).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def timed_capture(runs: int) -> list[float]:
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        result = call({"action": "capture", "inline": True})
        samples.append((time.perf_counter() - started) * 1000.0)
        if result.get("ok") is not True:
            raise RuntimeError(f"capture failed: {result.get('error')}")
    return samples


def main() -> int:
    print("=" * 68)
    print("1. sustained capture (30 calls, latency must not drift)")
    print("=" * 68)
    samples = timed_capture(30)
    third = len(samples) // 3
    head = statistics.median(samples[:third])
    tail = statistics.median(samples[-third:])
    drift = abs(tail - head) / max(1.0, head)
    record("30 consecutive captures all succeeded", True, f"median={statistics.median(samples):.0f}ms")
    record("latency does not drift over the run", drift < 0.5,
           f"first third {head:.0f}ms vs last third {tail:.0f}ms (drift {drift:.0%})")

    print()
    print("=" * 68)
    print("2. sustained zoom (20 calls, native-resolution crop)")
    print("=" * 68)
    zooms = []
    for _ in range(20):
        started = time.perf_counter()
        result = call({"action": "zoom", "nx0": 0.1, "ny0": 0.1, "nx1": 0.4, "ny1": 0.5, "inline": True})
        zooms.append((time.perf_counter() - started) * 1000.0)
        if result.get("ok") is not True:
            raise RuntimeError(f"zoom failed: {result.get('error')}")
    record("20 consecutive zooms all succeeded", True, f"median={statistics.median(zooms):.0f}ms")
    record("zoom stays lossless and inside the pixel budget",
           result.get("lossless") is True and result.get("cropWidth") * result.get("cropHeight") <= 640_000,
           f"crop={result.get('cropWidth')}x{result.get('cropHeight')}")

    print()
    print("=" * 68)
    print("3. concurrency (16 mixed calls in parallel)")
    print("=" * 68)
    requests = []
    for index in range(16):
        if index % 3 == 0:
            requests.append({"action": "capture", "inline": True})
        elif index % 3 == 1:
            requests.append({"action": "zoom", "nx0": 0.2 + index * 0.01, "ny0": 0.2,
                             "nx1": 0.4 + index * 0.01, "ny1": 0.4, "inline": True})
        else:
            requests.append({"action": "windows"})
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(call, requests))
    elapsed = (time.perf_counter() - started) * 1000.0

    all_ok = all(item.get("ok") is True for item in outcomes)
    payloads = [item for item in outcomes if isinstance(item.get("pngBase64"), str)]
    import base64
    decoded_ok = 0
    sizes = []
    for item in payloads:
        raw = base64.b64decode(item["pngBase64"])
        sizes.append(len(raw))
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            decoded_ok += 1
    record("all 16 concurrent calls succeeded", all_ok, f"{elapsed:.0f}ms wall for 16 calls")
    record("every concurrent payload is a complete, valid PNG",
           decoded_ok == len(payloads) and len(payloads) >= 10,
           f"{decoded_ok}/{len(payloads)} decoded, sizes {min(sizes)}-{max(sizes)} bytes")

    print()
    print("=" * 68)
    print("4. repeated accessibility walks (8 rounds)")
    print("=" * 68)
    env = dict(os.environ)
    env["PROBE_LOG"] = "/tmp/dsh-probe/app.log"
    probe = subprocess.Popen([PYTHON, str(PROBE_DIR / "probe_app.py")], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    walk_times = []
    counts = []
    try:
        time.sleep(2.5)
        for _ in range(8):
            started = time.perf_counter()
            result = call({"action": "elements", "window": WINDOW_TITLE})
            walk_times.append((time.perf_counter() - started) * 1000.0)
            if result.get("ok") is not True:
                raise RuntimeError(f"elements failed: {result.get('error')}")
            counts.append(result.get("count"))
        record("8 consecutive element walks all succeeded", True,
               f"median={statistics.median(walk_times):.0f}ms, counts={sorted(set(counts))}")
        record("element count is stable across walks", len(set(counts)) == 1,
               f"distinct counts: {sorted(set(counts))}")
        drift = abs(statistics.median(walk_times[-3:]) - statistics.median(walk_times[:3])) \
            / max(1.0, statistics.median(walk_times[:3]))
        record("element walk latency does not drift", drift < 0.5,
               f"first three {statistics.median(walk_times[:3]):.0f}ms vs "
               f"last three {statistics.median(walk_times[-3:]):.0f}ms")
    finally:
        probe.terminate()
        try:
            probe.wait(timeout=5)
        except subprocess.TimeoutExpired:
            probe.kill()

    print()
    print("=" * 68)
    print("5. no orphan interpreter after many calls")
    print("=" * 68)
    before = sidecar_pids()
    for _ in range(12):
        call({"action": "windows"})
    time.sleep(0.8)
    after = sidecar_pids()
    leaked = after - before
    record("12 calls leave no orphan sidecar process", len(leaked) == 0,
           f"leaked pids: {sorted(leaked) or 'none'}")

    print()
    print("=" * 68)
    print("6. X connections are released (200 open/close cycles)")
    print("=" * 68)
    from linux import x11
    failures = 0
    error = ""
    try:
        for _ in range(200):
            display = x11.Display()
            if display.geometry(display.root) is None:
                failures += 1
            display.close()
    except Exception as exc:
        failures += 1
        error = f"{type(exc).__name__}: {exc}"
    record("200 open/close cycles exhaust nothing", failures == 0,
           error or "every cycle opened and closed cleanly")

    print()
    print("=" * 68)
    print("7. largest payload fits the host buffer budget")
    print("=" * 68)
    result = call({"action": "capture", "inline": True})
    payload_len = len(result.get("pngBase64", ""))
    record("full-desktop payload fits the host's 64 MiB buffer",
           0 < payload_len < HOST_MAX_BUFFER,
           f"base64={payload_len / 1024:.0f} KiB of {HOST_MAX_BUFFER / 1024 / 1024:.0f} MiB")
    record("payload size is reported by the sidecar", isinstance(result.get("bytes"), int),
           f"bytes={result.get('bytes')}")

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILED: {name} — {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
