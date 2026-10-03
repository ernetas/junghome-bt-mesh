# 12 — Bounded back-pressure, stalled-store repair

Phase P0 · Wave 3 · Size S–M · Closes: D9 (R4-2 = S4-5; corrects review-3 Status), D-low R4-4; folds in R I-3, I-4,
S I3.

Follow the [conventions](README.md#conventions) in full.

## Goal

Store back-pressure is retried for a bounded time; real sequence exhaustion propagates so the link watchdog keeps
working; a store that cannot be written raises its own repair instead of a misleading `pdus_dropped`; and a send with
no link fails before it reserves a sequence number.

## Background

HA custom integration `custom_components/junghome_ble`; library `jhmesh`. `HAState.reserve_seq`
(`coordinator.py:1064-1094`) raises `SequenceExhausted` (a `ConnectionError`) for three conditions: superseded by a
newer hub, store back-pressure (`SequenceStalled`, a subclass, `jhmesh/client.py:89-102`), and the 24-bit space really
used up (`LocalState.reserve_seq`, `client.py:632-645`).

- `_while_seq_stalls` (`coordinator.py:2873-2891`) catches `SequenceExhausted` — the base class — and retries every
  `SEQ_STALL_RETRY` for as long as `self.connected`. `_keep_alive` (`:2335-2347`) sits behind it, so under real
  exhaustion or a store that never lands a write (an SD card remounted read-only) it never returns and `_watch_link`
  never drops a silent proxy. Reproduced: with `seq = SEQ_TX_LIMIT + 1`, `_keep_alive` loops thousands of times.
- Review 3's Status lists "repair when the seq store refuses for minutes" as done; it was never built: no stall issue
  key in `const.py`, `strings.json` or `repairs.py`. The user instead gets `pdus_dropped` after
  `FILTER_STATUS_TIMEOUT` (`_filter_status_overdue`, `coordinator.py:2497-2521`, which does not ask whether the filter
  request ever went out); its fix skips 2^20 ahead and cannot persist either.
- R4-4: `ProxyClient._send` reserves (`next_seq()`, which persists) before `_write` finds `self.client is None`
  (`client.py:1211`, `:1226-1227`, `:1332-1359`); also `set_filter`, `_send_ack`. The `PropertyReader` worker
  (`config_entities.py:1241-1252`) and `KeepAwake` (`keep_awake.py:113-129`) keep sending while unlinked; after
  `async_stop` such a send clears the clean-close mark (`coordinator.py:1006`). Reproduced: ten sends with no link used
  ten numbers.

## Read first

`coordinator.py:491-540` (`SeqStore`), `:900-1125` (`HAState`, the HAC notes), `:2335-2350`, `:2497-2521`,
`:2840-2891`, `:4388-4410`; `jhmesh/client.py:89-102`, `:1096-1198`, `:1300-1420`, `:2048`; `keep_awake.py:94-134`;
`config_entities.py:1241-1252`; `tests/test_seq_store.py`, `tests/test_properties_seq_store.py` (`FlakySeqStore`).

## Steps

1. `_while_seq_stalls`: `except SequenceStalled` retries until `SEQ_STALL_DEADLINE` (about 120 s); plain
   `SequenceExhausted` re-raises.
2. `_keep_alive`: a refused send means "no verdict": decide by `_last_rx` alone and let the watchdog drop the link
   after the usual silence.
3. `HAState`: track `_stalled_at` (exists) and the last `WriteError` text (wrap `SeqStore`'s write); a timer raises
   `seq_store_unwritable` (not fixable; names the storage path and the error) once stalled for about 60 s, and the
   first successful reserve deletes it. Text must not advise restoring a backup.
4. No `pdus_dropped` while `state._stalled_at is not None` or when no filter request was written on this link (count
   writes in the client).
5. `ProxyClient`: raise `ConnectionError("not connected to a proxy")` before any `next_seq` / `reserve_seq` when
   `self.client is None`, still under `_send_lock` (order of reserve and write unchanged) — in `_send`,
   `_send_segments_once`, `set_filter`, `_send_ack`.
6. Reader worker and keep-awake: `await hub.async_wait_connected(...)` while there is no link instead of spinning.
7. Diagnostics fields: `stalled_for`, `last_write_error`, `durable_headroom`.

## Tests to add

- Keep-alive returns under real exhaustion (the reviewer's repro as a test); the watchdog drops a silent proxy while the
  store is stalled.
- A `FlakySeqStore` failing for 60 s raises `seq_store_unwritable` and no `pdus_dropped`; recovery clears it.
- A send with no link leaves `state.seq` unchanged and `_closed` intact (repro as a test); library tests for every send
  path's early refusal (100 % branch).
- State-machine tests unchanged and green.

## Acceptance criteria

Gates green; `tests/test_properties_seq_store.py` passes unchanged; existing `pdus_dropped` tests pass where the store
is healthy.

## Verifiable on air here?

Regression only: commands and the connect refresh still work, no `pdus_dropped`. Exhaustion and a read-only store are
test-only (a read-only remount on a test host is possible, never on the production host).

## Risks / off-by-default / "unverified on air"

The send path is nonce-critical: the early refusal must happen before reserving, never after; do not reorder reserve
and write under the lock.

## Depends on

08 (same `HAState` / `SeqStore` region). Briefs 15, 16, 20, 26 build on it.

## Files touched

`coordinator.py`, `jhmesh/client.py` (send paths), `keep_awake.py`, `config_entities.py` (reader worker), `const.py`,
`diagnostics.py`, `strings.json`, `translations/en.json`, `tests/test_seq_store.py`, `tests/jhmesh/test_client.py`,
`tests/test_keep_awake.py`, `CHANGELOG.md`, `docs/ha-integration.md` (troubleshooting).
