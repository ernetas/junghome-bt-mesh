# Review 4 — implementation briefs

One brief per deduplicated work item of [`../plan.md`](../plan.md), numbered in schedule order. Each brief stands
alone for an agent with no other context; the conventions below apply to every one of them.

## How to launch a wave

1. Make sure the briefs are readable from the main checkout: an agent reads its brief by absolute path,
   `<main checkout>/docs/review-4/briefs/NN-slug.md` (a brief changed but not committed yet is not in a fresh
   worktree).
2. Spawn one worktree agent per brief of wave N, in parallel, each with the prompt
   `Implement <main checkout>/docs/review-4/briefs/NN-slug.md`.
3. When they are done, cherry-pick their commits onto `main` in brief-number order. Conflicts in `CHANGELOG.md`,
   `docs/ha-integration.md`, `strings.json`, `translations/en.json` and `const.py` are expected: keep both sides.
   Conflicts the wave table in the plan flags in code files are resolved by hand, re-reading both briefs.
4. Run the gates below on `main` after the last cherry-pick of the wave, fix what the combination broke in a
   follow-up commit, and record the wave in the plan's Status section.
5. Do not start wave N+1 until wave N is on `main` and green: later briefs assume earlier ones landed.

Briefs marked *human* in the index need the maintainer or someone at home (probes, pressing keys, an app action, a
GitHub setting). An agent prepares everything offline and stops at the step that needs a person.

## Conventions

Every brief links here; follow all of it.

- **Worktree.** Work in your own git worktree of the main checkout, branched from `main`. Commit
  there, with a subject that names the finding IDs the brief closes (for example `D2 (W4-1, S4-2, P4-4): …`). Do not
  push and do not open a pull request.
- **Commit trailer.** Every commit message ends with the line
  `Claude-Session: https://claude.ai/code/session_01WiarruU6FbaU7R8Kq3m85K`
