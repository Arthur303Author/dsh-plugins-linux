"""AT-SPI element access for the probe (prototype of the plugin's element layer).

Mirrors what the reference implementation gets from Windows UI Automation:

  role          <- get_role_name()
  name          <- get_name()
  automationId  <- get_accessible_id()   (often empty on Linux)
  actions       <- Action interface: get_n_actions()/get_action_name(i)
  states        <- get_state_set().contains(...)
  bounds        <- Component.get_extents(CoordType.SCREEN)

Measured facts that drive the design:

  * An accessibility tree is built lazily, so a snapshot samples until two
    consecutive rounds agree and reports whether it stabilized.
  * Probing the Action interface is the expensive part (one D-Bus round trip per
    element), so a cheap pre-filter runs first and only survivors are probed.
  * An application's AT-SPI name is its PROGRAM name (`gnome-calculator`,
    `probe_app.py`), never its window title. Matching on a title silently finds
    nothing -- measured, and the reason `resolve_app` exists.
"""

from __future__ import annotations

import hashlib
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

# States worth reporting: they answer "can I act on this, and what is it now".
REPORTED_STATES = (
    "ENABLED", "VISIBLE", "SHOWING", "FOCUSED", "SENSITIVE",
    "CHECKED", "CHECKABLE", "EXPANDED", "EXPANDABLE", "COLLAPSED",
    "SELECTED", "SELECTABLE", "EDITABLE", "FOCUSABLE", "PRESSED",
)


def init() -> None:
    Atspi.init()


def _safe(call, fallback=None):
    try:
        return call()
    except Exception:
        return fallback


# --------------------------------------------------------------------------
# Describing one node
# --------------------------------------------------------------------------


def _states(node) -> list[str]:
    state_set = _safe(node.get_state_set)
    if state_set is None:
        return []
    out = []
    for name in REPORTED_STATES:
        try:
            if state_set.contains(getattr(Atspi.StateType, name)):
                out.append(name.lower())
        except Exception:
            continue
    return out


def _actions(node) -> list[str]:
    iface = _safe(node.get_action_iface)
    if iface is None:
        return []
    out = []
    count = _safe(iface.get_n_actions, 0) or 0
    for index in range(count):
        name = _safe(lambda i=index: iface.get_action_name(i))
        if name:
            out.append(name)
    return out


def _rect(node):
    iface = _safe(node.get_component_iface)
    if iface is None:
        return None
    extents = _safe(lambda: iface.get_extents(Atspi.CoordType.SCREEN))
    if extents is None:
        return None
    try:
        if extents.width <= 1 or extents.height <= 1:
            return None
        return [int(extents.x), int(extents.y), int(extents.width), int(extents.height)]
    except Exception:
        return None


def describe(node, include_actions: bool = True) -> dict:
    role = _safe(node.get_role_name, "?") or "?"
    entry = {
        "role": role,
        "name": _safe(node.get_name, "") or "",
        "id": _safe(node.get_accessible_id, "") or "",
        "states": _states(node),
    }
    if include_actions:
        entry["actions"] = _actions(node)
        entry["rect"] = _rect(node)
    return entry


def walk(node, depth: int, budget: list[int], out: list[dict], path: str = "0",
         include_actions: bool = True, max_depth: int = 12) -> None:
    if budget[0] <= 0 or depth > max_depth:
        return
    count = _safe(node.get_child_count, 0) or 0
    for index in range(count):
        if budget[0] <= 0:
            return
        budget[0] -= 1
        child = _safe(lambda i=index: node.get_child_at_index(i))
        if child is None:
            continue
        child_path = f"{path}/{index}"
        entry = describe(child, include_actions=include_actions)
        entry["path"] = child_path
        entry["depth"] = depth + 1
        out.append(entry)
        walk(child, depth + 1, budget, out, child_path, include_actions, max_depth)


# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------


def applications() -> list[dict]:
    desktop = Atspi.get_desktop(0)
    out = []
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        if app is None:
            continue
        out.append({
            "index": index,
            "name": _safe(app.get_name, "") or "",
            "childCount": _safe(app.get_child_count, 0) or 0,
            "pid": _safe(app.get_process_id, 0),
            "node": app,
        })
    return out


