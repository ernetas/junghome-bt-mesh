"""Three-way merge of project files: carry one side's changes over onto a newer copy from the other side.

The JUNG HOME app uploads its whole project to the gateway after every change, but never downloads one: a change
another writer (Home Assistant) made to the file — and uploaded — is missing from the app's next upload. The
nodes still hold it (every Config message went out), so the file must not forget it. `diff_documents(base,
ours)` lists what *we* changed since the app's last upload (`base`), and `apply_changes(theirs, changes)` puts
those changes onto the app's new upload, leaving everything the app changed in between alone.

Documents are `{"network": <CDB tree>, "meta": <app meta block>}` (`ProjectFile.snapshot()`). Arrays are matched
by what identifies their entries — a node by its UUID, an element by its index, a model by its id, a room by its
address, an app device by (node, locations), a key's or load's `*Exports` row by its element address, a network
exclusion by its IV index — so an entry the app added or removed elsewhere does not shift ours, and a row both
sides changed stays one row (matched by content, HA's and the app's version of a key's mode both
survived); an array of plain values (subscriptions, bound keys) is merged as a set; any other array of objects is
treated as a set of whole rows. A change whose old value the app has changed since is a conflict: the app's value
is kept (its Config messages went out later) and the change is reported, not applied. Unverified on air against
the iOS app's import of a merged file.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "IDENTITY",
    "IGNORED",
    "MISSING",
    "Change",
    "Key",
    "Row",
    "Step",
    "apply_changes",
    "diff_documents",
]


class _Missing:
    """The value of a key or entry that is not there (a singleton that survives `copy.deepcopy`)."""

    def __repr__(self) -> str:
        return "MISSING"

    def __deepcopy__(self, memo: dict[int, Any]) -> _Missing:
        return self


MISSING: Any = _Missing()

# the CDB timestamp is the file's, not a change of either side
IGNORED: frozenset[tuple[str, ...]] = frozenset({("network", "timestamp")})


def _upper(value: Any) -> Any:
    return value.upper().replace("-", "") if isinstance(value, str) else value


def _address(value: Any) -> int | None:
    """Return the element address of a `meta` row: an int, or hex text (`devices.as_int`); None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16)
        except ValueError:
            return None
    return None


def _device_identity(row: dict[str, Any]) -> Any:
    did = row.get("deviceId")
    if not isinstance(did, dict) or did.get("nodeId") is None:
        return None
    locations = did.get("locationIds")
    locs = (
        tuple(sorted(str(x) for x in locations)) if isinstance(locations, list) else ()
    )
    return (_upper(did["nodeId"]), locs)


def _scene_info_identity(row: dict[str, Any]) -> Any:
    scene, device = row.get("scene"), _device_identity(row)
    return None if scene is None or device is None else (scene, device)


# what identifies an entry of an array, by the key the array sits under
IDENTITY: dict[str, Callable[[dict[str, Any]], Any]] = {
    "nodes": lambda r: _upper(r.get("UUID")),
    "provisioners": lambda r: _upper(r.get("UUID")),
    "elements": lambda r: r.get("index"),
    "models": lambda r: r.get("modelId"),
    "groups": lambda r: _upper(r.get("address")),
    "scenes": lambda r: _upper(r.get("number")),
    "netKeys": lambda r: r.get("index"),
    "appKeys": lambda r: r.get("index"),
    "userGroups": lambda r: r.get("address"),
    "elementConnectionGroups": lambda r: r.get("groupAddress"),
    "devices": _device_identity,
    # one row per scene and app device (`SceneInfoRepositoryImpl.saveInfoFor`)
    "sceneInfo": _scene_info_identity,
    # one row per element in the app's tables (`docs/android/network-logic.md`: `address` + one value)
    "keyModeSceneConfigExports": lambda r: _address(r.get("elementAddress")),
    "buttonLayoutExports": lambda r: _address(r.get("elementAddress")),
    "actuatorExports": lambda r: _address(r.get("elementAddress")),
    "networkExclusions": lambda r: r.get("ivIndex"),
}


@dataclass(frozen=True)
class Key:
    """Step into an array entry by its identity."""

    ident: Any


@dataclass(frozen=True)
class Row:
    """Step onto a whole row of an array matched by content (no identity)."""

    text: str


Step = str | Key | Row


@dataclass
class Change:
    """One change: the value at `path` went from `old` to `new` (either may be MISSING).

    `members`: `old` / `new` are arrays of plain values, merged as sets.
    """

    path: tuple[Step, ...]
    old: Any
    new: Any
    members: bool = False

    def where(self) -> str:
        """Render the path for a log line (keys only, never values: a value may be a key)."""
        parts = []
        for step in self.path:
            if isinstance(step, Key):
                parts.append(f"[{step.ident}]")
            elif isinstance(step, Row):
                parts.append("[row]")
            else:
                parts.append(f".{step}")
        return "".join(parts).lstrip(".")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _is_plain(items: list[Any]) -> bool:
    return all(not isinstance(x, (dict, list)) for x in items)


