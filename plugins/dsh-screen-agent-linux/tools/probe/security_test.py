#!/usr/bin/env python3
"""Security verification for the sidecar.

The threat this file is about is not a remote attacker -- it is the agent
itself. This plugin hands a model a real pointer, a real keyboard, and read
access to every window's accessibility tree, on the same desktop that is driving
the model. So the questions worth answering are:

  * can it act on the window that is driving it?  (the guard)
  * is the guard applied at EVERY entry point, not just the obvious one?
  * is the guard configurable, and does the default fail safe?
  * does a refused action leave the desktop untouched?
  * can text cross into a command, or a request write a file?
  * are the resource limits real?

Every dangerous case is aimed at a throwaway window this script starts, and the
guard marker is pinned to that window's own title -- so a guard failure costs a
probe window rather than the browser running this session.

Usage:  python3 security_test.py
"""

from __future__ import annotations

import json
import os
import re
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
WINDOW_TITLE = "DSH Probe Target"
AUDIT = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")) / "screen-agent" / "audit.jsonl"
PWNED = Path("/tmp/dsh-screen-pwned")

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def call(req: dict, env_extra: dict | None = None, timeout: int = 120) -> dict:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run([PYTHON, str(SIDECAR)], input=json.dumps(req), capture_output=True,
                          text=True, env=env, timeout=timeout)
    out = (proc.stdout or "").strip()
    if not out:
        return {"ok": False, "error": f"no output; stderr={proc.stderr[-200:]}"}
    return json.loads(out)


def refused(result: dict) -> bool:
    return result.get("ok") is False and "protected" in str(result.get("error", ""))


def kill_stray_probes() -> None:
    me = os.getpid()
    listing = subprocess.run(["pgrep", "-af", "python3"], capture_output=True, text=True).stdout
    for line in listing.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) != me:
            if "probe_app" in parts[1] and "screen_tools" not in parts[1] and "security_" not in parts[1]:
                try:
                    os.kill(int(parts[0]), signal.SIGTERM)
                except Exception:
                    pass


def tool_parameter_blocks(source: str) -> dict[str, str]:
    """Each tool's `parameters` object, extracted by brace matching.

    A plain "does the file mention path?" check is useless here: the OUTPUT
    schema legitimately carries a `path` (the on-disk fallback when no image can
    reach model context). Only the INPUT schema matters for this question, so the
    block has to be isolated.
    """
    blocks: dict[str, str] = {}
    for match in re.finditer(r"name: '(screen_[a-z_]+)'", source):
        name = match.group(1)
        start = source.find("parameters: {", match.end())
        if start < 0:
            continue
        opening = source.index("{", start)
        depth = 0
        for position in range(opening, len(source)):
            if source[position] == "{":
                depth += 1
            elif source[position] == "}":
                depth -= 1
                if depth == 0:
                    blocks[name] = source[opening:position + 1]
                    break
    return blocks


