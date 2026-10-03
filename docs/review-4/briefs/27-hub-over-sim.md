# 27 — The HA hub over the simulated mesh, a flapping-link soak, fake conformance

Phase P1 · Wave 6 · Size M · Closes: Q4-19 (rest) — folds in A4-15 (report 8 brief B7), Q T1, Q T6, R I-12.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
`Claude-Session` trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock
times, CHANGELOG bullet + docs update).

## Goal

Hub-level tests can run against `tests/sim` (RPL, SAR, proxy filter, relays, loss, IV updates) instead of only the
idealised `FakeProxyLink`; a seeded soak shows flapping links keep the invariants; one conformance suite keeps the
three proxy fakes in line. Tests only — no production change.

## Background

`custom_components/junghome_ble` (HA integration) is tested through `tests/conftest.py` `FakeProxyLink` (~600
lines), the library through `tests/jhmesh/conftest.py` `FakeBleak`, and `tests/sim` (~2500 lines, a full simulated
mesh used only by `tests/jhmesh/test_sim_mesh.py`). The HA fake answers Set Filter Type with a fixed reject list,
shares one sequence counter across injected sources (brief 01 fixes the counter) and never delivers relayed
duplicates. Flapping links (R4-1, R4-5) went unnoticed because nothing exercises them. `wait_until` exists twice
(`tests/conftest.py:1133`, `tests/property_helpers.py:159`).

## Read first

- `tests/sim/*` (module docstring invariants, `Mesh`, `ProxyNode`, `clock.py`), `tests/jhmesh/test_sim_mesh.py`,
  `tests/jhmesh/fixture_network.py`.
- `tests/conftest.py` (`fake_link`, `init_integration`, `mock_bluetooth_env`, `fast_sleep`, `no_link_loss_grace`),
  `tests/property_helpers.py`.
- `coordinator.py` `establish_connection` use and brief 15's link lifecycle.

## Steps

1. Drop `property_helpers.wait_until` and import the conftest one (check signatures).
2. Fixture `sim_mesh(cdb)` over the fixture network (default hop matrix, no loss) on HA's test loop.
3. Fixture `sim_link(sim_mesh)` patching `custom_components.junghome_ble.coordinator.establish_connection` to return
   `sim_mesh.proxy(<proxy unicast>).connect(mtu=247)`, with the advert set up as `mock_bluetooth_env` does.
4. Pilot tests (`tests/test_hub_sim.py`): entry loads and a light's state comes from the simulated OnOff server;
   `light.turn_on` changes it; a node four hops away answers; teardown asserts the sim's invariants (no replays,
   nothing undecryptable).
5. Soak (marker `sim`): a scaled synthetic network (~300 nodes), a proxy that drops every k virtual seconds, seeded
   loss and duplication. Invariants: per-source (iv, seq) unique, `PropertyReader` jobs and hub dicts bounded, grace
   honoured, the refresh completes once the link is stable. Deterministic seed; seconds of wall time.
6. Conformance module run against `FakeProxyLink`, `FakeBleak` and the sim: filter type answers, SAR and acks, RPL,
   IV handling. Fix `FakeProxyLink` to answer the filter type it was asked.
7. Keep `tests/sim` free of HA imports (the library job uses `--confcutdir=tests/jhmesh`).

## Tests to add

The steps above are the tests. Pilot tests pass 20 runs in a row with no flake.

## Acceptance criteria

Gates green; total suite time grows by less than about 10 s outside the `sim` marker; no production file changed.

## Verifiable on air here?

Not applicable (tests only).

## Risks / off-by-default / "unverified on air"

HA's fixtures own the loop: if virtual time does not mix, run the soak at hub level with a minimal `hass` stub.
Cancel the sim's beacon timer at teardown. The sim may encode assumptions the firmware does not share — cross-check
with `docs/sniffer.md`.

## Depends on

01 (per-source counters), 15 (link lifecycle the soak exercises). Brief 59 builds on it.

## Files touched

`tests/conftest.py`, `tests/property_helpers.py`, `tests/sim/*`, new `tests/test_hub_sim.py`, new
`tests/test_fake_conformance.py`, `pyproject.toml` (marker), `CHANGELOG.md` (Internal), `docs/ha-integration.md`
(testing notes).
