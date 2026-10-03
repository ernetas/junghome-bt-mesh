# 08 — Survive an HA backup restore without nonce reuse

Phase P0 · Wave 2 · Size M · Closes: D5 (S4-1); folds in S I1.

Follow the [conventions](README.md#conventions) in full.

## Goal

A sequence-number record restored from a Home Assistant backup (or left by an HA that died during one) never resumes
below numbers already sent.

## Background

HA custom integration `custom_components/junghome_ble`; library `jhmesh`. HA's own source address sends with a 24-bit
sequence number per IV index; reusing (src, IV, seq) reuses an AES-CCM nonce under the NetKey / AppKey. The counter
lives in three HA stores: primary `.storage/junghome_ble.seq.<mesh>`, `.backup`, and `.floor` (written by the
lost-record repair), all in the config directory. `HAState` (`coordinator.py:900-1125`) keeps the in-memory counter
within what both copies durably hold plus a margin.

- No backup platform exists (`custom_components/junghome_ble/backup.py` is absent). HA's restore replaces the whole
  config directory, so primary, backup and floor come back together, readable, with the `clean` mark either way.
- `JungHomeHub.async_create` (`coordinator.py:1536-1577`) trusts a readable record and resumes at its seq + margin,
  re-sending every number sent since the backup. If the IV index moved meanwhile, the first beacon restarts the counter
  at 0 under the new index (`LocalState.apply_beacon`, `jhmesh/client.py:691-696`), reusing what was sent there.
- Detection today is reactive (`pdus_dropped` after the filter and the refresh went out); `seq_store_lost` never
  fires because the records are fine. Reproduced on the project's own `HAStateMachine`: snapshot storage mid-run, keep
  sending, restore, restart → the counter restarts below the highest number sent.

## Read first

`coordinator.py` `SeqStore` (~491), `async_skip_seq_store_ahead` (~686-741), `HAState` (~900-1126),
`JungHomeHub.async_create` (~1508-1578); `jhmesh/client.py` `apply_beacon`, `seq_guard` (~647-700);
`tests/test_properties_seq_store.py` (whole); HA `homeassistant/backup_restore.py`,
`homeassistant/components/backup/manager.py` (`async_pre_backup` / `async_post_backup` platform hooks); an existing
`backup.py` platform in HA core (recorder) as an example.

## Steps

1. Add `custom_components/junghome_ble/backup.py`:
   - `async_pre_backup(hass)`: for every live `HAState` (`hass.data[SEQ_OWNERS]`) set `backup_token = <random hex>`,
     force an immediate save of both copies, and wait (bounded, about 10 s) until `store.written` and
     `backup_store.written` carry the token. On timeout log and continue: never block a backup.
   - `async_post_backup(hass)`: clear the token and force a save.
2. Include `in_backup: <token>` in the snapshot while set; bump `SEQ_STORAGE_MINOR_VERSION` (this brief owns the bump;
   brief 09 adds optional fields without one). The token never appears in `to_dict()` / diagnostics.
3. `async_create`: a record carrying `in_backup` → WARNING ("restored from a backup, or Home Assistant stopped during
   one"), rewrite it as the lost-record repair does: seq + `SEQ_SKIP_AHEAD` (capped at `SEQ_TX_LIMIT`),
   `seq_guard = SEQ_GUARD_FIRST_BEACON`, `iv_known` kept, `clean: False`; write the floor first; save before the
   `HAState` is built.
4. Optional second signal: a record whose IV index is behind the first authenticated beacon by ≥ 2 is treated as
   restored (the guard keeps the counter from restarting at 0 under an index it may have used).
5. Docs: what a restore costs (2^20 of 2^24 numbers per index, once) and why.

## Tests to add

- State-machine rule `backup_taken` (runs the pre/post hooks, deep-copies the seq keys of `hass_storage` between them)
  and `restore` (puts the copy back, marks the stores dead as `killed` does, re-creates). The existing reuse assertion
  is the oracle; today this sequence reuses numbers.
- Hooks: token set and waited for; timeout path; no owners; a superseded owner.
- `async_create` with a token record skips ahead and writes the floor first.
- The token never leaks into diagnostics.

## Acceptance criteria

Gates green; the new rule runs in the default Hypothesis profile and finds no reuse.

## Verifiable on air here?

Yes, with lights only. Take an HA backup, toggle a light many times, restore the backup, restart: lights answer at
once and no `pdus_dropped` issue appears (today the filter goes unanswered first).

## Risks / off-by-default / "unverified on air"

A crash between pre and post leaves the token: the next start skips ahead once (acceptable, documented). Check that
Supervisor backups run the same hooks. The automatic skip is decision M6.

## Depends on

01. Brief 16 extends the store format after this.

## Files touched

New `backup.py`, `coordinator.py` (`HAState`, `async_create`, store version), `tests/test_properties_seq_store.py`,
new `tests/test_backup.py`, `CHANGELOG.md`, `docs/ha-integration.md`.
