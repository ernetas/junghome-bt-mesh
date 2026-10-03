# Review 2 — findings and plan

Seven parallel read-only reviews after the roadmap's code steps were complete (every remaining roadmap item needs
hardware, a capture, or a user action on pypi.org / home-assistant/brands). Scopes: jhmesh transport · jhmesh
messages/model/export · HA core · config writing (mesh_config / services / schedules / thresholds) · entity
platforms · config flow + security · tests / CI / docs. Items fixed by `docs/review-1/` were excluded.
Baseline: ruff / mypy clean, 2018 tests pass, 100 % line coverage of the integration.

IDs: `T` transport, `M` model, `C` core, `W` config writing, `P` platforms, `S` flow/security, `Q` quality.

## Phase 1 — correctness bugs that bite the installation now

| ID | Where | Defect | Fix |
|---|---|---|---|
| W1 | `coordinator.py:1298-1358` | The unknown-node export refresh writes the gateway's export but not `CONF_GATEWAY_SYNCED` → every later room/key/scene/threshold action raises `service_gateway_export_newer` until a reconfigure. Also: no lock, no digest guard, no `.bak`, a meta-less `/project/cdb` doc can overwrite the full export. | Route through `MeshConfigurator` under its lock (`_gateway_state`, `write_private_with_backup`, `_mark_synced`); refuse a meta-less doc. |
| M1 | `jhmesh/messages.py:126` | Health Fault Clear opcodes swapped (spec / ESP-IDF: `0x802F` = Clear, `0x8030` = Clear Unack). The "acked Clear is not answered" field note is this bug. | Swap; fix `test_messages.py:1165`, the docstring at `:1167`, `hidden-features.md` §10; the Clear-faults button can then use the acked form. |
| T1 | `client.py:1164-1166`, `:1204-1206` | A request's reply waiter is registered before `_send` gets `_send_lock` → a status published while the request is queued (e.g. behind a 7 s segmented Config send) is taken as its answer. | Register the waiter after the lock is taken (callback into `_send`), or ignore messages received before the send. |
| W2 | `mesh_config.py:564-565` | `_load()` resets `recorded` / `adopted`; `set_threshold`/`delete_threshold` call `set_threshold_devices` per socket → a failure on socket 2 hides socket 1's saved write, no reload, stale model. | Drop the reset in `_load` (`_run` already resets per call). Optionally one plan / one upload for all sockets. |
| P1 | `number.py:267-272`, `config_entities.py:762-775` | Setting *Switch-on colour temperature* re-sends a stale lightness from the cached CTL Default → reverts *Switch-on brightness*. | Patch the cached CTL Default's lightness on every Lightness Default Status (or use the cached Lightness Default). |
| P2 | `config_entities.py:972-1001`, `1048-1066` | Property / setup writes report success when neither the Set nor the read-back was answered, or the read-back differs. | Raise `no_answer` / a `not_applied` error. |
| P3 | `select.py:106-112` | LED colour select writes `night_mode=False` when the mode is not cached yet → clears night mode. | Read first; raise `led_colour_unknown` if still unknown (as the night-mode switch). |
| C1 | `coordinator.py:465-469` | Legacy seq migration removes the 0.2 record even when the save failed (`Store.async_save` swallows `WriteError`) → seq restarts at 0, nodes drop everything as replays. | Check `store.written is data` after the save; keep the legacy file otherwise. |
| C2 | `coordinator.py:1798-1802`, `1646-1656`, `1493-1495` | `SequenceExhausted` (store back-pressure, a `ConnectionError`) aborts the whole connect-time sequence (Time Set, location, energy, heartbeats, scene actions, faults) and breaks the keep-alive. | Catch it separately; wait `SEQ_STALL_RETRY` and retry while connected. |
| C4 | `__init__.py:265-269` | `async_remove_config_entry_device` reads `runtime_data` of a not-loaded entry → `AttributeError`. | Return True when the entry is not LOADED. |

## Phase 2 — gateway trust and repairs (security)

