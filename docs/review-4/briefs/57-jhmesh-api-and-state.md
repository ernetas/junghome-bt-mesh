# 57 — A stable public API for `jhmesh`, with `LocalState` in `jhmesh/state.py`

Phase P4 · Wave 15 · Size M · Closes: A4-9 (report 8, brief B6).

Follow the [conventions](README.md#conventions) in full. Behaviour-identical refactor: one "Internal:" bullet,
docs only for the module table and the library's API note.

## Goal

`jhmesh` states which names are public API; sequence-state persistence moves out of the client module.

## Background

`jhmesh` is published as a wheel (CI builds and checks it). `jhmesh/client.py` (about 2150 lines) holds
`SequenceExhausted`, `SequenceStalled`, `_check_range`, `StateInUse` and `LocalState` (persistence, sequence
numbers, RPL, IV state) next to `ProxyClient`. No module has an `__all__`; `client` re-exports `pdu` constants that
tests import through it.

## Read first

`jhmesh/client.py` (the classes above, the `fcntl` import guard), `jhmesh/__init__.py`, importers of `LocalState`
(`coordinator.HAState` or `seq_store.HAState` after 52, `tools/mesh_poc.py`, tests), `tests/jhmesh/*` patch targets
(`client.fcntl`, `client.time`), `.github/workflows/ci.yml` `library` job.

## Steps

1. Move `LocalState` and its exceptions (plus the constants they need) to `jhmesh/state.py`; re-export them from
   `client.py` with `from .state import X as X`.
2. Add `__all__` to every `jhmesh` module listing exactly today's non-underscore names; rename nothing. Compatibility
   re-exports in `client.__all__` get a comment.
3. `tests/jhmesh/test_api_surface.py`: per module, `sorted(module.__all__)` equals a literal list; every name exists;
   none starts with `_`.
4. No eager imports in `jhmesh/__init__.py` (importing `jhmesh` stays free of `bleak` / `cryptography` work); document
   the stable modules and the "underscore = private" policy in its docstring.
5. Move `monkeypatch.setattr(client, "fcntl" | "time", …)` targets to `state` where `LocalState` reads them.

## Tests to add

`tests/jhmesh/test_api_surface.py`.

## Acceptance criteria

Gates pass, including `mypy --python-version 3.13 -p jhmesh` and 100 % line + branch on `tests/jhmesh`; `client.py`
under about 1700 lines; wheel build and `twine check` unchanged; `py.typed` unchanged.

## Verifiable on air here?

Regression only: a CLI `listen` and an HA reconnect.

## Risks / off-by-default / "unverified on air"

`HAState` overrides `LocalState` hooks (`_owns_the_store`, `persist`, `_limit`); keep their exact names.
`to_stored` / `parse_record` move verbatim, so the stored format is unchanged.

## Depends on

None within wave 15 (52 keeps importing from `jhmesh.client`). 61 and 62 build on it.

## Files touched

`jhmesh/client.py`, new `jhmesh/state.py`, every `jhmesh/*.py` (`__all__`), `jhmesh/__init__.py`, new
`tests/jhmesh/test_api_surface.py`, `tests/jhmesh/*` patch targets, `CHANGELOG.md`, `README-pypi.md` (API note),
`docs/dev/architecture.md` (module table).
