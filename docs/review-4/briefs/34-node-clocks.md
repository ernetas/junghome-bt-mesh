# 34 — Node clocks, time zone and stored location as diagnostics

Phase P2 · Wave 9 · Size S · Closes: — (F4-8; parity rows `air:access:5d`, `msg:op:5d`, `msg:op:8237`,
`msg:op:823b`, `msg:op:40`, `msg:op:8225`).

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
`Claude-Session` trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock
times, CHANGELOG bullet + docs update).

## Goal

HA can tell whether each node's clock, zone offset and stored location — which its schedules depend on — are right,
and raises a fixable repair when not.

## Background

`custom_components/junghome_ble` (HA integration). Schedules (JH Scheduler, astro) run on the nodes' clocks and stored
location; HA only broadcasts Time Set, Time Zone Set and Location Global Set (`coordinator.py:2974-3019`) and never
confirms. There is no Time Status handler (the ledger's `msg:op:5d` "implemented" is wrong; brief 07 fixes the row);
every node's answer to Time Set is dropped. A CLI probe saw 27 nodes answer a Time Get within ±0.9 s
(`docs/hidden-features.md` §9). The Location Global Get builder and decoder exist (`messages.py:558-560`, `:825`),
unused.

## Read first

- `jhmesh/messages.py:120-135`, `:1333-1400`, `:1485-1500` (Time Set / Status describe), `:550-560`, `:820-830`
  (Location).
- `coordinator.py:2974-3030` (`_send_time`, `_send_location`, daily timer); `config_entities.py:1300-1400`
  (`_ask_time_role`, `remember_node_info`: the once-per-node pattern); `diagnostics.py`.

## Steps

1. Builders `time_get()` (`0x8237`), `time_zone_get()` (`0x823B`); decoders returning dataclasses for Time Status
   (`0x5D`) and Time Zone Status (`0x823D`).
2. A `STATUS_HANDLERS` entry for Time Status (unicast replies and published ones).
3. After the daily Time Set, ask the nodes that host a JH Scheduler with used slots (or all mains nodes, chunked, once
   per day) for Time, Time Zone and Location; keep per node: clock offset vs HA (seconds), zone offset, location.
4. A diagnostic sensor *Clock offset* per node (disabled by default); the values in diagnostics.
5. Repair `node_clock_wrong` when |offset| > 60 s or the zone offset differs from HA's for a node with schedules;
   fixable by "send Time Set now".
6. Ledger rows → implemented.

## Tests to add

Builders and decoders byte-exact (spec layouts); handler; repair raised, fixed and cleared; diagnostics snapshot;
battery nodes skipped.

## Acceptance criteria

Gates green; ledger rows updated.

## Verifiable on air here?

Yes, read-only. Probe first: `tools/mesh_poc.py get` / a Time Get to a few nodes with `listen` running, compare with
HA's clock.

## Risks / off-by-default / "unverified on air"

None (Gets). Keep the daily volume chunked.

## Depends on

None. Brief 36 uses the clock to interpret night-light behaviour.

## Files touched

`jhmesh/messages.py`, `coordinator.py`, `config_entities.py`, `sensor.py`, `diagnostics.py`, `repairs.py`,
`strings.json`, `translations/en.json`, `docs/parity/ledger-*.json`, `tests/jhmesh/test_messages.py`,
`tests/test_coordinator.py`, `tests/test_sensor.py`, `tests/test_diagnostics.py`, `CHANGELOG.md`,
`docs/ha-integration.md`.