def _keyed(items: list[Any], name: Any) -> dict[Any, dict[str, Any]] | None:
    """Entries by identity when the array has one and every entry carries a distinct one; else None."""
    ident = IDENTITY.get(name) if isinstance(name, str) else None
    if ident is None or not all(isinstance(x, dict) for x in items):
        return None
    out: dict[Any, dict[str, Any]] = {}
    for item in items:
        key = ident(item)
        if key is None or key in out:
            return None
        out[key] = item
    return out


def diff_documents(base: Any, ours: Any) -> list[Change]:
    """Return the changes that turn `base` into `ours`."""
    changes: list[Change] = []
    _diff(base, ours, (), changes)
    return changes


def _diff(a: Any, b: Any, path: tuple[Step, ...], out: list[Change]) -> None:
    if path in IGNORED:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in (*a, *(k for k in b if k not in a)):
            _diff(a.get(k, MISSING), b.get(k, MISSING), (*path, k), out)
        return
    if isinstance(a, list) and isinstance(b, list):
        if _is_plain(a) and _is_plain(b):
            if a != b:
                out.append(Change(path, list(a), list(b), members=True))
            return
        name = next((s for s in reversed(path) if isinstance(s, str)), None)
        ka, kb = _keyed(a, name), _keyed(b, name)
        if ka is not None and kb is not None:
            for k in (*ka, *(k for k in kb if k not in ka)):
                _diff(ka.get(k, MISSING), kb.get(k, MISSING), (*path, Key(k)), out)
            return
        ra = {_canonical(x): x for x in a}
        rb = {_canonical(x): x for x in b}
        for text, row in ra.items():
            if text not in rb:
                out.append(Change((*path, Row(text)), row, MISSING))
        for text, row in rb.items():
            if text not in ra:
                out.append(Change((*path, Row(text)), MISSING, row))
        return
    if a != b or type(a) is not type(b):
        out.append(Change(path, copy.deepcopy(a), copy.deepcopy(b)))


def _entry_index(items: list[Any], name: Any, step: Key | Row) -> int | None:
    if isinstance(step, Row):
        return next(
            (i for i, x in enumerate(items) if _canonical(x) == step.text), None
        )
    ident = IDENTITY.get(name) if isinstance(name, str) else None
    if ident is None:
        return None
    return next(
        (
            i
            for i, x in enumerate(items)
            if isinstance(x, dict) and ident(x) == step.ident
        ),
        None,
    )


def _resolve(doc: Any, path: tuple[Step, ...]) -> tuple[Any, Any] | None:
    """Return (container, name of the array it is, or None) holding the last step of `path`; None when gone."""
    node, name = doc, None
    for step in path[:-1]:
        if isinstance(step, str):
            if not isinstance(node, dict) or not isinstance(
                node.get(step), (dict, list)
            ):
                return None
            node, name = node[step], step
        else:
            if not isinstance(node, list):
                return None
            i = _entry_index(node, name, step)
            if i is None:
                return None
            node = node[i]
    return node, name


def _current(container: Any, name: Any, step: Step) -> Any:
    """Return the value the last step of a path names (MISSING when absent); `_resolve` checked the container."""
    if isinstance(step, str):
        return container.get(step, MISSING)
    i = _entry_index(container, name, step)
    return MISSING if i is None else container[i]


def _fits(container: Any, step: Step) -> bool:
    """Whether `container` is what the step expects: a dict for a key, an array for an entry or row."""
    return isinstance(container, dict if isinstance(step, str) else list)


def _write(container: Any, name: Any, step: Step, value: Any) -> None:
    if isinstance(step, str):
        if value is MISSING:
            container.pop(step, None)
        else:
            container[step] = copy.deepcopy(value)
        return
    # an entry or row is only ever added or removed: a change inside one is a change of its own
    if value is MISSING:
        i = _entry_index(container, name, step)
        assert i is not None  # it was current, which is why it is removed
        del container[i]
    else:
        container.append(copy.deepcopy(value))


def _same(a: Any, b: Any) -> bool:
    if a is MISSING or b is MISSING:
        return a is b
    return _canonical(a) == _canonical(b)


def apply_changes(
    doc: Any, changes: Iterable[Change]
) -> tuple[list[Change], list[Change]]:
    """Apply `changes` to `doc` in place; return (applied, conflicts). Already-present changes count as applied."""
    applied: list[Change] = []
    conflicts: list[Change] = []
    for change in changes:
        where = _resolve(doc, change.path)
        if where is None:
            # what the change sits in is gone (the app removed the node / model): nothing left to change
            conflicts.append(change)
            continue
        container, name = where
        step = change.path[-1]
        if not _fits(container, step):
            conflicts.append(change)
            continue
        current = _current(container, name, step)
        if change.members:
            if not isinstance(current, list):
                conflicts.append(change)
                continue
            added = [x for x in change.new if x not in change.old]
            removed = [x for x in change.old if x not in change.new]
            merged = [x for x in current if x not in removed]
            merged += [x for x in added if x not in merged]
            _write(container, name, step, merged)
            applied.append(change)
            continue
        if _same(current, change.new):
            applied.append(change)
        elif _same(current, change.old):
            _write(container, name, step, change.new)
            applied.append(change)
        else:
            conflicts.append(change)
    return applied, conflicts
