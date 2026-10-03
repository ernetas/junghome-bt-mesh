# 04 — Proof-gated key-refresh following

Phase P0 · Wave 1 · Size M · Closes: D4 (P4-1; review-3 N2b incomplete); folds in P I-1.

Follow the [conventions](README.md#conventions) in full.

## Goal

Home Assistant moves to key-refresh Phase 2 / 3 only on proof that the mesh moved, so one compromised node (or its
extracted flash) cannot switch HA to a key of its choice, and an aborted app refresh does not move HA either.

## Background

HA custom integration `custom_components/junghome_ble`; library `jhmesh`. HA follows the JUNG app's NetKey refresh
passively: it holds every device key from the export, and the proxy forwards the app's device-key-sealed Config
messages.

- `_follow_key_refresh` (`jhmesh/client.py:1806-1831`) accepts any device-key-sealed Config NetKey Update / Key Refresh
  Phase Set for index 0: no check of sender, addressee or agreement. `_deliver` tries the device key of **src first**,
  then dst (`client.py:2111-2118`). `_key_refresh_to` (`:1833-1864`) switches TX at Phase 2, drops the old key at Phase
  3 and persists `(new key, 3)`; `_resume_key_refresh` (`:816-839`) restores it at start.
- HA side: `_on_key_refresh` (`coordinator.py:4361-4373`) rewrites the entry `unique_id` to the new Network ID;
  `async_apply_followed_key_refresh` (`coordinator.py:744-769`) swaps the export's NetKey at every setup.
- Reproduced: a node sends, src = dst = itself, sealed with its own device key, NetKey Update(attacker key), Phase Set
  2, Phase Set 3 → HA transmits with the attacker's key, drops the real one, persists it, survives a restart deaf and
  mute. Non-adversarial variant: HA follows *requests*; the app aborts a refresh to phase 0 when a node lags
  (`docs/android/transport-provisioning.md:423`).

## Read first

`jhmesh/client.py:816-864`, `:1788-1864`, `:2087-2160`, `LocalState.set_key_refresh` / `parse_record`;
`coordinator.py:744-769`, `:4355-4375`; `jhmesh/config_messages.py:351-383`, `:954-966`; `tests/test_key_refresh.py`;
`tests/sim/mesh.py:130-160`; `docs/android/transport-provisioning.md` §4.2; Mesh Protocol 1.1 §3.10.4, §4.3.2.8.

## Steps

1. New pure module `jhmesh/keyrefresh.py`: `KeyRefreshFollower` with candidate key, phase, the set of nodes that
   confirmed each phase, and `to_stored` / `from_stored` (same persisted shape as today's `key_refresh` record, plus
   optional proof fields; absent = no proof).
2. Learn a candidate (Phase 1, harmless: an extra RX key) from a NetKey Update only when `msg.dst` is a node primary
   in the CDB, the message was opened with **dst's** device key (`_deliver` reports `key == f"dev:{dst:04X}"`) and
   `msg.src` is not an element of a JUNG node.
3. Count device-key-sealed NetKey Status / Key Refresh Phase Status (status 0, phase ≥ requested) per distinct src.
4. Advance to Phase 2 / 3 only on: a Secure Network beacon authenticated under the new key (KR=1 for 2, KR=0 for 3;
   `_parse_beacon` already detects it), or Phase Status confirmations from ≥ 2 distinct nodes, or from `proxy_addr`.
   Requests alone never advance.
5. Persist phase 3 and fire `on_key_refresh(0, …)` (which moves the entry `unique_id`) only after that proof.
6. A new candidate during Phase 2 keeps the Phase-2 key for RX; never drop an RX key except at a proven Phase 3.
7. Log transitions with their proof source (no key material). Update `docs/ha-integration.md` "A key refresh is
   followed".

## Tests to add

- Library: the forged three-message sequence from one node (src = dst, own device key) → no phase change, nothing
  persisted, TX key unchanged (the reviewer's repro, written as a test).
- Library: same with Phase Status only from that one node → no Phase 2 / 3.
- Library: legitimate sequence via new-key beacons; via statuses from two nodes; via the proxy's status.
- Library: abort and restart with a second key → old key still in RX, no Phase 3.
- HA level: `unique_id` and the export key unchanged after a forged sequence and a restart.
- `tests/sim`: a full app-driven refresh is still followed end to end.
- Hypothesis state machine over `KeyRefreshFollower`: the export's key is never dropped without proof.

## Acceptance criteria

Gates green; existing key-refresh tests pass unchanged or with documented intent; `jhmesh` at 100 % line + branch.

## Verifiable on air here?

Regression only: normal operation (connect, beacons, commands) must be unchanged. The positive path needs the app's
key renewal, which changes the real NetKey — do not run it on this installation.

## Risks / off-by-default / "unverified on air"

If a JUNG proxy sends no beacon on a phase change and the statuses are not heard, HA stays one phase behind; that is
safe (Phase 2 nodes still accept the old key until Phase 3, and a Phase-3 beacon moves HA on). Mark the proof rule
"unverified on air".

## Depends on

None. Briefs 14, 16, 24 and 29 build on it.

## Files touched

New `jhmesh/keyrefresh.py`, `jhmesh/client.py` (key refresh, `_deliver`, `_parse_beacon`, `_resume_key_refresh`),
`coordinator.py` (`:744-769`, `:4361-4373`), `tests/test_key_refresh.py`, `tests/jhmesh/` (new follower tests),
`tests/sim`, `CHANGELOG.md`, `docs/ha-integration.md`.
