#!/usr/bin/env python3
"""GTK3 probe target.

Every interaction that actually reaches this window is appended to a JSONL log.
That log is the ground truth for the whole probe: an injected click or keystroke
either shows up here or it did not happen, no matter what the X server says.

Environment:
  PROBE_LOG   log path (default /tmp/dsh-probe/app.log)
  PROBE_POS   "x,y" window position (default 40,1180)
  PROBE_SIZE  "w,h" window size (default 480,300)
"""

from __future__ import annotations

import json
import os
import sys
import time

import gi

# Both namespaces must be pinned before the import: `from gi.repository import
# Gdk, Gtk` resolves Gdk first, and an unpinned Gdk loads 4.0, which then
# conflicts with Gtk 3.0.
gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

LOG_PATH = os.environ.get("PROBE_LOG", "/tmp/dsh-probe/app.log")
POS = os.environ.get("PROBE_POS", "40,1180")
SIZE = os.environ.get("PROBE_SIZE", "480,300")
# Two probe windows are only distinguishable in a screenshot if they look
# different; identical twins made an occlusion test read as "diff = 0.00%".
TAG = os.environ.get("PROBE_TAG", "A")
BG = os.environ.get("PROBE_BG", "#2f2f35")


def log(kind: str, **fields) -> None:
    record = {"ts": round(time.time(), 3), "kind": kind}
    record.update(fields)
    with open(LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()


class ProbeWindow:
    def __init__(self) -> None:
        width, height = (int(value) for value in SIZE.split(","))
        x, y = (int(value) for value in POS.split(","))

        self.window = Gtk.Window(title="DSH Probe Target")
        self.window.set_default_size(width, height)
        self.window.move(x, y)

        provider = Gtk.CssProvider()
        provider.load_from_data(f"window {{ background-color: {BG}; }}".encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.window.set_events(
            Gdk.EventMask.KEY_PRESS_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.STRUCTURE_MASK
            | Gdk.EventMask.FOCUS_CHANGE_MASK
        )
        self.window.connect("key-press-event", self.on_key)
        self.window.connect("button-press-event", self.on_button)
        self.window.connect("focus-in-event", lambda *_: log("FOCUS_IN"))
        self.window.connect("focus-out-event", lambda *_: log("FOCUS_OUT"))
        self.window.connect("destroy", Gtk.main_quit)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(12)
        self.window.add(box)

        self.button = Gtk.Button(label="PROBE_BUTTON")
        self.button.connect("clicked", self.on_activated)
        box.pack_start(self.button, False, False, 0)

        self.check = Gtk.CheckButton(label="PROBE_CHECK")
        self.check.connect("toggled", lambda w: log("CHECK_TOGGLED", active=bool(w.get_active())))
        box.pack_start(self.check, False, False, 0)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("PROBE_ENTRY")
        self.entry.connect("changed", lambda w: log("TEXT_CHANGED", text=w.get_text()))
        self.entry.connect("activate", lambda w: log("ENTRY_ACTIVATED", text=w.get_text()))
        box.pack_start(self.entry, False, False, 0)

        self.label = Gtk.Label(label=f"PROBE_LABEL {TAG}")
        box.pack_start(self.label, False, False, 0)

        self.counter = Gtk.Label(label="PROBE_COUNTER 0")
        box.pack_start(self.counter, False, False, 0)

        # Two counters, because they mean different things: a press anywhere in
        # the window (proves where a click landed) versus the button's own
        # activation (proves a click OR an AT-SPI invoke reached it).
        self.presses = 0
        self.activations = 0
        self.window.show_all()
        log("STARTED", pid=os.getpid(), pos=f"{x},{y}", size=f"{width}x{height}")

    def on_key(self, _widget, event) -> bool:
        name = Gdk.keyval_name(event.keyval) or "?"
        log("KEY_PRESS", keyval=name, keycode=int(event.hardware_keycode), state=int(event.state))
        self.label.set_text(f"key={name}")
        return False

    def on_button(self, _widget, event) -> bool:
        self.presses += 1
        log(
            "BUTTON_PRESS",
            button=int(event.button),
            x=int(event.x),
            y=int(event.y),
            x_root=int(event.x_root),
            y_root=int(event.y_root),
            pressIndex=self.presses,
        )
        return False

    def on_activated(self, _widget) -> None:
        """The button's own activation signal.

        Both a real mouse click and an AT-SPI `invoke` land here, so the counter
        shows accessibility actions in the same place as clicks -- which is the
        point of having ground truth at all.
        """
        self.activations += 1
        log("BUTTON_CLICKED", activationIndex=self.activations)
        self.counter.set_text(f"PROBE_COUNTER {self.activations}")


def main() -> int:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    ProbeWindow()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
