# Review 3 — findings and plan

Eight parallel read-only reviews of the whole repository, two of them dedicated to *maximum functionality over
Bluetooth Mesh* (device level and network level). Scopes: jhmesh transport · jhmesh messages/model · HA core +
security · config writing + services · entity platforms · tests / CI / tools / docs · device functionality ·
network functionality (app / gateway replacement). Items fixed by `docs/review-1/` and `docs/review-2/` were
excluded unless the fix is incomplete. Baseline: ruff clean, 2087 tests pass, 99.85 % line + branch coverage.

IDs: `T` transport, `M` model, `C` core, `S` security, `W` config writing, `P` platforms, `Q` quality,
`F` device functionality, `N` network functionality. Evidence classes for functionality: **a** verified on this
installation, **b** from the decompiled app / gateway firmware, **c** SIG-spec-possible, JUNG support unknown.

## Phase 1 — correctness bugs that bite the installation now

| ID | Where | Defect | Fix |
|---|---|---|---|
| T1 | `jhmesh/client.py:1267`, `:1306` (rebinding at `:1282`, `:1315`) | Review-2 T1 fix incomplete: `partial(self._waiters.append, …)` binds the list *object* before `_send` waits for the lock; every `finally` rebinds `self._waiters` to a new list → a waiter queued behind another request lands on an orphaned list. Reproduced: 3 concurrent requests, the third's reply arrives and is lost (full timeout + resend). Hits every connect refresh (`REFRESH_CHUNK = 5`), gives false keep-alive misses, and link loss does not fail those waiters. | Register via a closure evaluated under the lock (`lambda: self._waiters.append(...)`) or never rebind (remove in place). Test: 3 concurrent requests, one completing in between. |
| T2 = C2 | `client.py:784-793`, `:884-900`; `coordinator.py:675`, `:1633-1725` | Found by two reviewers. `HAState.reserve_seq` back-pressure raises `SequenceExhausted`; `attach()` treats it as a real exhaustion, marks the filter as set and never re-sends. A connect-time beacon moving the IV (or a slow first save) leaves the proxy on its default whitelist: unicast replies still arrive, **every group publication (rocker events, status publications, scene recalls) is lost** until the next link, and `_filter_status_overdue` raises a misleading "address in use" repair. | Tell back-pressure from exhaustion; on back-pressure queue a `_resend_filter` through `_while_seq_stalls`; treat Filter Status as the ack of Set Filter Type (retry 2–3×, only then `pdus_dropped`); pass `beacon_wait≈1 s` from `_connect_to` and `standalone.connect` (T7). |
| C1 | `coordinator.py:1259`, `:1477-1484`; `__init__.py:169-171` | Unknown-node export refresh is tried **once per MAC, ever**. The one attempt fails at setup (advert history replayed before the configurator exists; link not up for the `0xC003` check) or because the app has not uploaded yet — a node added in the app never appears until a reload. `dynamic-devices: done` does not hold. | Create the configurator before `hub.async_start()`; first attempt once linked; retry with back-off (1/5/15/60 min) and on every new link while `unknown_nodes` is non-empty; `GatewayBusy` retryable. |
| C3 | `coordinator.py:2263-2299` | A node that reached `UNREACHABLE_AFTER` is never asked again while the link lasts (re-check timer cancelled; HA skips unavailable entities so the user cannot poke it). A breaker off for five minutes → entities unavailable for days. | Slow re-probe of unreachable nodes (one Get every 5–10 min) while connected. |
| C4 | `coordinator.py:1578-1606` | Keep-alive targets are the first three OnOff elements in export order, including unreachable / dead / battery nodes → a healthy quiet link is dropped after ~11 min, the proxy goes on cooldown, every entity flaps. | Skip unreachable, dead and battery nodes; order by last heard; fall back to the proxy node's own element. |
| S1 | `coordinator.py:1018-1044`, `:619-634`; `client.py:243-256` | Sequence-store loss → **nonce reuse**: both copies unreadable → send from 0; a primary that parses as JSON but fails `_parse_record` starts at 0 and its first immediate save overwrites the good backup; a restored old HA backup regresses silently. | Fall back to the backup *record*, not only on JSON failure; when neither is usable and history exists raise `ConfigEntryError` + a fixable repair ("skip ahead 2^20" / "new address"); offer "skip ahead" from `pdus_dropped` too. |
| W1 | `mesh_config.py:914-967` (`_adopt`); `docs/ha-integration.md:760-762` | Gateway sync assumes the app pulls from the gateway — it never does (`network-logic.md` §6). An app edit re-uploads a project without HA's changes; `_adopt` sees "only the gateway changed" and overwrites the file → HA's model forgets key wiring that is still live on the mesh; a later `clear_key` leaves stale subscriptions. | Journal HA's applied `ModelChange`s / meta rows since the last sync (`.storage`); on adopt re-apply what is missing and re-upload, or raise a repair; optionally verify affected elements with Publication / Subscription Get (see N5 audit). Correct the doc. |
| W3 | `__init__.py:162`; `config_flow.py:369-372` | HA's unicast is checked free only at setup and never reserved in the export. A second app user gets provisioner range `0x0CCE..`, which contains the default `0D00` → the app may provision a node or pick its own address there → replay drops, misattributed replies. | Re-validate on every setup / adopt with a repair on clash; long term N1 (HA as a provisioner with its own range). |
| T3 | `client.py:1340-1355`; `coordinator.py:2963-2965` | Key-refresh detection is dead code: KR=1 appears only in Phase 2 beacons, which are secured with the *new* key, so "authenticated under our key and KR" never happens. Separately the ERROR log fires for any beacon with the cleartext KR bit, forged ones included. | Treat an unauthenticated KR=1 beacon on our own proxy link as a hint; move the ERROR inside the authenticated branch; properly: N2b (follow the app's key refresh). Fix the docs that claim the repair works. |
| P1 | `config_entities.py:123`, `:130`, `:1035-1042`; `switch.py:170-176` | Review-2 P4 half done: battery wall transmitters get a *Status LED* switch enabled by default; the unacknowledged write to a sleeping node is cached as applied → HA shows *on* forever. | Drop `PB_BATTERY` from `STATUS_LED_PRODUCTS`, or refuse unless a key event arrived within the wake window. |

## Phase 2 — more from the mesh with no hardware risk (class a, read-only or already verified)

| ID | Capability | Mechanism | Surface | Effort |
|---|---|---|---|---|
| F1 | Firmware / hardware revision, manufacture date, model id | SIG `0x001A` (already read and cached, `coordinator.py:2733`), `0x0010`, `0x000C`; PID | `DeviceInfo.sw_version` / `hw_version` / `model_id`; diagnostic date sensor | S |
| F2 | Relay switching / power-on cycle counters (wear) | LBC Admin `0x100F` / `0x1010` (read on the sockets: 118 / 79); give them an `Int(4)` codec | diagnostic `total_increasing` sensors | S |
| F9 | Link quality per node | advert RSSI per public MAC (best scanner); hops from heartbeats / rx TTL (fix the off-by-one docstring, T6) | diagnostic RSSI / hops / last-seen sensors, disabled by default | S |
| F8 | Reboot / mains-blip detection | per-element SEQ high byte jumps to the next `0x010000` block on boot (seen on two nodes) | "last restart" sensor + logbook | S–M |
| N2 | IV Update readiness | follow-beacon code exists but never ran on air; the gateway-initiated update is forecast within months | diagnostic sensors: IV index, update in progress, highest node seq, HA seq headroom; repair when a source passes `0xC00000`; replay test fed with recorded beacons | S |
| N5 | Network-health / audit surface | Publication / Subscription / App Get audit vs the export, Relay / NetTx / TTL / Beacon / Proxy Get, on-demand hop matrix (ports of CLI `config audit` / `hopmatrix`) | `audit_network` + `hop_matrix` actions, diagnostics dump | M |
| F7 | Energy history backfill after HA downtime | User `0x5010` (24 × u16 hourly) / `0x5011` (31 × u24 daily) | `recorder.async_import_statistics` | M |
| F11 | Colour temperature without resending lightness | Light CTL Temperature Set `0x8264` to the `1306` element (model present) | `light` internals (also fixes a stale-lightness jump) | S |
| F21 | One-message state refresh for dimmers / DALI | Admin `0x000E = 01` (verified) | coordinator refresh | S |
| N12 | Export / backup | admin-only download of `share_json` (warn: every key); on a restored seq store offer a one-time jump | action / repair | S |

## Phase 3 — functionality that needs a short, safe probe on the installation (class b/c)

Each item: probe with the CLI / sniffer first (reversible, someone at home where noted), then implement.

- **F3 Energy puck (0x10)** — gets no power / energy / reset / threshold entities (`devices._is_socket` needs
  `SOCKET_PIDS`). Generalise "metered load" to any load whose node has a meter element. (M)
- **F5 Blinds** — lock-out protection (`0x0009 02 FE t`), wind alarm (`01 FF …`) as select/button + safety
  `binary_sensor`; cover raises while locked; `STOP_TILT`; ventilation position `0x110A/0x110B`; reference run
  `0x110D` diagnostic. (S–M)
- **F4 Lock bits** — Admin `0x0001` bits 1–4 (lock operation / factory reset / RTR key / config lock),
  read-modify-write. (M)
- **F6 / F16 / P2 RTR** — boost `0x120D` as `PRESET_BOOST` with a read-back at ~5 min (the RTR ends it itself);
  `0x1246` as `HVACMode.AUTO`; set-point limits `0x1242/0x1243` → `min_temp`/`max_temp`; holiday `0x120E` →
  `PRESET_AWAY`; window-open `0x1225` binary sensor; floor temperature `0x1223`; cooling `0x1202/0x1206`;
  half-degree precision; name + area of the node device from app metadata (P7). (M, needs an RTR)
- **F10 Mini-actuator inputs** — in state mode (edge evaluation off) the input is a stateful door/window
  `binary_sensor`, not only press events; name inputs E1/E2 as the app does. (M)
- **F12 Hold-to-dim** — Generic Move / Delta on the element-0 Level server; `start_dim` / `stop_dim` / `step`
  actions; derive hold start/end and direction for SIG-wired rockers from the Delta/Move sequence. (S–M)
- **F19 Transitions** — transition + delay on Lightness Set and Scene Recall (`messages.py:302`, `:342`),
  `LightEntityFeature.TRANSITION` only after an on-air test on the DALI insert. (S)
- **F17 DST** — Time Zone Set with the next change (or Time Set at the switch); Time Role Get/Set for the app's
  time keeper (PP2 pucks). (S)
- **F13 / F14 / F22 Detectors** — walking test `0x6001/0x6003` (auto-off), per-zone PIR bits `0x6005`, fallback lux
  `0x6004`, motion hold from the run-on time `0x1007` instead of a fixed 120 s, constant light / night light
  `0x6018–0x6020`; `0x6016` must be a read-only sensor, not a writable select (P3). (M, needs a detector)
- **F15 / N7 Remaining connection types** — key → scene (KeyMode 2, Scene Client → `0xFFFF`, `0x5002`, write
  `meta.keyModeSceneConfigExports`), locking-function links (`0x5006–0x5008`, KeyMode 3), property-mode room
  functions, RTR links and RTR → heating actuators (`0x1014`), detector → device / room, target-element choice
  (slats, colour-temperature element), multi-room membership, icons. Capture the app doing each first. (M)
- **W4 / F24 Battery nodes** — config plans and property writes to sleeping transmitters need the app's keep-alive
  (Admin Get `0x5001` every 6 s) or a "press a key, then run" guard; send the sleepy node's steps first. (S–M)
- **F18 / F23 / F25** — DALI hotel / night / presentation dimming, push-button expert properties (`0x500A/0x500C/
  0xA000/0x0F00`), OnOff run-on remaining time and the "controlled by thermostat" flag. Meaning unknown: read on
  every node, flip one, sniff. (M)
- **F20 Gateway status** — `0xC000` bits (API available / awaiting approval) and `0xC002` IP as diagnostics. (S)
- **Library gaps behind the above (M section 2)** — Admin Property Get with extra bytes (astro register `0x0007`
  needs an index; `cli_ops prop get 0x0007` sends a malformed Get), Lightness Last / Linear, Default Transition
  Time builders, Health Period (push faults instead of polling), Node Identity, Model App Get, `0x004D`/`0x0055`
  in the library catalogue, astro codec accepting the `31:00` sentinel (M5).

## Phase 4 — owning the network (app / gateway replacement)

Order matters: N1 before anything that creates or removes nodes; N3 before N4, N6 and N9 (it is the only way back
from their failure modes). Nothing in provisioning, removal, key refresh or IV update has run on this
installation yet — everything below is class b/c. The installation runs the **iOS** app; every import/allocation
rule so far comes from the Android decompile, so each file-format change is tried on a spare app install first.

| ID | Capability | Design sketch | Effort | Risk |
|---|---|---|---|---|
| N1 | **HA provisioner identity + key vault** | CDB `provisioners[]` entry with HA's own unicast / group / scene ranges clear of the phone's; HA's address inside it (fixes W3); `jhmesh/vault.py` (device keys, pending changes) merged into every file HA uploads (fixes the device-key half of W1); `_provisioner_range()` picks HA's range | M | no mesh traffic; import validation by app / gateway |
| N2b | **Follow the app's key refresh passively** | HA holds every device key and the blacklist filter forwards phone→node traffic: decrypt the app's NetKey Update, keep old + new `NetKeyMaterial`, authenticate beacons with both, switch at Phase 2, drop the old key at Phase 3, persist | M | low; turns the app's only key change into a non-event (fixes T3) |
| N3 | **Provisioning over PB-GATT** | `jhmesh/provisioning.py` (Invite → Complete, No-OOB P-256), `jhmesh/commission.py` = the app's post-provisioning sequence as data (AppKey add, binds, proxy / TTL / relay / NetTx copied from an existing node, element groups, `FEF5–FEF9` subscriptions, defaults, time), CDB + meta rows, gateway upload; HA discovery flow on `0x1827` | L | medium: half-configured node recoverable by Node Reset; first target a spare / stray device |
| N4 | **Node removal** | app order: unwire pub/sub pointing at the node's element groups, clear thresholds, Config Node Reset, `networkExclusions`, drop meta rows / groups / scene addresses, upload; `remove_device` action + `async_remove_config_entry_device` | M | irreversible per node until re-provisioned |
| N8 | **Hot-standby proxy link** | one `ProxyClient` / `LocalState` over a bearer pool; send on the primary, filter on each, promote on loss; RPL already de-duplicates. Do the grace period, C5 and command-driven liveness first | M–L | low for the mesh; seq-store races are the bug class |
| N6 | **Key refresh driven by HA** | resumable state machine (`jhmesh/keyrefresh.py`) with pre-flight (all nodes reachable), stop-before-Phase-3 rehearsal | L | **high: can split the network** |
| N9 | **Firmware update (Silabs OTA over GATT)** | `jhmesh/ota.py`, HA `update` entity per node, user-supplied images; refuse schema changes unless N3 exists | L | medium: a failed update leaves an unprovisioned node |
| N10 | Relay / transmit tuning | planner from N5 data; one node at a time, revert on regression | M | medium: partition |
| N11 | Gateway takeover / decommission | guided flow: N1 → reset gateway (N4) → key refresh (N6) | M | high |
| N13 | IV Update initiation (fallback without a gateway) | `ProxyClient.send_beacon()` near `SEQ_TX_LIMIT`, only after a real network-initiated update was observed | S | medium |

## Phase 5 — robustness, performance, smaller defects

- **T4** `_send_lock` is held across a whole SAR exchange (~7 s per attempt, ~20 s with retries) and a fixed 0.25 s
  grace runs even when fully acked → a segmented Config send to an absent node blocks every light command. Hold
  the lock only to reserve and write; per-destination SAR lock; skip the grace when all bits are acked.
- **C5** proxy choice by one scanner's stale RSSI, blind to free connection slots, never re-evaluated → age filter,
  best RSSI among scanners with free slots, idle re-evaluation with hysteresis.
- **C6** an unexpected exception ends the `link` task for good → catch, log, back off, continue.
- **Link-loss UX:** ~20 s grace before marking entities unavailable; command-driven liveness (N unanswered acked
  Sets → keep-alive now, not after 660 s idle); clean detach on `EVENT_HOMEASSISTANT_STOP`; bound `async_stop`;
  repair when the seq store refuses for minutes; reachability diagnostics in the first connect-failure warning;
  TTL on faults / scene actions / versions re-read per link.
- **T5** authenticated beacons outside [current, current+42] are dropped silently → distinct repair.
  **T8** shield the frame loop of a multi-frame proxy PDU against cancellation. **T9** `describe()` evaluated on
  every TX even without DEBUG. **T10** standalone link has no silence watchdog.
- **W2** clearing / reassigning a key leaves its `keyModeSceneConfigExports` row (app shows "Scene N"). **W5** a
  stopped room-link plan records subscriptions but not the metadata row (pass `prepare=`). **W6** a scene member
  stored without a JUNG action is dropped when a sibling is removed. **W7** single-generation `.bak` → rotate, keep
  the pre-adopt copy. **W8** `store_scene` error text untranslated; no `position` / `tilt_position`. **W9** a
  failing multi-load `create_schedule` does not report the slots already created. **W10** missing bind step
  before the socket `0x0527:1013` publication. **W11** unknown-node reload does not take `ENTRY_LOCKS`. **W12**
  `_clear_plan` raises on virtual / fixed-group subscriptions. **W13** link timeout raised as
  `ServiceValidationError`. **W14** wrong error context `applied_scene_cleared`. Hardening:
  `free_group_address` ignores meta groups and live pub/sub addresses.
- **P4** a timed lock set elsewhere is never read back. **P5** read-modify-write races on shared wire values (LED
  colour + night mode, edge bytes, lightness range) → per-address lock in `PropertyReader`. **P6** scene `members`
  keyed by name collapse duplicates. **P7** RTR / detector node devices have no app name or area. Battery sensor as
  `RestoreSensor` with the Battery Indicator fallback; queue wake-window reads by priority; scenes on the mesh
  device; cover `operation_mode` as a translated enum.
- **M1** TID restarts at 0 every process → the CLI's second Set within 6 s is taken as a retransmission; seed
  randomly. **M2** SIG / LBC property-id fallback mislabels logs. **M3** classify loads by InsertId `0x0002`
  (already read) rather than composition only (blinds insert on a push-button becomes a light). **M4** RTR's
  OnOff element may become a phantom light. **M6** `RecursionError` on deeply nested exports. **M7** names not
  type-checked. **M8** node `cid` and `excluded` ignored. **M9** battery levels 0x65–0xFE shown as percent. **M10**
  `AccessMessage` repr can print keys the sniffer decrypted.
- **C7** `async_rediscover_address` on entry removal. **S2** `allow_redirects=False` on gateway requests.
  Narrow the discovery matcher (`manufacturer_id` 1319) after checking HA merges the records; negative cache for
  foreign Node-Identity adverts.

## Phase 6 — tests, CI, packaging, docs

- **Q1 (first)** the HA-level fakes have no replay protection: a mutant that reuses sequence numbers (nonce reuse)
  passes all 1127 HA tests. Per-source RPL in `FakeProxyLink`, teardown asserting nothing replayed or
  undecryptable, and every (src, iv, seq) unique.
- **Q2** services tests replace `request` / `request_config` wholesale; drive them through `fake_link.config_reply`
  and add a stale-duplicate-status test.
- **Simulated mesh harness** `tests/sim/`: N nodes with keys, RPL, network cache, relays over a `hop-matrix`
  topology, seeded loss / reorder / duplication, minimal servers (OnOff, Lightness / CTL, Config, vendor property,
  Scene), proxy filter semantics, second proxy, firmware quirks as flags, IV Update on a virtual clock; invariants
  at teardown. Hypothesis property tests for reassembly, proxy SAR at every MTU, codec round-trips, `ProjectFile`
  round-trip, and a state machine over `LocalState` / `HAState` ("(iv, seq) never repeats").
- **Q4** `setuptools>=77` (PEP 639 license). **Q5** build sdist + wheel and `twine check --strict` in CI, publish
  exactly that artifact, pin the backend. **Q10** library job: 100 % coverage of `jhmesh` from `tests/jhmesh`,
  mypy for 3.13; consider `bleak` as an extra. **Q11** wall-clock assertion. **Q12** release notes from the
  CHANGELOG section instead of `--generate-notes`. Fixture regeneration check; `DeprecationWarning` as error;
  `persist-credentials: false`; Renovate `minimumReleaseAge`; actionlint / zizmor.
- **Q9** CLI `config subscribe/unsubscribe/bind/unbind` and group `prop set` write live without confirmation →
  require `--yes`, print "export not updated — run `config audit`". Document reserved CLI / HA addresses.
- **Docs:** Q3 (manual install / `package_ha.sh` describe a layout that no longer exists), Q6 / Q7 (README
  maintenance and PyPI pending-publisher notes), `ha-integration.md` replay-protection and segmentation statements,
  the key-refresh repair claims (T3), `poc-gatt-proxy.md` IV-update "still open", `network-features.md` "cannot send
  DevKey" and SIG-scheduler "decoder" claims, `vendor-models.md` Scene Register Status opcode and §7 rows,
  `hidden-features.md` Lightness Default `0xFFFF`, `properties.md` open questions already settled and `0xC000`
  row, `firmware-products.md` PID 12 and `miniaktor-2k` descriptions, W15 apply-and-record statements.
- **Quality scale:** drop the `platinum` claim until brands and dependency-transparency are done (Q8);
  `reauthentication-flow` is not exempt (revocable gateway token → reauth step); `appropriate-polling` is done, not
  exempt; fixable repairs for `pdus_dropped` and `gateway_token_rejected`; per-entry `CONFIGURATORS` onto the hub.

## Phase 7 — before the repository goes public

1. **Personal data in docs** — real node MACs / EUI-64s, the mesh and provisioner UUIDs and room names (including
   a third person's first name) in `network-topology.md`, repeated in `ios-app-data.md`, `cross-repo-analysis.md`
   and `hidden-features.md`; a possibly-neighbour's device MAC in `bluetooth-recheck.md` / `roadmap.md`; the
   capture host's name in `sniffer.md` / `mesh-sniff.service`. Regenerate from a pseudonymised dump
   (`mesh_report --anonymise`) and squash before the first public push; the third party's name goes regardless.
2. **Register the PyPI name `jhmesh`** (pending publisher or first release) before the repository names it publicly.
3. **User landing page** — HACS renders `README.md`, which opens with the reverse-engineering log: put a short
   what / install (HACS custom repository) / link block first; `SECURITY.md` (the integration stores every key).

## Suggested order

1. Phase 1 as one change set, each fix with a test that fails today (T1 and T2 first — they lose replies and
   group traffic on every connect; S1 prevents nonce reuse). Q1 lands alongside so the fakes can catch the next
   sequence regression.
2. Phase 2 (read-only functionality) — immediate user value, no mesh risk; N2 before the IV update arrives.
3. Phase 3 item by item, each preceded by its probe; F3, F5, F6, F10 and F12 have the highest value.
4. Phase 4: N1 → N2b → N3 (on a spare device) → N4 → N8; N6 / N9 / N10 / N11 / N13 only after N3 is proven.
5. Phase 5 and Phase 6 interleaved with the above; the simulator harness before Phase 4's network-changing work.
6. Phase 7 before any public push.

## Status

Done and on `main`:

- **Phase 1:** all of it, with Q1 — T1, T2 (+ T7), C1, C3, C4, S1, W1, W3, T3, P1.
- **Phase 2:** F1, F2, F7, F8, F9, F11, N2, N5 (audit; no hop matrix: it needs Heartbeat Sets on every pair of
  nodes), N12. F21 left out (few Gets saved, weaker reachability counting).
- **Phase 3:** F17, blinds (F5, F4, F20), thermostat and detectors (F6, F16, P2, F13, F14, F22, P3), inputs,
  dimming and key → scene (F10, F12, F15 / N7), each built from the documented evidence and marked "unverified on
  air"; F3 (energy puck: any load with a meter element; Energy falls back to 0x006A only on a definite sign), and
  W4 / F24 (battery keep-alive). This installation has no puck, blind, RTR, detector or battery node, so F3, F5,
  F6 / F16 / P2, F13 / F14 / F22 and W4 cannot be checked here at all.
  **On air** (build a23450c on the HA host, checked remotely against the gateway integration as the
  reference): all 27 loads both integrations know agreed; a DALI light switched on at 30 % / 3000 K from here read
  the same through the gateway, and off from the gateway read off here; F12 `start_dim` / `stop_dim` / `step_dim`
  dimmed, stopped and stepped (−40 % exact) with the gateway agreeing to ±1; F20 *API available* and *IP address*
  matched the gateway's `GET /config` — *Client awaiting approval* read on with nothing pending (the firmware's
  `api_client_name_asking !== ""` on a configuration without that setting), now off by default; F17 Time Set carries
  local time with the right offset and the next change is found on the last Sunday of October, 01:00 UTC (the switch
  itself: on that day). Metering sockets: power equal; the Energy total runs ~160 Wh above the gateway's (a different counter; the
  same before this build). Still open on air: F10 inputs and F4 lock bits (need someone to operate an input / a
  key), F15 key → scene (rewires a real key), F19 transitions (not offered by the light entity yet).
