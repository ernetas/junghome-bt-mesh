# 16 — Skip target, floor, mesh-level state, CLI address

Phase P1 · Wave 4 · Size S–M · Closes: D-low S4-7, S4-8, S4-9, S4-11; folds in S I5 (fresh-address offset), S I7,
S I12, optional S I11 (CLI counter batching).

Follow the [conventions](README.md#conventions) in full.

## Goal

The lost-record repair only trusts range-checked numbers, its floor keeps up with the counter, key-refresh progress is
saved at once and at mesh level, an address used before never restarts at 0 because its record is missing, and the
CLI cannot share HA's counter.

## Background

HA custom integration `custom_components/junghome_ble`; library `jhmesh`; CLI `tools/mesh_poc.py`. HA's sequence store
has a primary, a `.backup` and a `.floor` (written only by the `seq_store_lost` repair).

- S4-7: `_furthest` / `_seq_skip_target` (`coordinator.py:654-683`) use `_tx_rank` without range checks; a record with
  `iv_index` 2^40 makes the repair write an unusable record and a floor no repair recovers from; a negative `seq` gives
  a target of 0 under its index (reproduced). `repairs.py:57-63` ignores a `None` return and deletes the issue anyway.
- S4-8: the floor (`coordinator.py:585-600`, `:676-678`) is written only by the repair, so "a second loss cannot reuse
  nonces" holds only until the address has sent about 2^22 more under one index.
- S4-9: `set_key_refresh` (`jhmesh/client.py:573-578`) persists through the 2 s debounced save, and the key refresh
  is stored per address (`coordinator.py:744-769`): a crash within 2 s loses the new key; a unicast change after a
  followed phase-3 refresh loses it too.
- S I5: a first-ever record starts at 0 even when the export or vault shows the address existed (only a warning,
  `coordinator.py:963-973`).
- S4-11 (plausible): the CLI protects only `0D00` (`tools/mesh_poc.py:189-226`), not HA's configured address.
- S I11 (optional): `LocalState.reserve_seq` fsyncs per number (`client.py:632-645`).

## Read first

`coordinator.py:585-770`, `:924-974`, `async_create`; `jhmesh/client.py` `set_key_refresh`, `_resume_key_refresh`,
`reserve_seq`, `_check_range`; `repairs.py`; `jhmesh/vault.py` `recognise`; `tools/mesh_poc.py` (`with_client`,
`source_problem`, `state_path`); `tests/test_properties_seq_store.py` (docstring: "documented start-at-0 case").

## Steps

1. `_furthest`: skip values outside `0..IV_INDEX_MAX` / `0..SEQ_MAX` (reuse `_check_range`). The repair flow aborts
   with `floor_not_written` (translated) when `async_skip_seq_store_ahead` returns `None` and keeps the issue.
2. `HAState` updates the floor whenever the transmit index changes and every 2^20 numbers; `_seq_skip_target` trusts
   only what landed (`written`).
3. Key refresh: immediate save in `set_key_refresh`; store `key_refresh` at mesh level
   (`{"addresses": …, "mesh": {"key_refresh": …}}`), read there with a fallback to the per-address record; a minor
   store version bump after brief 08's.
4. Fresh address with evidence of earlier use (export node or provisioner node at this address via
   `vault.recognise`, a vault identity, other addresses in the store): start at `SEQ_SKIP_AHEAD` with
   `seq_guard = SEQ_GUARD_FIRST_BEACON`; otherwise 0. Decision M6 (always skip once?).
5. CLI `--ha-storage <path to .storage>`: read `junghome_ble.seq.<mesh>` and refuse any `--source` it holds.
6. Optional: CLI reserves blocks of 64 and persists the block end, covered by the margin.

## Tests to add

- `_seq_skip_target` with out-of-range and negative values (the reviewer's repro as a test); the repair aborts on a
  failed floor write and keeps the issue.
- State-machine rule: an address sends past 2^22 under one index, then loses both copies twice → no reuse.
- `set_key_refresh` + a simulated kill keeps the phase; a unicast change after phase 3 still applies the key.
- `async_create` with each kind of evidence; CLI refusal tests in `tests/test_cli.py`.

## Acceptance criteria

Gates green; state machines extended and green; the store stays readable by the previous release (minor bump only).

## Verifiable on air here?

Regression only; optionally a fresh store on a used address answers on the first link instead of after the
`pdus_dropped` repair (lights only, on a test install).

## Risks / off-by-default / "unverified on air"

More floor writes (negligible). Spending 2^20 numbers on a genuinely fresh install that matches the evidence: note it
in the CHANGELOG.

## Depends on

04, 08, 09, 12.

## Files touched

`coordinator.py`, `repairs.py`, `jhmesh/client.py` (`set_key_refresh`, optional `reserve_seq`), `tools/mesh_poc.py`,
`strings.json`, `translations/en.json`, `tests/test_seq_store.py`, `tests/test_properties_seq_store.py`,
`tests/test_cli.py`, `CHANGELOG.md`, `docs/ha-integration.md`.
