# 38 — Key connections: tunable-white element, slats, lock-function keys

Phase P2 · Wave 10 · Size M · Closes: F4-4 (report 6 brief B8); ledger rows `net:enum:deviceconnection.element`,
`net:uc:setdeviceconnection`, `net:uc:setlockingfunctionconnection`, `prod:key-mode:property`, `prop:0x5003`,
`net:uc:requestlockfunctionforcontrolkeyselection`, `net:uc:getconnection`,
`net:uc:configurepublicationsforpropertyuser`, `net:uc:configureminiactuatorconnection`.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
Claude-Session trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock times,
CHANGELOG bullet + docs update).

## Goal

`assign_key` can do what the app's connection screens do with the devices here: make a key drive a tunable-white
light's colour temperature (or a blind's slats), lock / unlock a light or socket, and decode back what a key in
property mode (key mode 3) does.

## Background

The integration (`custom_components/junghome_ble`, library `jhmesh`) wires keys in `mesh_config.py` (`KEY_MODES`,
`KEY_MODE_CLIENTS` at `:160-260`, `assign_key` around `:2467`). Missing:

- Target element choice: the key's Level client `1003` publishing to the TW light's temperature element
  (`Light.temperature_address`) or a blind's slat element.
- Key mode 3 (lock function): Admin Set `0x5003 = 3`, `0x5006` `[0x0009 u16][STATEFUL]`, `0x5007` / `0x5008` up /
  down values (lock `02 01 <s>`, unlock as the app writes), LBC Property Client `0527:1015` publication to the target
  (`docs/android/properties.md:190-200`). `mesh_config.py:194` lacks mode-3 clients.
- Decoding `0x5006–0x5008` into `KeyConnection` (`jhmesh/devices.py:397-430`), so the event entity's `connection`
  attribute says "lock".
- Property-user wiring for mini-actuator targets, both inputs.

## Read first

`mesh_config.py:160-260`, `:1881` (target element), `:2220` (`_reset_property_mode`), `:2467` (`assign_key`);
`jhmesh/devices.py:397-430`; `jhmesh/properties.py:1247-1262`; `docs/android/network-logic.md` §2.1–§2.6;
`docs/android/properties.md:190-200`; `docs/sniffer.md`.

## Steps

1. **Capture first (maintainer, person at home):** with `tools/mesh_sniff.py capture` running, make each connection
   in the JUNG app on a spare key (TW temperature element, lock function on a light and on a socket, a mini-actuator
   input to a property user), then `tools/mesh_sniff.py decode --json`. Keep only the decoded message sequences
   needed (addresses mapped to fixture addresses, no keys) as test data. Restore the key's original connection.
2. `assign_key(target_element="color_temperature" | "slat")`: plan the Level client publication to that element.
3. `assign_key(mode="lock", lock_seconds=…)`: add mode 3 to `KEY_MODES` / `KEY_MODE_CLIENTS`; write `0x5003`,
   `0x5006–0x5008`; wire the `0527:1015` publication and the target's subscription as the capture shows; read `0x0009`
   on the target afterwards.
4. Decode `0x5006–0x5008` into `KeyConnection.kind = "lock"`.
5. Write the `meta` rows as the app does; keep the new modes in `UNTESTED_MODES` until verified on air.

## Tests to add

Plans byte-exact against the captured sequences; decode of mode-3 keys; refusal for unsupported targets; `tests/sim`
run where the sim supports the models.

## Acceptance criteria

All gates green; ledger rows updated; the new modes listed as untested until the maintainer confirms them on air.

## Verifiable on air here?

Yes, with push-buttons, the DALI TW light and sockets — only with someone at home pressing keys, after the capture in
step 1.

## Risks / off-by-default / "unverified on air"

Medium: a wrong plan leaves a wall key dead until reassigned or cleared. Run only with someone at home; mark every
new mode "unverified on air".

## Depends on

Brief 35 (lock state must show in HA), brief 30 (review-3 F15 key → scene verified on air).

## Files touched

`mesh_config.py`, `jhmesh/devices.py`, `jhmesh/properties.py`, `services.py` / `services.yaml` (new fields),
`strings.json`, `translations/en.json`, `docs/parity/ledger-net.json`, `docs/parity/ledger-prod.json`,
`docs/parity/ledger-prop.json`, `docs/ha-integration.md`, `CHANGELOG.md`, tests (`tests/test_mesh_config.py`,
`tests/jhmesh/test_devices.py`).