- **Gates.** Run from the worktree root with the main checkout's venv,
  `PY=<main checkout>/.venv/bin/python`:
  1. `$PY -m ruff check .`
  2. `$PY -m ruff format .` (then `$PY -m ruff format --check .` must be clean)
  3. `$PY -m mypy` (and `$PY -m mypy --python-version 3.13 -p jhmesh` when `jhmesh` changed)
  4. `$PY -m pytest tests -q --cov --cov-report=term-missing`, then
     `$PY -m coverage json --include='custom_components/junghome_ble/*' -o /tmp/cov-NN.json -q` must list no
     `missing_lines`: the integration stays at 0 missed lines (the CI step "Every line of the integration and the
     package is covered").
  5. `$PY -m pytest tests/jhmesh --confcutdir=tests/jhmesh --import-mode=importlib -q -p no:cacheprovider
     --cov=custom_components/junghome_ble/jhmesh --cov-branch --cov-fail-under=100`
  6. `$PY -m pytest tests/test_parity.py -q` when a parity ledger row changed (and `tools/parity.py check`).
- **Docstrings.** Match the surrounding code's docstring density and its "why, not what" style. Moved code keeps its
  text.
- **User strings.** Every new user-visible string goes into both `strings.json` and `translations/en.json`;
  `tests/test_translations.py` stays green.
- **On air.** Anything not seen working on this installation is marked "unverified on air" in its docstring, its
  description string and `docs/ha-integration.md`. Never contact a real device, gateway or host from a test; a probe
  on the real mesh is run by the maintainer, or only when the maintainer allows it.
- **Secrets.** Never log, print, commit or quote key material (NetKey, AppKey, device keys, derived keys, the gateway
  token or password). Do not quote MACs, UUIDs, e-mail addresses or names of the installation either.
- **No dates or clock times** in files, docs, code comments or commit messages.
- **Changelog and docs.** Add a bullet to `CHANGELOG.md` under `## 0.3.0 (unreleased)` (or the next version
  section once that one is released) and update `docs/ha-integration.md` (after brief 43: the matching page under
  `docs/user/` or `docs/dev/`). Behaviour-identical refactors (Phase 4) add one "Internal:" bullet and touch the docs
  only for the module table.
- **Report.** Finish with: what changed, the gate results, anything left for the maintainer (probes, on-air checks,
  decisions), and the commit hashes.

## Index

Phases: P0 safety · P1 runtime, HA and test correctness · P2 functionality · P3 UX and docs · P4 architecture ·
P5 release · P6 HACS. On air: **yes** = verifiable with the lights, sockets, push-buttons and gateway here · **partly**
· **regr.** = regression check only · **no** = needs hardware or an action not available here · **local** = nothing to
check on air. *human* = needs a person.

| # | Title | Phase | Wave | Size | On air | Depends on | Main files |
|---|---|---|---|---|---|---|---|
| [01](01-fake-link-strictness.md) | Strict fake teardown, per-source counters | P0 | 1 | S | local | — | `tests/conftest.py` |
| [02](02-key-file-modes.md) | Key-holding files owner-only; one atomic writer | P0 | 1 | S | local | — | `jhmesh/client.py`, `jhmesh/export.py`, `coordinator.py`, `SECURITY.md` |
| [03](03-pending-nodes.md) | Pending nodes: no address reuse, a way back | P0 | 1 | M | partly (spare device) | — | `onboard.py`, `jhmesh/onboarding.py`, `commission.py`, `vault.py`, `services.py` |
| [04](04-key-refresh-proof.md) | Proof-gated key-refresh following | P0 | 1 | M | regr. | — | new `jhmesh/keyrefresh.py`, `jhmesh/client.py`, `coordinator.py` |
| [05](05-cancel-safe-plans.md) | Cancel-safe apply-and-record, plan journal | P0 | 1 | M | local | — | `mesh_config.py`, `services.py` |
| [06](06-ci-hardening.md) | CI without branch artifacts, faster, Renovate rule | P1 | 1 | M | local | — | `.github/workflows/*`, `renovate.json` |
| [07](07-parity-ledger-corrections.md) | Parity ledger corrections, symbol citations | P1 | 1 | M | local | — | `docs/parity/*`, `tools/parity.py` |
| [08](08-backup-restore.md) | Survive an HA backup restore without nonce reuse | P0 | 2 | M | yes | 01 | new `backup.py`, `coordinator.py` |
| [09](09-iv-timing-guards.md) | IV timing guards, fixable IV mismatch | P0 | 2 | M | regr. | 01 | `jhmesh/client.py`, `coordinator.py`, `repairs.py` |
| [10](10-allocate-away-from-app.md) | Allocate away from the app, surface conflicts | P0 | 2 | S–M | yes | 03 | `jhmesh/export.py`, `commission.py`, `mesh_config.py`, `onboard.py` |
| [11](11-test-suite-hardening.md) | Clocks, timeouts, snapshots, regen, secret scan | P1 | 2 | M | local | 01 | `tests/*`, timeout reads in four modules |
| [12](12-seq-store-stall.md) | Bounded back-pressure, stalled-store repair | P0 | 3 | S–M | regr. | 08 | `coordinator.py`, `jhmesh/client.py`, `keep_awake.py` |
| [13](13-scene-cleanup-and-admin.md) | Safe scene clean-up, held numbers, admin actions | P0 | 3 | S–M | yes | 05, 10 | `mesh_config.py`, `services.py`, `services.yaml` |
| [14](14-key-refresh-vault-nodes.md) | Carry HA-provisioned nodes through a key refresh | P1 | 3 | M | no | 03, 04 | `coordinator.py`, `onboard.py`, `vault.py` |
| [15](15-link-lifecycle.md) | One link lifecycle, grace, short-link penalty | P1 | 4 | M | partly *human* | 12 | `coordinator.py`, `entity.py` |
| [16](16-seq-store-hygiene.md) | Skip target, floor, mesh-level state, CLI address | P1 | 4 | S–M | regr. | 04, 08, 09, 12 | `coordinator.py`, `repairs.py`, `tools/mesh_poc.py` |
| [17](17-vault-durability.md) | Vault durability, key recorded before Provisioning Data | P1 | 4 | M | no | 03 | `identity.py`, `onboard.py`, `jhmesh/provisioning.py` |
| [18](18-merge-and-sync.md) | Merge identities, both-changed merge, safe refetch | P1 | 4 | M | yes *human* | 10 | `jhmesh/merge.py`, `mesh_config.py`, `config_flow.py` |
| [19](19-button-events-and-holds.md) | Button events from the hub, key-aware triggers, holds | P1 | 5 | S–M | yes *human* | 15 | `event.py`, `device_trigger.py`, `coordinator.py` |
| [20](20-own-source-detection.md) | Own-source detection, proxy-config checks | P1 | 5 | S–M | partly | 12 | `jhmesh/client.py`, `coordinator.py` |
| [21](21-connect-schedule-and-reader.md) | Reader dedup, connect schedule, bounded GATT | P1 | 5 | M | yes | 15 | `config_entities.py`, `coordinator.py`, `standalone.py` |
| [22](22-node-truth-writes.md) | Sensor publication, remove_device, small action fixes | P1 | 5 | M | partly | 05 | `switch.py`, `mesh_config.py`, `services.py`, `device_names.py` |
| [23](23-gateway-reauth.md) | Gateway reauthentication flow | P1 | 5 | M | yes *human* | — | `config_flow.py`, `mesh_config.py`, `gateway_status.py` |
| [24](24-discovery-and-setup-hygiene.md) | Discovery: own mesh, stale export, JUNG-only | P1 | 6 | S–M | partly | 04 | `config_flow.py`, `coordinator.py`, `manifest.json` |
| [25](25-diagnostics-and-logging.md) | Diagnostics in every state, log once, link history | P1 | 6 | S–M | yes | 15 | `diagnostics.py`, `sensor.py`, `jhmesh/client.py` |
| [26](26-perf-and-adverts.md) | O(1) lookups, parse cache, advert hygiene | P1 | 6 | S–M | regr. | 12 | `jhmesh/cdb.py`, `jhmesh/client.py`, `coordinator.py` |
| [27](27-hub-over-sim.md) | HA hub over the simulator, soak, fake conformance | P1 | 6 | M | local | 01, 15 | `tests/conftest.py`, `tests/sim/*` |
| [28](28-no-reload-config.md) | Apply configuration changes without a reload | P1 | 7 | L | yes | M5 | `services.py`, `__init__.py`, `coordinator.py`, platforms |
| [29](29-mesh-privacy-beacons.md) | Mesh 1.1 private beacons and identities, spec vectors | P1 | 7 | S–M | regr. | 04 | `jhmesh/client.py`, `pdu.py`, `crypto.py` |
| [30](30-on-air-sweep.md) | On-air verification sweep of what is built | P2 | 8 | S | yes *human* | — | `docs/parity/*`, `docs/` |
| [31](31-transitions.md) | Transitions on lights, scenes, key scenes | P2 | 8 | S–M | yes *human* (probe) | 30 (key scenes) | `jhmesh/messages.py`, `coordinator.py`, `light.py`, `scene.py` |
| [32](32-update-entity-reads.md) | `update_entity` reads; config re-read per link | P2 | 8 | M | yes | — | `entity.py`, `config_entities.py`, `sensor.py`, `climate.py` |
| [33](33-insert-and-layout.md) | Insert function and layout from the advert | P2 | 8 | S–M | yes | — | `coordinator.py`, `jhmesh/cdb.py`, `devices.py`, `entity.py` |
| [34](34-node-clocks.md) | Node clocks, zone and location as diagnostics | P2 | 9 | S | yes | — | `jhmesh/messages.py`, `coordinator.py`, `sensor.py` |
| [35](35-lock-awareness.md) | Lock awareness on lights and sockets | P2 | 9 | S–M | yes *human* (probe) | — | `coordinator.py`, `light.py`, `switch.py`, `config_entities.py` |
| [36](36-firmware-only-properties.md) | Probe and expose firmware-only properties | P2 | 9 | M | yes *human* (probe) | 34 (*soft*) | `jhmesh/properties.py`, `config_entities.py`, platforms |
| [37](37-room-membership.md) | Several rooms per load, leaving a room | P2 | 9 | S–M | yes | 10 | `mesh_config.py`, `services.py`, `jhmesh/export.py` |
| [38](38-key-connections.md) | Key connections: TW element, slats, lock keys | P2 | 10 | M | yes *human* (capture) | 30, 35 | `mesh_config.py`, `jhmesh/devices.py` |
| [39](39-app-write-backs.md) | App coexistence write-backs | P2 | 10 | M | no (spare app install) | 33 | `jhmesh/export.py`, `schedules.py`, `mesh_config.py` |
| [40](40-commissioning-completeness.md) | Commissioning completeness, time keeper, OOB | P2 | 10 | M–L | no (spare device) | 03, 17, 33 | `jhmesh/commission.py`, `onboard.py`, `provisioning.py` |
| [41](41-network-extras.md) | Audit / locator extras, gateway approve, firmware entity | P2 | 10 | M | partly | — | `jhmesh/audit.py`, `gateway_api.py`, new `update.py` |
| [42](42-absent-hardware-refinements.md) | Blinds, RTR, detector, battery refinements | P2 | 10 | M | no | — | `cover.py`, `climate.py`, `binary_sensor.py` |
| [43](43-docs-split-and-user-guide.md) | User guide, reference split, entity reference | P3 | 11 | M–L | local | — | `docs/**`, `README.md`, new `tools/gen_entity_reference.py` |
| [44](44-repairs-that-fix.md) | Repairs that fix, learn-more links, troubleshooting | P3 | 12 | M–L | partly | 23, 43 | `repairs.py`, `const.py`, issue sites |
| [45](45-mesh-health-dashboard.md) | Mesh health at a glance, calmer dashboard | P3 | 12 | M | yes | 25 | `binary_sensor.py`, `sensor.py`, central entities |
| [46](46-blueprints.md) | Blueprints and a button cookbook | P3 | 12 | M | yes *human* | 19 | new `blueprints/`, `tests/test_blueprints.py` |
| [47](47-rooms-to-areas.md) | Rooms to areas, button and node areas, area sync | P3 | 13 | M | yes | 28 (*soft*) | `config_flow.py`, `entity.py`, `__init__.py` |
| [48](48-follow-the-app.md) | Follow the app automatically | P3 | 13 | M | yes *human* | 18 | `coordinator.py`, `mesh_config.py`, `button.py` |
| [49](49-action-ergonomics-and-dry-run.md) | Dry runs, structured responses, action forms | P3 | 13 | M | yes | 05, 13 | `services.py`, `services.yaml`, `mesh_config.py` |
| [50](50-onboarding-polish.md) | Zeroconf gateway, unicast under Advanced, icons | P3 | 14 | S–M | yes | 24 | `manifest.json`, `config_flow.py`, `icons.json` |
| [51](51-german-translation.md) | German translation | P3 | 14 | M–L | local | M4 | `translations/de.json`, `tests/test_translations.py` |
| [52](52-seq-store-module.md) | Persistence out of `coordinator.py` | P4 | 15 | S | regr. | 16, 20 | `coordinator.py` → `seq_store.py`, `node_info.py` |
| [53](53-dispatch-errors-conversions.md) | One dispatch helper, error mapper, conversions | P4 | 15 | S | regr. | — | new `dispatch.py`, `errors.py`, `conversions.py` |
| [54](54-config-entities-split.md) | Split `config_entities.py` | P4 | 15 | M | regr. | — | `config_entities.py` → `properties/` |
| [55](55-mesh-config-split.md) | Split `MeshConfigurator` | P4 | 15 | M–L | regr. | — | `mesh_config.py` → `configurator/` |
| [56](56-services-split.md) | Split `services.py` into `actions/` | P4 | 15 | M | regr. | — | `services.py` → `actions/` |
| [57](57-jhmesh-api-and-state.md) | `jhmesh` stable API, `LocalState` in `state.py` | P4 | 15 | M | regr. | — | `jhmesh/client.py` → `jhmesh/state.py`, `__all__` |
| [58](58-hub-gestures-component.md) | First hub component: `ButtonGestures` | P4 | 16 | M | regr. | 52, 53 | `coordinator.py` → `hub_gestures.py` |
| [59](59-test-strength-nightly.md) | Nightly: mutation, thorough Hypothesis, traces | P5 | 16 | M | local | 27 | `.github/workflows/nightly.yml`, `tests/traces/` |
| [60](60-hub-remaining-components.md) | Remaining hub components, lifecycle registry | P4 | 17 | L | regr. | 58 | `coordinator.py` → hub components |
| [61](61-observability-counters.md) | Link counters and a trace logger | P4 | 18 | S | regr. | 57, M1 | `jhmesh/client.py`, `diagnostics.py` |
| [62](62-plan-model-and-typing.md) | Plan model into `jhmesh`, Protocols, `const.py` diet | P4 | 19 | M | regr. | 55, 57, 60 | `jhmesh/plan.py`, `export.py`, `const.py` |
| [63](63-dead-code-and-docstrings.md) | Dead code, docstrings, describe tables, CLI parsers | P4 | 20 | S | regr. | 62 | nearly every file |
| [64](64-privacy-tree-and-scanner.md) | Tree scrub, `.gitignore`, privacy scanner, dev tooling | P5 | 21 | M | local | — | docs, tests, new `tools/privacy_scan.py` |
| [65](65-public-release.md) | Fresh public repository, first-release notes | P5 | 22 | M | local *human* | 02, 06, 43, 64, M2 | new repository, `CHANGELOG.md`, `CONTRIBUTING.md` |
| [66](66-hacs-first-release.md) | First full release and a green HACS run (HACS-2) | P6 | 23 | S–M | local *human* | 06, 65, M14 | `ci.yml`, `release.yml`, `CHANGELOG.md` |
| [67](67-hacs-default-submission.md) | hacs/default submission (HACS-3) | P6 | 24 | S | local *human* | 66, M14, M15 | `README.md`, `hacs.json` |

Briefs 01 and 02 should be cherry-picked first in wave 1; every later brief assumes the strict fake teardown.
