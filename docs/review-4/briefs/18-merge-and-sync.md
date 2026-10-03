# 18 — Merge identities, both-changed merge, safe refetch

Phase P1 · Wave 4 · Size M · Closes: D16 (S4-4), D17 (S4-6) — folds in S I8, H I-10 (sync bookkeeping out of
`entry.data`) and a merge property test (S I9).

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
`Claude-Session` trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock
times, CHANGELOG bullet + docs update).

## Goal

The three-way merge never duplicates identity-bearing rows; "both sides changed" merges instead of forcing a
destructive re-fetch; identical gateway and disk content counts as synced; a re-fetch keeps the file it replaces;
the sync bookkeeping no longer lives in `entry.data`.

## Background

The repository is a Home Assistant custom integration (`custom_components/junghome_ble`) for JUNG HOME Bluetooth
Mesh devices, with a bundled mesh library (`custom_components/junghome_ble/jhmesh`, also `jhmesh/`). HA edits the
app's mesh export (rooms, keys, scenes) and uploads it to the JUNG gateway; when the app uploads its own version,
HA adopts it and carries its own changes over with a three-way merge (`jhmesh/merge.py`, base = the `.app` copy).

- **S4-4.** `jhmesh/merge.py:57-70` `IDENTITY` has no entry for `keyModeSceneConfigExports`, `buttonLayoutExports`,
  `actuatorExports` and `networkExclusions`. Rows are content-matched (`merge.py:172-180`), and a removal of a row
  already gone counts as applied (`:256-266`), so HA changing key 328 to mode 5 while the app changes it to mode 3
  yields two rows for the same `elementAddress`, with no conflict reported.
- **S4-6.** `mesh_config.py:1377-1384` (`_adopt`) refuses with `service_gateway_export_newer` when both the disk and
  the gateway differ from the synced digest; `_upload` (`:1083-1094`) refuses on `gateway_digest != synced`. The
  remedy (Reconfigure → fetch again) `os.replace`s the export without a backup (`config_flow.py:1039-1046`), losing
  HA's groups and scenes that the nodes still use. A crash between a successful POST and the delayed save of
  `CONF_GATEWAY_SYNCED` (`_mark_synced`, `mesh_config.py:1235-1243`) leaves `gateway == disk != synced` and blocks
  every change.
- **H I-10.** `_mark_synced` rewrites `entry.data` (`CONF_GATEWAY_SYNCED`, `CONF_GATEWAY_LAST_SYNC`) on every sync,
  and `sensor.py:645-647` registers `entry.add_update_listener` from an entity to notice it.

## Read first

- `jhmesh/merge.py` (whole), `tests/jhmesh/test_merge.py`.
- `mesh_config.py`: `_carry_over` (~954-1001), `_upload` (~1036-1131), `_gateway_state` / `_synced_digest` /
  `_mark_synced` (~1189-1243), `_adopt` (~1345-1430).
- `config_flow.py`: `_async_keep_incoming` (~1039), `_async_forget_replaced_export` (~1145).
- `sensor.py:630-660` (the last-sync sensor and its update listener).
- `tests/fixtures/` (the Android share export has the `*Exports` arrays), `tests/test_mesh_config.py` adoption tests.
- `docs/android/` notes on the meta arrays, to confirm `elementAddress` is unique per row.

## Steps

1. Add `IDENTITY` entries: `keyModeSceneConfigExports`, `buttonLayoutExports`, `actuatorExports` by
   `elementAddress`; `networkExclusions` by `ivIndex`. Leave the ranges as rows.
2. Add a Hypothesis merge state machine / property test in `tests/jhmesh`: base / ours / theirs from random edits of
   a fixture (set, add, remove a row, change a scalar). Assert: no identity appears twice in an array that has one;
   `apply_changes` is idempotent; a path the app did not touch ends as ours; a path both changed differently is a
   conflict and the app's value is kept.
3. In `_adopt` and `_upload`: when `gateway_digest == disk_digest`, record it as synced and proceed.
4. In `_adopt`: when disk and gateway both differ from `synced` and `app_copy_path(self.path)` exists, carry over
   (as for "only the gateway changed") instead of raising; keep the pre-adopt copy; report conflicts through the
   repair brief 10 added. Without `.app`, keep today's refusal.
5. In `_async_keep_incoming`: when the final file exists, keep it (`.pre-reconfigure` copy or
   `write_private_with_backup` semantics) and fsync the directory.
6. Move `CONF_GATEWAY_SYNCED` / `CONF_GATEWAY_LAST_SYNC` to a small per-entry `Store` (read at setup, migrated once
   from `entry.data`); signal the last-sync sensor through a dispatcher signal instead of an entry update listener.

## Tests to add

- The property test of step 2.
- A unit test: HA sets key 328 to mode 5 / scene 5, the app sets it to mode 3 / scene 7 → one row per array for
  element 328, and a conflict listed.
- Adoption: both changed with `.app` present → merged, uploaded, `.pre-adopt` kept; without `.app` → refused.
- `gateway == disk != synced` → no refusal, synced recorded.
- Reconfigure keeps the replaced export.
- Sync bookkeeping: migration from `entry.data`, no `entry.data` write on a sync, the sensor updates.

## Acceptance criteria

Gates green; existing merge and snapshot tests unchanged or updated with a stated reason; no
`service_gateway_export_newer` in the both-changed-with-base case.

## Verifiable on air here?

Yes, with the gateway and push-buttons, person needed: change a key's mode in HA while the gateway is unreachable,
change another setting in the app, reconnect; the file holds both changes and the app shows one row per key.

## Risks / off-by-default / "unverified on air"

The merge is unverified against the iOS app's import: keep conflicts app-wins as today. New identities change which
edits conflict — watch the existing merge tests. The store migration must keep a downgrade readable (leave
`entry.data` keys in place for one release, or document).

## Depends on

10 (`_adopt` conflict repair). Brief 48 builds on this.

## Files touched

`jhmesh/merge.py`, `mesh_config.py`, `config_flow.py`, `sensor.py`, `const.py`, `tests/jhmesh/test_merge.py`,
`tests/test_mesh_config.py`, `tests/test_config_flow.py`, `CHANGELOG.md`, `docs/ha-integration.md`.
