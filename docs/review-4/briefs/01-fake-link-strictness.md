# 01 — Strict fake teardown, per-source counters

Phase P0 · Wave 1 · Size S · Closes: D26 (Q4-5; review-3 Q1 half done), D-low Q4-19 (per-source counters part).

Follow the [conventions](README.md#conventions) in full.

## Goal

The HA-level fake proxy link fails a test whenever the hub sent a PDU the fake could not decrypt, not only when it
replayed a sequence number; and injected node traffic uses one sequence counter per source address, as real nodes do.
This is the safety net for every Phase 0 brief that touches the send path: cherry-pick it first.

## Background

The repository is a Home Assistant custom integration (`custom_components/junghome_ble`) for JUNG HOME Bluetooth Mesh
devices, with a mesh library `jhmesh` (also reachable as `jhmesh/`) and CLI tools in `tools/`. HA-level tests drive
the hub through `FakeProxyLink` in `tests/conftest.py`.

- Review 3 Q1 asked for a teardown "asserting nothing replayed **or undecryptable**". Only the replay half exists:
  `tests/conftest.py:1043-1046` asserts `link.expect_replays or not link.replayed`. `link.undecryptable` is filled
  (`conftest.py:560-569`) but checked only by `tests/test_key_refresh.py:66`. A regression that encrypts a Segment Ack,
  an unacknowledged Set or a heartbeat config with the wrong key or IV index passes every other test.
- `FakeProxyLink` uses one sequence counter for every injected source (`conftest.py:894-896`), unlike real nodes;
  a per-source replay list in the hub cannot be exercised realistically.

## Read first

- `tests/conftest.py:418-1050` (`FakeProxyLink`, `inject`, `_next`, the `fake_link` fixture and its teardown).
- `tests/test_key_refresh.py` (feeds foreign-key traffic on purpose), `tests/test_coordinator.py` (undecodable counters).
- `docs/review-3/plan.md` Phase 6 Q1.

## Steps

1. Add `expect_undecryptable: bool = False` to `FakeProxyLink`, next to `expect_replays`.
2. In the `fake_link` teardown assert `link.expect_undecryptable or not link.undecryptable`, with a message listing
   the first few entries (lengths and positions only, never PDU bytes that could contain key-derived data in clear).
3. Run the full suite; set `expect_undecryptable = True` only in tests that deliberately send traffic the hub cannot
   open (foreign keys, garbage, a key refresh before it is followed). Each opt-out gets a one-line comment why.
4. Replace the shared injection counter with `dict[int, int]` keyed by source address, each starting at a per-source
   offset; keep `_next()` for the fake's own proxy PDUs. Keep the existing helper signatures.
5. If enabling the check reveals a real hub bug, fix it in a separate commit with its own test and name it in the
   report.

## Tests to add

- A self-test of the fixture: a test that makes the fake record one undecryptable PDU without the opt-out fails at
  teardown (use `pytest.raises` around the teardown helper, or factor the assertion into a function and test it).
- Two sources injected alternately keep independent, increasing sequence numbers.
- During development only: corrupt the IV index of one Segment Ack and confirm the new teardown catches it; revert.

## Acceptance criteria

- Gates green; every opt-out justified in a comment; no assertion's expected value changed elsewhere.
- `grep -n expect_undecryptable tests` lists only intentional foreign-traffic tests.

## Verifiable on air here?

Local only: test infrastructure.

## Risks / off-by-default / "unverified on air"

May reveal existing hub bugs; fix them separately. Tests that count sequence numbers of injected traffic may need
their expected values recomputed: only where the counter itself is under test, and say so in the commit.

## Depends on

None. Every later brief assumes it.

## Files touched

`tests/conftest.py`, tests that need the opt-out (expected: `tests/test_key_refresh.py`, `tests/test_coordinator.py`),
`CHANGELOG.md` (an "Internal:" bullet), `docs/ha-integration.md` testing notes.
