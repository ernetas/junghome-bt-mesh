"""Property tests (Hypothesis) of the three-way merge (`jhmesh.merge`, review-4 S I9).

Base, ours (Home Assistant) and theirs (the app) are random edits of one small export holding every kind of
array the merge matches by identity: set a field of a row, add a row, remove one, change a scalar, add or drop a
member of a plain-value list. Whatever the edits, the merge never puts an identity twice into an array that has
one, applying the same changes again changes nothing, a change at a path the app left alone lands as Home
Assistant made it, and a path both sides changed differently is a conflict that keeps the app's value.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from jhmesh.merge import (
    IDENTITY,
    MISSING,
    Change,
    Key,
    Row,
    apply_changes,
    diff_documents,
)


@dataclass(frozen=True)
class Array:
    """One array the edits work on: where it sits, its identity field and pool, the field a "set" changes."""

    where: tuple[str, str]
    ident: str
    pool: tuple[Any, ...]
    field: tuple[str, ...]
    values: tuple[Any, ...]

    def make(self, ident: Any, value: Any) -> dict[str, Any]:
        row: dict[str, Any] = {self.ident: ident}
        inner = row
        for step in self.field[:-1]:
            inner = inner.setdefault(step, {})
        inner[self.field[-1]] = value
        return row


SMALL = (0, 1, 2, 3)
NAMES = ("a", "b", "c")
ARRAYS = (
    Array(
        ("meta", "buttonLayoutExports"),
        "elementAddress",
        (328, 329, 562, 768),
        ("mode",),
        SMALL,
    ),
    Array(
        ("meta", "keyModeSceneConfigExports"),
        "elementAddress",
        (328, 329, 562),
        ("sceneConfig", "sceneId"),
        SMALL,
    ),
    Array(
        ("meta", "actuatorExports"),
        "elementAddress",
        (328, 562, 768),
        ("actuatorId", "actuatorFunctionId"),
        SMALL,
    ),
    Array(("meta", "userGroups"), "address", (49153, 49154, 49155), ("name",), NAMES),
    Array(("meta", "scenes"), "number", ("0001", "0002", "0003"), ("name",), NAMES),
    Array(("network", "groups"), "address", ("C001", "C002", "C003"), ("name",), NAMES),
    Array(
        ("network", "nodes"),
        "UUID",
        (
            "00000001-0000-4000-8000-000000000001",
            "00000002-0000-4000-8000-000000000002",
            "00000003-0000-4000-8000-000000000003",
        ),
        ("name",),
        NAMES,
    ),
    Array(("network", "networkExclusions"), "ivIndex", (0, 1, 2), ("note",), NAMES),
)
MEMBERS = ("0002", "0003", "0004")


def base_doc() -> dict[str, Any]:
    doc: dict[str, Any] = {
        "network": {"name": "mesh", "timestamp": "stamp"},
        "meta": {"version": 1},
    }
    for array in ARRAYS:
        section, key = array.where
        doc[section][key] = [
            array.make(ident, array.values[0]) for ident in array.pool[:2]
        ]
    for row in doc["network"]["networkExclusions"]:
        row["addresses"] = ["0002"]
    return doc


@st.composite
def edited(draw: st.DrawFn, base: dict[str, Any]) -> dict[str, Any]:
    """`base` after a few random edits, every identity still unique in its array."""
    doc = copy.deepcopy(base)
    for _ in range(draw(st.integers(0, 6))):
        op = draw(st.sampled_from(("set", "add", "remove", "scalar", "member")))
        if op == "scalar":
            doc["network"]["name"] = draw(st.sampled_from(("mesh", "x", "y")))
            continue
        if op == "member":
            exclusions = doc["network"]["networkExclusions"]
            if exclusions:
                row = draw(st.sampled_from(exclusions))
                member = draw(st.sampled_from(MEMBERS))
                members = row.setdefault("addresses", [])
                if member in members:
                    members.remove(member)
                else:
                    members.append(member)
            continue
        array = draw(st.sampled_from(ARRAYS))
        rows = doc[array.where[0]][array.where[1]]
        if op == "add":
            ident = draw(st.sampled_from(array.pool))
            if all(r[array.ident] != ident for r in rows):
                rows.append(array.make(ident, draw(st.sampled_from(array.values))))
            continue
        if not rows:
            continue
        index = draw(st.integers(0, len(rows) - 1))
        if op == "remove":
            del rows[index]
            continue
        inner = rows[index]
        for step in array.field[:-1]:
            inner = inner[step]
        inner[array.field[-1]] = draw(st.sampled_from(array.values))
    return doc


def lookup(doc: Any, path: tuple[Any, ...]) -> Any:
    """The value at a change's path in `doc` (MISSING when it, or what holds it, is not there)."""
    node, name = doc, None
    for step in path:
        if isinstance(step, str):
            if not isinstance(node, dict) or step not in node:
                return MISSING
            node, name = node[step], step
            continue
        assert isinstance(
            step, Key
        )  # every row here carries its identity: none is matched by content
        ident = IDENTITY[name]
        node = next((x for x in node if ident(x) == step.ident), MISSING)
        if node is MISSING:
            return MISSING
    return node


