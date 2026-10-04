"""Everything the integration keeps in `hass.data` is under a typed `HassKey` constant, never a raw key.

A raw key (`hass.data["junghome_ble_x"]`) is `Any` to the type checker and can collide with another integration's.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

COMPONENT = Path(__file__).parent.parent / "custom_components" / "junghome_ble"
SOURCES = sorted(COMPONENT.rglob("*.py"))
HASS_KEY = re.compile(r"^(\w+): HassKey\[", re.MULTILINE)
DICT_METHODS = {"get", "setdefault", "pop"}


def _is_hass_data(node: ast.expr) -> bool:
    """`hass.data` or `<anything>.hass.data`."""
    if not (isinstance(node, ast.Attribute) and node.attr == "data"):
        return False
    owner = node.value
    return (isinstance(owner, ast.Name) and owner.id == "hass") or (
        isinstance(owner, ast.Attribute) and owner.attr == "hass"
    )


def _keys(tree: ast.AST) -> list[tuple[int, ast.expr]]:
    """Every key `hass.data` is read or written with: subscripts and the dict methods' first argument."""
    keys: list[tuple[int, ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_hass_data(node.value):
            keys.append((node.lineno, node.slice))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in DICT_METHODS
            and _is_hass_data(node.func.value)
            and node.args
        ):
            keys.append((node.lineno, node.args[0]))
    return keys


def test_every_hass_data_key_is_a_hass_key() -> None:
    declared = {
        name for path in SOURCES for name in HASS_KEY.findall(path.read_text("utf-8"))
    }
    assert declared, "no HassKey constant found (pattern out of date?)"
    used: set[str] = set()
    raw: list[str] = []
    for path in SOURCES:
        for line, key in _keys(ast.parse(path.read_text("utf-8"))):
            if isinstance(key, ast.Name) and key.id in declared:
                used.add(key.id)
            else:
                raw.append(f"{path.relative_to(COMPONENT)}:{line} {ast.unparse(key)}")
    assert used, "no hass.data access found (pattern out of date?)"
    assert raw == []
