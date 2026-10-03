# Fix plan

84 findings: **2 P0 · 9 P1 · 29 P2 · 44 P3** before verification. After verification: **1 P0 · 10 P1**. TLC-06 dropped from P0 to P2; every other P0/P1 was confirmed. Verdicts are in `09-verification.md`.

## Rules for the implementer (read first)

1. **One step = one commit.** Work top to bottom and tick the box when the step is committed. Don't reorder within a phase unless a step says it can go in parallel.
2. **Where the spec lives.** Each finding's spec is in its shard file (`01`–`08`). **If `09-verification.md` has a section for that ID whose "Fix spec review" says *Corrected*, that corrected spec replaces the shard file's Fix/Test.** Read both before starting.
3. **Environment.** Always use the repo venv, from the repo root:
   - one test: the finding's **Verify** command, with `python` replaced by `.venv/bin/python`
   - full gate before every commit: `.venv/bin/python -m pytest tests -q --cov --cov-report=term-missing` (100% line coverage is enforced), `.venv/bin/python -m ruff check .`, `.venv/bin/python -m ruff format --check .`, `.venv/bin/python -m mypy`
   - baseline: 1662 passed.
4. **Write the test first** and watch it fail, then fix. If the test passes before the fix, stop: the spec is wrong. Record it in the log (rule 7) and skip the step.
5. **Scope.** Change only what the step names. No drive-by refactors. P3 cleanups have their own phase.
6. **Commit message:** `Fix <ID>[, <ID>]: <title>`, a body with 1–3 sentences of *why*, then the session trailer.
7. **Log.** Append one line per step to `10-implementation-log.md`: `<ID> — done <sha> | skipped: <reason> | deviated: <what and why>`. If the code no longer matches the finding (line moved, already fixed), check the behaviour, not the line number. If the behaviour is already correct, log `no_change_needed`.
8. **Stop and ask a human** if a step would need to change the on-disk format of the SEQ store, the export or the entry data in a way the spec doesn't describe, or if you're about to delete a test that asserts security behaviour.

⚠ = high-risk step (nonce safety or live-mesh rewiring). Re-read the diff against the spec twice, and check that every invariant the spec states is covered by a test.

---

## Status: every phase done

All five phases are committed. Their findings are removed from the shard files and `09-verification.md`; what
each step changed, and every deviation from its spec, is in `10-implementation-log.md`. The Phase 1 and 2 diffs
were re-reviewed adversarially (two follow-up fixes), and so were Phases 3–5 (see the log).

## Still open: needs a device

These cannot be settled from the code, the docs or the tests; each needs one check on a real installation.

- **MOD-05** (`04-model-export.md`): does the app swap a blind position's raw 0 / 255 on *write* too? The codec
  swaps both ways. Write 0 % to 0x1106 (blind position on power), Get it back, and watch which end the blind
  drives to after a power cycle.
- **PLT-03**: do the nodes answer a Generic Manufacturer Property Get for 0x001A (software version)? Only the test
  fake is known to. Check the log for "did not answer the Get of its software version", or the illuminance of a
  detector on firmware ≤ 1.4.0.0 (whole lux, not lux / 100).
- **CFG-08**: the per-channel limit of JUNG scene actions (8, from a code comment). No check was added; the dead
  constants were removed. If a 9th scene on one channel keeps reporting "only the description is missing", the
  limit is real and a pre-check is worth adding.