- **Phase 4:** N2b (key refresh followed), N3 (provisioning library, commissioning plan, `add_device`), N4
  (`remove_device`), N1 (`jhmesh/vault.py`, `identity.py`: ranges clear of every provisioner and address in use,
  provisioner entry appended after the app's with a node at HA's address, the vault's nodes put back, rooms /
  scenes / new nodes allocated in HA's ranges — all behind the *provisioner identity* option, off by default, whose
  off path writes and uploads byte-identical files; the vault keeps device keys from provisioning on regardless.
  **Not yet imported by any app**: try it on a spare app install first). Not started: N6, N8 (little gain next to
  the link-loss grace, costs a proxy connection slot), N9, N10, N11, N13.
- **Phase 5:** T4, T5, T8, T9, T10, C5 (freshness only), C6, C7, S2, the link-loss UX items (but the repair when
  the seq store refuses for minutes: listed here as done, never built — review 4 D9 built it), W2, W5–W14 and the
  `free_group_address` hardening, P4–P7, M1–M4, M6–M10, the battery `RestoreSensor` and the cover mode enum.
- **Phase 6:** Q1, Q2, Q4, Q5, Q9, Q10, Q11, Q12, the doc corrections, the quality-scale correction (no tier claimed
  until Bronze holds), `persist-credentials`, Renovate `minimumReleaseAge`, `DeprecationWarning` as error. The
  simulated mesh harness (`tests/sim`) and the Hypothesis property tests (reassembly, proxy SAR at every MTU, codec
  and `ProjectFile` round-trips, state machines over `LocalState` / `HAState`); the state machines found four
  sequence-reuse paths (a backup that stops being written, a `.bak` left on the old IV index, the lost-record repair
  restarting under the mesh's index) and a lock kept after a failed start — all fixed. Q2: the services tests' Config and AppKey requests go through the real transport
  (`FakeProxyLink.config_reply` / `app_reply`); the stale-duplicate tests (Config: a Publication Set; AppKey: the
  scene-action sibling check) found the configurator's Scene Action Setup requests unmatched on the scene — fixed.
- **Phase 7:** the README landing section and `SECURITY.md`; `mesh_report --anonymise`; the in-tree scrub of
  personal data (item 1): node MACs / EUI-64 UUIDs (vendor OUI kept), the mesh, provisioner and older-mesh UUIDs,
  the Network ID, the ESPHome proxies' MAC suffixes, room / scene / device names (generic stand-ins, the third
  person's name gone), the capture host (`sniffhost`) — the same stand-in for the same value in every doc, and the
  fixtures' mesh UUID no longer echoes the real one's first 32 bits. `network-topology.md` was scrubbed in place (no
  dump at hand to regenerate it). **Git history still holds every original** (docs, fixtures, commit messages naming
  the host): squash or rewrite it before the first public push. The PyPI name remains.

Everything that changes the mesh without having run on this installation (the Phase 3 entities, adding and removing
devices, following a key refresh) is either off by default or says "unverified on air" where it is offered.

