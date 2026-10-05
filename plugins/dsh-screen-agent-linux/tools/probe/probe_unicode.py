#!/usr/bin/env python3
"""P0 probe — how non-ASCII text actually reaches an application.

The keyboard test showed the event ARRIVES (the window logged keyval U+4F60,
U+597D) while the entry stayed empty. So the question is not "can XTest send
CJK" but "who swallows it after it arrives". The main suspect is the input
method: this desktop runs fcitx5 and every GTK process inherits
GTK_IM_MODULE=fcitx.

The matrix below separates the two variables (input method on/off, synthesized
keys versus the accessibility text interface) inside a window this probe starts
itself.
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
from probe_inject import LOG_PATH, PYTHON, WINDOW_TITLE, type_text, wait_for_window  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
OUT_DIR.mkdir(exist_ok=True)


def records() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    out = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def run_case(label: str, im_module: str | None, method: str, text: str) -> dict:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["PROBE_LOG"] = str(LOG_PATH)
    if im_module is None:
        env.pop("GTK_IM_MODULE", None)
    else:
        env["GTK_IM_MODULE"] = im_module

    proc = subprocess.Popen([PYTHON, str(Path(__file__).parent / "probe_app.py")], env=env)
    display = x11.Display()
    result = {"case": label, "imModule": im_module or "(unset)", "method": method, "text": text}
    try:
        window = wait_for_window(display, WINDOW_TITLE, 15)
        if window is None:
            result["error"] = "probe window never appeared"
            return result
        time.sleep(1.0)
        result["focus"] = x11.request_focus(display, window, 2.0).get("ok")

        if method == "xtest":
            result["inject"] = type_text(display, text)
        elif method == "atspi_insert":
            atspi.init()
            result["inject"] = atspi.perform(pid=proc.pid, action="insert_text",
                                             role="text", value=text)
        elif method == "atspi_set":
            atspi.init()
            result["inject"] = atspi.perform(pid=proc.pid, action="set_value",
                                             role="text", value=text)
        else:
            result["error"] = f"unknown method {method}"
            return result

        time.sleep(0.7)
        entries = records()
        result["keyPresses"] = [r.get("keyval") for r in entries if r.get("kind") == "KEY_PRESS"]
        result["entryText"] = "".join(r.get("text", "") for r in entries
                                      if r.get("kind") == "TEXT_CHANGED")
        result["ok"] = text in result["entryText"]
        return result
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        display.close()


def main() -> int:
    cases = [
        ("A fcitx  + synthesized keys", None, "xtest", "你好"),
        ("B simple + synthesized keys", "gtk-im-context-simple", "xtest", "你好"),
        ("C fcitx  + atspi insert", None, "atspi_insert", "你好"),
        ("D fcitx  + atspi set", None, "atspi_set", "你好"),
        ("E simple + atspi insert", "gtk-im-context-simple", "atspi_insert", "你好"),
        ("F fcitx  + atspi insert (ascii)", None, "atspi_insert", "abc"),
        ("G fcitx  + atspi insert (emoji)", None, "atspi_insert", "🐳"),
    ]
    results = []
    for label, im_module, method, text in cases:
        outcome = run_case(label, im_module, method, text)
        results.append(outcome)
        print(f"{label:34} method={method:13} im={outcome['imModule']:20} "
              f"keyPresses={outcome.get('keyPresses')} "
              f"entry={outcome.get('entryText', '')!r:14} {'PASS' if outcome.get('ok') else 'FAIL'}")

    print()
    print("结论线索:")
    a = next(r for r in results if r["case"].startswith("A"))
    b = next(r for r in results if r["case"].startswith("B"))
    c = next(r for r in results if r["case"].startswith("C"))
    if not a.get("ok") and b.get("ok"):
        print("  -> 输入法(fcitx) 是吞掉合成按键的原因：关掉 IM 后同样的注入就成功")
    elif not a.get("ok") and not b.get("ok"):
        print("  -> 不是输入法：即使 GTK_IM_MODULE=simple，合成键也没能变成文本")
    if c.get("ok"):
        print("  -> AT-SPI insert_text 绕过键盘事件，直接写进控件：非 ASCII 的可靠路线")

    path = OUT_DIR / "unicode.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nraw data -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
