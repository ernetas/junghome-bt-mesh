# 42 — Blinds, room thermostat, detector and battery refinements

Phase P2 · Wave 10 · Size M · Closes: F4-16 (offline only).

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
Claude-Session trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock times,
CHANGELOG bullet + docs update).

## Goal

Close the remaining parity gaps for device classes this installation does not have, from the decompiled app's rules,
with offline tests only and every new behaviour marked "unverified on air".

## Background

The integration (`custom_components/junghome_ble`, library `jhmesh`) already supports blinds, the room thermostat
(RTR), detectors and battery transmitters from documented evidence, all unverified on air. Open ledger rows:

- Blinds: parameter gating (`prod:param:blind:*`, five rows; `prop:0x1103`), end-position skips
  (`ui:vm:blindsviewmodel.*`).
- RTR: key mode 4 / temperature connections / multi-connection / "controlled by thermostat"
  (`prod:key-mode:rtr`, `net:uc:setmulticonnection`, `net:uc:removemulticonnection`, `net:uc:getconnecteddevices`,
  `net:uc:isanyrtrdeviceconnected`, `net:uc:observertrconnectionmode`, `prod:param-gate:rtr-connection`,
  `ui:vm:devicesviewmodel.isactiondisabled`, `net:uc:setrtrpropertyconnection`, `net:ui:connectiontemperature`), boost
  poll / slider lock (`ui:vm:roomtemperatureviewmodel.*`), `prop:0x1249`.
- Detectors: as a connection source (`prod:ui:detector_connection_title`), forced-off on the load
  (`ui:state:detectorforcedoffcapability`), PIR detents (`ui:ctl:detectorsensorviewmodel.pirsensitivity`), 5 lx steps
  (`prod:param:detector:switch-on-brightness`).
- Battery: sleep-mode entity (`ui:state:sleepmode`).

## Read first

`cover.py`, `climate.py`, `binary_sensor.py`, `number.py`, `config_entities.py` (`describe` gates),
`mesh_config.py` key modes; the rows above in `docs/parity/ledger-*.json` and their `docs/android/` sources;
`tests/fixtures` networks with blinds, RTR, detectors (the snapshot test of brief 11 pins their unique ids).

## Steps

1. Take the rows one class at a time; for each, implement exactly what the decompile documents, cite the source in the
   docstring and mark it "unverified on air".
2. Blinds: hide / gate parameters by blind mode; skip end-position moves as the app does.
3. RTR: key mode 4 and temperature connections in the configurator; "controlled by thermostat" gate on loads; boost
   read-back poll; `0x1249` codec and entity (disabled).
4. Detectors: detector as a connection source in `assign_key`-like configurator call (or a dedicated action),
   forced-off state on the load, PIR sensitivity detents and 5 lx steps on the existing number entities.
5. Battery: a sleep-mode diagnostic entity.
6. Ledger rows → implemented with `verify: on-air` kept.

## Tests to add

Per row, a test against the fixture networks (blinds, RTR, detectors, battery); snapshot identity unchanged for
existing entities; new entities appear disabled where they are expert settings.

## Acceptance criteria

All gates green; every new docstring, description string and docs line says "unverified on air".

## Verifiable on air here?

No: there is no blind, room thermostat, detector or battery node. Offline tests only.

## Risks / off-by-default / "unverified on air"

Wrong guesses cannot be caught here; keep every new write path behind the existing expert entities, disabled by
default, and every write "unverified on air".

## Depends on

None (brief 11's per-network snapshots help catch unique-id changes).

## Files touched

`cover.py`, `climate.py`, `binary_sensor.py`, `number.py`, `config_entities.py`, `mesh_config.py`,
`jhmesh/properties.py`, `strings.json`, `translations/en.json`, `docs/parity/ledger-*.json`, `docs/ha-integration.md`,
`CHANGELOG.md`, tests (`tests/test_cover.py`, `tests/test_climate.py`, `tests/test_binary_sensor.py`).
