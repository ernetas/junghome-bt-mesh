# 39 — App coexistence write-backs

Phase P2 · Wave 10 · Size M · Closes: F4-6 (report 6 brief B10); ledger rows `net:export:meta.sceneinfo`,
`net:uc:observesceneinfosfordevice`, `net:uc:createjhschedule`, `mgmt:flow:removedevice`,
`net:export:meta.actuatorexports`, `net:export:meta.buttonlayoutexports`.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
Claude-Session trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock times,
CHANGELOG bullet + docs update).

## Goal

What Home Assistant changes looks right in the JUNG app after it re-imports the project: scenes show their stored
values, schedules can be edited in place, removed nodes leave no rows behind, and nodes HA adds carry the app's
actuator and button-layout rows.

## Background

The integration (`custom_components/junghome_ble`, library `jhmesh`) writes the app's export file
(`jhmesh/export.py` `ProjectFile`) and uploads it to the gateway. Gaps:

- `store_scene` writes no captured values into `meta.sceneInfo` (`export.py:1238`); the app shows them per device.
- No update-in-place of a JH Scheduler slot (`schedules.py:418-445`): only create / delete.
- Node removal leaves `schedulerMetaInfo` / timer rows (`export.py:1387`).
- `meta.actuatorExports` / `buttonLayoutExports` are never written for nodes HA adds, nor dropped on removal.

## Read first

`jhmesh/export.py:1238`, `:1387`; `schedules.py:418-460`; `services.py` schedule handlers (around `:1355`);
`mesh_config.py:2980-3000` (`store_scene(s)`); `docs/gap-analysis/network-features.md` §8 (meta JSON shapes); brief 33
(advert data for actuator / layout rows); the ledger rows above.

## Steps

1. `meta.sceneInfo` rows from the values `store_scene` settled on (`async_wait_settled`): lightness, colour
   temperature, blind / slat position, temperature — in the shape the app writes (§8).
2. `update_schedule` action rewriting a slot by index (same validation as `create_schedule`); schema, `services.yaml`,
   icon, strings.
3. Removal (`remove_node`) drops `schedulerMetaInfo`, timer, actuator and button-layout rows of the node.
4. Nodes HA adds get actuator / button-layout rows from brief 33's advert data (product id, actuator function, layout).
5. Update the ledger rows; document that the shapes come from the Android decompile and are unverified against the
   iOS app.

## Tests to add

`ProjectFile` round-trips with the new rows (extend the existing Hypothesis round-trip); `store_scene` writes
`sceneInfo`; `update_schedule` service tests (fake link); removal clean-up; rows for an added node.

## Acceptance criteria

All gates green; ledger rows updated; the maintainer's check that a file written by HA imports into a spare app
install and shows the scene values is listed as the open on-air item.

## Verifiable on air here?

No, not with the production app: the check needs a **spare app install** importing the file (the installation runs
the iOS app; the rules come from Android). Everything else is offline.

## Risks / off-by-default / "unverified on air"

The app imports the whole file: a shape mistake is user-visible. Try every format change on a spare app install
first; mark the rows "unverified on air" until then.

## Depends on

Brief 33 (actuator / layout data), brief 10 (allocator), brief 05 (apply-and-record on cancel).

## Files touched

`jhmesh/export.py`, `schedules.py`, `mesh_config.py`, `services.py`, `services.yaml`, `icons.json`, `strings.json`,
`translations/en.json`, `docs/parity/ledger-net.json`, `docs/parity/ledger-mgmt.json`, `docs/ha-integration.md`,
`CHANGELOG.md`, tests (`tests/jhmesh/test_export.py`, `tests/test_schedules.py`, `tests/test_mesh_config.py`).
