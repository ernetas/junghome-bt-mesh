# Code review 1 (max effort)

Base: `fix/code-review-findings`. The previous review's fixes landed in the previous fix pass; those fixes are in scope too.

## Files

| File | Scope |
|---|---|
| `00-plan.md` | Ordered fix plan (written after verification) |
| `01-crypto-pdu.md` | `jhmesh/crypto.py`, `pdu.py`, `advert.py`, `sniffer.py` |
| `02-client.md` | `jhmesh/client.py`, `standalone.py` |
| `03-messages.md` | `jhmesh/messages.py`, `config_messages.py`, `vendor_models.py` |
| `04-model-export.md` | `jhmesh/properties.py`, `devices.py`, `cdb.py`, `export.py` |
| `05-ha-core.md` | `coordinator.py`, `__init__.py`, `migration.py`, `const.py`, `entity.py` |
| `06-ha-config.md` | `mesh_config.py`, `config_flow.py`, `gateway_api.py`, `tls.py` |
| `07-ha-platforms.md` | `services.py`, `config_entities.py`, entity platforms, `diagnostics.py`, `logbook.py`, `device_trigger.py`, strings |
| `08-tools-ci.md` | `tools/*.py`, `.github/workflows/*`, `scripts/`, packaging |
| `09-verification.md` | Verdicts on every P0/P1 finding. A spec marked *Corrected* replaces the shard file's Fix/Test. |
| `10-implementation-log.md` | Written by the implementer: one line per plan step |

## Severity

- **P0**: security or safety: key/nonce reuse, secret leakage, bricking or mis-rewiring a real mesh, data loss.
- **P1**: wrong behaviour a user will hit: crash, wrong state shown, command lost or sent to the wrong node, a stuck state that doesn't recover.
- **P2**: an edge-case bug, robustness problem, or missing validation at a system boundary.
- **P3**: cleanup: dead code, duplication, misleading names or comments, test gaps with no known bug.

## Finding format (every finding, appended as soon as it's found)

```markdown
### <SHARD>-<NN>: <one-line title>
- **Severity:** P0|P1|P2|P3
- **Confidence:** high|medium|low
- **Location:** `path/to/file.py:LINE` (function `name`)
- **Problem:** What is wrong, in 1–3 sentences.
- **Failure scenario:** Concrete inputs/state → wrong result. Quote the offending code (≤10 lines).
- **Fix:** Exact change: which function, what to add/remove/replace. Include a code sketch when it's more than a one-liner. State any invariant the fix must keep.
- **Test:** The test to add: file, test name, setup, assertion. It must fail before the fix and pass after.
- **Verify:** Command, e.g. `python -m pytest tests/test_x.py -k name -q`.
- **Related:** Other finding IDs, if any.
```

Implementers: take findings in the order given in `00-plan.md`. Each finding is meant to be self-contained. After each fix, run its **Verify** command, then `python -m pytest -q`, `ruff check .`, and `mypy`, which must all stay green (100% coverage gate).
