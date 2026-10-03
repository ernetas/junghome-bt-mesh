# 37 — Several rooms per load, leaving a room

Phase P2 · Wave 9 · Size S–M · Closes: F4-5 (membership part), W I12; ledger rows `net:uc:adddevicetogroups`,
`net:uc:deletedevicefromgroups`. Area sync is **not** here (brief 47).

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
Claude-Session trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock times,
CHANGELOG bullet + docs update).

## Goal

A load can belong to several JUNG rooms, as in the app, or to none; Home Assistant can add a load to a room and take
it out of one without moving it.

## Background

The integration (`custom_components/junghome_ble`, library `jhmesh`) wires rooms as group subscriptions on the load's
servers and records them in the export. `set_room` (`mesh_config.py:2419-2470`) moves a load into exactly one room;
there is no way out of a room. The app allows several rooms per device. A typo in `set_room` creating a room is
handled by brief 22 (`create` flag); this brief builds on that.

## Read first

`mesh_config.py:2419-2470` (`set_room`, `set_rooms`); `services.py` `set_room` schema and handler (around `:880-920`);
`services.yaml` `set_room`; `jhmesh/export.py` room mutators (meta groups, CDB groups, subscriptions);
`docs/android/network-logic.md` §2.4; the two ledger rows.

## Steps

1. Configurator operations `add_to_room(load, room)` and `remove_from_room(load, room)`: Config Model Subscription Add
   `0x801B` / Delete `0x801C` of the room group on the load element's OnOff `1000`, Level `1002`, Lightness `1300`,
   CTL `1303` and Scene `1203` servers — exactly the models `set_room` subscribes now — plus the `meta` room rows and
   the gateway upload (`_upload`), through the existing apply-and-record executor (`_send` / `_record`).
2. Actions `junghome_ble.add_to_room` and `junghome_ble.remove_from_room` (admin services like the other rewiring
   actions after brief 13), schemas in `services.py`, entries in `services.yaml`, icons in `icons.json`.
3. Refuse removing a load from a room that a key connection targets for it unless `force: true`; the error names the
   key.
4. Room central entities (*All lights in …*) are re-derived after the change (they follow the device model on
   reload, or brief 28's in-place update when merged).
5. Update the ledger rows and `docs/ha-integration.md`.

## Tests to add

Plans (subscriptions per model, byte-exact Config messages via the fake link's `config_reply`); export rows after
add / remove; refusal with and without `force`; a stopped plan records what was accepted; a `tests/sim` run of the
plan if the sim models subscriptions.

## Acceptance criteria

All gates green; ledger rows implemented; after an on-air run `tools/mesh_poc.py config audit <node>` shows no
disagreement with the export.

## Verifiable on air here?

Yes, with lights and sockets: add a light to a second room, recall both rooms' central entities, remove it again,
then `config audit`. No person needed beyond watching the light.

## Risks / off-by-default / "unverified on air"

Low. The app's own view after import is unverified for several rooms per load written by HA (say so in the docs).

## Depends on

Brief 10 (allocation, `_adopt`), brief 22 (`set_room` create flag), brief 13 (admin registration).

## Files touched

`mesh_config.py`, `services.py`, `services.yaml`, `icons.json`, `jhmesh/export.py`, `strings.json`,
`translations/en.json`, `docs/parity/ledger-net.json`, `docs/ha-integration.md`, `CHANGELOG.md`, tests
(`tests/test_mesh_config.py`, `tests/test_services.py`).