| ID | Where | Defect | Fix |
|---|---|---|---|
| S1 | `config_flow.py:741-748`, `coordinator.py:1243-1278` | First-contact TLS pin (from `junghome.local`) is never compared with the certificate the gateway reports over the mesh (`0xC003`) → a LAN impostor at setup stays pinned and receives the full export with every key on each upload. | On the first link of a gateway entry, and before any upload, read `0xC003` and compare; on mismatch stop using the gateway and raise `gateway_certificate_changed`. |
| S2 | `coordinator.py:1252-1277` | `async_follow_gateway` silently re-pins host *and* certificate from mesh replies (spoofable by anyone holding NetKey + AppKey — i.e. from any one node) → escalation to the DevKeys. | Follow host changes automatically (validated as IP / hostname); a certificate change raises the repair and needs Reconfigure. |
| S7 | `config_flow.py:773-783` | `gateway_certificate_changed` is only raised inside the reconfigure flow and stays when that flow is cancelled; a runtime mismatch never raises it. | Delete on unfinished flow removal; raise it from the runtime path (with S1/S2). |
| S8 | `config_flow.py:606-631` | One click overrides a pin the gateway itself vouched for over the mesh. | Record the pin's source; refuse (or warn harder) when it came from the mesh. |
| S5 | `quality_scale.yaml:47-49`, `mesh_config.py:619-632` | Reauth exemption is wrong (the token is used at runtime); a revoked token yields a misleading "check reachability" issue with no way out. | Start a reauth flow on `GatewayAuthError` (reuse `gateway_register` / password) or a distinct issue pointing at Reconfigure; fix the exemption. |
| S6 | `mesh_config.py:660-700`, `__init__.py:247-261` | `gateway_sync_failed` issue id is global (not per entry) and not removed with the entry. | `issue_id(entry, ISSUE_GATEWAY_SYNC)`; delete on removal. |
| S4 | `config_flow.py:514-515` | An uploaded export (all keys) stays in HA's temp upload dir when the address check fails. | Consume the upload before any validation that can fail. |
| S3 | `migration.py:334-366` | Gateway import ignores `async_unload` results → half-applied migration (our entity removed, theirs not moved), "Unknown error", our entry left unloaded. | Abort with a translated reason on a failed unload before touching the registry; reload in `finally`. |

## Phase 3 — robustness of device writes

| ID | Where | Defect | Fix |
|---|---|---|---|
| W3 | `schedules.py:405-430` | A new schedule goes active before its action is written; a failed action write + failed rollback leaves an enabled slot with a previous (deleted) schedule's action. | Write inactive → action → type-only Set to active; roll back on `_write_schedule` failure too. |
| W4 | `services.py:1001-1041` | Multi-load `create_schedule` failing midway leaves schedules on earlier loads; a retry duplicates them. | Pre-check free slots on every load, or report the created slots in the error. |
| W5 | `thresholds.py:139-150` | `set_threshold` without `enabled` re-enables a disabled threshold. | Default to the current `active`. |
| W6 | `mesh_config.py:1593-1597` | `delete_threshold` fails with `service_no_element_group` after clearing, when there is nothing to unwire. | Early no-op when `wanted` is empty and there is no group. |
| W7 | `mesh_config.py:1590-1592` | `threshold_not_supported` passes `address=`, message wants `{name}`. | Pass `name=`. |
| W8 | `schedules.py:324-331`, `406-411`, `463` | Short list Status → `schedule_slots_full` / cached as "no schedules"; `set_enabled` read-back failure leaves a stale cache; `ActionError` text untranslated inside `{error}`. | `schedule_no_reply` on `slots is None`; update the cache from the written type; translation key per `ActionError`. |
| C5 | `coordinator.py:1923-1942` | `async_wait_settled` accepts a Get answered before the Set took effect; a silent load takes ~70 s and ~20 warnings. | Compare against the requested values; `quiet=True`; one overall deadline. |
| C3 | `coordinator.py:1127-1143`, `2203-2238`, `2320-2336` | The heartbeat disable round can be undone by a concurrent reprobe; `disable` counts any status as confirmation. | Stop the heartbeat timer/tasks during the rebuild; require `not status.enabled`. |
| M2 | `jhmesh/export.py:1146-1159` | Newer-export guard lets a file changed externally with an unchanged CDB timestamp (meta-only edit, clock skew) be overwritten. | Any digest mismatch → `NewerExportError` (re-load and re-plan) unless forced. |
| T2 | `client.py:721`, `:1217` | Notifications from a replaced/released GATT client are still processed (proxy address, replay list, SAR buffers). | Per-client notify closure; best-effort `stop_notify` on release. |
| T3 | `client.py:1166-1167`, `862-871` | `request(timeout=)` does not bound lock wait + send; `write_gatt_char` has no timeout. | `wait_for` on the GATT write → `ConnectionError`; document or extend the timeout's scope. |
| T5 | `client.py:260-272` | Stored seq record values not range-checked → `OverflowError` loop on a corrupted record. | Validate ranges in `_parse_record`; `ValueError` → existing `.bak` fallback. |

## Phase 4 — devices this installation lacks (low priority, unverifiable here)

