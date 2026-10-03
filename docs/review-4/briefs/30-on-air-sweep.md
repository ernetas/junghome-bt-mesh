# 30 — On-air verification sweep of what is already built

Phase P2 · Wave 8 · Size S (human) · Closes: — (report 6 Wave 0 / brief B9; review-3 Status "still open on air").

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
`Claude-Session` trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock
times, CHANGELOG bullet + docs update).

## Goal

Turn the "unverified on air" items that this installation *can* check into verified ones, or into bugs with a
capture and a regression test — no new features.

## Background

`custom_components/junghome_ble` (HA integration for JUNG HOME Bluetooth Mesh) has a parity ledger
(`docs/parity/ledger-*.json`, rules in `docs/parity/README.md`) whose `partial` rows with `verify: on-air` carry, in
their `missing` text, the procedure to check them. Review 3 left F10 (mini-actuator inputs), F4 (lock bits) and F15
(key → scene) open on air because they need someone to operate a key or an input.

Rows to settle: `air:access:8264` (CTL Temperature Set), `msg:op:8241` (Scene Get after connect), `msg:op:826b` /
`prod:param:lamp:tunable-white-range` (expected "not applied" on the DALI insert), `net:uc:createthreshold`,
`net:uc:togglethreshold`, `net:uc:deletethreshold`; plus F10, F4, F15. Brief 07 may already have reclassified some of
these as implemented with an on-air note; keep its result.

## Read first

- `tools/parity.py check --build` output; each listed row's `missing` text.
- `docs/sniffer.md` (capture and decode), `tools/mesh_sniff.py`, `tools/mesh_poc.py` (`listen`, `get`, `config audit`).
- `docs/review-3/plan.md` Status (F10, F4, F15 notes).

## Steps

1. Agent (offline): produce a checklist file in the worktree under `docs/` (or append to `docs/sniffer.md`) with,
   per row, the HA action or CLI command, what to capture and what outcome means pass. Stop there and hand it to the
   maintainer.
2. Maintainer (person at home): for each row, start `tools/mesh_sniff.py capture`, run the action, then `decode
   --json`; compare bytes and outcome with the row. Thresholds: use a socket with a harmless load. F15: note the key's
   current connection first and restore it afterwards. F10: someone operates the input. F4: someone tries the locked
   key.
3. Agent (second pass, with the maintainer's notes): flip each passing row to `implemented` (citing the capture by its
   sequence number and session name, not a date); for each failure open a regression test reproducing the captured
   bytes and fix it in a separate commit, or record it for a later brief.
4. Update `docs/ha-integration.md`: remove "unverified on air" where verified.

## Tests to add

One regression test per bug found (bytes from the capture, re-encoded with fixture keys, never the real ones).

## Acceptance criteria

Every listed row decided; `tests/test_parity.py` and the gates green.

## Verifiable on air here?

That is the point. Person needed for F10, F4, F15 and the threshold load.

## Risks / off-by-default / "unverified on air"

Thresholds switch a real load; F15 rewires a real key (restore it). Captures contain installation data: commit only
decoded, re-keyed fixtures, never raw captures.

## Depends on

None. Briefs 31 (key-scene transitions) and 38 (key connections) wait for F15.

## Files touched

`docs/parity/ledger-*.json`, `docs/sniffer.md` or a checklist under `docs/`, `docs/ha-integration.md`, tests for any
bug, `CHANGELOG.md`.
