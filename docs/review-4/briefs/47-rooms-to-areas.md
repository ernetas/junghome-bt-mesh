# 47 — Rooms to areas, button and node areas, area sync

Phase P3 · Wave 13 · Size M · Closes: U4-2 (U4 F2, F3) and the area-sync part of F4-5.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
Claude-Session trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock times,
CHANGELOG bullet + docs update).

## Goal

No duplicate areas on first setup; every JUNG device, rockers included, lands in a sensible HA area; node devices carry
a recognisable name; optionally, HA areas follow JUNG room changes for devices the user never placed by hand.

## Background

The integration (`custom_components/junghome_ble`) sets `suggested_area = rooms[0]` on light / socket / blind devices
(`entity.py` device-info builders), none on the buttons gang (`entity.py:305-316`) and only for thermostats and
detectors on node devices (`entity.py:177-178`). HA creates an area for every `suggested_area` it does not find *by
name* (aliases ignored), so German room names next to English areas duplicate areas. `suggested_area` applies only at
device creation (`entity.py:178`, `:247`, `:265`, `:287`): a room changed in the app or by `set_room` never moves the
HA device (F4-5). Node devices are named `"<node name> <address>"` (`entity.py:171`).

## Read first

`entity.py:140-320`, `config_flow.py` (user, discovery, reconfigure flows), `__init__.py` (`_async_entry_updated`),
`const.py`, `jhmesh/devices.py` (`Device.rooms`), HA `helpers/area_registry.py` (`async_get_area_by_name`,
`async_get_areas_by_alias`), `migration.py` (device-registry edits), `tests/test_config_flow.py`.

## Steps

1. Room list: the room names `jhmesh.devices` derives, unique and sorted.
2. Flow step `areas` after a successful export load (user, discovery, reconfigure): one optional `AreaSelector` per
   room, defaulting to an existing area whose name or alias matches case-insensitively; empty = create an area named
   after the room (today's behaviour); a boolean `assign_areas` (default on) skips the mapping. Store
   `{room: area_id}` in the entry **options** (`CONF_ROOM_AREAS`); compare in the update listener as other options are.
3. `entity.py`: `room_area_name(hub, room)` returns the mapped area's name, else the room name, else None when
   `assign_areas` is off; use it for every `suggested_area`. Buttons gang: the room of a load on the same node, or for
   a wall transmitter the room its key drives; node device: the area of its first unit; the gateway none.
4. Node device name: `"<unit name> (<product name>)"` when the node has exactly one named unit, else unchanged. Only
   `name`, never `name_by_user`; entity IDs unchanged.
5. Reconfigure option `areas`, prefilled; on submit move only devices whose `area_id` is None or equals the area the
   previous mapping gave them; report the count (`areas_updated`, `{count}`).
6. Option `sync_areas` (off by default): after an export adoption or a room action, apply step 5's rule for devices
   whose room changed.
7. Docs: the step, the option, that user-placed devices are never moved.

## Tests to add

Flow: alias prefill, empty keeps today's behaviour, `assign_areas` off. Registry: buttons and node areas, wall
transmitter by its key's room, reconfigure move leaves user-placed devices alone, `sync_areas` on / off after a
`set_room`. Snapshot update (names, areas) reviewed.

## Acceptance criteria

All gates green; on the synthetic fixture with a pre-existing area carrying the room name as an alias, setup creates no
duplicate area; every buttons device of a node with a load has an area.

## Verifiable on air here?

Yes, HA-side only: run the reconfigure option on the installation and check the device pages; no mesh traffic.

## Risks / off-by-default / "unverified on air"

Moving devices between areas is visible: never touch a device whose area the user set; `sync_areas` off by default.
Name changes alter device names (not entity IDs): note it in the CHANGELOG.

## Depends on

Brief 28 (*soft*: in-place apply makes sync smoother); brief 37 (several rooms per load: use the first room).

## Files touched

`config_flow.py`, `entity.py`, `__init__.py`, `const.py`, `strings.json`, `translations/en.json`, `docs/user/`,
`docs/ha-integration.md`, `CHANGELOG.md`, tests (`tests/test_config_flow.py`, new `tests/test_areas.py`),
`tests/snapshots/`.
