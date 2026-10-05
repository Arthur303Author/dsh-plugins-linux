"""Key names and modifier combinations, resolved against the live X keymap.

The DSL is deliberately the one the reference implementation exposed, because it
is what a model reliably produces: a modifier may prefix a key with "+", and a
bare character, digit, function key or well-known name is accepted.

    ["esc"]            ["ctrl+z"]        ["f3", "enter"]
    ["ctrl+shift+t"]   ["alt+F4"]        ["pagedown"]
"""

from __future__ import annotations

MODIFIER_KEYSYMS = {
    "ctrl": "Control_L",
    "control": "Control_L",
    "shift": "Shift_L",
    "alt": "Alt_L",
    "super": "Super_L",
    "win": "Super_L",
    "meta": "Super_L",
}

NAMED_KEYSYMS = {
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "enter": "Return",
    "return": "Return",
    "space": "space",
    "spacebar": "space",
    "backspace": "BackSpace",
    "bksp": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "ins": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pgup": "Prior",
    "pagedown": "Next",
    "pgdn": "Next",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "print": "Print",
    "printscreen": "Print",
    "menu": "Menu",
    "pause": "Pause",
    "capslock": "Caps_Lock",
    "minus": "minus",
    "equal": "equal",
    "comma": "comma",
    "period": "period",
    "slash": "slash",
    "backslash": "backslash",
    "semicolon": "semicolon",
    "apostrophe": "apostrophe",
    "grave": "grave",
    "bracketleft": "bracketleft",
    "bracketright": "bracketright",
}

# F1..F24 are spelled exactly that way in the X keysym table.
for _n in range(1, 25):
    NAMED_KEYSYMS[f"f{_n}"] = f"F{_n}"

USAGE = (
    'a key combination is one or more modifiers joined by "+" and one key, '
    'e.g. "esc", "ctrl+z", "ctrl+shift+t", "f3", "pagedown"'
)


def parse_combo(combo) -> tuple[list[str], str]:
    """`"ctrl+shift+t"` -> `(["Control_L", "Shift_L"], "t")`.

    Raises ValueError with a model-readable reason for anything unusable.
    """
    if not isinstance(combo, str) or not combo.strip():
        raise ValueError(f"each key combination must be a non-empty string; {USAGE}")
    parts = [part.strip() for part in combo.strip().split("+") if part.strip()]
    if len(parts) == 0:
        raise ValueError(f"{combo!r} contains no key; {USAGE}")

    modifiers: list[str] = []
    for part in parts[:-1]:
        keysym = MODIFIER_KEYSYMS.get(part.lower())
        if keysym is None:
            known = ", ".join(sorted(MODIFIER_KEYSYMS))
            raise ValueError(
                f"{part!r} in {combo!r} is not a modifier; modifiers are {known}",
            )
        modifiers.append(keysym)

    name = parts[-1]
    keysym_name = NAMED_KEYSYMS.get(name.lower(), name)
    return modifiers, keysym_name
