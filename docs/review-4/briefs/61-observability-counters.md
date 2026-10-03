# 61 — Link counters and a trace logger

Phase P4 · Wave 18 · Size S · Closes: A4-14 (with P4 I-9's counter ideas).

Follow the [conventions](README.md#conventions) in full. **Not behaviour-identical**: log record names change and
diagnostics gain keys — needs decision M1 first.

## Goal

Traffic logging can be switched on alone, and the link's counters are visible in diagnostics.

## Background

The library logs under `"jhmesh"`, `"jhmesh.provisioning"`, `"jhmesh.standalone"`; per-PDU TX/RX lines share the
`jhmesh` logger with link lifecycle messages, so enabling DEBUG for traffic floods everything else. Counters are
scattered (hub: `_rx_messages`, `_rx_to_us`, `_rx_decoded_link`, `_rx_undecodable_link`, `link_count`; client:
`rx_undecryptable`, `rx_garbage`); there are no TX, retransmission, timeout, replay-drop or proxy-config-replay
counters.

## Read first

`jhmesh/client.py` (TX/RX log lines, the counters), `jhmesh/pdu.py` logger, `diagnostics.py`, the link history
from brief 25, `tests/test_diagnostics.py`, `tests/jhmesh` tests using `caplog` with logger names.

## Steps

1. A `jhmesh.trace` child logger for per-PDU TX/RX lines; lifecycle lines stay on `jhmesh`.
2. A `LinkStats` dataclass on `ProxyClient`: tx, rx, undecryptable, garbage, replays dropped, segment
   retransmissions, request timeouts, proxy-config replays; reset per link, cumulative totals kept.
3. Diagnostics: `link_stats` (current and cumulative); fold the hub's scattered counters into it.
4. One structured DEBUG line per link-state transition.
5. Update `caplog` expectations; document the logger names for users (`logger:` YAML in the docs).

## Tests to add

Counters move on the matching events (library tests, 100 % branch); diagnostics snapshot updated and reviewed;
enabling only `jhmesh.trace` logs PDUs and not lifecycle lines.

## Acceptance criteria

Gates pass; the diagnostics snapshot diff is limited to the new keys; docs list the logger names.

## Verifiable on air here?

Regression only: enable `jhmesh.trace` on the HA host and check traffic lines appear alone.

## Risks / off-by-default / "unverified on air"

Users' existing `logger:` filters on `jhmesh` keep working (child loggers propagate); CHANGELOG says so.

## Depends on

57 (stable `jhmesh` API); decision M1.

## Files touched

`jhmesh/client.py` (or `jhmesh/state.py` / `stats.py`), new `jhmesh/stats.py`, `diagnostics.py`, `coordinator.py`
(counters), tests, `CHANGELOG.md`, `docs/ha-integration.md` (logging section).
