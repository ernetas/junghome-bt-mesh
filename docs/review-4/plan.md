# Review 4 — findings and plan

## Scope and method

Nine parallel read-only reviews of the whole repository after review 3's Status list. Items fixed by reviews 1–3 were
excluded unless the fix is incomplete. Baseline: ruff and mypy clean; 2853 tests pass (464 snapshots, no order
dependence under a shuffled and a reversed run); every line of the integration covered, `jhmesh` at 100 % line +
branch from `tests/jhmesh`; every fixture regenerates byte-identical.

| Report | IDs | Scope |
|---|---|---|
| 1 protocol | `P4` | `jhmesh` crypto, PDUs, client, provisioning, vault, identity, commissioning, audit — against Mesh Protocol 1.1 |
| 2 state | `S4` | sequence store, backup, floor, repairs, IV / key-refresh persistence, vault, export files, three-way merge |
| 3 runtime | `R4` | connection loop, proxy choice, grace, reachability, keep-alive, reader, standalone link |
| 4 config | `W4` | configurator, services, onboarding, thresholds, schedules, sensor publication, allocators |
| 5 HA | `H4` | flows, discovery, platforms, strings, repairs, diagnostics, quality-scale rule by rule |
| 6 functionality | `F4` | model coverage, ranked missing features, wrong parity-ledger statuses |
| 7 quality | `Q4` | tests, fakes, CI / packaging, docs accuracy, personal data before a public push |
| 8 architecture | `A4` | measurements and behaviour-identical refactors |
| 9 UX | `U4` | the owner's experience: language, areas, buttons, onboarding, repairs, health, docs |