def main() -> int:
    kill_stray_probes()
    time.sleep(0.8)
    if PWNED.exists():
        PWNED.unlink()

    LOG = Path("/tmp/dsh-probe/app.log")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["PROBE_LOG"] = str(LOG)
    probe = subprocess.Popen([PYTHON, str(PROBE_DIR / "probe_app.py")], env=env,
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
        cx = (geometry[0] + geometry[2] // 2) / (display.geometry(display.root)[2] - 1)
        cy = (geometry[1] + geometry[3] // 2) / (display.geometry(display.root)[3] - 1)
        guard = {"DSH_SCREEN_AGENT_PROTECT": WINDOW_TITLE}
        print(f"probe window {hex(window)} at {geometry}")

        # ---- 1. every entry point honours the guard --------------------
        print("\n1. guard coverage at every entry point")
        x11.request_focus(display, window, timeout=3.0)
        time.sleep(0.4)
        entries = [
            ("click", {"action": "click", "nx": cx, "ny": cy}),
            ("key", {"action": "key", "keys": ["esc"]}),
            ("type", {"action": "type", "text": "x"}),
            ("elements", {"action": "elements", "window": WINDOW_TITLE}),
            ("act", {"action": "act", "window": WINDOW_TITLE, "elementAction": "invoke",
                     "name": "PROBE_BUTTON", "role": "push button"}),
            ("window", {"action": "window", "window": WINDOW_TITLE}),
        ]
        for label, request in entries:
            result = call(request, guard)
            record(f"{label} refuses a protected window", refused(result),
                   str(result.get("error", ""))[:58])

        # ---- 2. a refused action leaves no trace ----------------------
        print("\n2. refusal happens before any side effect")
        before = len(LOG.read_text(encoding="utf-8").splitlines())
        for _label, request in entries:
            call(request, guard)
        time.sleep(0.4)
        after = len(LOG.read_text(encoding="utf-8").splitlines())
        record("six refused calls produced zero window events", after == before,
               f"log lines {before} -> {after}")

        # ---- 3. the guard is configurable, and matches what it is told --
        print("\n3. guard matching")
        result = call({"action": "elements", "window": WINDOW_TITLE},
                      {"DSH_SCREEN_AGENT_PROTECT": "no-such-window-title"})
        record("an unrelated marker does not block an innocent window",
               result.get("ok") is True, f"count={result.get('count')}")
        result = call({"action": "elements", "window": WINDOW_TITLE},
                      {"DSH_SCREEN_AGENT_PROTECT": ""})
        record('an empty marker disables the guard (explicit opt-out)',
               result.get("ok") is True, f"count={result.get('count')}")
        result = call({"action": "elements", "window": "deepseek harness"})
        record("the default marker blocks the harness window without configuration",
               result.get("ok") is False and "protected" in str(result.get("error", "")),
               str(result.get("error", ""))[:58])

        # ---- 4. text cannot become a command --------------------------
        print("\n4. injection surface")
        x11.request_focus(display, window, timeout=3.0)
        time.sleep(0.3)
        hostile = "$(touch " + str(PWNED) + "); `touch " + str(PWNED) + "`; && touch " + str(PWNED)
        result = call({"action": "type", "text": hostile})
        time.sleep(0.6)
        record("shell metacharacters in text are typed, never executed",
               result.get("ok") is True and not PWNED.exists(),
               f"type={result.get('delivery')} pwned={PWNED.exists()}")

        # ---- 5. a request cannot write a file the host did not ask for --
        print("\n5. file-write surface")
        source = (PLUGIN / "lib/index.js").read_text(encoding="utf-8")
        request_builders = re.findall(r"request\.(\w+)\s*=", source)
        record("the host half never forwards an `out` path to the sidecar",
               "out" not in request_builders,
               f"forwarded keys: {sorted(set(request_builders))}")

        blocks = tool_parameter_blocks(source)
        offenders = sorted(name for name, block in blocks.items()
                           if re.search(r"\b(out|path)\s*:", block))
        record("no tool's INPUT schema exposes a filesystem path",
               len(blocks) >= 11 and not offenders,
               f"{len(blocks)} tools parsed; offenders: {offenders or 'none'}")

        # ---- 6. resource limits are real ------------------------------
        print("\n6. resource limits")
        limits = [
            ("clicks above the cap", {"action": "click", "clicks": 11}),
            ("clicks below the cap", {"action": "click", "clicks": 0}),
            ("holdMs above the cap", {"action": "click", "holdMs": 5001}),
            ("text above the cap", {"action": "type", "text": "x" * 20_001}),
            ("NaN coordinate", {"action": "click", "nx": float("nan"), "ny": 0.5}),
            ("negative coordinate", {"action": "click", "nx": -0.5, "ny": 0.5}),
            ("coordinate above one", {"action": "click", "nx": 1.5, "ny": 0.5}),
            ("too many key combinations", {"action": "key", "keys": ["esc"] * 21}),
            ("region inverted", {"action": "zoom", "nx0": 0.6, "ny0": 0.1, "nx1": 0.2, "ny1": 0.5}),
        ]
        for label, request in limits:
            payload = json.dumps(request).replace("NaN", "1e999")   # JSON has no NaN
            proc = subprocess.run([PYTHON, str(SIDECAR)], input=payload, capture_output=True,
                                  text=True, timeout=60)
            try:
                result = json.loads((proc.stdout or "{}").strip() or "{}")
            except json.JSONDecodeError:
                result = {"ok": True, "error": "unparseable"}
            record(f"rejects {label}", result.get("ok") is False,
                   str(result.get("error", ""))[:56])

        # ---- 7. audit trail exists and stays small -------------------
        print("\n7. audit trail")
        before_lines = AUDIT.read_text(encoding="utf-8").splitlines() if AUDIT.exists() else []
        call({"action": "move", "nx": cx, "ny": cy})
        call({"action": "click", "nx": cx, "ny": cy})
        time.sleep(0.3)
        after_lines = AUDIT.read_text(encoding="utf-8").splitlines() if AUDIT.exists() else []
        added = after_lines[len(before_lines):]
        parsed = [json.loads(line) for line in added]
        record("actions are written to the audit log",
               len(parsed) >= 2 and any(item.get("action") == "click" for item in parsed),
               f"{len(parsed)} new entries")
        record("audit entries record intent, not pixels",
               all(len(line) < 600 and "pngBase64" not in line for line in added),
               f"longest new entry {max((len(line) for line in added), default=0)} bytes")

        # ---- 8. a window that vanishes mid-flight ---------------------
        print("\n8. tolerance for a window that disappears")
        marker = WINDOW_TITLE
        probe.terminate()
        try:
            probe.wait(timeout=5)
        except subprocess.TimeoutExpired:
            probe.kill()
        time.sleep(0.6)
        for label, request in [
            ("elements", {"action": "elements", "window": marker}),
            ("act", {"action": "act", "window": marker, "elementAction": "invoke",
                     "name": "PROBE_BUTTON", "role": "push button"}),
            ("window", {"action": "window", "window": marker}),
        ]:
            result = call(request)
            crashed = "no output" in str(result.get("error", ""))
            record(f"{label} on a vanished window reports cleanly, no crash",
                   result.get("ok") is False and not crashed,
                   str(result.get("error", ""))[:56])

    finally:
        display.inject_motion(original_pointer[0], original_pointer[1])
        display.sync()
        if original_active:
            x11.request_focus(display, original_active, timeout=2.0)
        if probe.poll() is None:
            probe.terminate()
            try:
                probe.wait(timeout=5)
            except subprocess.TimeoutExpired:
                probe.kill()
        display.close()
        if PWNED.exists():
            PWNED.unlink()

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILED: {name} — {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
