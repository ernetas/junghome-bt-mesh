# 20 — Own-source detection and proxy-configuration checks

Phase P1 · Wave 5 · Size S–M · Closes: P4-7 — folds in S I2 (own-source detection).

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
`Claude-Session` trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock
times, CHANGELOG bullet + docs update).

## Goal

Tell "another client uses our address" apart from "our counter is stale", stop sending in the first case so nothing
reuses nonces, and give proxy configuration PDUs the header and replay checks every other PDU has.

## Background

`custom_components/junghome_ble` (HA integration) talks to the mesh through `jhmesh/client.py` `ProxyClient`.
Sequence numbers per source address must never repeat under one IV index.

- **S I2.** `_on_network_pdu` (`jhmesh/client.py:1866-1867`) drops every PDU whose SRC is ours as "our own echo".
  A PDU from our address with an (IV, SEQ) we never handed out (above our counter, or under an index we never used)
  proves a second client on our address — a second HA instance, the CLI on HA's address, a restored store. Today
  this surfaces only as the `pdus_dropped` guess (`coordinator.py:2497-2521`). The blacklist filter forwards other
  clients' traffic, so the proxy does deliver it.
- **P4-7.** Proxy configuration PDUs (`client.py:1752-1777`) go through `_network_decrypt(proxy=True)` with no
  `_is_replay` and no CTL=1 / DST=0x0000 check; a replayed Filter Status sets `_filter_acked` and `proxy_addr`. Only
  the proxy itself can exploit it (plausible, low).

## Read first

- `jhmesh/client.py`: `_on_network_pdu` (~1860-1930), the proxy-configuration branch (~1752-1777), `set_filter` /
  `_resend_filter` (~1096-1198), `LocalState` (`seq`, `tx_iv_index`, `seq_guard`).
- `coordinator.py`: `HAState.reserve_seq` (~1064), `_filter_status_overdue` (~2497), `_report_pdus_dropped`,
  `async_skip_ahead` (~4388); brief 12's stall handling.
- `repairs.py` (the existing skip-ahead fix flows), `const.py` issue keys, `strings.json` `issues`.
- `tests/sim` (proxy echo behaviour), `tests/jhmesh/test_client.py`.

## Steps

1. In `_on_network_pdu`, when `n.src == state.src`: if `(n.iv_index, n.seq)` is above `(tx_iv_index, seq - 1)` or
   under an index never transmitted, call a new `on_foreign_own_source(iv, seq)` callback (once per link,
   rate-limited); otherwise drop silently as today.
2. Hub: on that callback raise a fixable issue `address_shared`. Its fix skips past the seen number +
   `SEQ_RESTART_MARGIN`; a second sighting after the fix tells the user to give HA another address. While the issue
   is open, `HAState` refuses sends with an `AddressShared` subclass of `SequenceStalled` (no reuse meanwhile).
3. Proxy configuration: require `n.ctl and n.dst == 0`, and keep a per-link "last proxy-config SEQ" so an older or
   equal one is ignored; count drops for diagnostics.
4. Strings for the issue and its fix flow in both files; a troubleshooting entry in the docs.

## Tests to add

- Library: a PDU from our address with a higher seq → callback once; our own echo (lower seq) → ignored; an IV
  update edge (our PDU under the old index after the switch) → ignored.
- HA level with the fake link: inject such a PDU → issue raised, sends refused, the fix skips and clears it.
- Proxy config: CTL=0 or DST≠0 rejected; a replayed Filter Status ignored. 100 % branch on the new library code.

## Acceptance criteria

Gates green; today's `pdus_dropped` tests pass unchanged where the store is healthy.

## Verifiable on air here?

Partly. With lights only, run `tools/mesh_poc.py` from a second checkout on a spare address that HA is
temporarily configured to use (or the reverse), send one command, and check the issue. Do not leave two clients on
one address longer than that test.

## Risks / off-by-default / "unverified on air"

A proxy that echoes our PDU unchanged is ≤ the reserved seq and stays ignored — verify with `tests/sim`. A false
positive costs one skip of 2^20 numbers; keep the rate limit.

## Depends on

12 (stall handling and `SequenceStalled` use). Decision M6 if the skip is made automatic.

## Files touched

`jhmesh/client.py`, `coordinator.py`, `repairs.py`, `const.py`, `strings.json`, `translations/en.json`,
`tests/jhmesh/test_client.py`, `tests/test_seq_store.py` (or a new `tests/test_own_source.py`), `CHANGELOG.md`,
`docs/ha-integration.md`.
