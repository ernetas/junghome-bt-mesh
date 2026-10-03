# 11 — Clocks, timeouts, snapshots, regen, secret scan

Phase P1 · Wave 2 · Size M · Closes: D27 (Q4-6), D28 (Q4-7), D-low Q4-12 (fixture regeneration), Q4-13, Q4-20;
folds in Q T8 and Q C5 (duration budget).

Follow the [conventions](README.md#conventions) in full.

## Goal

No test waits on the real clock or passes trivially on a slow runner; one patch covers every property timeout; every
fixture network's registry identity is pinned; fixtures provably regenerate; the secret scan catches every encoding
of every key.

## Background

HA custom integration `custom_components/junghome_ble`; tests under `tests/` (HA level), `tests/jhmesh` (library),
`tests/sim` (simulated mesh).

- Q4-6: `tests/test_sensor.py:783-840` (`test_node_versions_survive_a_restart_and_go_with_the_entry`) takes ~10 s:
  three unanswered property Gets wait the real timeout. `fast_timeouts` (`tests/property_helpers.py:195-214`) patches
  only modules already in `sys.modules`; `switch.py:67`, `:659` binds `PROPERTY_READ_TIMEOUT` and is not imported by the
  package `__init__`. Key-refresh tests wait HA's real 1 s `Store` delay.
- Q4-7: `tests/test_snapshots.py:42-58` snapshots only the base network; cover and climate snapshot as `[]`; the
  detectors, puck, RTR and blinds fixtures are never snapshotted, so unique-id changes on the "unverified on air"
  devices would orphan users' entities unseen. The `.ambr` is large because it dumps full state.
- Q4-12: the fixture generators reproduce `tests/fixtures/` byte for byte today, but nothing enforces it.
- Q4-13: `tests/test_diagnostics.py:121-135` searches the dump for the hex of four keys only; derived keys
  (encryption, privacy, beacon, identity, private beacon) and the key-refresh key, and other encodings, are not
  checked; the docstring at `:110` is stale (`inject` segments now, `conftest.py:937-940`).
- Q4-20: `tests/test_config_entities.py:1104-1137` and `tests/test_sensor.py:822` assert absence after `real_wait`.
- Flaky under `-n auto`: `tests/test_reachability.py::test_link_state_follows_the_link_and_bluetooth` failed in 2
  of 12 full parallel runs and never alone or serially (the log of a failing run showed `connecting to
  00:00:5E:00:53:14 failed: no free connection slot; retry in 2s` and left `coordinator.py:1044` uncovered, so the
  run also misses the coverage gate). It asserts the exact sequence of link states (`seen[3:] == ["disconnected",
  "searching"]`) after `drop_link()`, which a reconnect attempt racing the emptied scanner list can extend.
- Coverage flake: one line of `coordinator.py` (the seq-store path near the start of the module's store helpers;
  find it with `coverage report -m` when a run reports it) is reached only by some random examples of
  `tests/test_properties_seq_store.py`, so a run occasionally reports it missed and the integration's 0-missed-lines
  gate fails. Give it a deterministic test (or an `@example`).

## Read first

`tests/conftest.py` (`fast_sleep`, `settle`, `wait_until`), `tests/property_helpers.py`, the tests named above,
`tests/test_snapshots.py`, `tests/fixtures/` and their generators (see `README.md` on fixtures),
`config_entities.py`, `switch.py`, `schedules.py`, `energy_history.py` (timeout imports).

## Steps

1. Read `PROPERTY_READ_TIMEOUT` / `PROPERTY_WRITE_TIMEOUT` at call time from one place (`const` module attribute or a
   hub attribute) in `config_entities.py`, `switch.py`, `schedules.py`, `energy_history.py`; `fast_timeouts` patches
   that one name; fix its docstring.
2. Add `fast_timeouts` to the 10 s test; replace `real_wait`-then-assert-absence with `fast_sleep` + `settle()`.
3. A conftest hook that fails a test whose call phase exceeds 5 s (opt-out marker `slow_ok`, registered in
   `pyproject.toml`).
4. Snapshots: `test_registry_identity[network]` for each fixture network (base, blinds, RTR, detectors, puck, Android
   share export): unique_id, platform, translation_key, entity_category, `disabled_by`, device identifiers,
   connections, `via_device`. Keep the full-state snapshot for the base network. Review the regenerated `.ambr`.
5. `tests/test_fixtures_regen.py`: copy the generators to `tmp_path`, run them, compare every output byte for byte.
6. `assert_no_secrets`: every key and derived key (and the key-refresh key from a refresh scenario), each in lower
   and upper hex, base64, `list(bytes)` and `repr(bytes)`; fix the stale docstring.
7. Make `test_link_state_follows_the_link_and_bluetooth` deterministic: stop the reconnect attempt racing the
   emptied advertisement list (empty `infos` before the drop, or wait for the loop to park), then assert the states
   in order with duplicates allowed rather than an exact slice; run the file 50 times under `-n auto`
   (`pytest --count` is not installed: a shell loop) to show it holds.
8. T8: one module that, after a key-refresh scenario, scans every written file, captured log record and diagnostics
   dump for each key in every encoding (file modes are brief 02's).

## Tests to add

The steps above are the tests. Add a deliberately failing check during development (a key printed into a log record)
to see the scanner catch it, then revert.

## Acceptance criteria

Gates green; slowest test under 5 s; the `.ambr` covers every fixture network; a shuffled run passes.

## Verifiable on air here?

Local only.

## Risks / off-by-default / "unverified on air"

Snapshot regeneration must be reviewed, not rubber-stamped. Reading timeouts at call time must not change values.

## Depends on

01 (conftest changes land first).

## Files touched

`tests/property_helpers.py`, `tests/conftest.py` (duration hook), `tests/test_sensor.py`,
`tests/test_config_entities.py`, `tests/test_snapshots.py`, `tests/snapshots/`, `tests/test_diagnostics.py`, new
`tests/test_fixtures_regen.py`, new secret-scan module, `config_entities.py`, `switch.py`, `schedules.py`,
`energy_history.py`, `pyproject.toml` (marker), `CHANGELOG.md` (Internal).
