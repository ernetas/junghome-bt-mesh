# 26 — Large-mesh performance and advert hygiene

Phase P1 · Wave 6 · Size S–M · Closes: R4-8, R4-9 — folds in R I-6 (O(1) lookups, parse cache), R I-7, R I-10
(fewer entity writes).

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
`Claude-Session` trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock
times, CHANGELOG bullet + docs update).

## Goal

Constant-time address lookups, a cached sequence-store restart point, no connection-loop wake-ups for foreign
adverts, a negative cache for Node-Identity classification, and no entity state write when nothing it shows changed.

## Background

`custom_components/junghome_ble` (HA integration) with the `jhmesh` library. On a mesh of hundreds of elements the
hot paths repeat linear work:

- **R4-9.** `CDB.element` / `node_by_addr` scan every node (`jhmesh/cdb.py:529-540`) per message (`_heard_from`,
  `_mark_alive`), per entity availability check and per keep-alive target; `Devices.by_meter` rebuilds `metered` per
  status (`jhmesh/devices.py:537-545`); `HAState._limit` re-parses both store records per reservation
  (`coordinator.py:1027-1062`, about 0.25 ms of CPU per sent PDU with 600 replay-list sources).
- **R4-8.** `_adv_seen` sets `_link_lost` for every proxy advert while unlinked, any network
  (`coordinator.py:1836-1837`), and the no-candidate branch returns at once, so each foreign advert costs a full
  `visible_proxies()` pass; `classify_service_data` (`jhmesh/client.py:882-896`) runs one AES per node per key for every
  Node-Identity advert, uncached (about 1.65 ms against 300 nodes). Review 3 listed the negative cache; it was never
  built.
- **R I-10.** One busy element has 22 dispatcher listeners; every status writes each entity even when unchanged.

## Read first

- `jhmesh/cdb.py` (`element`, `node_by_addr`, everything that mutates `nodes`: `grep -rn "cdb.nodes"
  custom_components tools`), `jhmesh/client.py` `classify_service_data`, `add_node` (~857).
- `jhmesh/devices.py` `metered` / `by_meter`.
- `coordinator.py`: `visible_proxies` (~1811), `_adv_seen` (~1830), `_check_unknown_node` (~1847), `HAState._limit` /
  `_restart_point` (~1027-1062), `_ctl_light_of`.
- `entity.py` `_handle_update`; `tests/test_key_refresh.py` (keys change at runtime).

## Steps

1. `CDB`: a lazily built `_by_addr: dict[int, Element]` (and node map), invalidated by an explicit `reindex()` that
   `ProxyClient.add_node`, provisioning and removal paths call; a debug assertion helper for tests.
2. `Devices.by_meter`: a dict built in `add`.
3. `HAState`: cache `_restart_point` keyed on the identity of the `written` object (keep the object to avoid id
   reuse).
4. `_adv_seen`: set `_link_lost` only when `classify_service_data` matches our keys.
5. `classify_service_data`: an LRU (about 256 entries) `bytes(service_data) → result`; clear it on a key change (key
   refresh) and in `add_node`.
6. `JungHomeEntity._handle_update`: skip `async_write_ha_state` when the rendered state and attributes are unchanged
   (compare a cheap tuple), unless availability changed.
7. Optionally a script `scripts/bench_mesh.py` (300 synthetic nodes) instead of a timing test.

## Tests to add

- Lookups correct after `add_node` and after a removal; stale-index assertion helper.
- The cache is cleared on a key refresh.
- A foreign Network-ID advert while unlinked does not wake the loop (count `visible_proxies` calls: 100 foreign
  adverts → 0 scans).
- `_restart_point` computed once per written record.
- An unchanged status writes no state; a changed one does. 100 % branch on library changes.

## Acceptance criteria

Gates green; behaviour unchanged (snapshots unchanged).

## Verifiable on air here?

Regression only (the installation is small): normal operation, discovery and unknown-node detection still work.

## Risks / off-by-default / "unverified on air"

A stale index after an in-place export change (adopt, provisioning, `remove_device`) would misroute replies or
device keys — every mutation path must reindex. The advert cache must not survive a key refresh. Skipping writes must
not hide an availability change.

## Depends on

12 (touches `HAState` first). Soft: brief 52 later moves `HAState`.

## Files touched

`jhmesh/cdb.py`, `jhmesh/client.py`, `jhmesh/devices.py`, `coordinator.py`, `entity.py`, `tests/jhmesh/*`,
`tests/test_coordinator.py`, `tests/test_key_refresh.py`, `CHANGELOG.md`, `docs/ha-integration.md`.