def norm(value: Any) -> Any:
    """`value` with its plain-value lists as sets (the merge keeps them as sets, not in order)."""
    if isinstance(value, dict):
        return {k: norm(v) for k, v in value.items()}
    if isinstance(value, list):
        if all(not isinstance(x, (dict, list)) for x in value):
            return frozenset(value)
        return [norm(x) for x in value]
    return value


def same(a: Any, b: Any) -> bool:
    if a is MISSING or b is MISSING:
        return a is b
    return bool(norm(a) == norm(b))


def duplicated_identities(doc: Any, name: Any = None) -> list[Any]:
    """Every identity that appears more than once in an array that has one, anywhere in `doc`."""
    found: list[Any] = []
    if isinstance(doc, dict):
        for k, v in doc.items():
            found += duplicated_identities(v, k)
    elif isinstance(doc, list):
        if name in IDENTITY:
            idents = [IDENTITY[name](x) for x in doc if isinstance(x, dict)]
            found += [i for i in set(idents) if idents.count(i) > 1]
        for x in doc:
            found += duplicated_identities(x)
    return found


def listed(change: Change, changes: list[Change]) -> bool:
    return any(c is change for c in changes)


@given(data=st.data())
def test_the_merge_keeps_identities_unique_and_the_app_wins_conflicts(
    data: st.DataObject,
) -> None:
    base = base_doc()
    ours = data.draw(edited(base), label="ours")
    theirs = data.draw(edited(base), label="theirs")
    changes = diff_documents(base, ours)
    assert all(not isinstance(step, Row) for c in changes for step in c.path)
    merged = copy.deepcopy(theirs)
    applied, conflicts = apply_changes(merged, changes)
    assert len(applied) + len(conflicts) == len(changes)

    assert duplicated_identities(merged) == []

    again = copy.deepcopy(merged)
    apply_changes(again, changes)
    assert again == merged

    for change in changes:
        before, after = lookup(theirs, change.path), lookup(merged, change.path)
        if change.members and before is not MISSING:
            # members merge as sets, never a conflict
            added = set(change.new) - set(change.old)
            removed = set(change.old) - set(change.new)
            assert set(after) == (set(before) - removed) | added
            assert listed(change, applied)
        elif change.members:
            # the app removed the row the list sat in: nothing left to merge into
            assert listed(change, conflicts)
            assert after is MISSING
        elif same(before, change.old) or same(before, change.new):
            # the app left the path alone (or made the same change): it ends as Home Assistant made it
            assert same(after, change.new)
            assert listed(change, applied)
        else:
            # both changed it, differently: a conflict, and the app's value stays
            assert listed(change, conflicts)
            assert same(after, before)
