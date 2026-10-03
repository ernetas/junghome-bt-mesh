# 15 — One link lifecycle, grace, short-link penalty

Phase P1 · Wave 4 · Size M · Closes: D13 (R4-1), D14 (R4-3), D-low R4-6 = H4-5; folds in R I-1, part of R I-2,
R I-11.

Follow the [conventions](README.md#conventions) in full.

## Goal

Every way a proxy link ends goes through one helper that starts the link-loss grace, sets the link state and logs the
reason; a proxy that keeps dropping is set aside; central entities share the grace; a command interrupted by a link
change is retried once on the new link.

## Background

HA custom integration `custom_components/junghome_ble`. The hub (`coordinator.py`, `JungHomeHub`) keeps one GATT
proxy link; a 20 s `LINK_LOSS_GRACE` keeps entities available while the next proxy takes over (`link_available`,
`coordinator.py:1743-1755`).

- R4-1: `_connection_pass` (`coordinator.py:2188-2242`) puts a proxy on cooldown only when connecting fails or the
  watchdog drops it for silence. A link that comes up and is lost seconds later leaves no trace: after a 1 s pause
  the next pass picks the same strongest proxy again. Each link restarts the connect-time refresh, renews heartbeat
  deadlines, cancels re-probes, and Time Set / location never go out (sent only after a complete refresh,
  `:2594-2601`). Reproduced: a second proxy in range, the strongest dropped six times, reconnected all six.
- R4-3: only `ProxyClient.handle_disconnected` calls `on_disconnect` (`jhmesh/client.py:1085-1094`); `detach()` does
  not. So the hub's own drops — watchdog silence (`coordinator.py:2270`), probe (`:2287`), `async_skip_ahead`
  (`:4402`), the loop's error path (`:2179`) — get no grace: entities go unavailable at once and HA's service helper
  silently skips them, dropping automation commands. A link lost during `attach()`'s 0.3 s settle is reported
  connected by `_connect_to` (`:2459-2466`). Reproduced after `async_skip_ahead`.
- R4-6 = H4-5: `JungHomeCentralEntity.available` returns `hub.connected` (`entity.py:525-528`), not `link_available`.
- R I-11: a link change during `_load_command` (`:4487-4500`) raises `TimeoutError` instead of retrying once.

## Read first

`coordinator.py:2159-2300` (`_connection_loop`, `_connection_pass`, `_watch_link`), `:2414-2470` (`_connect_to`),
`:2557-2610` (`_on_disconnect`, `_start_grace`, `_after_connect`), `:4388-4410`, `:4432-4500`; `const.py:277-292`
(grace design); `jhmesh/client.py:1079-1094` (read only); `entity.py:420-440`, `:520-530`; `tests/test_link_loss.py`,
`tests/test_coordinator.py` (`test_reconnects_to_the_strongest_visible_proxy`,
`test_connect_failures_back_off_and_rotate_proxies`), `tests/test_central.py`.

## Steps

1. `_drop_link(reason: str, *, penalise: bool)`: cancel the refresh, `await proxy.detach()` (bounded), `_start_grace()`,
   `_set_link_state(LINK_DISCONNECTED)`, log the reason; use it in every self-detach path. Expose a link-loss hook for
   later briefs (19 ends dim holds there, 25 records link history).
2. `_connect_to`: check `self.proxy.connected` right after `attach()`; a link already gone raises `ConnectionError`
   (failure path, no "connected" blip).
3. Record `connected_since`; in `_connection_pass` a link shorter than `SHORT_LINK` (about 60 s, in `const.py`) marks
   the proxy failed and doubles the back-off; reset the back-off only after a long link; after 2–3 short links in a row
   prefer the next candidate; keep the `or cands` fallback.
4. Optional: keep `_alive_deadline` across a short link; send Time Set and location right after the filter (or leave
   that to brief 21 and say so).
5. Central entities: `available` → `self.hub.link_available`.
6. `_load_command`: when the link generation changed during the wait, retry once on the new link.

## Tests to add (marker `link_loss_grace` where it matters)

- A watchdog drop and `async_skip_ahead` keep entities available for the grace; a command during it waits for the
  next link (the reviewer's repro: today `available` is False straight away after `async_skip_ahead`).
- A drop during attach's settle shows no available → unavailable blip.
- A proxy dropped three times right after connecting is passed over for the second proxy (the flap repro as a test);
  back-off grows across short links, resets after a long one.
- A central entity stays available within the grace and its command waits.
- A command interrupted by a link change succeeds on the next link.

## Acceptance criteria

Gates green; the new tests fail on `main` and pass after the change.

## Verifiable on air here?

Partly, with a person at home: switch off the breaker of the node HA is connected through (a socket or actuator);
check a light command issued within 20 s still goes out on the next proxy and entities do not flap. A silent proxy
cannot be forced safely: the watchdog path stays test-only.

## Risks / off-by-default / "unverified on air"

Reordering detach and grace can reintroduce the "event still set" race documented at `async_wait_connected`. A too
long `SHORT_LINK` can rotate away from the only good proxy.

## Depends on

12 (`_keep_alive` / `_watch_link` area). Briefs 19, 21, 25, 27 build on it.

## Files touched

`coordinator.py`, `const.py`, `entity.py`, `tests/test_link_loss.py`, `tests/test_coordinator.py`,
`tests/test_central.py`, `CHANGELOG.md`, `docs/ha-integration.md`.
