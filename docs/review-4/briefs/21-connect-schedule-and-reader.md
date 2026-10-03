# 21 — Reader dedup, connect-time schedule, standalone beacon wait, bounded GATT calls

Phase P1 · Wave 5 · Size M · Closes: R4-5, R4-10, R4-11 — folds in R I-5 (connect-time scheduler), R I-13.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
`Claude-Session` trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock
times, CHANGELOG bullet + docs update).

## Goal

The property reader never queues the same job twice and drops jobs of lost links; node versions are read once per
hub unless the node restarted; Time Set and location go out right after the proxy filter; the CLI link waits for
the beacon; GATT subscribe and disconnect cannot hang the connection loop.

## Background

`custom_components/junghome_ble` (HA integration) reconnects through a GATT proxy and runs a connect-time sequence
(`_after_connect`) plus a `PropertyReader` queue for configuration entities.

- **R4-5.** `config_entities.py:1255-1266` `schedule_version` checks only `_version_link == link_count`; the job of
  the previous link stays queued (`_jobs` never pruned, `:1194-1200`); `_take_chunk` (`:1202-1214`) is O(queue). After
  five quick drops a fixture network held five version jobs per node.
- **R I-5.** `_after_connect` (`coordinator.py:2594-2608`) sends Time Set and location only after a complete refresh,
  so a flapping link never sets the nodes' clocks; every step repeats on every link.
- **R4-10.** `jhmesh/standalone.py:90` calls `proxy.attach(client)` without `beacon_wait` (review-3 T7 half done; the
  hub passes `CONNECT_BEACON_WAIT`, `coordinator.py:2442`).
- **R4-11 (plausible).** `client.py:977` awaits `start_notify` and `:1046` `client.disconnect()` with no limit; only
  `async_stop` bounds `detach()`.

## Read first

- `config_entities.py` `PropertyReader` (~1157-1400) and `ConfigEntity._maybe_read` (~1697).
- `coordinator.py`: `_after_connect` (~2594), `_chunked` (~2861), `_send_time` / `_send_location` (~2974-3019),
  `_note_seq` / `restarted` (reboot detection), the `detach()` calls (~2179, 2270, 2287, 4402) — after brief 15 these
  go through `_drop_link`.
- `jhmesh/client.py` `attach` (~928-1046), `detach`; `jhmesh/standalone.py`.
- `tests/test_config_entities.py`, `tests/test_node_info.py`, `tests/test_coordinator.py`, `tests/jhmesh` standalone
  tests.

## Steps

1. Reader: store `(addr, key, link)` per job; keep a pending set and skip a job already queued; `_take_chunk` skips
   jobs of an older link; replace `deque.remove` with an index or rebuild.
2. `schedule_version`: once per hub, again only when `hub.restarted[node]` is newer than the last read.
3. Move `_send_time` / `_send_location` right after the filter is acknowledged (they are unacknowledged
   broadcasts); keep the daily and DST timers.
4. Record when each `_after_connect` step last completed; skip heartbeats, scene actions, faults and current scenes
   when they completed within `CONNECT_STEP_FRESH` (about 15 min) and the previous link lasted long enough. Keep the
   per-link state refresh and the reads of values the app can change.
5. `standalone.connect`: `attach(client, beacon_wait=1.0)`.
6. `asyncio.wait_for(client.start_notify(...), GATT_TIMEOUT)`; bound `client.disconnect()` in `detach` with
   `GATT_TIMEOUT` and log a WARNING on timeout.

## Tests to add

- Several quick drops leave at most one version job per node (adapted from the reviewer's repro: drop the fake link
  five times, count queued jobs).
- A version is asked again after a detected restart.
- Time Set is sent even when the refresh is cancelled by a drop.
- Steps skipped within the freshness window and run after it.
- Standalone connect waits for the beacon (fake client); a hung `start_notify` / `disconnect` times out and the
  caller continues. 100 % branch on the library changes.

## Acceptance criteria

Gates green; `tests/snapshots` unchanged.

## Verifiable on air here?

Yes, with lights, sockets, push-buttons and the gateway: compare connect-to-`connected` time and the debug `TX`
count before and after; check a reconnect does not re-ask versions; check node clocks after a reconnect against the
gateway's `GET /config` or brief 34's clock offset. Any CLI command checks the standalone change.

## Risks / off-by-default / "unverified on air"

Fewer reads mean a stale value after the app changed something while HA was linked elsewhere — keep per-link reads
for app-changeable values. Firmware-gated entities depend on the version read.

## Depends on

15 (link generation / `_drop_link`).

## Files touched

`config_entities.py`, `coordinator.py`, `const.py`, `jhmesh/standalone.py`, `jhmesh/client.py`,
`tests/test_config_entities.py`, `tests/test_node_info.py`, `tests/test_coordinator.py`, `tests/jhmesh/*`,
`CHANGELOG.md`, `docs/ha-integration.md`.
