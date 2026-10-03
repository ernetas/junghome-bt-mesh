# 13 — Safe scene clean-up, held numbers, admin actions

Phase P0 · Wave 3 · Size S–M · Closes: D3 (W4-3), D21 held-set part (W4-8), D22 (W4-9); folds in W I7.

Follow the [conventions](README.md#conventions) in full.

## Goal

`delete_unused_scenes` can no longer delete the app's scenes from a stale export; a scene number still held by a node
`delete_scene(force)` skipped is not reused; the skipped members are reported; every rewiring or deleting action is
admin-only.

## Background

HA custom integration `custom_components/junghome_ble`. Actions live in `services.py`; the configurator in
`mesh_config.py` plans on the mesh export, which comes from the gateway when reachable, otherwise from the file on
disk (`_load`, `mesh_config.py:1322-1343`).

- `delete_unused_scenes` (`mesh_config.py:3269-3339`) sends `Scene Delete` to every node's register for every number
  the loaded export lacks. On an entry set up from a file (no gateway), or a gateway entry whose gateway is
  unreachable (silent fallback to disk), scenes and timer scenes the user created in the app since the last export are
  deleted from every device, while the app still lists them.
- W4-8: `delete_scene(force)` keeps the scene on members that did not answer (logged only, `:3245-3254`); the number
  is reused by the next `create_scene`, and the skipped node joins every recall of the new scene. (The allocator part —
  numbers named by key rows and an `avoid` parameter — is brief 10.)
- W4-9: `services.py:465-480` registers `set_room`, `create_room`, `rename_room`, `delete_room`, `assign_key`,
  `clear_key`, the scene actions incl. `delete_scene` / `delete_unused_scenes`, `sync_gateway`, schedules and
  thresholds with `hass.services.async_register`; only `export_network`, `add_device`, `remove_device` are admin.

## Read first

`mesh_config.py:1322-1343`, `:3139-3339`; `services.py:436-537`, `:1159-1185`; `services.yaml`; HA
`async_register_admin_service`; `jhmesh/export.py:906-913` (after brief 10: `free_scene_number(avoid=…)`);
`docs/gap-analysis/network-features.md` §3, §4.2; `tests/test_translations.py`.

## Steps

1. `delete_unused_scenes(dry_run=True, numbers=None, confirm_stale_export=False)`: the dry run returns
   `{register: [numbers]}` and sends nothing. A gateway entry requires that `_gateway_state()` answered (no silent
   fallback); a file entry requires `confirm_stale_export: true` or an explicit `numbers` list.
2. `delete_scene(force)`: persist the skipped (number, element) pairs in a small per-entry `Store`; pass the held
   numbers to `free_scene_number(avoid=…)`; `delete_unused_scenes` clears them when it deletes them. Return
   `{skipped: [...]}` and raise a repair naming the skipped members instead of a log line.
3. Register every rewiring / deleting action with `async_register_admin_service`; keep `get_schedules`,
   `audit_network`, `find_new_devices` (and `store_scene`, decide and document) as they are.
4. `services.yaml`, strings, translations; CHANGELOG notes the behaviour changes (dry run default, admin-only).

## Tests to add

- Dry run sends nothing (the fake link records no PDU) and lists numbers.
- Refused on a file entry without the flag; refused when the gateway is unreachable.
- Held numbers are not reused; held set recorded on force-skip and cleared by `delete_unused_scenes`.
- A non-admin user context gets `Unauthorized` for each rewiring action.

## Acceptance criteria

Gates green; translations test green.

## Verifiable on air here?

Yes, lights and the gateway: create a scene in the app on a gateway entry, run the dry run, confirm it is not listed.

## Risks / off-by-default / "unverified on air"

Behaviour changes for existing automations (decisions M3, M8): document both. The dry run is the safe default.

## Depends on

05 (both edit `delete_scene`), 10 (allocator `avoid`).

## Files touched

`mesh_config.py`, `services.py`, `services.yaml`, `strings.json`, `translations/en.json`, `tests/test_mesh_config.py`,
`tests/test_services.py`, `CHANGELOG.md`, `docs/ha-integration.md`.
