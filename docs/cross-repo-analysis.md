# Cross-repo analysis & work tracker (audit)

Five parallel read-only audits of the three JUNG HOME artefacts on this machine, consolidated here as the tracking
document for the resulting work. Tick items as they land; keep the evidence citations so later sessions can re-verify.

| Label | What | Where |
|---|---|---|
| **B** | this repo — Bluetooth-direct HA integration `custom_components/junghome_ble` + `jhmesh` | `~/junghome-bt-mesh` |
| **G** | gateway-based HA integration `custom_components/junghome` (HACS, 1.5.0b6, REST + WS) | `~/w/p/junghome` (git) — its own tracker: `docs/cross-repo-analysis.md` there |
| **FW** | JUNG HOME Gateway microSD dump, firmware v2.1.3 build 2840, API 1.5.0 | `~/jung/sdc1..4`; `sdc2` is byte-identical to `~/w/p/junghome/disk_dump/jung/sdc2` and a later dump's `sdb2` (verified with `diff -rq`); only the data partition (`sdc4`/`sdb4`) differs (`bt_mesh_project.json`, `cdb_functions.json`, `jung_home_project.json`, `btmesh_sequence_number` 0x9FC000 → 0xA68000) |

FW path shorthands: `MW` = `sdc2/opt/middleware/dist`, `BT` = `sdc2/opt/bt_tunnel/lbc-gw-bt-tunnel_pi-zero`,
`RES6` = `sdc4/middleware/res_6`. Never copy key material from the dump or the iOS backup into any doc.

Legend: `[ ]` open · `[x]` done (wave 1 landed) · **bug** = wrong behaviour today · **rob** = robustness · **imp** = improvement · **doc** = documentation fix.

Health at audit time — **B**: 264 tests pass, mypy strict clean, 100 % line coverage, `dist/junghome_ble` identical to
`custom_components/junghome_ble`. **G**: 453 tests + 35 snapshots pass, 98.56 % branch coverage, ruff + mypy clean, git
clean on `main` @ `54266c8`. (Historical snapshot, not current status — B alone is at 1662 tests / 98.76 % coverage
as of the review-fix pass; re-run each repo's suite for today's numbers rather than trusting this line.)

Note: most line-number citations below are as of the audit. Large passes since then (notably one of 98
files) have shifted many of them; treat a citation as a starting point to re-locate the claim, not
as exact today.

---

## 1. Facts settled by the gateway firmware (reference, not tasks)

Both repos describe the **same installation**: B's iOS CDB (30 nodes, 23 OnOff loads, 4 CTL, 2 sockets, ~20
gateway-mode button elements) matches G's live WS counts (`OnOff 23, ColorLight 4, Socket 2`). Derived mapping that
neither repo stated: **gateway "function" = one mesh element; gateway group `id` = `"id"` + decimal group address
(`id49186` = `C022` "Balcony A"); scene `value` = mesh scene number.**

### 1.1 Gateway architecture
- Radio = **Silicon Labs EFR32 NCP** (Bluetooth Mesh SDK 4.4.6, encrypted `.gbl` images in `sdc2/opt/wireless_module/`);
  Pi onboard BT/WiFi disabled (`sdc1/config.txt`). Not BlueZ, not nRF.
- Transport: UART `/dev/ttyAMA0` ↔ `BT` (BGAPI host binary, symbols present) ↔ line-delimited JSON on
  `/tmp/lbc-bt-tunnel.soc` ↔ Node middleware `MW` (compiled TS, zero npm deps) ↔ api-server (TCP 127.0.0.1:1024) ↔
  nginx REST/WS. BGAPI ids in `MW/const/bt_api_ids.js`.
- The gateway is an **ordinary node** (`00DC`, pid `0x0B`, one element) provisioned by the phone (PB-ADV+PB-GATT
  beaconing 20 min, `MW/services/ncp_service.js:427-437`). **Not a provisioner, not a Config Client** — only local
  `test_*` BGAPI commands exist (`test_bind_local_model_app`, `test_add_local_model_sub`, `test_set_gatt_proxy`,
  `test_set_relay`, `test_add_local_key`, `test_set_iv_index`, `test_set_element_seqnum`; `ncp_service.js:299-351`).
  All configuration of other nodes is done by the app.
- Keys come from the project file the app POSTs (`set_project_file`); the app first reads the gateway's token / IP /
  TLS fingerprint **over the mesh** from gateway-hosted vendor props `0xC000..0xC003`
  (`MW/services/btmesh_property_service.js:38-45,151-170`; `sdc4/board_ctrl/ncp_ctrl/res/lbc_properties.json`).
- Vendor (LBC) property models are implemented **inside `BT`** on `sl_btmesh_vendor_model_*`; the middleware only sees
  `lbc_cmd` user messages 0–6 (`MW/handler/bt_event_handler.js:419-509`).
- State acquisition: self-config subscribes the gateway's client models to every element group the devices publish to
  (`MW/services/self_config_service.js:104-158,279-346`; gateway log: 221 desired / 202 current subscriptions) **and**
  polls with Get every 15 s (`MW/const/config.json btmesh.device_state_poll_interval_sec`).
- Sends: `generic_client_set` with **flags=1**, global 8-bit TID, transition 0, delay 0 (`MW/util/device_state_helper.js:236-272`);
  `scene_client_recall` to `0xFFFF` (`:366-403`); `sensor_client_get` (`:437-467`); vendor get/set via `lbc_cmd 5/6`;
  LED via `lbc_cmd 2`. (Silabs `generic_client_set` bit 0 = "response required" → acked Set; consistent with B's on-air
  observation that gateway Sets are acked yet answered only by the group publication. Unverified: TTL of those Sets.)
