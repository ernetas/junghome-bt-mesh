# 59 — Nightly test strength: mutation testing, thorough Hypothesis, replayed traces

Phase P5 · Wave 16 · Size M · Closes: Q4 improvements T2, T3, T5, T7, C4.

Follow the [conventions](README.md#conventions) in full.

## Goal

A scheduled job that measures how strong the tests are, and regression fixtures made from real on-air traffic that
contain no real key, address or MAC.

## Background

Review 3 found a nonce-reuse mutant that passed 1127 tests; nothing checks that routinely. The Hypothesis profiles
include a `thorough` profile (`tests/jhmesh/hypothesis_profiles.py`) that CI never runs. "Verified on air" claims
(publication doubling, TTL from the gateway, the acked-Set reply rule, button counters) have no replayable fixture.
No test loads a stored `.storage` snapshot of a released version.

## Read first

`.github/workflows/ci.yml` (job layout after brief 06), `tests/jhmesh/hypothesis_profiles.py`, `tests/sim/*`, the
`sim_link` fixture from brief 27, `tools/mesh_sniff.py` (`capture`, `decode --json`), `docs/sniffer.md`,
`tests/conftest.py` (`FakeProxyLink.inject`).

## Steps

1. `.github/workflows/nightly.yml` (`schedule` + `workflow_dispatch`, never on PRs, actions pinned by SHA like the
   others, `persist-credentials: false`): `HYPOTHESIS_PROFILE=thorough` over the property tests with an
   `actions/cache` example database; `mutmut` (or `cosmic-ray`) restricted to `jhmesh/crypto.py`, `pdu.py`,
   `client.py` / `state.py` (SAR, RPL, IV) and the seq-store module, with a surviving-mutant summary in the job
   summary; the integration tests against the newest `pytest-homeassistant-custom-component` (non-blocking).
2. `tools/trace_to_fixture.py`: input a decoded capture (NDJSON from `tools/mesh_sniff.py decode --json`, made with
   the real export **on the capture host**); map every unicast and group address to a fixture address; drop RSSI,
   MACs and absolute timestamps (keep order and rounded relative gaps); re-encrypt each access PDU under the fixture
   keys only. Output: NDJSON of (delay, src, dst, access PDU hex). The converter refuses to write output containing
   any input key, address or MAC. Only converted, hand-reviewed traces are committed under `tests/traces/`.
3. `tests/test_traces.py`: replay each trace through `FakeProxyLink.inject` (or the sim) and snapshot entity states
   and bus events.
4. `tests/upgrade/`: a scaffold that loads stored `.storage` fixtures of a released version into the current code
   (empty until the first release; one synthetic fixture now).
5. `pyproject.toml`: marker `trace`, mutmut configuration.

## Tests to add

Converter unit tests on a synthetic capture: no input key, MAC or address appears in the output; the mapping is
stable; a round trip decodes to the same access PDUs. The replay tests.

## Acceptance criteria

Gates pass; the nightly workflow runs green or reports survivors; at least one committed trace per verified device
class (light, socket, DALI CTL, rocker) once the maintainer has produced them — until then the converter and one
synthetic trace.

## Verifiable on air here?

Local only. Producing real traces needs the maintainer on the capture host (*human*).

## Risks / off-by-default / "unverified on air"

Privacy: the converter runs where the real export lives and only its output may leave; review each trace by hand.
Mutation runs are slow: module list only. If the repository is not yet public (brief 65), commit no trace from the
installation.

## Depends on

27 (sim adapter). 65 before committing traces from the installation to a public repository.

## Files touched

New `.github/workflows/nightly.yml`, new `tools/trace_to_fixture.py`, new `tests/traces/`, new `tests/test_traces.py`,
new `tests/test_trace_converter.py`, `tests/upgrade/`, `pyproject.toml`, `CHANGELOG.md`, `docs/dev/testing.md`
(testing notes).
