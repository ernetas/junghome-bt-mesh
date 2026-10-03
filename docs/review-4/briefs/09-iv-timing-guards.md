# 09 — IV timing guards, fixable IV mismatch

Phase P0 · Wave 2 · Size M · Closes: D10 (P4-2 + H4-3); folds in P I-2, H I-11.

Follow the [conventions](README.md#conventions) in full.

## Goal

HA follows the spec's IV Update timing (at least 96 h in each state, at most one IV Index Recovery per 192 h), so
authenticated beacons cannot ratchet its IV index; and a user whose HA is ahead of the mesh has a repair that works.

## Background

HA custom integration `custom_components/junghome_ble`; library `jhmesh`. `LocalState.apply_beacon`
(`jhmesh/client.py:647-701`) adopts any authenticated beacon up to `iv_index + 42` with no time state: ten beacons at
+42 move HA from 5 to 425 (reproduced); every node then drops HA's PDUs and no real beacon is accepted again
(`iv_index < self.iv_index`). Mesh Protocol 1.1 §3.10.5–§3.10.6 bound this in time (Zephyr enforces 96 h for
followers). Who can send such beacons: a NetKey holder acting as proxy, and perhaps (unproven) anyone in range if JUNG
proxies forward ADV-bearer beacons to their GATT client.

`_check_iv_index` (`coordinator.py:4283-4320`) raises `iv_index_mismatch` with `is_fixable=False`. H4-3: its text
(`strings.json` `issues.iv_index_mismatch`) tells the user to remove the address's store record — setup then resumes
from `.backup` with the same index, or a `.floor` record raises `seq_store_lost`; hand edits are overwritten. The only
working manual path is a new unicast address.

## Read first

`jhmesh/client.py` `LocalState` (`:224-701`: `apply_beacon`, `seq_guard`, `parse_record`, `to_stored`);
`coordinator.py` `HAState` (`:900-1120`), the `seq_store_lost` repair (`:686-740`), `_check_iv_index` (`:4283`);
`repairs.py`; `tests/jhmesh/test_client_state.py`; `tests/property_helpers.py`; `tests/sim/mesh.py:302-315`;
`tests/test_seq_store.py::test_seq_repairs_never_advise_restoring_the_store`.

## Steps

1. Stored record: `iv_changed_at`, `iv_recovered_at` (wall-clock seconds), absent = unknown = no restriction. No
   store minor-version bump here (brief 08 owns it); the old reader ignores extra keys as with `seq_guard`.
2. `apply_beacon(…, now=None)` with an injectable clock; take `max(stored, now)` against clock jumps.
3. Refuse a recovery (index > current + 1, or +1 without the flag) within 192 h of the last; refuse Normal ↔ In
   Progress within 96 h of the last change. A fresh state (`not iv_known`) adopts the first beacon as today. Log each
   refusal once per index at WARNING.
4. Make `iv_index_mismatch` fixable only when HA is *ahead* of the mesh: the fix flow sets the network's beacon index
   with `iv_known=False` and `seq_guard` covering every index HA transmitted under, writing primary, backup and floor
   consistently through the existing helpers. Never move (iv, seq) to a pair already used.
5. Reword the issue: the manual path is "give Home Assistant a new unicast address (Reconfigure)"; never advise
   restoring or editing the store.

## Tests to add

- Ratchet: repeated +42 beacons are refused after the first recovery (the reviewer's repro as a test).
- A legitimate update is followed after 96 h on a fake clock; a recovery after an outage is adopted once.
- Clock jumping backwards does not stall forever (documented rule).
- Fix flow: refused when the mesh is ahead of HA; on confirm, setup starts at the mesh index and every copy is written.
- Property test in a new `tests/jhmesh/test_iv_timing.py`: (src, iv, seq) never repeats across the new transitions and
  the rewind.
- `tests/sim` IV update still followed.

## Acceptance criteria

Gates green; existing state machines find no reuse; `test_seq_repairs_never_advise_restoring_the_store` passes.

## Verifiable on air here?

Regression only: connect and receive beacons. A real IV update or a mismatch cannot be produced safely.

## Risks / off-by-default / "unverified on air"

Must not refuse a legitimate update: keep the fresh-state and first-update cases. Two updates within 96 h are out of
spec. The rewind touches nonce safety: property test first.

## Depends on

01. Brief 16 edits `repairs.py` after this; backlog "IV Update initiation" builds on the timing state.

## Files touched

`jhmesh/client.py` (`LocalState`), `coordinator.py` (`_check_iv_index`), `repairs.py`, `strings.json`,
`translations/en.json`, new `tests/jhmesh/test_iv_timing.py`, `tests/test_seq_store.py` or a repairs test,
`CHANGELOG.md`, `docs/ha-integration.md`.