- Time: gateway hosts Time Server 0x1200 but `publish_time_interval_minutes = 0` → **the gateway never publishes time**.
- Matter: nothing implemented in this firmware (`sdc2/opt/matter-interface/` empty; only commissioning setup data).
- Relay on (retransmit count 0), GATT proxy on, no friend/LPN, no heartbeat, TTL never set by middleware (project
  default TTL 5). IV index persisted in `RES6/btmesh_iv_index` (0); seq persisted hourly rounded up to 0x4000
  (`MW/services/seq_number_service.js:31-36,117-119`); `node_request_ivupdate` when < 128 reboots-worth
  (128 × 0x4000 = 2 M seq) remain, warnings from 256.

### 1.2 Button events (`0x5012 KEY_EVT`) — the mechanism behind both repos' observations
`MW/services/btmesh_property_service.js:186-219` (verified by reading the code):

| event byte | gateway output | `buttonState` |
|---|---|---|
| 0 / 1 | `pushed_down` / `pushed_up` | `[1, 0]` — **release synthesised by the gateway** |
| 2 / 3 | `held_down` / `held_up` | `[1]` |
| 4 | `released`, side = `prevButtonType` | `[0]` |
| 5 | `pushed`, side = **toggle of `prevButtonType`** ("for downwards compatibility") | `[1, 0]` |
| 6 | `held`, side toggled | `[1]` |

- `_prevButtonType` is **one field on the service**, shared by every button in the network. Byte 0 (the counter) is
  ignored (`Number(values[1])`). No dedupe anywhere in middleware or `BT` (only scene status has a 1 s debounce).
- Device FW 2.2.0.2 publishes every access message **twice ~1 s apart with fresh SEQ, same counter** (B:
  `docs/poc-gatt-proxy.md:50-51,99`; CDB publish-retransmit is 0 everywhere) → each copy of a click re-triggers
  `[1,0]` → G's "one tap = two press/release pairs, 0.40–0.53 s pulse"; a hold's second copy is state-unchanged →
  suppressed → one pair. G's "not BT-mesh retransmission" and B's "application-layer double publish" are both right.
- On **key** elements (event 5, what B captured from `01B9`/`01BA`) the two copies land on *alternating* sides
  (`up` then `down`); on rocker halves (events 0/1) on the same side. G's "not alternating" capture and this code are
  both right for their element type.
- `0x5013 KEY_STATUS` (1 byte, manufacturer, read-only for users) = the status LED; the gateway writes it with a
  **User Property Status** (opcode 0x11) to the button element (`sendKeyStatus`). Not `0xA003`.

### 1.3 Vendor opcode table (from `BT` .rodata; on air `(0xC0|op) 27 05`)
| server | ListGet | ListStatus | Get | Set | SetUnack | Status |
|---|---|---|---|---|---|---|
| Admin `0x05271011` | C0 | C1 | C2 | C3 | C4 | C5 |
| Manufacturer `0x05271012` | C6 | C7 | C8 | C9 | CA | CB |
| User `0x05271013` | CC | CD | CE | CF | **D0** | D1 |

