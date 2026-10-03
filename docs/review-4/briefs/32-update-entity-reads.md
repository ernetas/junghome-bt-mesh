# 32 — `update_entity` reads the device; configuration values re-read per link

Phase P2 · Wave 8 · Size M · Closes: H4-10 — folds in F4-7, H I-13.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
`Claude-Session` trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock
times, CHANGELOG bullet + docs update).

## Goal

`homeassistant.update_entity` on any JUNG entity asks the device for a fresh value, rate-limited; configuration
entities re-read once per link (rate-limited) so changes made in the app become visible.

## Background

`custom_components/junghome_ble` (HA integration, push-based, `should_poll = False`). Only the meter sensor implements
`async_update` (`sensor.py:~482`); lights, sockets, lock state, boost, detector illuminance and config entities do
not, so an automation cannot force a read. Config entities read once: `config_entities.py:1690-1708` `_maybe_read`
returns once `_read_done` (H4-10, plausible) — a property changed in the app is answered to the app's address, so HA
keeps the old value until the next hub. Review 3 fixed this only for the timed lock.

## Read first

- `entity.py:400-460` (`JungHomeEntity`), `sensor.py:464-492` (the meter's `async_update`), `coordinator.py:3045-3078`
  (`async_refresh_element`), `config_entities.py` `_maybe_read` (~1690) and `read_current` (~1779), `climate.py` boost
  read, `sensor.py` detector illuminance.
- Ledger rows `ui:state:presencebrightnesscapability`, `ui:vm:roomtemperatureviewmodel.requestboostfunction`.

## Steps

1. `JungHomeEntity.async_update` → refresh the entity's element by kind via `hub.async_refresh_element`.
2. `PropertyEntity.async_update` → `read_current(spec)`.
3. Per-address rate limit (one read per about 2 s); skip battery nodes with a debug log; when the link is down, log at
   debug and keep the cached state (never raise: HA logs update failures loudly).
4. `_maybe_read`: re-read once per link, at most every few hours per entity (constant in `const.py`), through the
   reader queue; keep the first-read behaviour.
5. Update the ledger rows' "update_entity does not trigger a read" sentences.

## Tests to add

`update_entity` on a light, socket, config number, detector illuminance and climate; rate limit; link down; battery
node skipped; config re-read on a new link after the interval, not before.

## Acceptance criteria

Gates green; ledger rows updated; snapshots unchanged.

## Verifiable on air here?

Yes with lights, sockets and config entities: change an LED colour or run-on time in the app, then call
`homeassistant.update_entity` and see the new value; reconnect and see a re-read after the interval.

## Risks / off-by-default / "unverified on air"

Traffic only; keep the rate limits. Detector and climate paths are "unverified on air" here (no such devices).

## Depends on

None.

## Files touched

`entity.py`, `config_entities.py`, `sensor.py`, `climate.py`, `const.py`, `docs/parity/ledger-ui.json`,
`tests/test_light.py`, `tests/test_switch.py`, `tests/test_config_entities.py`, `tests/test_climate.py`,
`tests/test_sensor.py`, `CHANGELOG.md`, `docs/ha-integration.md`.