def resolve_app(app_name: str | None = None, pid: int | None = None) -> tuple[dict | None, list[dict]]:
    """Find one AT-SPI application: pid is exact, name is exact then substring.

    Matching by pid is what the plugin uses for an application it launched
    itself, because a program name may be ambiguous or localized.
    """
    apps = applications()
    if pid is not None:
        for app in apps:
            if app["pid"] == pid:
                return app, apps
    if app_name is not None:
        for app in apps:
            if app["name"] == app_name:
                return app, apps
        needle = app_name.lower()
        for app in apps:
            if needle in app["name"].lower():
                return app, apps
    return None, apps


def snapshot_app(app: dict, limit: int = 400, max_depth: int = 12,
                 include_actions: bool = True) -> list[dict]:
    out: list[dict] = []
    walk(app["node"], 0, [limit], out, path=str(app["index"]),
         include_actions=include_actions, max_depth=max_depth)
    return out


def fingerprint(entries: list[dict]) -> str:
    """Identity-only digest: role + name + id, per node, in tree order."""
    digest = hashlib.md5()
    for entry in entries:
        digest.update(entry.get("role", "").encode("utf-8", "replace"))
        digest.update(b"\x01")
        digest.update(entry.get("name", "").encode("utf-8", "replace"))
        digest.update(b"\x01")
        digest.update(entry.get("id", "").encode("utf-8", "replace"))
        digest.update(b"\x02")
    return digest.hexdigest()


def measure(app_name: str | None = None, pid: int | None = None, limit: int = 400,
            rounds: int = 3, interval: float = 0.22, max_depth: int = 12) -> dict:
    """Sample one application until two consecutive rounds agree."""
    target, apps = resolve_app(app_name, pid)
    if target is None:
        return {"app": app_name or f"pid:{pid}", "found": False,
                "available": [{"name": a["name"], "pid": a["pid"], "children": a["childCount"]}
                              for a in apps]}

    started = time.time()
    previous = None
    stable = False
    used_rounds = 0
    for round_index in range(1, rounds + 1):
        used_rounds = round_index
        cheap = snapshot_app(target, limit=limit, max_depth=max_depth, include_actions=False)
        current = fingerprint(cheap)
        if previous is not None and current == previous:
            stable = True
            break
        previous = current
        if round_index < rounds:
            time.sleep(interval)

    # The expensive Action probing happens once, on the settled tree.
    entries = snapshot_app(target, limit=limit, max_depth=max_depth, include_actions=True)
    elapsed = int((time.time() - started) * 1000)

    actionable = [e for e in entries if e.get("actions")]
    named = [e for e in entries if e.get("name")]
    return {
        "app": target["name"],
        "found": True,
        "pid": target["pid"],
        "nodeCount": len(entries),
        "namedCount": len(named),
        "actionableCount": len(actionable),
        "roles": _histogram(entries, "role"),
        "actionNames": _action_histogram(entries),
        "actionable": [
            {"role": e["role"], "name": e["name"], "id": e["id"],
             "actions": e["actions"], "states": e["states"], "rect": e.get("rect")}
            for e in actionable[:40]
        ],
        "stable": stable,
        "rounds": used_rounds,
        "elapsedMs": elapsed,
    }


def _histogram(entries: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for entry in entries:
        out[entry[key]] = out.get(entry[key], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1])[:20])


