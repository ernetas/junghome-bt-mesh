# 28 — Apply configuration changes without reloading the entry

Phase P1 · Wave 7 · Size L · Closes: D23 (H4-1) — folds in H I-1. Gated by decision M5.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
`Claude-Session` trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock
times, CHANGELOG bullet + docs update).

## Goal

Room, key, scene, threshold and sensor-publication actions, device renames and the unknown-node export adoption
update the running hub's device model in place: no entity passes through `unavailable` / `unknown`, the BLE link
stays up, cached states and property values survive.

## Background

`custom_components/junghome_ble` (HA integration). `services._run` (`services.py:803-829`) calls
`hass.config_entries.async_reload(entry_id)` whenever an operation changed the export. The reload replaces the hub
(its `states` cache, `coordinator.py:~1332`), drops the property reader (`config_entities.py:1606-1617`) and removes
every entity, which writes `unavailable`. A reviewer's test showed a light going `unavailable → unknown → off` after
`rename_room` (which sends nothing on air): every `state` trigger without `from:` fires again, and every enabled
config entity re-reads its property over the mesh.

**Decision M5 first.** If the maintainer chooses the interim, do only step 6.

## Read first

- `services.py:803-860` (`_run`, `async_configure`); `__init__.py:150-224` (setup order), `:387-409`
  (`_remove_stale_devices`).
- `coordinator.py`: `JungHomeHub.async_create`, `load_network`, `_reload_for_export`, the `states` dict.
- `config_entities.py:1606-1617` (reader lifetime); `entity.py` device-info builders, `current_device_identifiers`,
  room central entities; every platform's `async_setup_entry`.

## Steps

1. Refactor each platform's `async_setup_entry` into `build_entities(hub) -> dict[unique_id, Entity]` plus a stored
   `AddConfigEntryEntitiesCallback` on the hub.
2. `JungHomeHub.async_apply_model(cdb, devices)`: swap `cdb`, `devices` and metadata; re-register parent devices and
   update names / areas; per platform add entities whose unique id is new and remove (registry) the ones no longer
   produced; rebind kept entities to the new `Device` objects and signal a refresh; keep `states`, reader caches and
   the link.
3. `_run` and `_reload_for_export`: call `async_apply_model` when only the export changed; keep the full reload for
   unicast, key or option changes.
4. Fallback: if the in-place apply raises, log and do today's reload.
5. Docs: remove the "a few seconds of unavailable" notes; state the remaining reload cases.
6. Interim (if M5 says so): on a reload `_run` starts, hand the old hub's `states` and the reader cache to the new hub
   so values return without `unknown` and without the read wave; document the trigger side effect.

## Tests to add

- A fixture that records state transitions and asserts none through `unavailable` across `tests/test_services.py`.
- `rename_room`, `set_room`, `assign_key`, `store_scene` leave light states unchanged.
- An adopted export with a new node adds its entities; a deleted room removes its central entities; the `rooms`
  attribute updates.
- A failing apply falls back to a reload.

## Acceptance criteria

Gates green; snapshots unchanged; no `unavailable` transition in any services test.

## Verifiable on air here?

Yes, with lights and push-buttons: `create_room`, `set_room` on a light, `rename_room`, `assign_key` on a rocker;
the light's history shows no gap.

## Risks / off-by-default / "unverified on air"

Entities holding references to old `Device` / `Node` objects; config entities' `EntityTarget` identity; room
central entities' `members`; device-trigger lookups through `hub.devices`. Audit every holder.

## Depends on

Decision M5. Lands after waves 1–6 (it touches every platform). Briefs 25, 47, 48 benefit.

## Files touched

`services.py`, `__init__.py`, `coordinator.py`, `entity.py`, `config_entities.py`, every platform module
(`light.py`, `switch.py`, `sensor.py`, `binary_sensor.py`, `number.py`, `select.py`, `button.py`, `cover.py`,
`climate.py`, `event.py`, `scene.py`), `tests/test_services.py`, `tests/conftest.py` (transition recorder),
`CHANGELOG.md`, `docs/ha-integration.md`.