| ID | Where | Defect | Fix |
|---|---|---|---|
| P4 | `binary_sensor.py:94`, `button.py:52-54`, `config_entities.py:560-569` | Battery nodes (`BATTERY_PIDS`) get Fault / Identify / Clear-faults / LED / night-mode entities that never work, and every link queues reads to them. | Skip them for Health; property entities read only after a key-event wake-up. |
| M3 | `jhmesh/devices.py:581-585`, `715-742` | A blinds-only node's slat element that also hosts an OnOff server becomes a spurious Light. | Claim every non-first Level element of `BLIND_ONLY_PIDS`; `load_kind` → blind. |
| P5 | `climate.py:446-448` | *All thermostats* subscribes the same signal twice → a dispatcher warning on every unload. | De-duplicate `watched`. |
| P6 | `coordinator.py` | No handler for Light CTL Temperature Status (`0x8266`) — colour temperature changed elsewhere may stay stale (plausible; unverified whether full CTL Status is published too). | Handler mapping the temperature element to its light. Check on air first (dimmer `0232`). |
| T4 | `client.py:453-454` | A full replay list evicts entries (NetKey holder can flood it). | Refuse unknown sources when full, or keep control-only sources in a separate table. |
| M4 | `properties.py:291-294` | MOD-05 (`Position.encode` 0↔255) still open — needs a blind. | — |

## Phase 5 — tests, CI, docs, dead code

- **Q1 — the flaky tests' root cause.** `async_add_executor_job` from background tasks lands in
  `hass._background_tasks`, which neither `async_block_till_done()` nor `conftest.settle()` waits for. Reproduced
  deterministically by delaying executor jobs: the two known flakes plus `test_init._fire_setup_retry`,
  `test_button.py:390`, `test_switch::test_night_mode_reads_an_unknown_colour_first`. Fix at the source:
  `settle()` also waits for non-Task executor futures in `hass._background_tasks`; plus the two targeted fixes
  (`wait_until` on `hub._export_refresh`; `wait_background_tasks=True` after the flow abort).
- **Q2** Renovate's test-stack group lets the library job's `pytest` drift from phacc's pin (9.1.1 vs 9.0.3) and
  can propose an HA version phacc does not pin yet. Derive the library job's pytest from phacc's metadata, or let
  only phacc move.
- **Q3** No `timeout` (pytest-timeout is installed) and no `timeout-minutes` on CI jobs.
- **Q4** `fail_under = 95` while the docs promise 100 %: raise the gate (100 % for the integration).
- **Q5** ruff: CI 0.16.9, venv 0.16.8, README 0.16.7 — one source (dev extra / requirements file).
- **Q6** CI concurrency key can let a fork's `main` PR cancel upstream `main` runs (matters once public).
- **Q7** `quality_scale.yaml` / roadmap say brands is "the one open Bronze item" — `dependency-transparency` is
  open too; reconsider the `platinum` claim.
- **Q8** `release.yml`: fail when `CHANGELOG.md` still says `(unreleased)` for the tag; check the tag is on `main`.
- **Q9** `cache: pip` on the setup-python steps.
- **Q10 docs vs code:** `ha-integration.md` (says never run live; "only periodic poll" and "no energy counter" /
  "`0x006A` not read" are stale; 8 vs 16 trigger subtypes; `set_room`/`assign_key` targets omit blinds; "nothing is
  reconfigured on devices"; "gateway gets no entities"), `roadmap.md` §1 table and §3 candidates (thresholds,
  schedules, scenes, central functions, gateway sync, availability, Health all done), README test count / coverage.
- **Q11 dead code:** `coordinator.ENERGY_PROPERTIES`, `advert.MAC_LENGTH`, `messages.KEY_MODE`,
  `messages.BUTTON_EVENT`, `mesh_report.NOTIFICATION_TYPES`; test-only survivors `mesh_poc.parse_address`,
  `pdu.upper_encrypt_app/dev`; unused test helpers (`MAC_*`, `SENSOR_READINGS`, `NODE_DALI`); `services.ATTR_KEY` /
  `ATTR_SCENE` duplicated from `const`; `cli_ops.write_private` non-atomic copy.
- **Q12** `test_malformed_heartbeat_publication_status_is_ignored` asserts nothing.
- **C6** (legacy/hand-edited setups only) two entries for one mesh: the first is muted by `SEQ_OWNERS`, the
  duplicate issue goes stale — refuse the second entry's setup.

## Suggested order

1. Phase 1 as one change set (small, each with a test), then deploy to the HA host — W1 and M1 are the ones the
   installation will actually hit (W1 on the next unknown-node refresh, M1 on every *Clear faults*).
2. Q1 before anything else lands in bulk: it removes the flakes the later phases would otherwise trip over.
3. Phase 2 (gateway trust) as its own change — it changes user-visible repair / reconfigure behaviour.
4. Phase 3, then Phase 5 housekeeping; Phase 4 when such devices appear or opportunistically.
