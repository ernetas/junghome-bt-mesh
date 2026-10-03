# 10 — Allocate away from the app, surface conflicts

Phase P0 · Wave 2 · Size S–M · Closes: D6 (W4-2), D18 (W4-5), D21 allocator part (W4-8), D-low W4-14; folds in W I2,
I11.

Follow the [conventions](README.md#conventions) in full.

## Goal

With provisioner identity off (the default), rooms, scenes and element groups HA allocates cannot collide with the
app's next allocation; numbers still recalled by keys are not handed out; a carry-over conflict becomes a repair the
user sees.

## Background

HA custom integration `custom_components/junghome_ble`; library `jhmesh`. HA edits the app's mesh export and uploads
it to the JUNG gateway; the app never downloads the project (`docs/android/network-logic.md` §6.1), so its database
does not know HA's additions.

- `ProjectFile.free_group_address` (`jhmesh/export.py:894-904`) and `free_scene_number` (`:906-913`) return the lowest
  free value of the first provisioner's range — the app's own next allocation. Used by `create_room`, `set_room`
  creating a room (`mesh_config.py:2436-2440`), `create_scene`, and `commission.Plan._allocate`
  (`jhmesh/commission.py:424-428`) for `add_device`. Reproduced: HA's room and the app's next room get the same group,
  HA's scene and the app's next scene the same number; on air both rooms become one group.
- On adopt, `merge.apply_changes` keeps the app's value and `_adopt` only logs the conflicts
  (`mesh_config.py:1418-1422`): HA's room disappears from the export while the nodes keep it (W4-5).
- W4-8 (allocator part): `remove_scene` (`export.py:1246-1260`) leaves `keyModeSceneConfigExports` rows; the next
  `create_scene` reuses the number and the old key now recalls the new scene.
- W4-14: `remove_node` matches groups by name (`export.py:1410-1412`), while `cdb_element_groups`
  (`mesh_config.py:605-619`) also accepts `meta` rows.
- Unicast placement already searches from the top (`onboarding.py:54-58`); groups and scenes do not.

## Read first

`jhmesh/export.py:860-913`, `:1006-1040`, `:1186-1260`, `:1387-1415`; `jhmesh/commission.py:400-445`;
`mesh_config.py:600-620`, `:954-996`, `:1345-1430`; `jhmesh/merge.py` docstring; `docs/android/network-logic.md` §1.1,
§6.4; parity rows `net:alloc:room-group`, `net:alloc:scene-number`, `net:alloc:element-group`; `onboard.py` (`_place`,
after brief 03).

## Steps

1. `free_group_address(*, policy="app"|"top", avoid=())` and `free_scene_number(*, policy=…, avoid=())`: "top"
   scans down from the top of the provisioner's range (groups below the reserved device-type groups); `avoid` takes
   extra numbers (brief 13 passes its held set).
2. `free_scene_number` always skips numbers named by a `keyModeSceneConfigExports` row.
3. Policy: "top" when HA has no own provisioner (identity off); "app" for the parity tests and the CLI where it mirrors
   the app; identity on keeps HA's own ranges. `Plan._allocate` takes the same policy (passed from `onboard`).
4. Refuse a top-down pick that lies at or below the app's highest used address + a margin (the range is no longer
   sparse); translated error.
5. `_adopt`: a non-empty `conflicts` list raises a per-entry repair `carry_over_conflict` (placeholders: the paths,
   and what the nodes still hold); a clean adopt deletes it; keep the list in diagnostics.
6. `remove_node` derives the node's groups via `cdb_element_groups`.
7. Update the parity rows' notes and the docs (`create_room` / `create_scene` / `add_device` descriptions: the app
   cannot see HA's additions until it imports a file).

## Tests to add

- HA room / scene vs the app's next room / scene no longer collide (the reviewer's repro: today both get the same
  group address and scene number, and the merge reports four conflicts).
- `add_device` element groups come from the top.
- A deleted scene's number named by a key row is not reused.
- Repair raised on a conflicting adopt, deleted on a clean one.
- `remove_node` removes a group known only from `meta`.
- `tests/test_parity.py` green with the updated rows.

## Acceptance criteria

Gates green; parity ledger check green.

## Verifiable on air here?

Yes, lights only: create a room in HA, then one in the app; `tools/mesh_poc.py config audit <node>` (or HA's
`audit_network`) shows different group addresses. The gateway needed for the adopt path.

## Risks / off-by-default / "unverified on air"

A user comparing addresses with the app may be surprised (document). Decision M7: top-down now vs making provisioner
identity the default later.

## Depends on

03 (same `onboard.py` / `commission.py`). Briefs 13, 18, 37, 39 build on it.

## Files touched

`jhmesh/export.py`, `jhmesh/commission.py`, `mesh_config.py` (`_adopt`), `onboard.py`, `strings.json`,
`translations/en.json`, `docs/parity/ledger-net.json`, `tests/jhmesh/test_export.py`, `tests/test_mesh_config.py`,
`tests/test_onboard.py`, `CHANGELOG.md`, `docs/ha-integration.md`.
