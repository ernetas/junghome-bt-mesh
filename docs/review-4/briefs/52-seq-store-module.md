# 52 — Persistence out of `coordinator.py`

Phase P4 · Wave 15 · Size S · Closes: A4-1 (report 8, brief B1).

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the ids, the
Claude-Session trailer, gates, no key material, no dates or clock times). This is a behaviour-identical refactor: add
one "Internal:" bullet to `CHANGELOG.md` and touch the docs only for the module table.

## Goal

`coordinator.py` loses about 790 lines of hub-independent persistence. The nonce-safety code (sequence store,
backup copy, floor, `HAState`) and the node-info store get modules of their own, with no behaviour change.

## Background

`coordinator.py` is the largest module (about 4650 lines; `JungHomeHub` alone about 3350). Its top part, from
`NODE_VERSIONS` through `HAState` (about `coordinator.py:404-1192`), has no dependency on the hub: `NodeInfoStore`,
`SeqStore`, the seq-store helpers (`seq_store_for_uuid`, backup and floor stores, `_furthest`, `_seq_skip_target`,
`async_skip_seq_store_ahead`, `merge_legacy_seq_store`, `async_migrate_legacy_seq_store`), the legacy migration and
`HAState`. This is where nonce reuse is decided, so it should be readable and testable alone. Earlier review-4
briefs (08, 12, 16, 20) changed this code; this brief moves the result verbatim.

## Read first

- `custom_components/junghome_ble/coordinator.py`: the blocks above (line numbers drift; find them by name), the
  `SEQ_*` / `STORAGE_VERSION` constants near the top, `_refuse_duplicate_mesh` and `_settled` (move them only if
  they have no hub dependency).
- `tests/test_seq_store.py`, `tests/test_properties_seq_store.py`, `tests/test_node_info.py`.
- Every importer: `grep -rn "from .coordinator import\|coordinator\." custom_components tests tools`.

## Steps

1. List every name in the ranges and its users (`grep -rn "<name>" custom_components tests tools`).
2. Move the node-info block to `custom_components/junghome_ble/node_info.py` and the seq-store block plus `HAState`
   to `seq_store.py`, byte for byte. The new modules import only `homeassistant`, `const` and `jhmesh` names, never
   `coordinator` (not even under `TYPE_CHECKING`).
3. In `coordinator.py`, import back what the hub uses and re-export every moved public name other modules or tests
   import from `coordinator` (`from .seq_store import HAState as HAState, ...`): `STORAGE_VERSION`, `SEQ_TX_LIMIT`,
   `SEQ_RESTART_MARGIN`, `SEQ_GUARD_FIRST_BEACON`, `SEQ_STALL_RETRY`, `seq_store_for_uuid`, `merge_legacy_seq_store`,
   `NodeInfoStore`, `NODE_VERSIONS`, and the rest your grep finds. `__init__.py`, `config_flow.py`, `repairs.py`,
   `sensor.py`, `diagnostics.py`, `backup.py` stay unchanged.
4. Update test patch targets that must follow the code (for example
   `custom_components.junghome_ble.coordinator._newest_corrupt_seq_store` → `...seq_store._newest_corrupt_seq_store`).
   Search for every monkeypatch of a moved name.
5. Do not reflow docstrings or comments; keep `git diff --color-moved` reviewable.

## Tests to add

None expected. Add one only if coverage shows a moved branch is no longer reached.

## Acceptance criteria

- Gates pass; `tests/snapshots/test_snapshots.ambr` unchanged (never run `--snapshot-update`).
- No assertion changed; test edits limited to import paths and patch targets.
- `coordinator.py` shrinks by roughly 790 lines; `seq_store.py` has no `JungHomeHub` reference.

## Verifiable on air here?

Regression only: after the cherry-pick, the hub connects, lights answer, no `pdus_dropped` repair appears.

## Risks / off-by-default / "unverified on air"

- A moved function that reads a monkeypatched module constant at call time silently stops seeing the patch: grep the
  tests for every moved name.
- `HAState` subclasses `jhmesh.client.LocalState`; keep importing it from `jhmesh.client` (brief 57 keeps it
  re-exported there).

## Depends on

16 and 20 (last behaviour changes to this code) landed; conflicts with 58 (serial after it). Parallel with 53–57.

## Files touched

`custom_components/junghome_ble/coordinator.py`, new `seq_store.py`, new `node_info.py`, `tests/test_seq_store.py`
(patch targets), `CHANGELOG.md` (Internal bullet), `docs/ha-integration.md` (module table).
