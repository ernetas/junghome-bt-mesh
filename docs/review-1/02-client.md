# CLI — mesh client

> Fixed and removed from this file: CLI-01, CLI-02, CLI-03, CLI-10, CLI-14, CLI-04, CLI-05, CLI-06, CLI-07, CLI-08, CLI-09, CLI-11, CLI-12, CLI-13. What changed and why is in `10-implementation-log.md`.

## Shard summary

| Severity | Count | IDs |
|---|---|---|
| P0 | 1 | CLI-01 |
| P1 | 1 | CLI-02 |
| P2 | 5 | CLI-03, CLI-04, CLI-05, CLI-10, CLI-14 |
| P3 | 7 | CLI-06, CLI-07, CLI-08, CLI-09, CLI-11, CLI-12, CLI-13 |

Suggested order: CLI-01 → CLI-02 → CLI-03 (all three touch the counter's safety; CLI-02 and CLI-03 both edit `LocalState.__init__`/`_restore`/`_backup`, so do them together) → CLI-10 / CLI-14 (both touch `apply_beacon`/`attach`) → CLI-04 → CLI-05 → P3s. CLI-06 builds on CLI-01's reservation.

Checked and found clean:
- `apply_beacon` transitions: Normal → In Progress (index + 1, flag set), In Progress → Normal, recovery up to +42. The stale "in progress" flag for the current index is ignored. The transmit index never goes backwards, and `seq` resets only when it grows.
- IV-index/sequence pairing everywhere except CLI-01. `_send` reads `tx_iv_index` inside `_send_lock`. `_send`, `_send_ack`, `set_filter` and the first segmented round read the index and allocate numbers in the same synchronous stretch. `reserve_seq` never wraps and consumes nothing on `SequenceExhausted`.
- File state (CLI path): the `flock` on a separate `.lock` inode, the atomic `.tmp` + fsync + `os.replace`, and the refusal of a second process. HA's `HAState` (path `None`) never touches `.bak`, so CLI-02/03 are CLI-only apart from `_restore`'s partial assignment.
- the previous fix pass's RPL rework: the segmented replay check on SeqAuth at reassembly start, `note_received` never lowering an entry, persistence batching plus `flush()` on link release, and a purge that keeps current−1. the previous fix pass's malformed lower/access PDU drops, the SeqZero-before-IV-start drop, the SegO>SegN and SegN mismatch drops, and the BlockAck 0 cancel.
- SAR TX: the ack event is cleared before the writes (early acks are kept), the block bitmap is cumulative, and the ack waiter is always popped in `finally`. The group path sends twice with no waiter.
- Request/response: waiters are removed in `finally` on timeout and cancellation, a late reply to a timed-out segmented send is still returned, one status resolves one (the oldest) waiter, and `request_config` matches on the device-key label and source.
- Link management: attach-over-attached releases and disconnects the old client, attach failure (including cancellation) detaches, a late disconnect callback for a foreign client is ignored, `_spawn` keeps task references and logs failures, and link release cancels tasks and fails waiters. `_write_lock` keeps one PDU's SAR frames together.
- `standalone.py`: `_wake` is cleared before connecting (the previous fix pass), the scanner stops on cancellation (context manager), the failed-proxy cooldown works and the `or cands` lone-proxy fallback applies, backoff is capped at 30 s, and `stop()` detaches after cancelling.
