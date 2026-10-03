# 25 — Diagnostics in every entry state, log once per node, link history

Phase P1 · Wave 6 · Size S–M · Closes: H4-6, H4-7, H4-8 — folds in H I-4, H I-5, R I-9 (link history), H I-8
(entity defaults, only with decision M9).

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
`Claude-Session` trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock
times, CHANGELOG bullet + docs update).

## Goal

Diagnostics download works while an entry retries or failed and includes the options; an unreachable device logs one
line per transition; the link-state sensor is the visible health indicator; diagnostics show why recent links
ended; translators get no English prose in placeholders.

## Background

`custom_components/junghome_ble` (HA integration) for a JUNG HOME Bluetooth Mesh; `jhmesh/client.py` is its library
transport.

- **H4-6.** `diagnostics.py:206`, `:316` read `entry.runtime_data` unguarded → `AttributeError` (HTTP 500) in
  `SETUP_RETRY` / `SETUP_ERROR`, exactly when diagnostics help; `entry.options` are not dumped.
- **H4-7.** `jhmesh/client.py:1650` logs a WARNING per unanswered attempt unless `quiet`; the connect refresh and load
  commands are not quiet, so each dead element logs several lines on every hub start (`log-when-unavailable`).
- **H4-8.** `coordinator.py:1943-1950` puts a full English paragraph into the `unknown_nodes` `{gateway}`
  placeholder.
- **H I-5.** *Link state* is disabled by default while *Proxy node* is on (`sensor.py:1077-1090`).
- **R I-9.** Nothing records why a link ended; brief 15 adds `_drop_link(reason)` and link durations.
- **H I-8.** Many rarely used CONFIG entities are on by default and each is read over the mesh at every start.

## Read first

- `diagnostics.py`; HA `components/diagnostics/__init__.py` (download view).
- `jhmesh/client.py` request retry logging (~1600-1660); `coordinator.py` `_get_state` (~3045-3080), `_missed_answer`
  (~3272-3310), `unknown_nodes` issue (~1930-1955), brief 15's `_drop_link`.
- `sensor.py:1047-1110`; `config_entities.py` `enabled_default` definitions; `migration.enable_now_default`.
- `tests/test_diagnostics.py`, `tests/test_link_loss.py`, `tests/test_snapshots.py`.

## Steps

1. Diagnostics: when the entry is not LOADED, return redacted entry data, options, state and reason, visible
   connectable proxies (MACs redacted; Network ID or Node-Identity flag and whether it matches the export), an
   export summary if `load_network` succeeds in the executor, and this domain's issue ids. When loaded, add
   `"options"` and a `link` block: the last 20 links (proxy redacted, duration, drop reason, refresh duration, how
   long sends were held back), from a `deque(maxlen=20)` filled by `_drop_link`.
2. Library: per-attempt request lines to DEBUG (keep the final `TimeoutError`); the coordinator's per-node transition
   WARNING stays.
3. `unknown_nodes`: two translation keys (`unknown_nodes`, `unknown_nodes_gateway`), only `host` as placeholder.
4. *Link state* enabled by default (new registrations).
5. Only if decision M9 says so: turn off by default *Time change active*, *Manual switch-off during run-on time*,
   *Use previous brightness*, *Warm dimming*, LED colours, *Run-on time*, *Switch-on brightness / colour
   temperature* — new registrations only; keep *Status LED*, *LED night mode*, *Lock operation*.

## Tests to add

- Diagnostics in `SETUP_RETRY` (no proxy) and with options; no key in the dump (reuse the secret scan).
- Link history after two drops (reasons present).
- An unreachable load during refresh logs no WARNING from `jhmesh` (caplog).
- `unknown_nodes` placeholders per variant; translations test.
- Snapshot diff limited to `disabled_by` changes.

## Acceptance criteria

Gates green; the snapshot diff reviewed and explained in the report.

## Verifiable on air here?

Yes: pull a light's breaker and read the log and link-state sensor; disable the Bluetooth adapter and download
diagnostics while the entry retries.

## Risks / off-by-default / "unverified on air"

Fewer default entities change docs (update the parameter section); library log-level changes affect CLI users
(they use DEBUG anyway). Redact every MAC and address-like value the new blocks add.

## Depends on

15 (`_drop_link` feeds the ring buffer). Decision M9. Soft: 28 (fewer reload re-read waves). Brief 45 builds on it.

## Files touched

`diagnostics.py`, `sensor.py`, `jhmesh/client.py`, `coordinator.py`, `config_entities.py` (only with M9),
`strings.json`, `translations/en.json`, `tests/test_diagnostics.py`, `tests/test_link_loss.py`,
`tests/snapshots/test_snapshots.ambr`, `CHANGELOG.md`, `docs/ha-integration.md`.
