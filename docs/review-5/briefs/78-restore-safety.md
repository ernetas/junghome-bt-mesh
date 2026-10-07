# 78 — Restore safety: the skip a restored record takes, allocation away from heard sources

Review 5 · Wave 24 · Closes: S5-1 (high), S5-5, S5-4.

Follow the [conventions](../../review-4/briefs/README.md#conventions) in full, with these additions: CHANGELOG bullets
go under `## 1.5.0 (unreleased)` (create it above the newest released section; never edit released sections); every
new "unverified on air" marker is cited in `docs/on-air-sweep.md`. The findings are summarised in
[`../plan.md`](../plan.md); the review reports with each finding's full evidence are handed over in the prompt.

## Goal

A sequence record restored from a Home Assistant backup (or rebuilt with nothing left) must never resume below a
number already sent, however old the backup; and a restore must not let Home Assistant hand out an address or group a
device provisioned after the backup is using.

## Steps

1. Every sequence record carries the wall time of its write (`written_at`) and a send-rate checkpoint (for example a
   daily `(time, seq)` pair kept per address). The backup mark (`backup.py`) also records the backup's time.
2. On a restored record (`seq_store.py::_async_skip_restored_record`) skip `max(SEQ_SKIP_AHEAD, 2 × rate × (now −
   written_at))`; when that would pass `SEQ_TX_LIMIT`, do not cap silently: raise a repair (start an IV Update, or a
   new address) and send nothing. Correct the `const.py` rationale with the real rates (energy polls, heartbeat
   publication Sets to dead nodes).
3. S5-5: with store, `.backup` and `.floor` all gone but evidence of use (`_evidence_of_use`), continue
   `SEQ_SKIP_UNKNOWN`, not 2^20 from 0 (or route it through the `seq_store_lost` repair).
4. S5-4: every allocator (unicast blocks and element groups) avoids every source heard on air (`state.rpl`, the
   liveness `last_seen`), on top of the export and the vault. Document in `backup.py` that a restore rolls the vault
   and the export back, and what that means for a device provisioned after the backup.
5. Diagnostics: per address, numbers per day and how many days a restore skip covers.

## Tests to add

A Hypothesis state machine step "restore a backup taken at any earlier step" over sends at a measured rate: no
number is ever reused. The skip, the refusal past the limit, the evidence-of-use start, allocation avoiding heard
sources, the diagnostics fields.

## Files touched

`seq_store.py`, `backup.py`, `const.py`, `coordinator.py` (`_evidence_of_use`), allocators (`configurator/store.py`, `onboard.py`, `jhmesh/export.py`), `diagnostics.py`
