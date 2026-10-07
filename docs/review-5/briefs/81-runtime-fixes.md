# 81 — Runtime: double clicks, the Time Set retry, the reader's wait, stop order, link readiness

Review 5 · Wave 24 · Closes: R5-1, R5-2, R5-3, R5-4, R5-5, R5-6, A5-2.

Follow the [conventions](../../review-4/briefs/README.md#conventions) in full, with these additions: CHANGELOG bullets
go under `## 1.5.0 (unreleased)` (create it above the newest released section; never edit released sections); every
new "unverified on air" marker is cited in `docs/on-air-sweep.md`. The findings are summarised in
[`../plan.md`](../plan.md); the review reports with each finding's full evidence are handed over in the prompt.

## Goal

The runtime findings of review 5, each a small fix with its regression test.

## Steps

1. R5-1: the click that completed a double click is no longer the last click.
2. R5-2: the Time Set retried while the store stalls is rebuilt with the current time.
3. R5-3: the property reader waits for the connect-time refresh on every link (an event, not a fixed delay).
4. R5-4: `async_stop` detaches the link (or stops key events) before `gestures.cancel_all`, and nothing arms a timer
   after it.
5. R5-5: "link up" for the hub means the client's `ready` (attach through), not `proxy.connected`.
6. R5-6: a gateway entry's unconfirmed pin is checked once per link at most, after the refresh, logs once, and a
   malformed answer is caught and logged.
7. A5-2: the topology image draws a link change at once (as the overview does since wave 12).
8. Improvements: hop bands without heartbeats where possible (or the hint in the band's heading); heartbeats and
   authenticated control PDUs count as link traffic for the watchdog; `drop_link` never drops a newer link;
   a per-link config re-read timer for links that hold for days; `async_begin_rebuild` guarded on `stopping`.

## Tests to add

A regression test per finding.

## Files touched

`hub/gestures.py`, `hub/clock.py`, `properties/reader.py`, `hub/refresh.py`, `coordinator.py`, `hub/link.py`, `image.py`