Client `0x05271015` subscribes to the six Status/ListStatus opcodes. Set/Status payload = `pid u16 LE` +
`[userAccess u8]` (Admin/Manufacturer only) + value. Matches `jhmesh/messages.py:31-37,129-137`.
`0x05271016`/`0x05271017` (JH Scheduler / Scene Action Setup, B's `docs/android/vendor-models.md`) are present on every
device element but unknown to the middleware.

### 1.4 Other FW tables worth cross-referencing
- Property catalogue `MW/models/btmesh_property_ids.js:17-217` (0x00xx general, 0x10xx light, 0x11xx blinds, 0x12xx RTR
  incl. `0x120B HVACMODE_DISPLAY` 0 ""/1 heat/2 cool/3 frost and `0x1246 SCHEDULER_ENABLE`, 0x13xx scene escape,
  0x50xx keys incl. `0x5003 KEY_MODE` 0..6 = light/blinds/scene/property/thermostat/switch/gateway, 0x60xx detectors,
  0xA0xx LEDs ×16 × {CH_SELECTION, ON_MODE, OFF_MODE}, 0xA100 BATTERY_CHANGED, 0xA200/1 wind alert, 0xC00x gateway).
  Temperatures 0x1203–0x1206 sint16 LE 0.01 °C, 0x8000 unknown. `0x1014 SWITCH_OPERATION_MODE` unsupported on device
  FW 2.0.0.4 ("Lbc Property doesn't exist").
- Product IDs `MW/models/btmesh_product_ids.js:6-25`: 1/2 PB 1/2-gang, 3 socket energy, 4 switch act mini, 5/6 PB
  battery, 7/8 motion 1 m/2 m, 9 presence, 0xA RTR, **0xB gateway**, 0xC socket, **0xD blinds act mini**, 0x10 switch
  energy, 0x11 2-gang switch, 0x12 dimmer, 0x13 blinds PP2, 0x14 DALI, 0x15/0x16 mini sensor mains/bat.
- Device type from element models `MW/util/project_file_helper_methods.js:57-113`; function types
  `MW/const/cdb_types_functions.json`; datapoint map `MW/const/cdb_types_datapoints.json` (CT range 2000–10000 there;
  the 2000–6000 clamp is in `ColorTemperatureState.js`, which uses the CTL-Temperature state when present else
  **Generic Level on element+1**, `:110-122`); blinds = Level Move `0x7FFF` down / `0x8000` up / 0 stop, transition
  `0xFFFE` (`PositionState.js:93-113`).
- Sensor props polled: 0x004F ×0.5 °C, 0x0055 (÷100 for FW > 1.4.0.0), 0x0057, 0x005C, 0x005D, 0x0052, 0x0081 ×0.1 W,
  0x004D (`MW/models/device_sensor_states/*.js:63-65`).
- Live DB `RES6/bt_mesh_project.json`: 31 nodes, 127 groups `C000–C07E` + `FEF5` (lights) / `FEF8` (sockets),
  11 scenes, IV 0, `networkExclusions` 0x0002–0x01D7 gaps; all publications TTL 255 (=default), retransmit 0/50 ms,
  period 0. Gateway element group `C005`; 8 elements in KEY_MODE 6 publish to it (nodes 0158, 01B9, 01BC, 01D8).
- REST routes in `sdc2/opt/api-server/dist` include `/bt_mesh_project`, `/jung_home_project`, `/cdb` (CDB with keys!),
  `/functions/:id/datapoints/:id`, `/scenes/:id`, `/groups`, `/states/:state_id`.
- Seq/IV forecast: gateway seq 0x9FC000 → 0xA68000 seven weeks later (two firmware dumps) → ~0xB0D6xx (B sniff) = 9–18 k
  msgs/day. IV-update request threshold (seq ≈ 0xE00000) is **~6–12 months away**, warnings from ≈ 0xBFFFFF in
  ~2–4 months. B's "a few years" (`docs/poc-gatt-proxy.md:118-120`) is optimistic.

---

## 2. Divergences between B and G (verdicts)

| # | Topic | B | G | Verdict / owner |
|---|---|---|---|---|
| D1 | Direct-control hardware | GATT-proxy client, plain BLE / ESPHome, working | needs EFR32 / nRF / BlueZ-mesh NCP (`docs/bt-mesh-direct.md:27-39`) | **B**; G doc superseded → G-doc |
| D2 | Sequence numbers | own address ⇒ own seq; only IV index must match (`poc-gatt-proxy.md:23-25`) | "continue the gateway's seq" (`bt-mesh-direct.md:16-19`, self-contradicted at `:24-25`) | **B** (spec + on-air) → G-doc |
| D3 | Gateway role | node `00DC` | "provisioner/proxy" (`gateway-architecture.md:116-117`) | **B**, FW confirms → G-doc |
| D4 | Button PDU | User Property **Set Unack** `D0 27 05` | "vendor property **status**" (`bt-mesh-direct.md:144-147`) | **B**, FW confirms → G-doc |
| D5 | Native gestures | click / hold-start / hold-end from the device | "no native click/hold", pulse = device granularity (`gateway-websocket.md:220,266-270`) | **B**; see §1.2 → G-doc |
| D6 | Alternating sides | — | "same channel, not alternating" (`CLAUDE.md:55-58`) | **both** per element type (§1.2) → G-doc + G code |
| D7 | CT mechanism | app: `Light CTL Set`; gateway seen polling CTL Temp Get + Level Get | Level on element+1 / fable: `LightCTLTemperatureServer` | **both**: conditional in `ColorTemperatureState.js:110-122` → G-doc |
| D8 | CT range | 2000–6000 "from observation" (`const.py:18-19`) | 2000–6000 = middleware clamp; app allows 2000–10000 | B copied a gateway clamp → **B code+doc** (read `0x8262`) |
| D9 | RTR heating demand | "no OnOff server" (`gap-analysis/control-and-state.md:211`) | OnOff 0x1000 server tracks heating PWM (`gateway-websocket.md:169-196`, issue #121) | **G** → B-doc |
| D10 | Vendor models 1016/1017 | JH Scheduler / Scene Action Setup | unknown | **B** → G-doc |
| D11 | Status LED property | guessed `0xA003` (`docs/android/properties.md:455`) | `status_led` datapoint, id unknown | **FW**: `0x5013` via User Property Status → B-doc + B code |
| D12 | Sensor delivery to gateway | "gateway evidently consumes group traffic" (`network-logic.md:706-712`) | "read via Sensor Client" | **both**: subscribes and polls every 15 s → B-doc |
| D13 | Detector quantities | unverified (open q.5) | `PRESENCE_DETECTED`, `PRESENT_ILLUMINANCE` reach the API | **G** → B-doc |
| D14 | Product id 0x0D | "Switch actuator 1-gang mini" (`entity.py:23`) | — | FW: blinds act mini; 0x10–0x16 pucks → **B code** |
| D15 | Scene-frame duplicates | every publication doubled | "just the scene triggered twice" (`gateway-websocket.md:88-92`) | likely the doubling; needs TID capture (§8) |
| D16 | Top/bottom on a KeyMode-6 rocker element | uncaptured (only keys captured) | both `up`/`down` exposed | FW: events 0–3 carry the side; confirm on air (§8) |
| D17 | Gateway bt_tunnel purpose | app has no gateway GATT path | `gateway-architecture.md:57` "app tunnel" vs `gateway-system-analysis.md:50` "UART↔NCP" | FW: UART↔NCP bridge → G-doc |
| D18 | `docs/bt-mesh-direct.md` prototype | — | still blasts 3 × 15 ms (v2.0.0) although the doc says v2.1.3 sends once | G-doc/tool |
| D19 | Manufacturer string | `"JUNG"` | `"Jung"` | align on `"JUNG"` (§7) |
| D20 | Event entity naming | `event.<x>_button_a` | `event.<x>_up` | align for 2-key rockers (§7) |

---

## 3. B — bugs

- [x] **bug** `jhmesh/client.py:122-128` — `apply_beacon` accepts "IV Update in Progress" on the *current* index after
  Normal Operation → `tx_iv_index` goes down, `seq` resets to 0 → SeqAuth reuse, every node drops our PDUs. Only allow
  `False→True` with `iv_index == self.iv_index + 1`; never reset `seq` when the TX IV index goes down.
  `tests/jhmesh/test_client.py:145-149` codifies the wrong behaviour — fix the test. (Time-bombed by §1.4 forecast.)
- [x] **bug** `coordinator.py:314-317` — vendor dedupe compares only the last counter; the on-air double-press pattern
  `16 05, 17 05, 16 05, 17 05` fires `click, double_click, click, double_click`. Keep a short per-element window of
  recent `(counter, t)` (≈2 s) or accept only `counter > last` mod 256. `tests/test_coordinator.py:285-300` only
  covers back-to-back duplicates. *(landed: 2 s per-element counter window, `BUTTON_REPEAT_WINDOW`)*
- [x] **bug** `coordinator.py:194-204` + `jhmesh/client.py:183-214` — a failure inside `attach()` (start_notify /
  filter write) after `establish_connection` leaks the BLE connection (one of 3 ESPHome slots); `handle_disconnected`
  ignores which client fired, so the leaked client's late callback tears down the healthy link. Wrap attach in
  try/except → `client.disconnect()`; ignore callbacks from `client is not self.client`. *(landed: `attach()` cleans up on failure; `handle_disconnected(client)` ignores stale clients; wired in coordinator + standalone)*
- [x] **bug** `config_flow.py:116-117` — after a NetKey refresh the Network ID (entry `unique_id`) changes, so the
  documented recovery ("reconfigure with the new export") aborts with `network_mismatch`
  (`tests/test_config_flow.py:252-261` asserts the abort). Use `mesh_uuid` as the identity check and pass the new
  `unique_id` to `async_update_reload_and_abort`; delete the `key_refresh` repair afterwards. *(landed: identity = `mesh_uuid` recorded in entry data, `unique_id` follows the new Network ID)*
- [x] **bug** `tools/mesh_poc.py:37` vs `const.py:12` — CLI and HA both default to source `0D00` with independent
  sequence counters (CLI state at seq 3132, HA starts at 512) → nodes silently drop HA's first ~2600 messages on the
  live test. Give the CLI `0D01` (or share the HA store) and document in `docs/ha-integration.md:372-379`. *(landed: CLI default `0D01`, per-address state files `tools/.jhmesh_state_<ADDR>.json`)*
- [x] **bug** `jhmesh/messages.py:27` — `0x801D` labelled "Model Publication Status"; it is Config Model Subscription
  Delete All (Publication Status = `0x8019`).
## 4. B — robustness

- [x] **rob** `jhmesh/client.py:183-192,216-221,344-347` — proxy filter is sent with the *stored* IV; if the IV moved
  while HA was off the proxy can't decrypt it and the filter stays the default empty whitelist → unicast-only reception
  until the next reconnect. Re-send `set_filter` whenever `apply_beacon` returns True (or wait ≤0.5 s for the beacon). *(landed: re-send on IV change; optional `attach(beacon_wait=)` not enabled)*
- [x] **rob** `coordinator.py:227-248,342-352` — no detection of "our PDUs are dropped" (lost seq store / address
  collision). Do the connect-time refresh with `ProxyClient.request()` matched on src; raise a repair issue when every
  Get times out while other traffic flows. Gives per-device availability (roadmap §19) for free. *(landed: refresh via `request()`, repair `pdus_dropped`; per-device availability still todo)*
- [x] **rob** `coordinator.py:187-192` — no link watchdog; a proxy that stops forwarding keeps everything "available".
  Reconnect when no PDU/beacon arrived for N minutes. *(landed: `LINK_IDLE_TIMEOUT` 300 s; review: 660 s + a keep-alive Get before dropping — a quiet gateway-less mesh must not flap)*
- [x] **rob** `__init__.py:31-33` / `jhmesh/client.py:71-83` — `HAState` (+512 restart margin, store write) is created
  before the visible-proxy check → every `ConfigEntryNotReady` retry burns 512 seq numbers. *(landed: proxy check before hub creation — Network-ID adverts only for the not-ready check)*
- [x] **rob** `coordinator.py:209` — `_refresh_all` task per connection is never cancelled; old tasks keep sending
  through the new link after quick reconnects.
- [x] **rob** `config_flow.py:63` / `jhmesh/cdb.py:62-80` — address validation ignores `provisioners[].allocatedUnicastRange`
  (0001–0CCC) and `networkExclusions` (554 addresses); a user-chosen address inside the phone's range passes. *(landed: `CDB.unicast_is_free()`)*
- [x] **rob** `jhmesh/client.py:402,412,415` — segment acks via untracked `loop.create_task`; keep references or accept
  a task-factory hook (`entry.async_create_background_task`).
- [x] **rob** `jhmesh/client.py:186,227` — MTU read once at `attach()`; BlueZ may still report 23 → 20-byte proxy frames
  all session. Re-read `mtu_size` lazily.
## 5. B — improvements

- [x] **imp** Time Set (roadmap #10) — higher priority than assumed: the gateway never publishes time (§1.1); only the
  phone keeps device clocks. `messages.time_set()` (0x5C, 10 bytes) to `0xFFFF` after `_refresh_all` + daily. *(landed: `messages.time_set()` + `coordinator._send_time` after every completed refresh and every `TIME_SET_INTERVAL`)*
- [x] **imp** `coordinator.py:290-296` — dedupe SIG client messages from keys (`OnOff Set`, `Scene Recall`,
  Level/Delta/Move) on `(src, opcode, tid)` within ~6 s; they are almost certainly double-published too (verify, §8). *(landed: `TID_REPEAT_WINDOW` 6 s keyed on src+opcode+payload so Delta transactions survive)*
- [x] **imp** `const.py:18-19`, `light.py:35-36` — per-light `Light CTL Temperature Range Get` (0x8262 → 0x8263) in
  `_refresh_all` instead of the fixed 2000–6000 K (D8). *(landed Phase 4: `0x8262` at every refresh, per-light min/max + clamp)*
- [x] **imp** `light.py:67-70` — brightness-only writes on a CTL light send `set_ctl` with a guessed 3000 K; use
  Lightness Set, CTL Set only when kelvin is given (G's `light.py:232-243` "don't guess" rule).
- [x] **imp** socket energy 0x006A / power-on hours 0x006D via SIG Admin Property Get 0x822D → Status 0x4A
  (`[pid u16][access][value LE]`); poll every few minutes; sensors `energy` (total_increasing) + `duration` (diagnostic). *(landed Phase 4: `_poll_energy` every 5 min, `energy` + `power_on_time` sensors)* *(on air: 0x006A not present on the socket — Admin / User Property Get answer a pid-only Status, Manufacturer Property Get nothing; sensor removed, power-on time kept)*
- [x] **imp** status LED switch — `0x5013` (D11): write via User Property Status like the gateway; entity as
  `EntityCategory.CONFIG` (G `switch.py:138-149`). *(landed Phase 4 in `config_entities.py`: written via User Property Status, assumed state)*
- [x] **imp** `cover.py` for `1002` Generic Level elements (roadmap step 15) — reuse G's settled HA semantics
  (`cover.py:66-102,244-250`: closedness convention, class from datapoints, stop → refresh); read `0x1104` for
  blind/shutter/awning instead of a user inversion option; `0x1108` = invert output. *(landed Phase 4b: `cover.py`,
  `Blind` device rule, Move Set open/close/stop, Level Set position/tilt, class from `0x1104`, stop → Level Get,
  blind parameters + reference run as config entities — spec-only, unverified on hardware)*
- [x] **imp** `climate.py` for RTR (roadmap step 16) — reuse G's `climate.py:19-28,66-93,143-155,206-223` (permanent
  HEAT, derived presets, PRESET_NONE no-op, 5–30 °C / 0.5); `hvac_action` from the RTR's own OnOff server (D9);
  presets via `0x120B` on FW ≥ 2.2.0.0, `0x1246` auto/manual. *(landed Phase 4b: `climate.py` on the node device,
  `Thermostat` device rule, presets from `0x1203–0x1205` + `0x120B`, boost / automatic operation as config switches —
  spec-only, unverified on hardware)*
- [x] **imp** detectors: OnOff Set publications from `1001`+`1100` elements → `binary_sensor` motion; `0x004D`/`0x0055`
  via Sensor Status (D13); `Generic Battery Get` 0x8223 after a 0x5012 event from PID 5/6/0x16 nodes; lock state
  `0x0009` `[cmd][prio][time u16][value]` as attribute. *(landed Phase 4b: `binary_sensor.py` motion / occupancy with
  the `DETECTOR_MOTION_HOLD` run-on, illuminance + battery sensors in `sensor.py`, `Detector` rule + `Button.battery`,
  second synthetic export `MeshNetwork-detectors.json` — spec-only, unverified on hardware; the lock-state attribute
  is not done)*
- [x] **imp** `coordinator.py:265,269,275` — statuses use the *target* field; the app uses *present*. Decide and document. *(decided Phase 4: present is the state, `target_*` kept internally)*
- [x] **imp** `coordinator.py:320-326` — option to delay `click` by `DOUBLE_CLICK_WINDOW` so `click` automations don't
  also fire on double presses. *(landed Phase 4b: options flow, `OPTION_CLICK_DELAY` — a `click` is held back for the
  window and dropped when the second click arrives; a hold ends the wait early)*
- [x] **imp** `coordinator.py:333-339` — delete the `key_refresh` repair on successful reload; add repairs for "seq store
  missing" / "export stale" / drop detection. *(landed: `key_refresh` deleted on start; `pdus_dropped` added; `export_stale` added — `ProxyClient.rx_undecryptable` + unauthenticated beacons, 20 on one link with nothing decodable)*
- [x] **imp** device triggers + bus event + logbook — adopt G's `device_trigger.py:53-58,107-169`, `event.py:126-149`
  (`junghome_ble_button_action`), `logbook.py:15-39` (`…_scene_recalled`). Update `quality_scale.yaml:21-23`. *(landed Phase 4: `device_trigger.py`, `logbook.py`, `junghome_ble_button_action` / `junghome_ble_scene_recalled`)*
- [x] **imp** diagnostics — `async_redact_data` on `entry.data` (host paths), BT MACs (`diagnostics.py:16,27,31`),
  per-device diagnostics (G `diagnostics.py:28,80-94,181-217`).
- [x] **imp** CDB from the gateway — `/bt_mesh_project` / `/jung_home_project` / `/cdb` (§1.4) as an alternative to the
  phone export; also a `FileSelector` upload into `.storage` instead of a typed path (`config_flow.py:25-33`). *(landed Phase 4: `gateway_api.py`, config-flow menu gateway/upload/path, one-click refetch)*
- [x] **imp** `manifest.json:4-9` bluetooth matcher matches *any* mesh proxy (0x1828); dedupe on Network ID is fine but
  consider filtering on the JUNG company id in adverts if present. *(investigated: the proxy advertisement forms carry no vendor marker and the JUNG `0xFF` structure is only documented for unprovisioned devices (transport-provisioning.md §2.3); matcher left as is, reason in ha-integration.md Known limitations; `mesh_poc.py scan --adv` shows what the nodes really advertise — narrow if a capture shows a marker)*
- [x] **imp** `entity.py:23` — pid 0x0D = blinds actuator mini; 0x10–0x16 = puck family (D14). `jhmesh/devices.py:133-135`
  — element-group name match only handles the iOS form; Android exports use decimal. `jhmesh/cdb.py:46` `iv_index` is
  never populated. `coordinator.py:206` `proxy_node` read before Filter Status arrives. *(landed: product names; `devices.py` element-group naming; `cdb.iv_index` = highest `networkExclusions[].ivIndex` bucket (documented lower bound); `proxy_node` set from `on_filter_status`)*
- [x] **imp** tooling parity with G — ruff (`select = ["ALL"]` style), SHA-pinned actions + `permissions: {}`
  (`ci.yml:11-13,29,31`), floor-version import job for `hacs.json` 2026.9.0, pinned
  `pytest-homeassistant-custom-component` (`ci.yml:18` unpinned → breaks on next phcc bump), branch coverage, tag-gated
  release workflow, renovate, syrupy snapshot tests, strings↔translations lockstep test. *(landed: phcc/HA pins, SHA-pinned actions + `permissions`, branch coverage, floor-import job; Phase 4b: ruff `select = ["ALL"]` in `pyproject.toml` with CI's `lint` job gating the rest, tag-gated `release.yml`, `renovate.json`, `tests/test_snapshots.py` (registry + state snapshots per platform and the device topology), `tests/test_translations.py` (strings ↔ en.json lockstep, icons, every key the code / services.yaml / config flow use))*
## 6. B — doc corrections

- [x] **doc** `docs/poc-gatt-proxy.md:87-104` — captured codes come from **key** elements (`01B9`/`01BA`, layout 0);
  add the full KEY_EVT table (§1.2) incl. 0–3 rocker codes; fix "bottom half of the rocker" (`:102-103`); state that
  the doubling is a general FW 2.2.0.x publication policy (also statuses `:50-51`), CDB retransmit = 0, and how the
  gateway turns each copy into a press/release pair. `:118-120` IV-update forecast → 6–12 months.
- [x] **doc** `docs/android/properties.md` — add `0x5013 KEY_STATUS` (status LED) and correct `:455` (`0xA003` guess);
  add the FW property names/ids from `btmesh_property_ids.js` that the APK table lacks.
- [x] **doc** `docs/gap-analysis/control-and-state.md:211-212,230,393-396` (RTR OnOff server = heating demand, D9);
  `:400-404` (detector quantities confirmed, D13); `:409-410` + `docs/ha-integration.md:59` (2000–6000 K is a gateway
  clamp, D8).
- [x] **doc** `docs/android/network-logic.md:706-712` — gateway subscribes *and* polls every 15 s (D12).
- [x] **doc** `docs/roadmap.md:66-68`, `docs/gap-analysis/network-features.md:426-427` — cite G's REST reference
  (`GET /project/junghome`, `/project/cdb`, `PATCH /project`, fw 1.5.0+; mDNS `_junghome._tcp` `serial=`); consider
  fetching the CDB from the gateway.
- [x] **doc** `docs/roadmap.md:84-90` "still to capture" — sync with §8.
- [x] **doc** `docs/ios-app-data.md` / `docs/network-topology.md` — add the derived cross-mapping (function ↔ element,
  group id ↔ decimal address, scene `value` ↔ number). *(topology picks up the new "Gateway id" column on the next `mesh_report.py` run)*
- [x] **doc** stale text: `README.md` "tests not set up yet" (264 tests exist); `docs/poc-gatt-proxy.md:7` path
  `tools/jhmesh/`; poc "what is still missing" (segmented TX and reconnect exist); `tools/mesh_report.py:50-60` vendor
  names + link to moved `docs/android/vendor-models.md`; `docs/android/vendor-models.md:484-489` doubling note.
- [x] **doc** `docs/ha-integration.md:372-379` — troubleshooting entry for the CLI/HA address collision (§3).
## 7. Parity / migration between G and B

Hard constraint: since HA 2026.8 a device belongs to exactly one config entry (`device_registry.py:982-985` in the
2026.9.2 venv) → **G and B devices can never merge**; the goal is a rename-free swap plus registry migration.

- [ ] manufacturer `"JUNG"` in both (B `entity.py:92,104,137,155,170,199`; G `entity.py:113`, `const.py:49`). *(B: every
  device says `manufacturer="JUNG"`, `entity.py`; G side to be checked in G)*
- [x] B buttons device keyed **per gang**, not per node (`entity.py:115-128`; on a 2-gang node the device name is
  whichever entity registered last). Key on `Metadata.entry_for()` location set (`jhmesh/devices.py:115-121`). *(landed: keyed on `Button.gang`, the location set of the app device entry (`Metadata.entry_for`, the hub keeps the metadata) — `buttons_device_id` = `{uuid}-{lowest key location}-buttons`, so two same-named gangs on one node stay separate)*
- [ ] B event translation keys `up`/`down` for 2-key rockers (G `const.py:37-41`, `strings.json:126-136`) so
  `event.<name>_up` matches; keep `button_{key}` for ≥3 keys. Needs the on-air side mapping (§8).
- [ ] optional shared identifier `("junghome_app", f"{nodeId}-{min(locationIds)}")` for tooling only.
- [x] G→B migration step in B's config/options flow: `er.async_update_entity_platform(entity_id, "junghome_ble",
  new_config_entry_id, new_unique_id, new_device_id)` (G entry must be unloaded; HA `entity_registry.py:2076-2110`);
  match by `slugify(name)` → G device `("junghome", slug)`, then by domain (+ `_power/_voltage/_current`, `up/down` ↔
  A/B); copy `area_id`, `name_by_user`, `labels` with `dr.async_update_device`. *(landed Phase 4b: `migration.py`
  (`ImportPlan`), the reconfigure menu's *Import the entities of the JUNG HOME Gateway integration* step, the
  `gateway_import` repair issue; customised entities are left alone; the G entry is disabled afterwards)*
- [ ] B `light`/`switch`/`scene` entity_ids already equal G's when the name is the same (both from the app's device name).
- [ ] longer term: one integration with two transports is the only way HA ≥ 2026.8 shows one device page. `jhmesh` is
  already a package; G can reuse `Metadata`/`CDB.parse` (`jhmesh/devices.py:88-125`, `jhmesh/cdb.py:49-80`).

## 8. Open questions — only a capture settles them

Instrument: the nRF Sniffer on the HA host + `tools/mesh_sniff.py` (`docs/sniffer.md`) — passive,
key-free capture of the whole advertising bearer, decoded with the export's keys. Items ticked below were settled
with it (details in `docs/sniffer.md` "What the first captures established").

- [ ] KEY_EVT codes 0–3 on a KeyMode-6 **rocker** element (node `0293`/`0297`, layout 5): press top vs bottom.
  *(needs someone at the rocker while `mesh_sniff.py capture` runs)*
- [ ] Are `Scene Recall` / `OnOff Set` from keys doubled too? (same TID, fresh SEQ ⇒ yes) — D15, §5 dedupe.
  *(same capture as above; the sniffer already showed that Sensor Status publications are **not** doubled while
  OnOff statuses are — the doubling is a status policy, `poc-gatt-proxy.md`)*
- [x] `Light CTL Temperature Range Get` 0x8262 on a DALI node (`016A`/`0210`/`0232`/`026E`) — D8: the gateway polls
  it and `016A→00DC Light CTL Temperature Range Status status=0 min=2000K max=6000K` was captured — the
  DALI node itself reports 2000–6000 K, so the gateway's clamp coincides with the device range here (B already reads
  `0x8262` per light at every refresh, Phase 4).
- [x] TTL / opcode of the gateway's `flags=1` Sets on air (§1.1): **TTL 5** (every node's default; the proxy capture's
  "TTL 2" was a thrice-relayed copy), `Generic OnOff Set` acked with `transition=0 delay=0`; unicast reply only when
  the state did not change. Poll cadence on air: one request every ~12 s round-robin over the elements.
- [ ] Simultaneous ms-resolution capture: `mesh_sniff.py decode --json` (sniffer µs timestamps, every copy) + G
  `tools/ws-capture/capture_ws.py` — pin the synthetic release timer and the copy spacing (statuses: 0.9–2.3 s).
- [ ] Real RTR composition (no RTR in this network) — OnOff server present? numeric ids behind G's
  `LBC_PROP_RTR_SCHEDULER_ENABLE_ID` / `LBC_PROP_RTR_HVACMODE_DISPLAY_ID` (FW says 0x1246 / 0x120B).
- [x] Whether the gateway's 15 s poll or the "sensor values for gateway" publications deliver socket/detector values:
  **both** — the sockets publish `Sensor Status` (power, current, voltage as three messages) to their element group
  every ~65 s and on change, and the gateway polls `Sensor Get 0x0081` / `0x0052` too.
- [ ] Sniff the status LED write while toggling G's `status_led` switch — confirm `0x5013` via opcode 0x11.
  *(`mesh_sniff.py decode --grep 5013` while toggling)*
- [ ] Manufacturer Property Set framing: access byte present (firmware reading, §1.3) or not (app `G7/d.java:25`)? Only 0x6004/0x6005 use it.
  *(do it once in the app while capturing)*
- [x] Socket energy counters — `0x006A` is **not** absent: it (and `0x0072` precise total, `0x000D` since turn-on)
  lives on the socket's *meter* element (location 0x0040), read (`docs/hidden-features.md` §2);
  the "absent from all three servers" verdict came from asking the main element. B: build the energy sensor.
- [ ] `0x0001` DeviceKeyLock bit order and the `Position` 0↔255 quirk (`properties.py`) — need a device.
- [ ] Provisioner `seq` in the export (§8.3 says write it; Nordic-Android has no field) — check `MeshNetworkRepositoryImpl.java:1220-1248`.
  *(the iOS 2.2.0 share export has no `seq` on the provisioner either)*
- [ ] Byte parity of `export.py` output with a real **Android** share export (`meta` address types, Gson escaping).
  [x] **iOS** share export (2.2.0): loaded and rendered unchanged → byte-identical apart from our trailing
  newline, once the writer kept the iOS top-level key order (`network` first; `export.Style.outer_keys`). Its
  `cachedGroupConnectionMetadata[].function` is the enum **name** (`"LIGHT"`), like Android's.

## 9. G items (tracked in `~/w/p/junghome/docs/cross-repo-analysis.md`)

Summary for cross-reference: reauth never reloads a `SETUP_ERROR` entry (`config_flow.py:501-518`, reproduced);
event entities available without WS; deleted scenes linger `unavailable`; WS-failure repair fires on every gateway
reboot; any correlated frame resolves a command as success; capability watcher reloads without debounce; pruner
guards unasserted in tests; button suppression must be per device (D6); `via_device` deprecated; hardware identity
available from `GET /project/junghome`; hub diagnostics leak the anchor; docs `bt-mesh-direct.md`,
`gateway-websocket.md`, `gateway-architecture.md`, `CLAUDE.md` corrections (D1–D7, D17, D18); security note on
`0xC001–0xC003` and `GET /project/cdb`.

## 9b. Phase 3 — landed on `main`

Dispatch registry (`coordinator.STATUS_HANDLERS` / `register_status_handler`) + element rule table
(`devices.ELEMENT_RULES`), `Metadata` kept on the hub (exact per-gang keying); `jhmesh/properties.py` codec catalogue +
`vendor_property_set`; DevKey transport + `config_messages.py`; SIG builders + `export.py` project-file writer.
Roadmap step 0.1–0.4 done; step 0.5 (config entities) and the wiring of DevKey/writer into HA are Phase 4.
Open encoding questions from the codec/export agents are appended to §8.

## 9c. Phase 4 — landed on `main`

Config entities from the codec table (roadmap 0.5 + step 1), rooms + key connections as services (step 3.11, first
users of DevKey transport + writer), energy/hours + CT range + present-vs-target, device triggers + logbook + bus
events, export from the gateway / upload in the config flow. Services registered in `async_setup`.

**Phase 4b** landed what was still open from §5 and §7: the `export_stale` repair, the click-delay
option, cover / climate / detectors + battery (spec-only, no hardware), the bluetooth-matcher investigation, the small
nits (product names, element-group naming, `iv_index`, `proxy_node`), tooling parity (ruff, release workflow, renovate,
snapshots, translations test) and the §7 migration tool. Only the §8 captures remain, plus the §7 items that need G
(manufacturer, `up`/`down` keys, shared identifier).

## 9d. Review — conformance / security audit, fixes in flight

Findings of the review of the Phase 4b tree, being fixed in the working tree (audit trail; the items are not repeated
in §3–§5):

- **P1** the gateway access token and the mesh keys could reach the log and the diagnostics download — redacted
  (token and keys in logs and diagnostics; the secret property `0xC001` reads `<redacted>` in `describe()`), and the
  per-message `RX` / `TX` lines of `jhmesh.client` moved to DEBUG.
- **P2** permissions of the stored export and leftovers of a removed entry; a cap on the segmented-message reassembly
  buffers; TLS certificate pinning for the gateway (`gateway_certificate` step, `certificate_changed` error,
  `gateway_certificate_changed` issue); the link watchdog; the segment-ack race in the proxy client; exception
  translations for the last three untranslated raise sites (`value_rejected`, `preset_temperature_unknown`,
  `led_colour_unknown`); the rocker `0x5012` codes 0–3.
- **Docs / quality scale** (this tracker, `docs/ha-integration.md`, `docs/roadmap.md`, `README.md`,
  `quality_scale.yaml`): stale statements brought in line with the code (light turn-on, climate `none`, device table
  and identifiers, energy read timing, illuminance precision, platform list, test count, roadmap ticks); `brands`,
  `dependency-transparency` and `dynamic-devices` honestly `todo` (the rules allow no exemption),
  `discovery-update-info` explained. *(brands: marked done later the same day, reverted to `todo` — see below)*
- **Brand icon**: the icon is served by Home Assistant's brands repository under
  `custom_integrations/junghome_ble` and fetched by domain from the brands CDN/API. We no longer bundle a `brand/`
  directory — redistributing the vendor's logo from this repo is a trademark/copyright exposure we do not need to
  take on, and HA already hosts the JUNG HOME brand. (Earlier this repo shipped the four PNGs locally because the
  brands repo had briefly stopped taking custom-integration PRs; that is no longer necessary.) The obsolete
  `tests/test_brand.py` was removed with the directory. **Correction:** the brands-repo entry does not
  exist — `domains.json` there lists only `junghome` (the gateway integration's domain), so
  `brands.home-assistant.io/_/junghome_ble/icon.png` is a 404 and HA shows a placeholder. `quality_scale.yaml`
  `brands` is back to `todo` until the `custom_integrations/junghome_ble` pull request to home-assistant/brands is
  merged (the going-public checklist in `roadmap.md`); ci.yml keeps the HACS `brands` validator ignored until then.
  **Superseded:** since Home Assistant 2026.3 the brands repository no longer takes custom integrations, which ship
  their own images instead; `brand/` is back with the JUNG HOME images the brands repository publishes for
  `custom_integrations/junghome`, `tests/test_brand.py` checks them, `brands` is `done` and the HACS `brands`
  validator gates.
- **Tests**: `tests/test_translations.py` also resolves constant translation keys, `services.yaml` ↔ `strings.json`,
  selector options ↔ the service enums, config-flow error / abort / step keys and the options data keys;
  `tests/test_snapshots.py` pins the enabled-by-default decisions in a second pass without the enable-all patch;
  `tests/test_binary_sensor.py` pins that an OnOff *off* schedules no hold timer.

## 10. Suggested order

1. §3 bugs 1–5 (IV update, dedupe window, connection leak, key-refresh reconfigure, CLI address) + §4 filter re-send.
2. Live HA test with ESPHome proxies (the real next milestone).
3. §5: drop detection + watchdog, Time Set, TID dedupe, 0x8262, status LED, triggers/logbook/diagnostics.
4. §6 doc corrections here; G tracker items in G.
5. §7 parity + migration tooling; §8 captures as opportunities arise.