def _action_histogram(entries: list[dict]) -> dict:
    out: dict[str, int] = {}
    for entry in entries:
        for action in entry.get("actions") or []:
            out[action] = out.get(action, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------
# Acting on an element
# --------------------------------------------------------------------------


def find(app_name: str | None = None, role: str | None = None, name: str | None = None,
         occurrence: int = 1, max_depth: int = 12, limit: int = 600,
         pid: int | None = None):
    """Re-find an element by what it IS.

    Deliberately not a persistent handle. Every sidecar call is a fresh process,
    so an element object cannot survive a snapshot anyway -- and the side effect
    is the good one: when the UI changes, the element is reported MISSING rather
    than acted on through a stale reference.
    """
    app, _apps = resolve_app(app_name, pid)
    if app is None:
        return None, {"role": role, "name": name, "found": 0,
                      "error": f"no AT-SPI application for {app_name!r} / pid {pid}"}

    matches: list = []
    visited = [0]

    def visit(node, depth: int, path: str):
        if depth > max_depth or visited[0] >= limit:
            return
        for index in range(_safe(node.get_child_count, 0) or 0):
            if visited[0] >= limit:
                return
            child = _safe(lambda i=index: node.get_child_at_index(i))
            if child is None:
                continue
            visited[0] += 1
            child_path = f"{path}/{index}"
            info = describe(child, include_actions=False)
            if (role is None or info["role"] == role) and (name is None or info["name"] == name):
                matches.append((info, child, child_path))
            visit(child, depth + 1, child_path)

    visit(app["node"], 0, str(app["index"]))

    if not matches:
        return None, {"role": role, "name": name, "found": 0, "app": app["name"],
                      "visited": visited[0]}
    if occurrence > len(matches):
        return None, {"role": role, "name": name, "found": len(matches), "app": app["name"],
                      "error": f"only {len(matches)} match(es); occurrence {occurrence} is out of range"}
    info, node, path = matches[occurrence - 1]
    return (info, node, path), {"role": role, "name": name, "found": len(matches),
                                "app": app["name"], "path": path, "actions": _actions(node)}


_DEFAULT_ACTION = {
    "invoke": "click",
    "click": "click",
    "press": "press",
    "toggle": "toggle",
    "expand": "expand",
    "collapse": "collapse",
    "select": "select",
    "activate": "activate",
}


def _default_action(action: str) -> str:
    return _DEFAULT_ACTION.get(action.lower(), action)


def perform(app_name: str | None = None, action: str = "invoke", role: str | None = None,
            name: str | None = None, occurrence: int = 1, value: str | None = None,
            action_name: str | None = None, pid: int | None = None) -> dict:
    """Drive one element through its own accessibility interface.

    No coordinate is measured, no screenshot is read, and the cursor never moves.
    """
    found, meta = find(app_name, role=role, name=name, occurrence=occurrence, pid=pid)
    if found is None:
        return {"ok": False, "error": "element not found", **meta}
    info, node, path = found

    if action == "describe":
        return {"ok": True, "path": path, **describe(node)}

    if action == "focus":
        iface = _safe(node.get_component_iface)
        if iface is None:
            return {"ok": False, "error": "element has no Component interface", **meta}
        ok = _safe(lambda: iface.grab_focus(), False)
        return {"ok": bool(ok), "path": path, "action": action, "actions": _actions(node)}

    if action == "set_value":
        editable = _safe(node.get_editable_text_iface)
        if editable is None:
            return {"ok": False, "error": "element has no EditableText interface", **meta}
        ok = _safe(lambda: editable.set_text_contents(value or ""), False)
        return {"ok": bool(ok), "path": path, "action": action, "value": value}

    if action == "insert_text":
        # Insert at the caret instead of replacing the field. This is the reliable
        # path for non-ASCII text: it goes through the toolkit, so no keyboard
        # event is synthesized and no input method gets a chance to swallow it.
        editable = _safe(node.get_editable_text_iface)
        if editable is None:
            return {"ok": False, "error": "element has no EditableText interface", **meta}
        payload = value or ""
        # `length` reaches gtk_editable_insert_text, which counts BYTES. Passing
        # the character count truncates non-ASCII input mid-sequence and the
        # toolkit drops the malformed tail. Measured: "abc" (3 chars = 3 bytes)
        # inserted fine while "你好" (2 chars, 6 bytes) and "🐳" (1 char, 4 bytes)
        # vanished entirely -- set_text_contents, which takes no length, worked
        # for the same string on the same widget.
        length = len(payload.encode("utf-8"))
        ok = _safe(lambda: editable.insert_text(-1, payload, length), False)
        return {"ok": bool(ok), "path": path, "action": action, "value": value}

    # Everything else goes through the element's own advertised actions.
    iface = _safe(node.get_action_iface)
    if iface is None:
        return {"ok": False, "error": "element has no Action interface", **meta}
    available = _actions(node)
    wanted = action_name or _default_action(action)
    chosen = None
    for index in range(_safe(iface.get_n_actions, 0) or 0):
        label = _safe(lambda i=index: iface.get_action_name(i)) or ""
        if wanted and wanted.lower() in label.lower():
            chosen = index
            break
    if chosen is None and available:
        chosen = 0
        wanted = available[0]
    if chosen is None:
        return {"ok": False, "error": f"element advertises no action (wanted {wanted!r})",
                "available": available, **meta}
    ok = _safe(lambda: iface.do_action(chosen), False)
    return {"ok": bool(ok), "path": path, "action": wanted, "available": available,
            "statesAfter": _states(node)}
