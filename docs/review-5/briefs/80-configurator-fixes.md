# 80 — Configurator: one room resolver, one reservation provider, scene channels, robustness

Review 5 · Wave 24 · Closes: W5-1, W5-2, F5-2, W5-3, W5-4, W5-5, W5-6, W5-7, U5-8 (decision M17).

Follow the [conventions](../../review-4/briefs/README.md#conventions) in full, with these additions: CHANGELOG bullets
go under `## 1.5.0 (unreleased)` (create it above the newest released section; never edit released sections); every
new "unverified on air" marker is cited in `docs/on-air-sweep.md`. The findings are summarised in
[`../plan.md`](../plan.md); the review reports with each finding's full evidence are handed over in the prompt.

## Goal

Every room action resolves rooms the same way, every allocator avoids the same reservations, and the smaller
correctness gaps of the configurator are closed.

## Steps

1. W5-2: one per-entry room resolver (name, mapped area from `CONF_ROOM_AREAS`, aliases) used by every room action,
   `_suggest_area` and the device areas; `room_area` with `create` never makes a room the mapping already names.
2. W5-1: one reservation provider for every allocator (vault unicasts and groups, held scenes, pending nodes); room
   allocation uses it.
3. F5-2: `store_scene` on a channel without its own Scene Setup server (a two-channel node without `0x0527:1017`) is
   refused, as the app refuses it.
4. W5-3: `_answer` merges dict extras (`preflight`) per entry instead of adding them. W5-4: `set_threshold` runs the
   pre-flight before writing the threshold. W5-5: schedule create / update are cancel-safe (free the slot / restore on
   cancellation, cache updated). W5-6: `record_node` checks the vault save and raises `vault_unwritable`. W5-7: the
   journal replay at setup is serialised with a plan still running (the entry lock or the store lock).
5. M17: split `force`. `force` keeps the action's own override; new `skip_preflight` skips the W I4 comparison; on
   `remove_from_room`, `delete_scene`, `remove_device` `force` alone no longer skips it (CHANGELOG *Upgrading*).
6. Improvements: a dry run reports unreachable and asleep nodes; `_unreachable` before the pre-flight reads of
   `delete_scene` / `remove_device`; pre-flight outcomes (differences, `skip_preflight` used) in the plan history and
   diagnostics.

## Tests to add

A regression test per finding, reproduced first. Snapshots change only by the new field.

## Files touched

`configurator/`, `actions/`, `schedules.py`, `areas.py`, `services.yaml`, strings