**Reproduced by the reviewers** (scratch runs on a copy, not kept): P4-1 (forged key-refresh sequence), P4-2 (IV
ratchet), P4-5 / Q4-1 (0644 state file holding a key), S4-1 (restore → reuse, on the project's own state machine),
S4-2 / W4-1 (same block twice), S4-4 (duplicate meta rows), S4-7, W4-2 (allocation collision), W4-4 (cancel never
records), R4-1 … R4-10 (flap, exhaustion loop, no grace, dead sends, reader growth, adverts, benchmarks), H4-1
(`unavailable → unknown → off` on `rename_room`), Q4-4, Q4-6. **Plausible only** (path read in code, failure not
produced): R4-11, H4-4, H4-10, S4-10, S4-11, P4-6, P4-7, W4-14, the S4-1 side effect on the export, the iOS app
deleting HA scenes (W4-2), the CI quota failure itself (Q4-2). P4-3 is class b/c (app behaviour from the decompile).
Everything else was established by reading code.

**Verification here.** For every high and medium row below, the cited code was opened in the current tree
(`main` at the review base) and still says what the report claims; no row was dropped or downgraded. Lows are listed
as reported. IDs `D1…` are the deduplicated findings; every source ID is kept.

### Corrections to review 3's Status

- "repair when the seq store refuses for minutes" (link-loss UX, review 3 Phase 5) was **never built**: no stall
  issue key exists, `_while_seq_stalls` retries forever (D9).
- Q1 is half done: the fake's teardown checks replays, not undecryptable PDUs (D26).
- T7 is half done: `standalone.connect` still attaches without `beacon_wait` (R4-10).
- N2b ("key refresh followed") follows *requests*, not proof that the mesh moved, so one node can forge it (D4).

## Deduplicated findings

Severity, then value per size. "Verified" = the cited code was re-read in the current tree and holds.

### High

| ID | Sources | Where | Defect | Fix | Check | Brief |
|---|---|---|---|---|---|---|
| D1 | Q4-1 = P4-5 (P: low → high) | `coordinator.py:546-598` (`SeqStore` without `private=True`); `jhmesh/client.py:507-512`, `:566-568` | The three seq stores and the CLI state file are written 0644; during a key refresh they hold the new NetKey. | `private=True`; `LocalState` writes via `os.open(…, 0o600)`; tighten on load; one mode test per key-holding file. | verified | 02 |
| D2 | W4-1 = S4-2 = P4-4 | `onboard.py:121-127` (`avoid=[src]` only); `jhmesh/onboarding.py:65`; `Vault.pending` never read outside the vault | A node provisioned but not recorded keeps its unicast block and element groups unreserved; the next `add_device` reuses them (shared SRC → nonce collision, replay drops); no way to reset it from HA. | Avoid every vault node's block and planned groups; clear RPL for new addresses; `reset_pending_device`; repair while pending. | verified | 03 |
| D3 | W4-3 | `mesh_config.py:3269-3339` | `delete_unused_scenes` deletes every scene the *disk* export lacks — the app's newer scenes and timer scenes on every node — with silent gateway fallback. | Dry run by default; require a fresh gateway read or an explicit flag; admin-only. | verified | 13 |
| D4 | P4-1 (review-3 N2b incomplete) | `jhmesh/client.py:1806-1864`, `:2111-2118`; `coordinator.py:744-769`, `:4361-4373` | Any node (or its extracted flash) can send device-key-sealed NetKey Update + Phase Set 2/3 and HA switches to the attacker's key, persists it and moves the entry `unique_id`; also follows aborted refreshes. | Advance Phase 2/3 only on proof (new-key beacon, or statuses from ≥ 2 nodes / the proxy); learn only from dst-sealed updates to CDB nodes. | verified | 04 |
| D5 | S4-1 | no `backup.py`; `coordinator.py:1536-1577` | Restoring an HA backup rolls primary, `.backup` and `.floor` back together; the next start resumes below numbers already sent (nonce reuse), silently. | Backup platform: `in_backup` token around backups; a record carrying it skips ahead with `seq_guard`. | verified | 08 |
| D6 | W4-2 | `jhmesh/export.py:894-913`; `jhmesh/commission.py:424-428`; `mesh_config.py:1418-1422` | With provisioner identity off (default) HA takes the app's *next* free group / scene / element group; the app (which never downloads) allocates the same, rooms merge on air and the adopt drops HA's room with only a log line. | Allocate top-down when no own provisioner; conflicts become a repair (D18). | verified | 10 |
| D7 | Q4-2 | `.github/workflows/ci.yml:135-140`, `:310-315` | Two artifacts uploaded on every branch push / PR; a full account quota fails `build` → `library` skipped, tags cannot release. | Merge `build` into `library`; upload only on tags; pin pip. | verified (code; quota effect from GitHub behaviour) | 06 |
| D8 | Q4-3 | whole history: 129 commits, 66 session trailers, no noreply author; remote `renovate/python-3.x` based on the first commit | Review 3's scrub covers the tree only: history, author metadata, the remote branch and unreachable commits on the existing remote still expose the originals. | New public repository with one clean commit; archive the private one (decision M2). | verified | 65 |

### Medium

| ID | Sources | Where | Defect | Fix | Check | Brief |
|---|---|---|---|---|---|---|
| D9 | R4-2 = S4-5 (+ R4-4 low) | `coordinator.py:2873-2891` catches `SequenceExhausted` (base of `SequenceStalled`); no stall key in `const.py` / `strings.json` | Real exhaustion and a never-landing store are retried forever: `_keep_alive` never returns (watchdog blind); an unwritable store is reported as `pdus_dropped`, whose fix cannot persist either. Review-3 Status wrongly lists the repair as done. | Retry `SequenceStalled` only, with a deadline; `seq_store_unwritable` repair; no `pdus_dropped` while stalled; refuse sends before reserving when unlinked. | verified | 12 |
| D10 | P4-2 + H4-3 | `jhmesh/client.py:647-701` (`limit = iv+42`, no time state); `coordinator.py:4283-4320` (`is_fixable=False`); `strings.json` `iv_index_mismatch` | Authenticated beacons can ratchet HA's IV index without the 96 h / 192 h limits (irreversible, HA mute); the mismatch repair's manual step does not work. | Persist IV change / recovery times, refuse early transitions; fixable rewind safe by `seq_guard`; reword. | verified | 09 |
| D11 | P4-3 (+ F4 ledger rows `mgmt:builder:netkeyupdate…`) | no caller of `netkey_update(` anywhere; `onboard.py:188-194` | Nodes HA provisioned are not in the app's database, miss its key refresh and are cut off at Phase 3; a node provisioned during Phase 1 gets the retiring key. | After D4's proof, send vault nodes NetKey Update / Phase Set; refuse `add_device` in Phase 1. | verified (class b/c) | 14 |
| D12 | W4-4 | `mesh_config.py:2066-2121` (no `CancelledError` path); `services.py:823-828` | A plan cancelled (script `mode: restart`, HA stop) after accepted steps records nothing and does not reload; later plans start from a wrong export. | Record shielded on cancel; reload on any exit when recorded; then a plan journal. | verified | 05 |
| D13 | R4-1 | `coordinator.py:2188-2242` | A proxy that drops seconds after connecting is never put on cooldown; the hub reconnects to it forever, refresh / Time Set never complete. | Short link = failure: cooldown, back-off, prefer the next candidate. | verified | 15 |
| D14 | R4-3 (+ R4-6 = H4-5 low) | `jhmesh/client.py:1085-1094` (only `handle_disconnected` calls `on_disconnect`); `coordinator.py:2179`, `:2270`, `:2287`, `:4402` | Self-initiated drops (watchdog, probe, skip-ahead, loop error) get no link-loss grace: entities go unavailable and HA silently drops automation commands; a link lost in `attach()`'s settle is reported connected. | One `_drop_link(reason)` helper; check `connected` after attach; central entities on `link_available`. | verified | 15 |
| D15 | S4-3 | `identity.py:116-124` (`_written = data` regardless), `:62-79` | A failed vault write (swallowed by HA's `Store`) is recorded as written; the only copy of a device key lives in RAM; an unreadable vault is deleted even if its aside copy failed. | `written`-tracked store with `.backup`; repair on failure; delete only after the aside copy landed. | verified | 17 |
| D16 | S4-4 | `jhmesh/merge.py:57-70` | `keyModeSceneConfigExports`, `buttonLayoutExports`, `actuatorExports`, `networkExclusions` have no identity: concurrent edits duplicate rows instead of conflicting. | Identities by `elementAddress` / `ivIndex`; merge property test. | verified | 18 |
| D17 | S4-6 | `mesh_config.py:1377-1384`, `:1083-1094`; `config_flow.py:1039-1046` | "Both changed" is refused and the only remedy (fetch again) replaces HA's file without a backup; `gateway == disk ≠ synced` (crash before the delayed save) blocks every change. | Three-way merge when `.app` exists; equal digests count as synced; keep the replaced export. | verified | 18 |
| D18 | W4-5 | `mesh_config.py:1418-1422` | Carry-over conflicts keep the app's value and drop HA's change from the export while the nodes keep it; only a log warning. | Per-entry repair naming the paths. | verified | 10 |
| D19 | W4-6 | `mesh_config.py:2721-2723`; `switch.py:613-689` | *Sensor values for IoT systems* shows the node's state but plans against the export: when they disagree the switch cannot change it. | Plan against the node's last answer. | verified | 22 |
| D20 | W4-7 | `mesh_config.py:1550-1561`; `strings.json` `remove_device_no_answer` | A node that reset but whose status was lost is reported "nothing was changed". | Look for its unprovisioned advert; word it "may have been reset"; guard the current proxy node. | verified | 22 |
| D21 | W4-8 | `jhmesh/export.py:1246-1260`, `:906-913`; `mesh_config.py:3245-3254` | A deleted scene's number is handed out again at once while non-member keys and force-skipped registers still recall it. | Allocator avoids numbers in key rows and a held set; report skipped members. | verified | 10 (allocator), 13 (held set) |
| D22 | W4-9 | `services.py:465-480` | Rewiring / deleting actions (rooms, keys, scenes, thresholds, schedules, `sync_gateway`) are callable by non-admin users. | `async_register_admin_service` (behaviour change; decision M8). | verified | 13 |
| D23 | H4-1 | `services.py:803-829` | Every configuration action reloads the entry: link rebuilt, every entity `unavailable → unknown → value` (state triggers fire), a mesh-wide re-read wave. | Apply export changes in place; interim: carry states and reader cache across the reload (decision M5). | verified | 28 |
| D24 | H4-2 | `event.py:184-211` is the only emitter of `EVENT_BUTTON_ACTION` | Disabling a key's event entity silently breaks its device triggers and logbook lines. | Fire the bus event from the hub's fan-out. | verified | 19 |
| D25 | Q4-4 | `renovate.json` (no python rule); open `renovate/python-3.x` | Renovate moves the library job off 3.13, leaving `requires-python >=3.13` untested. | `allowedVersions` rule or read the version from `pyproject.toml`; close the PR. | verified | 06 |
| D26 | Q4-5 (review-3 Q1 half done) | `tests/conftest.py:1043-1046` | Teardown asserts no replays but ignores undecryptable PDUs: a wrong-key Segment Ack or unacked Set passes every test. | Assert `not undecryptable` unless opted out; per-source counters. | verified | 01 |
| D27 | Q4-6 | `tests/property_helpers.py:195-214`; `switch.py:67`, `:659` (not imported by `__init__`); `tests/test_sensor.py:783` | One 10 s real-clock test; `fast_timeouts` patches only modules already imported. | Read timeouts at call time; `fast_timeouts`; a 5 s per-test budget. | verified | 11 |
| D28 | Q4-7 | `tests/test_snapshots.py:42-58` | Snapshots pin only the base network: unique IDs of blinds, RTR, detectors, puck, battery entities are unpinned. | Registry-identity snapshot per fixture network. | verified | 11 |
| D29 | Q4-8 | `docs/ha-integration.md:294-311` | About a dozen entities (*Last seen*, *Signal strength*, *Last restart*, cycle counters, sequence counters, *Key mode*, …) appear nowhere in the user docs. | Generated entity reference + test. | verified (6 names, 0 hits) | 43 |
| D30 | Q4-9 | `.github/workflows/ci.yml:92` | CI runs 2853 tests serially; lint gates every job. | `-n auto`; lint in parallel. | verified | 06 |
| D31 | F4 §2 (A–E) | `docs/parity/ledger-*.json` | About 50 ledger rows claim *implemented* for code that is absent or CLI-only (e.g. `msg:op:5d` with no Time Status handler, `msg:proxy:01` with no caller), others are too pessimistic, dup chains point wrong, and `coordinator.py` citations drift ~16 lines. | Fix the rows; cite `path::symbol`; make `tools/parity.py check` verify the symbol. | verified (spot checks) | 07 |

### Low (as reported; not re-verified unless noted)

| Sources | Defect (one line) | Brief |
|---|---|---|
| P4-6 | `add_device` provisions with an IV index not confirmed by a beacon on this link. | 03 |
| P4-7 | Proxy configuration PDUs skip replay and CTL/DST checks (only the proxy can exploit). | 20 |
| P4-8 | No-OOB provisioning only; capabilities ignored (accepted risk, document). | 40 |
| S4-7 | Skip-ahead target uses unchecked numbers: a garbled record can loop the repair or target seq 0. | 16 |
| S4-8 | The repair floor never advances: "cannot reuse" holds only for a bounded run. | 16 |
| S4-9 | Key-refresh progress saved on a 2 s debounce and per address. | 16 |
| S4-10 | Export writes: shared temp name, non-monotonic `touch`, unsynced backup copy. | 02 |
| S4-11 | The CLI protects only `0D00`, not HA's configured address (plausible). | 16 |
| R4-4 | Sends with no link still reserve numbers and clear the clean-close mark. | 12 |
| R4-5 | `PropertyReader` re-queues a version read per node per link, no dedup. | 21 |
| R4-6 = H4-5 | Central and room entities ignore the link-loss grace (verified: `entity.py:525-528`). | 15 |
| R4-7 | A Move dim hold has no end timeout and is not ended on link loss / stop. | 19 |
| R4-8 | Foreign proxy adverts wake the connection loop; Node-Identity hashed against every node, no negative cache. | 26 |
| R4-9 | Linear address scans and full record re-parses per message / send. | 26 |
| R4-10 | `standalone.connect` attaches without `beacon_wait` (review-3 T7 half; verified `standalone.py:90`). | 21 |
| R4-11 | `start_notify` / `disconnect()` unbounded (plausible hang). | 21 |
| W4-10 | A rename that adopted reloads from inside an entry task (10 s stall). | 22 |
| W4-11 | `add_device` does not validate the name before irreversible provisioning. | 03 |
| W4-12 | A typo in `set_room` creates a room and moves the loads. | 22 |
| W4-13 | Threshold actions misreport what was already written. | 22 |
| W4-14 | Element groups known only from `meta` are not removed with their node (latent). | 10 |
| H4-4 | After a key refresh the own mesh is "discovered" again; *Ignore* then collides the unique id (plausible). | 24 |
| H4-6 | Diagnostics raise on a not-loaded entry and omit options. | 25 |
| H4-7 | Several WARNING lines per unanswered request, on every link. | 25 |
| H4-8 | `unknown_nodes` embeds an English sentence as a placeholder. | 25 |
| H4-9 | 12 actions lack icons; unused `certificate_changed`; no `_set_confirm_only`; `GatewayBusy` → generic error. | 50 |
| H4-10 | Config entities never re-read after the first read (app changes unseen; plausible). | 32 |
| Q4-10, Q4-11 | README says real identifiers remain (they are pseudonyms); module table / numbers drift. | 43 |
| Q4-12 | Fixture-regeneration check and actionlint / zizmor never added. | 06, 11 |
| Q4-13 | The diagnostics secret scan looks for hex only, not derived keys or other encodings. | 11 |
| Q4-14, Q4-15, Q4-18 | Personal environment details in docs / tests; `.gitignore` gaps; home-range test IPs. | 64 |
| Q4-16 | The first public CHANGELOG is an internal fix log. | 65 |
| Q4-17 | Unpinned `pip` in the build job. | 06 |
| Q4-19 | Three proxy fakes drift; the HA fake shares one seq counter across sources. | 01, 27 |
| Q4-20 | Negative checks over real time (`real_wait`) pass trivially on a slow runner. | 11 |

Counts: **high 8 · medium 23 · low 38** (69 deduplicated from 94 source findings). None could not be confirmed.

## Improvements

Merged across reports, ranked within each theme (value per size). Brief numbers point at `briefs/`; "—" = backlog.

### Safety, protocol and robustness

| Item | Sources | Value / size | Brief |
|---|---|---|---|
| Proof-gated key-refresh follower as a pure state machine | P I-1 | H / M | 04 |
| Backup platform + restored-record detection | S I1 | H / M | 08 |
| Store-health repair, bounded back-pressure, fail fast with no link | S I3, R I-3, R I-4 | H / S–M | 12 |
| IV timing guards + rewind repair | P I-2, H I-11 | M / M | 09 |
| Own-source detection (another client on our address) | S I2 | H / S–M | 20 |
| Vault durability, pending record before Provisioning Data | S I6 | M–H / M | 17 |
| Vault-aware allocation + RPL hygiene | P I-7 | H / S | 03 |
| Mesh-level store state, floor refresh, range-checked target, fresh-address offset | S I5, I7, I12 | M / S–M | 16 |
| One atomic writer; key hygiene | S I10, P I-10 | M / S | 02 |
| Carry vault nodes through the app's key refresh | P I-3 | M / M | 14 |
| Mesh 1.1 private beacons / identities + spec vectors | P I-4, I-5 | M / S–M | 29 |
| Automatic bounded skip-ahead | S I4 | M / S | — (decision M6) |
| SAR receive ack timer; IV Update initiation | P I-8, I-11 | L–M / S | — |
| CLI counter batching | S I11 | L–M / S | 16 |

### Mesh functionality

| Item | Sources | Value / size | Brief |
|---|---|---|---|
| Transitions on lights, scenes, key scenes (probe first) | F4-1 (review-3 F19) | H / S–M | 31 |
| Lock awareness on lights and sockets | F4-2 | M–H / S–M | 35 |
| `update_entity` reads the device; config re-read per link | F4-7, H I-13 | M / S | 32 |
| Node clocks, zone, location as diagnostics + repair | F4-8 | L–M / S | 34 |
| Insert function and layout from the advert | F4-12 | L–M / S | 33 |
| Firmware-only properties after a supervised probe (hotel / night / presentation, 0x0F00, run-on remaining, LED RGB) | F4-3, F4-9, F4-10, F4-11 | M / M | 36 |
| Multiple rooms per load, leave a room | F4-5, W I12 | M / S–M | 37 |
| Key connections: TW element, slats, lock-function keys | F4-4 | M / M | 38 |
| App coexistence write-backs | F4-6 | M / M | 39 |
| Commissioning completeness, time keeper, honour OOB capabilities | F4-13, F4-14, P I-6 | M / M–L | 40 |
| Audit / locator extras, gateway approve, read-only firmware `update` entity | F4-15, F4-17, F4-18, U4-11 | L–M / M | 41 |
| Absent-hardware refinements (blinds, RTR, detectors, battery) | F4-16 | L here / M–L | 42 |
| On-air verification sweep of what is built | F4 Wave 0 | H / S (human) | 30 |
| Not worth doing (decision records): Light LC / HSL / SIG Scheduler, Sensor Cadence / Settings, 0x0052, Default Transition Time, virtual addresses, LPN / Friend, SAR tuning, Health Period | F4 §1 | — | 07 records them |

### HA and UX

| Item | Sources | Value / size | Brief |
|---|---|---|---|
| Apply config changes without a reload | H I-1 | H / L | 28 |
| Gateway reauthentication (Silver blocker) | H I-2, U4-5 | H / M | 23 |
| German translation | U4-1, U4-16 | H / M–L | 51 |
| Rooms → areas mapping, button / node areas, area sync | U4-2, F4-5 | H / M | 47 |
| Repairs that fix, `learn_more_url`, troubleshooting entries | U4-5, H I-11 | H / M–L | 44 |
| Follow the app automatically | U4-6 | H / M | 48 |
| Blueprints and a button cookbook | U4-3 | H / M | 46 |
| Mesh health at a glance; hide room central entities | U4-7, U4-12, H I-5 | M–H / M | 45 |
| Button events from the hub; key-aware triggers | H I-3, U4-9 | M / S | 19 |
| Discovery: JUNG-only matcher, stale-export error, key-refresh awareness, stable unique id | H I-6, I-7, I-9 | M / S–M | 24 |
| Diagnostics in every state, log once, link history | H I-4, I-5, R I-9 | M / S | 25 |
| Onboarding: zeroconf gateway, unicast under *Advanced*, icons | U4-8, H4-9 | M–H / S–M | 50 |
| Dry runs, structured responses, action ergonomics | W I3, I6, I7, I9, U4-13 | M–H / M | 49 |
| Fewer config entities on by default | H I-8 | M / S | 25 (decision M9) |
| Pre-flight reconcile before destructive plans | W I4 | H / M | — |
| Topology card, signed export download, French / Spanish / Italian / Dutch, per-key double click | U4-14, U4-17, U4-18, U4-19 | L–M | — |

### Architecture

| Item | Sources | Value / size | Brief |
|---|---|---|---|
| Persistence out of `coordinator.py` | A4-1 | H / S | 52 |
| One dispatch helper, error mapper, pure conversions | A4-2, A4-6, A4-7 | M / S | 53 |
| Split `config_entities.py`, `mesh_config.py`, `services.py` | A4-4, A4-5, A4-8 | M–H / M | 54, 55, 56 |
| `jhmesh` stable API, `LocalState` in `state.py` | A4-9 | H / M | 57 |
| Hub components (gestures first), lifecycle registry | A4-3, A4-13 | H / L | 58, 60 |
| Link counters and a trace logger (changes logger names) | A4-14 | M / S | 61 (decision M1) |
| Plan model into `jhmesh`, Protocols, TypedDicts, `const.py` diet | A4-10, A4-11, A4-12 | M / M | 62 |
| Dead code, docstring trim, table-driven describe, CLI parser tables | A4-16, A4-17, A4-18 | L / S | 63 |
| Hot paths: O(1) lookups, parse cache, fewer entity writes | R I-6, R I-10 | M / S | 26 |

### Tests and CI

| Item | Sources | Value / size | Brief |
|---|---|---|---|
| Strict fake teardown, per-source counters | Q4-5, Q4-19 | H / S | 01 |
| CI: no branch artifacts, `-n auto`, Renovate rule, actionlint / zizmor, release checks as a script | Q4-2, Q4-4, Q4-9, Q4-17, C3, C5 | H / M | 06 |
| Clock / timeout fixes, snapshot per network, regen check, secret scan, file-mode tests | Q4-6, Q4-7, Q4-12, Q4-13, Q4-20, T8 | M–H / M | 11 |
| HA hub over `tests/sim`, flapping soak, fake conformance | A4-15, Q T1, T6, R I-12 | H / M | 27 |
| "Stop anywhere" plan property test | W I10 | H / M | 05 |
| Nightly: mutation testing, `thorough` Hypothesis, latest HA, replayed traces, upgrade tests | Q T2, T3, T5, T7, C4 | M–H / M | 59 |
| Hub lifecycle state machine | Q T4 | H / L | — (after 27) |

### Docs

| Item | Sources | Value / size | Brief |
|---|---|---|---|
| User guide / reference split, generated entity reference, FAQ, German quick start, free-rocker recipe | U4-4, U4-10, Q B5 | H / M–L | 43 |
| Parity ledger corrections and symbol citations | F4 §2 | M / M | 07 |
| Troubleshooting for every repair | H4 rule table | M / S | 44 |

### Release and privacy

| Item | Sources | Value / size | Brief |
|---|---|---|---|
| Tree scrub, `.gitignore`, privacy scanner, pre-commit, noxfile | Q4-14, Q4-15, Q4-18, D1, D2, D3 | H / M | 64 |
| Fresh public repository, first-release CHANGELOG, CONTRIBUTING, issue templates, checklist | Q4-3, Q4-16, D4 | H / M | 65 |
| First full release with a green HACS run, hacs/default listing | HACS requirements | H / S–M | 66, 67 |
| Brand images | U4-15, F12 | done (maintainer: the existing JUNG HOME brand assets shipped locally) | — |

## HACS (default store)

What HACS needs for the default store: public repository with a description, issues and topics; at least one full
GitHub release (not only a tag) made after the HACS action and hassfest pass; `hacs.json` with `name`; the
`manifest.json` keys `domain`, `documentation`, `issue_tracker`, `codeowners`, `name`, `version` (all present); one
integration under `custom_components/` (holds); brand images; the HACS action and hassfest green with no ignored
validator; then a PR to hacs/default adding the repository alphabetically to the `integration` list, from the owner's
personal account, with the template filled in.

**Done — brand.** home-assistant/brands no longer accepts `custom_integrations/` entries; a custom integration ships
its images in `custom_components/<domain>/brand/` (local images take priority over the brands CDN). The existing
JUNG HOME brand assets (icon, icon@2x, dark_icon, dark_icon@2x from the brands repository's `junghome` entry) are
shipped locally in `custom_components/junghome_ble/brand/`, `brands` is off the `ci.yml` ignore list, and
`quality_scale.yaml`, the roadmap and the README say so (maintainer change). The HACS brands validator accepts a local
`custom_components/<domain>/brand/icon.png`. Bronze now waits only for `dependency-transparency`. Known limitation:
the HACS panel itself may not show inline icons yet; HA's own UI does.

TODO:

- [ ] Public repository (brief 65, decision M2).
- [ ] First full GitHub release `v0.3.0` through `release.yml`, CHANGELOG section as notes — needs the CI artifact fix
  (brief 06) first (brief 66).
- [ ] `hacsjson` and `integration_manifest` validators: their exemptions in `ci.yml` drop by themselves on the public
  repository; confirm both pass there with nothing ignored (brief 66). Both files already pass HACS's own schemas
  (`HACS_MANIFEST_JSON_SCHEMA`, `INTEGRATION_MANIFEST_JSON_SCHEMA` from hacs/integration, run locally); what fails
  on the private repository is only the unauthenticated download.
- [ ] PR to hacs/default, opened by the owner (brief 67, decision M14).
- [ ] README *Install* section with the my.home-assistant.io HACS repository link and an "unofficial, not affiliated
  with JUNG" line near the top (brief 67, decision M15).
- [ ] `country` key in `hacs.json`: recommended to leave it out — JUNG HOME is sold in several European countries
  (brief 67).

## Roadmap

Phases in order. "Here" = this installation: lights (switch, dimmer, DALI / tunable-white inserts), sockets
(metering), push-buttons, mini actuators, the gateway — **no** puck, blind, room thermostat, detector or battery
node. "Person" = someone at home to press keys or watch a light.

**Phase 0 — safety (briefs 01–05, 08–10, 12, 13).** File modes (02); pending-node addresses (03); proof-gated key
refresh (04); cancel-safe plans (05); backup restore (08); IV timing guards (09); allocation away from the app (10);
stalled-store repair (12); `delete_unused_scenes` guard and admin gating (13); plus the strict fake teardown (01) so
the fakes catch the next nonce regression while these land. *On air:* 08 and 10 and 13 here with lights and the
gateway; 04, 09, 12 regression only (a real key refresh or IV update cannot be triggered safely); 03 needs a spare
unprovisioned device; 01, 02, 05 local only.

**Phase 1 — runtime, HA and test correctness (06, 07, 11, 14–29).** Link lifecycle and short-link penalty (15),
store hygiene (16), vault durability (17), merge and sync (18), button events (19), own-source detection (20),
connect schedule (21), node-truth writes (22), reauth (23), discovery (24), diagnostics (25), performance (26), HA over
the simulator (27), no-reload config (28, decision), Mesh 1.1 privacy (29); CI (06), ledger (07) and test hardening
(11) ride along in early waves. *On air:* 15 (breaker on the proxy node, person), 18 (app edit + gateway), 19
(person pressing keys), 21, 23 (revoke HA in the app), 25, 28 here; 14 and 17 need an HA-added device / app key
renewal (not here); the rest regression or local.

**Phase 2 — functionality (30–42), from report 6's waves.** Wave 0 = 30 (on-air sweep: F10 inputs, F4 lock bits,
F15 key → scene, thresholds, CTL Temperature Set; person needed). Then 31 transitions, 32 `update_entity`, 33 insert
and layout, 34 node clocks, 35 lock awareness, 36 firmware-only properties, 37 room membership — all verifiable here,
31 / 35 / 36 after a supervised CLI probe; 38 key connections (capture the app first, person); 39 write-backs (spare
app install); 40 commissioning (spare device); 41 extras (read-only parts here); 42 absent hardware (offline only,
"unverified on air").

**Phase 3 — UX and docs (43–51).** Docs split first (43, solo), then repairs (44), health (45), blueprints (46),
areas (47), follow the app (48), dry runs (49), onboarding (50), German (51). *On air:* 45 (pull a breaker), 46
(rockers), 48 (an app edit), 49 (dry runs), 50 (zeroconf from the gateway) here.

**Phase 4 — architecture (52–58, 60–63).** Behaviour-identical moves, snapshots unchanged; regression on air only.

**Phase 5 — release (59, 64, 65).** Nightly test strength (59), tree scrub and scanner (64), then the public cut
(65, decision M2) after 02 and 06 have landed.

**Phase 6 — HACS default store (66, 67).** Brand already shipped. The first full GitHub release with a green HACS
action and hassfest on the public repository (66), then the owner's hacs/default pull request (67). See "HACS (default store)" above.

Probes to run before building (CLI `tools/mesh_poc.py`, sniffer `tools/mesh_sniff.py capture` / `decode`): 31
(`--transition` on `lightness` / `ctl` / `scene`, watch the light and `listen`), 35 (lock a load, then `set` /
`lightness`: does it answer?), 36 (supervised `prop get` / `prop set` of `0x0F00–0x0F02`, `0x500C`, `0x1008–0x1013`,
run-on, LED colour; restore every value), 38 (capture the app making each connection), 24 (does HA's advert for the
proxy MAC carry manufacturer data `0x0527`?), 34 (Time Get, read-only).

## Fan-out schedule

Each wave's briefs run in parallel in separate worktrees. Within a wave no two briefs edit the same *region* of a
code file; where two touch the same file in different functions the row says so (git's three-way merge normally
takes both). **Every brief appends to `CHANGELOG.md` and updates `docs/ha-integration.md`**, most add strings to
`strings.json` / `translations/en.json`: expect textual conflicts there at cherry-pick and keep both sides.

| Wave | Briefs | Main files | Conflicts to watch |
|---|---|---|---|
| 1 | 01, 02, 03, 04, 05, 06, 07 | 01 `tests/conftest.py`; 02 `jhmesh/client.py` (`LocalState._write`), `jhmesh/export.py` writers, `coordinator.py` (`SeqStore` constructors), `SECURITY.md`; 03 `onboard.py`, `jhmesh/onboarding.py`, `commission.py`, `vault.py`, `services.py` (registration); 04 new `jhmesh/keyrefresh.py`, `jhmesh/client.py` (key refresh, `_deliver`), `coordinator.py` (`:744-769`, `:4361`); 05 `mesh_config.py` (`_send`, scene loops, `remove_node`), `services.py` (`_run`); 06 `.github/`, `renovate.json`; 07 `docs/parity/`, `tools/parity.py` | `jhmesh/client.py`: 02 / 03 / 04 in different classes; `services.py`: 03 registration vs 05 `_run`; `tests/test_key_refresh.py`: 01 opt-out vs 04 tests. Cherry-pick 01 first. |
| 2 | 08, 09, 10, 11 | 08 new `backup.py`, `coordinator.py` (`HAState`, `async_create`), `tests/test_properties_seq_store.py`; 09 `jhmesh/client.py` (`apply_beacon`, record), `coordinator.py` (`_check_iv_index`), `repairs.py`; 10 `jhmesh/export.py` allocators, `commission.py`, `mesh_config.py` (`_adopt`), `onboard.py`; 11 `tests/*` helpers and snapshots, timeouts in `config_entities.py` / `switch.py` / `schedules.py` / `energy_history.py` | 08 owns the store minor-version bump; 09 adds optional record fields without a bump. |
| 3 | 12, 13, 14 | 12 `coordinator.py` (`SeqStore`, `reserve_seq`, `_keep_alive`, `_filter_status_overdue`, `_while_seq_stalls`), `jhmesh/client.py` send paths, `keep_awake.py`, `config_entities.py` reader worker; 13 `mesh_config.py` (scene delete paths), `services.py`, `services.yaml`; 14 `coordinator.py` (key-refresh hook), `onboard.py`, `vault.py`, `diagnostics.py` | `coordinator.py`: 12 `:4405` vs 14 `:4361` are adjacent — merge by hand if needed. |
| 4 | 15, 16, 17, 18 | 15 `coordinator.py` (`:2159-2600`, `:4388`), `entity.py:525`; 16 `coordinator.py` (`:585-770`), `repairs.py`, `jhmesh/client.py` (`set_key_refresh`), `tools/mesh_poc.py`; 17 `identity.py`, `onboard.py` (`_keep_key`), `jhmesh/provisioning.py`; 18 `jhmesh/merge.py`, `mesh_config.py` (`_adopt`, `_upload`), `config_flow.py` (`_async_keep_incoming`) | none in code |
| 5 | 19, 20, 21, 22, 23 | 19 `event.py`, `device_trigger.py`, `logbook.py`, `coordinator.py` (gestures `:4060-4265`); 20 `jhmesh/client.py` (`:1752-1930`), `coordinator.py` (callback, issue); 21 `config_entities.py` reader, `coordinator.py` (`_after_connect`), `standalone.py`, `jhmesh/client.py` (`:977`, `:1046`); 22 `switch.py`, `mesh_config.py` (`:1532-1602`, `:2436`, `:2698`), `services.py`, `device_names.py`; 23 `config_flow.py`, `mesh_config.py` (`:1159`), `gateway_status.py`, `quality_scale.yaml` | `coordinator.py`: 19 ends holds in 15's link-loss hook, 21 edits `_after_connect` nearby. |
| 6 | 24, 25, 26, 27 | 24 `config_flow.py` bluetooth steps, `coordinator.py` (`_on_key_refresh`), `__init__.py`, `manifest.json`; 25 `diagnostics.py`, `jhmesh/client.py:1650`, `sensor.py`, `coordinator.py` (`:1943`); 26 `jhmesh/cdb.py`, `devices.py`, `jhmesh/client.py:882`, `coordinator.py` (`:1811-1862`, `:1027-1062`); 27 `tests/conftest.py`, `tests/sim`, `tests/property_helpers.py` | `coordinator.py` `:1847` (26) vs `:1943` (25). Snapshots: 25 only. |
| 7 | 28, 29 | 28 `services.py`, `__init__.py`, `coordinator.py`, every platform's setup, `entity.py`; 29 `jhmesh/client.py`, `pdu.py`, `crypto.py` | none (29 is library-only) |
| 8 | 30, 31, 32, 33 | 30 ledgers, `docs/`; 31 `jhmesh/messages.py` (`:260-390`), `coordinator.py` (`:4502-4660`), `light.py`, `scene.py`, `mesh_config.py` (`assign_key`), `tools/mesh_poc.py`; 32 `entity.py`, `config_entities.py` (`PropertyEntity`), `climate.py`, `sensor.py`; 33 `coordinator.py` (`:1395-1460`), `cdb.py`, `devices.py`, `entity.py` (device info) | `entity.py`: 32 base class vs 33 device-info builders; ledgers: 30 vs 31 rows. |
| 9 | 34, 35, 36, 37 | 34 `jhmesh/messages.py` (Time builders), `coordinator.py` (`:2974-3030`), `config_entities.py` (node info), `sensor.py`, `diagnostics.py`; 35 `coordinator.py` (`:3272`, `:4467-4500`), `light.py`, `switch.py`, `config_entities.py` (`:1915-1990`); 36 `jhmesh/properties.py`, `config_entities.py` (`describe`), platforms for the new entities; 37 `mesh_config.py` (`set_room`), `services.py`, `jhmesh/export.py` | `sensor.py`: 34 vs 36 (new sensors appended); `config_entities.py` three regions. |
| 10 | 38, 39, 40, 41, 42 | 38 `mesh_config.py` (`KEY_MODES`, `assign_key`), `devices.py`; 39 `jhmesh/export.py`, `schedules.py`, `mesh_config.py` (`store_scene`); 40 `jhmesh/commission.py`, `onboard.py`, `provisioning.py`; 41 `audit.py`, `gateway_api.py`, new `update.py`; 42 `cover.py`, `climate.py`, `binary_sensor.py`, `number.py` | `mesh_config.py`: 38 vs 39 different regions |
| 11 | 43 (solo) | `docs/**`, `README.md`, `manifest.json` (`documentation`), new `tools/gen_entity_reference.py` | moves `docs/ha-integration.md`: nothing else in this wave |
| 12 | 44, 45, 46 | 44 `repairs.py`, `const.py`, every `async_create_issue` site, docs; 45 `binary_sensor.py`, `sensor.py`, central entities in `light.py` / `switch.py` / `cover.py` / `climate.py`; 46 new `blueprints/`, `tests/test_blueprints.py` | 44 touches issue sites in `coordinator.py` / `mesh_config.py` (one-line additions) |
| 13 | 47, 48, 49 | 47 `config_flow.py` (areas step), `entity.py`, `__init__.py`; 48 `coordinator.py` (app activity), `mesh_config.py` (`adopt_if_gateway_changed`), `button.py`; 49 `services.py`, `services.yaml`, `mesh_config.py` (dry-run plumbing) | `mesh_config.py`: 48 near `_adopt` vs 49 `_load` and operation signatures — cherry-pick 48 first. |
| 14 | 50, 51 | 50 `manifest.json`, `config_flow.py` (zeroconf, sections), `icons.json`; 51 `translations/de.json`, `tests/test_translations.py` | none |
| 15 | 52, 53, 54, 55, 56, 57 | 52 `coordinator.py` `:404-1192` → `seq_store.py`, `node_info.py`; 53 new `dispatch.py`, `errors.py`, `conversions.py`, small platform edits; 54 `config_entities.py` → `properties/`; 55 `mesh_config.py` → `configurator/`; 56 `services.py` → `actions/`; 57 `jhmesh/client.py` → `jhmesh/state.py`, `__all__` everywhere in `jhmesh` | none, provided every brief keeps re-exports (report 8's table) |
| 16 | 58, 59 | 58 `coordinator.py` → `hub_gestures.py`; 59 `.github/workflows/nightly.yml`, `tests/traces/`, `tools/trace_to_fixture.py` | none |
| 17 | 60 (serial PRs) | `coordinator.py` → hub components one at a time | serial by design |
| 18 | 61 | `jhmesh/client.py`, `diagnostics.py`, logging | decision M1 first |
| 19 | 62 | `jhmesh/plan.py`, `jhmesh/export.py`, `const.py`, typing across modules | touches many files: solo |
| 20 | 63 | nearly every file (docstrings, dead code) | solo, last code wave |
| 21 | 64 | docs, tests (IPs, zone), `.gitignore`, new `tools/privacy_scan.py`, `.pre-commit-config.yaml`, `noxfile.py` | solo |
| 22 | 65 | new repository, `CHANGELOG.md`, `CONTRIBUTING.md`, `.github/ISSUE_TEMPLATE/` | maintainer-driven |
| 23 | 66 | `.github/workflows/ci.yml` (exemptions), `.github/workflows/release.yml`, `CHANGELOG.md` | on the public repository; maintainer runs the tag |
| 24 | 67 | `README.md` (Install section, disclaimer line), `hacs.json` (only if `country` is decided) | the PR to hacs/default is opened by the owner |

Dependencies (hard unless marked *soft*):

- 01 → every later brief (stricter teardown; cherry-pick first).
- 03 → 10 (both edit `onboard.py` / `commission.py`), 14, 17, 40.
- 04 → 14, 16 (key-refresh persistence), 24 (`_on_key_refresh`), 29 (*soft*, `_parse_beacon`).
- 05 → 13 (both edit `delete_scene`), 22, 49.
- 08 → 16 (store format), 09 (*soft*, store minor version).
- 09 → 16 (`repairs.py`); 09 → backlog IV Update initiation.
- 10 → 13 (allocator `avoid` for held scene numbers), 18 (`_adopt`), 37, 39.
- 12 → 15 (`_keep_alive` / `_watch_link`), 16, 20, 26 (`HAState._limit`).
- 15 → 19 (link-loss hook for holds), 21 (*soft*, link generation), 25 (link history), 27 (*soft*).
- 17 → 40.
- 18 → 48.
- 22 → 49 (*soft*).
- 23 → 44.
- 24 → 50 (`manifest.json`, `config_flow.py`).
- 28 → 25 (*soft*: fewer reload re-read waves), 47, 48 (*soft*).
- 30 → 38 (F15 verified on air), 31 key-scene part.
- 33 → 39, 40.
- 35 → 38.
- 43 → 44 (anchors), 46 (*soft*), 65.
- 45 → 46 (*soft*: offline blueprint).
- 47 → 51 (*soft*, strings).
- 52, 53 → 58 → 60 → 62 → 63.
- 54 → 53's `errors.py` adoption (one-line follow-up); 55, 56 likewise.
- 57 → 61, 62.
- 02, 06, 64 → 65.
- 06, 65 → 66 → 67.

## Decisions for the maintainer

- **M1 — Observability (A4-14, brief 61).** A `jhmesh.trace` child logger and `LinkStats` change existing log record
  names and add diagnostics keys (snapshot update). Accept, or keep names and add counters only?
  **Taken:** accepted — the `jhmesh.trace` child logger and `LinkStats`, with the renamed log records and the new
  diagnostics keys (snapshot update), said so under *Upgrading* in the CHANGELOG.
- **M2 — Public release (Q4-3, brief 65).** A new repository with one clean commit (noreply author, no session
  trailers) and the private one archived — recommended — or rewrite and flip this one (unreachable commits and PR refs
  stay fetchable by SHA on the existing remote).
- **M3 — `delete_unused_scenes` (brief 13).** Dry run by default (recommended; breaks automations that rely on the
  delete) or explicit `confirm` only?
- **M4 — German wording (brief 51).** "du" (HA and the JUNG app) vs "Sie"; *Taster* vs *Wippe* for a key; *Raum* vs
  *Bereich* for a room (the app uses *Bereich* for areas).
  **Taken:** informal "du", as Home Assistant's own German. JUNG concepts take the German JUNG HOME app's terms, Home
  Assistant concepts Home Assistant's (*Bereich*, *Gerät*, *Entität*, *Aktion*, *Reparatur*, *Einstellungen → Geräte
  & Dienste*), and where the two collide Home Assistant keeps its word:

  | Concept | German | Note |
  |---|---|---|
  | key of a push-button ("Button A") | *Taste* (*Taste A*) | the app's *Taste* |
  | rocker | *Wippe* | the app's layouts *Wippe \| Taste* |
  | push-button (the product) | *Taster* (*Taster 1-fach*) | 1-gang / 2-gang: *1-fach* / *2-fach* |
  | gang of keys (one device) | *Tastengruppe* | no app term |
  | JUNG room | *Raum* | the app says *Bereich*; explained once where rooms meet areas |
  | Home Assistant area | *Bereich* | |
  | scene, group | *Szene*, *Gruppe* | |
  | insert | *Einsatz* (*Schalteinsatz*, *Dimmeinsatz*, *Nebenstelleneinsatz*) | |
  | key mode / key connection | *Tastenmodus* / *Verknüpfung* | modes as the app: *Beleuchtung*, *Schalten*, *Fahren* |
  | LED, LED colour | *LED*, *LED-Farbe (eingeschaltet / ausgeschaltet)* | the app's parameter names |
  | lock function, lock-out protection | *Sperrfunktion*, *Aussperrschutz* | |
  | time keeper, run-on time | *Zeitgeber*, *Nachlaufzeit* | |
  | continuous on / off | *Dauer-Ein* / *Dauer-Aus* | |
  | the app's export | *Export*, *Projektdatei*; *Projekt → Projektübergabe* | |
  | gateway access request | *Zugriffsanfrage*; *Einstellungen → Gateway → Zugriffsberechtigungen → Offene Anfragen* | |
  | node, proxy node, key refresh | *Knoten*, *Proxy-Knoten*, *Key-Refresh* | mesh terms stay technical |
  | "unverified on air" | *auf echten Geräten noch nicht überprüft* | the markers keep their meaning |
- **M5 — Reloads (H4-1, brief 28).** May configuration actions stop reloading the entry (in-place model update, L), or
  only carry states and the reader cache across the reload (interim, M)?
- **M6 — Skip-ahead policy (briefs 08, 12, 16).** Spend 2^20 numbers automatically on a restored record, on a fresh
  store at a used address, and on strong `pdus_dropped` evidence (S I4), or keep the user's click?
- **M7 — Allocation with identity off (brief 10).** Top-down group / scene allocation now, or make provisioner
  identity the default once an app import of such a file is verified on a spare install?
- **M8 — Admin-only actions (W4-9, brief 13).** Breaking for non-admin users and tokens: accept?
- **M9 — Entity defaults (briefs 25, 45).** Turn rarely used config entities off and hide room central entities, for
  new registrations only?
- **M10 — Discovery (brief 24).** JUNG-only matcher after the on-air check; move the entry unique id to the mesh UUID
  (entry migration)?
  **Taken:** yes to both (brief 69). The on-air probe: 28 of 29 JUNG proxies in Home Assistant's stored adverts carry
  manufacturer data 0x0527.
- **M11 — Hold end on link loss (R4-7, brief 19).** Fire `hold_end` with `reason: link_lost`, or drop the hold
  silently?
- **M12 — Follow the app (brief 48).** A periodic gateway GET (every few hours) plus activity-triggered adoption: OK?
  **Taken:** gateway entries GET the export a few minutes after the phone's last activity on the mesh (a burst is one
  GET, at most one per 15 minutes from activity) and every 6 hours, applying it only when its digest changed; both on
  by default and switchable off in the options. File entries never fetch: a repair points at the new-export upload
  once the phone was seen changing a device's configuration (once until resolved). A changed certificate or a
  rejected token is never accepted or re-registered silently: Reconfigure and the re-authentication as before.
- **M13 — Version and CI clean-up.** First public version 0.3.0 with "first public release" notes, or renumber; delete
  the old CI artifacts by hand once.
- **M14 — HACS listing timing (briefs 66, 67).** Submit to hacs/default right after the first public release, or let
  the release run as a custom repository for a while first. A PyPI release (`dependency-transparency`) is not a HACS
  requirement and need not wait.
  **Taken:** run as a custom repository first; the hacs/default PR (brief 67) waits until the releases have run that
  way for a while.
- **M15 — Display name (brief 67).** Keep "JUNG HOME (Bluetooth Mesh)" in `manifest.json` / `hacs.json`, or add
  "unofficial" wording; either way the README states near the top that the project is unofficial and not affiliated
  with JUNG (`DISCLAIMER.md`).
  **Taken:** no "unofficial" in the name, renamed *JUNG HOME Bluetooth Mesh* (without parentheses); the README and
  `DISCLAIMER.md` keep saying it is unofficial.

## Status

- **Wave 1 (briefs 01–07): done.** D26 strict fake link (01), D1 key files owner-only (02), D2 pending nodes and
  `reset_pending_device` (03), D4 proof-gated key refresh (04), D12 cancel-safe plans and the plan journal (05), D7 /
  D25 / D30 CI without branch artifacts, faster, linted (06), D31 parity rows and symbol citations (07). Merge
  follow-ups: brief 02's file-mode test moved to `KeyRefreshRecord`; 12 citations of the removed
  `ProxyClient._key_refresh_to` now cite `ProxyClient._key_refresh_moved`; `msg:op:8017` (Key Refresh Phase Status)
  is implemented, the statuses being proof. Brief 01's corrupted-Segment-Ack check was run by hand: the teardown
  caught it. Left for the maintainer: close the `renovate/python-3.x` PR, delete the old CI artifacts once, push a
  pre-release tag to see the tag-only uploads work (06); `reset_pending_device` / `add_device` and the key-refresh
  proof are unverified on air (03, 04). Brief 11 also owns `coordinator.py`'s one line that only some random
  examples of `test_properties_seq_store` reach, which now and then costs the coverage gate.
- **Wave 2 (briefs 08–11): done.** D5 an HA backup restore skips ahead instead of reusing nonces (08), D10 IV
  timing guards and a fixable `iv_index_mismatch` (09), D6 / D18 / D21 rooms, scenes and element groups allocated
  from the top, away from the app's next numbers, with a `carry_over_conflict` repair (10), D27 / D28 test-suite
  hardening: 5 s call budget, key-leak scan, registry snapshots of every fixture network, both flakes fixed (11).
  Merge follow-ups: a restored record keeps a concrete guard from a rewind (08 × 09; tested); the property state
  machines are `slow_ok` and skip real flushes. Unverified on air: backup restore, IV rewind, top-down allocation
  (`docs/ha-integration.md` says so). Decisions taken by default: M6 (the restore skip is automatic), M7 (top-down
  now). The IV timing is the spec's, with no test-mode bypass.
- **Wave 3 (briefs 12–14): done.** D9 bounded back-pressure, a `seq_store_unwritable` repair, no number without a
  link (12); D3 / D22 `delete_unused_scenes` a dry run by default and refused on a stale export, held scene numbers,
  rewiring and deleting actions admin-only (13, decisions M3 and M8 taken as recommended); D11 the devices Home
  Assistant added carried through the app's key renewal as far as it is proven (14). Changelog: from here on the
  `## 1.1.0 (unreleased)` section. Unverified on air: 13's gateway path, all of 14.
- **Wave 4 (briefs 15–18): done.** D13 / D14 one link lifecycle: the grace however a link ends, a short-link
  penalty that passes a flapping proxy over, a command re-sent on the next link (15); S4-7 / S4-8 / S4-9 / S I5
  sequence-store hygiene: range-checked skip targets, a floor that keeps up, the key refresh saved at once and for the
  mesh, a used address starting 2^20 in, `mesh_poc --ha-storage` (16; the state machine also found a skip-ahead past
  the end handing the last number out twice, fixed); D15 vault writes checked, the device key on disk before the
  Provisioning Data (17); S4-4 / S4-6 merge identities, a both-changed export merged, the replaced export kept, sync
  bookkeeping in its own store (18). Unverified on air: 15, 16's fresh-address skip, 17, 18's merge. Decision M6
  taken as automatic for 16 as well.
- **Wave 5 (briefs 19–23): done.** H4-2 button events published by the hub whatever the entity's state, key-aware
  device triggers, every hold ends (`reason`: decision M11 as recommended) (19); S I2 / P4-7 another client on Home
  Assistant's address detected, sends refused, a fixable `address_shared` repair; proxy configuration PDUs checked
  (20); R4-5 / R4-10 / R4-11 / R I-5 reader dedup, Time Set and location first, fresh connect-time steps skipped,
  bounded GATT calls (21); W4-6 / W4-7 / W4-10 / W4-12 / W4-13 / W I5 node-truth writes, remove_device that checks
  for the reset, `set_room` creates a room only with `create`, threshold progress in errors, plans refused for
  unreachable nodes (22); H I-2 the gateway reauthentication flow, `reauthentication-flow: done` (23). Unverified on
  air: most of it; the briefs' reports list the checks.
- **Wave 6 (briefs 24–27): done.** H4-4 / H I-6 discovery recognises the mesh across a key refresh and a stale
  export has its own error; JUNG-only discovery and the unique-id move wait for decision M10 (24); H4-6 / H4-7 /
  H4-8 / H I-5 diagnostics in every entry state with a link history, write-error paths redacted, one warning per
  silent device, the `unknown_nodes` text translated whole, *Link state* on by default for new installs; M9 left
  open (25); R4-8 / R4-9 O(1) lookups, a restart-point cache, adverts of other networks no longer wake the loop,
  unchanged states not written (26); the hub over the simulated mesh, a flapping-link soak and fake conformance (27).
- **New finding D32 (from brief 27's soak):** a Set lost on air while a Get of the same element is out is confirmed
  by the Get's Status (`ProxyClient.request` matches on element and status opcode), so the action succeeds though
  the load never changed. **Fixed:** a Status answers an acknowledged load Set only when it shows the requested state
  (present, or target while transitioning, within the load's step) or no other request waits for it; the Set is sent
  again otherwise. The soak fails on the case for seeds 27, 4, 5 and 14; unverified on air.
- **Wave 7 (briefs 28–29) and D32: done.** D23 configuration changes followed in place, a reload only as the fallback
  (28, decision M5 taken as the full option); Mesh 1.1 private beacons and identities, the specification's sample
  data pinned (29); D32 a Status that does not show a Set's state no longer confirms it.
- **Wave 8 (briefs 30–33): done.** The on-air sweep checklist `docs/on-air-sweep.md` and `tools/on_air.py`, which
  lists any "unverified on air" marker the checklist misses (30); transitions built and off until the probe in the
  checklist's B8 (31); `update_entity` reads the device, config values re-read per link (32); inserts and key layouts
  from the adverts, also kept when the export is followed in place (33).
- **Wave 9 (briefs 34–37): done.** F4-8 node clocks, zones and stored locations as diagnostics, a fixable
  `node_clock_wrong` repair (34); F4-2 lock awareness on lights and sockets: `locked` / `lock_until`, commands refused
  while locked, a locked load not marked unreachable (35); firmware-only properties held back until the supervised
  probe (on-air sweep A7 / C6), *Switches off at* from a reported remaining time (36); several rooms per load,
  `add_to_room` / `remove_from_room` (37). All unverified on air; the sweep has the checks.
- **Wave 10 (briefs 38–42): done.** F4-4 key connections: a tunable-white light's colour temperature, slats,
  lock-function keys (38); F4-6 the app's rows written back: scene values, `update_schedule`, removal clean-up, insert
  and layout rows (39); F4-13 / F4-14 / P4-8 commissioning by the app's rules, the time keeper, Static OOB and the
  HMAC algorithm (40; the specification's provisioning sample covers the CMAC algorithm only); F4-15 / F4-17 / F4-18
  audit keys and Friend, `locate_node`, `approve_gateway_client`, a read-only firmware entity (41); F4-16 the app's
  rules for blinds, room thermostats, detectors and battery devices, merged onto 38 with thermostats taking only the
  temperature mode and detectors only the load's own mode (42). All unverified on air; the sweep has the checks.
- **Wave 11 (brief 43): done.** The task-based user guide `docs/user/`, the developer docs `docs/dev/`, a German quick
  start, the generated entity reference with its drift test, and a link checker over every doc; `ha-integration.md`
  stays the reference, its headings pinned. Two flaky tests found while merging made deterministic.
- **Wave 12 (briefs 44–46): done.** U4-5 repairs that fix (gateway sync, a free address, a new export, a device
  name) and a *Learn more* link on every repair (44); U4-7 *Mesh connection*, *Unreachable devices* and *Mesh
  overview*, U4-12 room central entities hidden for new registrations only (M9) (45); U4-3 five tested blueprints
  in `blueprints/` (46). Merging found the overview holding a link change back behind its rate limit (fixed: a link
  change is written at once). Follow-ups: a *device offline* blueprint over *Unreachable devices*; `start_dim` is
  still called untried in the reference, the guide and the action strings while sweep section G says review 3 saw it
  work.
- **Wave 13 (briefs 47–49): done.** U4-2 rooms to areas: a *Rooms and areas* step matching areas by name and alias,
  keys and node devices placed, node devices named `<unit> - <product>`, `sync_areas` off by default (47); U4-6
  following the app (decision M12 taken): gateway entries fetch after the phone went quiet and every six hours, file
  entries get the `app_changed` repair (48); W I3/I6/I7/I9, U4-13 dry runs, optional responses, logbook lines,
  `confirm: true` for `remove_device` and a forced `delete_scene`, sectioned action forms (49). Merging renumbered the
  sweep's duplicate C8 / D11 items (C9, D12) and changed the node-name format from `<unit> (<product>)`, which nested
  parentheses for the metering socket. Open choice: a dry run does not read the gateway's newer export (documented).
- **Wave 14 (briefs 50–51): done.** U4-8, H4-9 the gateway found by zeroconf (`_junghome._tcp`, its advertisement
  seen on the installation; the setup from the card unverified on air), the unicast address under a collapsed
  *Advanced* section, an icon for every action, `gateway_busy` (50); U4-1, U4-16 German translation, device models and
  logbook lines in the server's language, the parity test over every translation (51, decision M4 taken). Brief 68
  added: the 24 further languages of the JUNG HOME gateway integration.
- **Wave 14b (brief 68): done.** U4-1 for every language the JUNG HOME gateway integration ships: 26 translation
  files, each complete; machine translations, said so in the CHANGELOG and the *Languages* section.
- **Wave 15 (briefs 52–57): done.** The large modules split without behaviour changes: A4-1 sequence-number
  persistence into `seq_store.py`, node information into `node_info.py` (52); A4-2, A4-6, A4-7 one dispatch helper,
  `errors.mesh_errors`, `conversions.py` (53); A4-4 `config_entities.py` into `properties/` (54); A4-5
  `MeshConfigurator` a facade over the `configurator/` package (55); A4-8 the actions into `actions/`, one module per
  domain (56); A4-9 `jhmesh` states its public API in `__all__`, `LocalState` in `jhmesh/state.py` (57). Follow-ups:
  `mesh_errors` at the remaining `send_failed` sites, stale comment references to the old private names.
- **Wave 16 (briefs 58–59) and the wave-15 follow-ups: done.** A4-3 the button gestures out of `JungHomeHub` into
  `hub_gestures.ButtonGestures`, the hub's first component (58); Q4 T2, T3, T5, T7, C4 a nightly workflow (thorough
  Hypothesis, mutation testing, the newest Home Assistant), `tools/trace_to_fixture.py` with a synthetic replayed
  trace and its privacy test, upgrade fixtures from 1.0.0 (59; traces of the installation's own devices wait for the
  maintainer's captures). Follow-ups: `mesh_errors` at seven more sites (`audit_network` keeps mapping only a lost
  link), comments name the moved code's new homes, `actions/` imports absolutely.
- **Wave 17 (brief 60): done.** A4-3, A4-13 `JungHomeHub` as the composition root of the `hub/` package: liveness,
  energy, clock, export watch, connect-time reads, repair issues, the link manager, gestures, and one registry that
  cancels the hub's timers and tasks in the old stop order, with a `Backoff` helper. The state cache, the status
  handlers, the commands, scenes and sequence accounting stay in `coordinator.py` (not in the brief's steps).
- **Wave 18, brief 61: done.** A4-14 `jhmesh.stats.LinkStats` per link and in total on `ProxyClient`, the hub's
  traffic counters folded into it, diagnostics `link_stats`, per-PDU lines on `jhmesh.trace`, one `link_state` debug
  line per change (decision M1). Follow-ups merged with it: a *device offline* blueprint, the dimming actions no
  longer called untried (review 3 saw them on air), the threshold errors one key per threshold in every language.
- **Decisions M14, M15:** custom repository first; the integration is called *JUNG HOME Bluetooth Mesh*.
- **Wave 18, brief 69: done.** H I-7 the Bluetooth matcher needs JUNG's manufacturer data next to the Mesh Proxy
  service (`not_jung` in the flow); H I-9 the entry's unique id is the mesh UUID (lower case, with dashes), entry
  version 1.3 migrates it at the first start and leaves an unreadable export's entry for the next one; discovery
  recognises a configured mesh by the Network ID or Node Identity of its keys and its nodes' MACs; a key refresh no
  longer moves the unique id (decision M10). The migration on the real entry and the absent card: sweep A11.
- **Wave 19 (brief 62): done.** A4-10 the plan model in `jhmesh.plan` (one class with the commissioning steps),
  `ProjectFile` methods for every meta row the configurator edited; A4-11 Protocols in `protocols.py` (the import
  graph has no cycle, `tools/import_graph.py --check` and an AST layer test keep it so), TypedDicts for the meta rows,
  a sequence-number record and the diagnostics, `data.py` for six of the `HassKey`s (the rest are read by test
  assertions or would bring the cycle back); A4-12 `const.py` keeps the Home Assistant-facing keys (236 → 147 names).
- **Wave 20 (brief 63): done.** A4-16 the one dead function (`_confirms_key_mode`) removed; A4-17 review ids out of
  the integration's and the tools' comments and docstrings, docstrings that restated the code trimmed; A4-18
  `describe` / `describe_config` and the tools' parsers built from tables, pinned by golden files. The `jhmesh`
  imports stay relative until the library is published on PyPI (`docs/roadmap.md`).
- **Wave 21 (brief 64): done.** Q4-14, Q4-18 personal environment details scrubbed from docs and tests (paths,
  host remarks, a zone, home-range test IPs, capture-looking identifiers); Q4-15 `.gitignore` covers `android/`,
  exports anywhere, `.claude/`, `*.tmp`; D2 `tools/privacy_scan.py` with its allowlist in CI's lint job and as a
  pre-commit hook (`--history` is run by hand: the public history's session trailers and author e-mail are the
  maintainer's call); D1 `.pre-commit-config.yaml`; D3 `noxfile.py` mirroring the CI jobs.
- **On-air sweep, groups A–D remote with the CLI only:** results in `docs/on-air-sweep.md`; no product failure; C1
  took outcome (b); C3 three new facts about locked loads; C6 the run-on time is never reported (a decision on
  *Switches off at*). The second pass (ledger rows, markers) waits for the maintainer's review.
- **Released:** 1.0.0 (waves 1–2) from the new public repository, a single commit; the earlier history is in the
  private `junghome-bt-mesh-private`. 1.1.0: waves 3–17.
- **HACS brand:** done — the existing JUNG HOME brand assets shipped in
  `custom_components/junghome_ble/brand/`, `brands` removed from the `ci.yml` ignore list, `quality_scale.yaml`,
  the roadmap and the README updated; `tests/test_brand.py` checks the images. `quality_scale.yaml` parses again
  (one unquoted comment held `: `, which no check caught).

Every other finding, every low item and brief 67 is TODO; the on-air sweep is the maintainer's.
