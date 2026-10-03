# Roadmap: from "control lights" to "configure JUNG HOME without the app"

Consolidated from the three gap analyses, which inventory *everything the JUNG HOME 2.2.0 app can do*
against the decompiled APK and mark what our code already covers:

- `gap-analysis/control-and-state.md` — runtime control & state per device kind (what tiles/detail pages do)
- `gap-analysis/device-settings.md` — every parameter screen per device type
- `gap-analysis/network-features.md` — rooms, connections, scenes, timers/schedules, central functions, gateway,
  project file / cloud sync, firmware update, provisioning
- `cross-repo-analysis.md` — audit against the gateway integration and the gateway firmware dump:
  settled protocol facts, divergences, and the **bug/robustness tracker that should be cleared before step 0**

Status legend used throughout: **done** = implemented in `jhmesh` + the HA integration and (where marked) verified on
air · **lib** = message builder/decoder exists, no HA surface · **todo** = nothing yet · **n/a / out of scope**.

## 1. Where we are

| Area | done | todo (highlights) |
|---|---|---|
| Control | lights on/off, brightness, tunable white (range read from the light); sockets; scene recall; socket power/voltage/current, energy (lifetime counter on the socket's meter element, found — `hidden-features.md` §2) and power-on hours; rocker events, device triggers, logbook; blinds (cover), thermostat (climate), detectors (motion/occupancy, lux) and battery level — the last four spec-only, unverified on hardware; the lock function (switch + time limit; for blinds also lock-out protection / wind alarm and a wind-alarm sensor, unverified on hardware); per-device availability (step 19, optionally with *Node heartbeats*); Health *Identify* / *Fault* / *Clear faults* per mains node; central functions (step 20) | |
| Device settings | vendor property **Get/Set** (CLI `prop get/set`) and the app's Parameters tabs as config entities (`number` / `select` / `switch` / `button` from the codec table, first-page defaults per device type, LEDs + night mode, status LED); power-up and switch-on setup states, edge evaluation, key mode (read-only), *Sensor values for IoT systems* (steps 6–9, 20); a battery node's settings read right after one of its keys reported; the `0x0001` lock flags (*Lock operation* on by default, its bit confirmed on air) | walking test |
| Network features | scene recall; rooms and key connections as HA actions (`set_room` … `clear_key`, DevKey config messages + project-file write-back); export fetched from the gateway; scene store/delete/actions (step 12), JH schedules and socket thresholds (step 13), gateway sync both ways (step 14) | the legacy SIG scheduler (deliberately not, step 20); room groups as light groups |
| Provisioning / removal / firmware update | — | out of scope (explicitly) |

## 2. The two blockers everything else hangs on — **both cleared** (`jhmesh.client.send_config` /
`request_config` + `jhmesh/config_messages.py`; `jhmesh/export.py` `ProjectFile`). Kept for the rationale:

1. **DevKey (Config Server) transport.** `jhmesh.send_access` only encrypts with AppKey 0. Rooms membership, every
   key connection, thresholds, the "sensor values for IoT" switch and the time keeper are `Config Model Publication
   Set / Subscription Add / Delete / App Bind` messages that must be encrypted with the node's **device key** (nonce
   type `0x02`, AKF 0, AID 0, unicast to the node). We already *decrypt* such traffic; sending is missing.
   `network-features.md` §13 lists the exact opcodes and layouts.
2. **Project-file writer.** The app imports the project file as a full replace and `DeleteUnusedScenes` removes any
   scene it does not know about. Whatever HA changes on the mesh must be written back into the Nordic CDB JSON +
   the `meta` block (`ExportDto`) and pushed to the gateway (`POST /api/junghome/config`), or the next app sync
   undoes it. `network-features.md` §8 documents the exact JSON shape and a per-operation "minimum write-back"
   matrix.

## 3. Prioritised plan

### Step 0 — library plumbing (unblocks everything)
1. ~~`messages.vendor_property_set(...)` + a per-property codec table~~ **done** — `jhmesh/properties.py`
   (`PROPERTIES`: 184 vendor ids, `SIG_PROPERTIES`: 11; `encode/decode/spec_for/for_product/describe_status`),
   `messages.vendor_property_set`. Open: manufacturer-Set access byte (app vs firmware framing), `0x0001` bit order,
   the Position 0↔255 quirk, hosting server of the firmware-only ids — all listed in `cross-repo-analysis.md` §8.
2. ~~Missing SIG builders/decoders~~ **done** — Level/Delta/Move, OnPowerUp, Battery, Location Global, SIG property
   Get/Set, Lightness Range/Default, CTL Range/Default, Time Set all in `messages.py`; LBC status decoding via
   `properties.describe_status` (not yet hooked into `describe()`).
3. ~~DevKey transport + Config builders/decoders (blocker 1)~~ **done** — `client.send_config/request_config`,
   `config_messages.py` (spec §8.3.6 vector byte-exact). Wired into the HA actions (`mesh_config.py` /
   `services.py`) and the CLI (`mesh_poc.py config …`).
4. ~~CDB writer + `ExportDto`/`meta` writer/merger with a guard (blocker 2)~~ **done** — `export.py` `ProjectFile`
   (both flavours, layout-preserving, atomic + `.bak`, `NewerExportError`, mutators per the §8 matrix returning the
   Config messages to send). Open: provisioner `seq` field, byte parity with Apple/Gson quirks (§8 questions).
5. ~~Generic **config entities** driven by the codec table~~ **done** (`config_entities.py` + `number`/`select`/
   `button` platforms and config switches; 56 property descriptions, first-page defaults per device type; status LED
   via User Property Status; LED colour selects + night mode). Not exposed: lock bits, struct-coded props (astro,
   thresholds, key config, edge detection), unsafe ids (`0x5001`, `0x5003`, `0x1014`, walking test).

### Step 1 — the user's installation (push-buttons, DALI dimmers, sockets, mini actuators)
6. ~~Load parameters: on/off delay, run-on, manual-off, blocking time, pre-warning, invert, DST flag
   (`device-settings.md` §2); DimMode; DALI DimToWarm (and use the read range instead of the fixed 2000–6000 K)~~
   **done** (config entities; `Light CTL Temperature Range Get` at every refresh). ~~OnPowerUp, Lightness
   Range/Default and CTL Default~~ **done** (*Behaviour after mains return* select per light and socket,
   *Minimum* / *Maximum brightness*, *Switch-on brightness*, *Use previous brightness* and *Switch-on colour
   temperature* on the dimmers, `config_entities.SETUP_STATES`; not yet tried on a real device).
7. ~~Push-button / socket LEDs (`0xA0xx` colour palettes per product) + Night mode~~ **done** (LED colour selects,
   night-mode switch). ~~Operation / factory-reset lock bits of `0x0001`~~ **done** (a switch per flag,
   `switch.JungHomeDeviceLockFlag`; bits 1 and 2 confirmed on air, *Lock operation* on by default).
8. ~~Metering socket: total energy (kWh, `total_increasing`) and power-on hours sensors~~ **done** (`_poll_energy`,
   every 5 min): the energy sensor had been removed when `0x006A` seemed absent on air — the probe had asked the
   socket's *main* element; the meter element (location 0x0040, the Sensor Server) listed the
   counters (`hidden-features.md` §2) and the sensors came back: *Energy* = SIG Manufacturer `0x0072` lifetime
   total (210,198 Wh), diagnostics *Energy since reset* = SIG Admin `0x006A` (the app's resettable total) and
   *Energy since switched on* = `0x000D` (`coordinator.COUNTER_READS`). ~~Reset button~~ **done**
   (*Reset consumption*, `0x006D` = 0 and `0x006A` = 0 in the app's order, `coordinator.reset_consumption`; not yet
   pressed on a real socket). ~~Lock function~~ **done** (a *Lock* switch + *Lock time limit* number
   per light, socket and blind, `02 01 <s>` / unlock with the read fields, `switch.JungHomeLockSwitch`; not yet
   tried on a real device). ~~Lock-out protection and wind alarm for blinds~~ **done** (*Lock function* select,
   *Wind alarm* binary sensor, the cover refusing commands while locked; unverified on hardware).
9. ~~Mini-actuator inputs: edge evaluation, behaviour selects~~ **done** (per input an *Edge evaluation*
   switch and *Rising edge* / *Falling edge* selects over the one `0x5009` byte, `config_entities.EdgeDetectionTarget`;
   not yet tried on a real device). ~~Key-mode read-only sensor~~ **done** (diagnostic *Key mode* per key, `0x5003`).
10. ~~`Time Set` broadcast on connect + daily.~~ *(and 11 below: rooms + key connections as services — done, `mesh_config.py`/`services.py`)* **done** (`coordinator._send_time`, after every refresh and every 24 h).
11. ~~Rooms (CDB + `meta` + Subscription Add/Delete) and **key connections** as HA services
    (`assign_key` / `clear_key`)~~ **done** (`set_room` / `create_room` / `rename_room` / `delete_room` /
    `assign_key` / `clear_key`, `docs/ha-integration.md` Actions); ~~the current assignment as an attribute of
    the event entity~~ **done** (`connection` / `connection_address` / `connection_name` from the
    export's publications, `devices.KeyConnection`).
12. ~~Scenes: Store / Delete / Register Get + vendor Scene Action Setup; services~~ **done**
    (`create_scene` / `rename_scene` / `store_scene` / `remove_from_scene` / `delete_scene`, `MeshConfigurator`
    scene operations with read-back and refusal handling, scene entities show every member's action; verified live
    on `0148`). ~~Blinds / thermostats as scene members~~ **done** (`store_scene` / `remove_from_scene`
    take covers and climates, the JUNG blinds-and-slats / target-temperature actions, `scene_action_for`;
    unverified on hardware). ~~Explicit target states in `store_scene`~~ **done** (`action` /
    `brightness_pct` / `color_temp_kelvin` / `temperature`, each load set and asked until it has settled,
    `JungHomeHub.async_wait_settled`; not blinds, which take too long to move).
13. ~~JH scheduler (16 slots, timed/sunrise/sunset; `Generic Location Global Set` from `zone.home`)~~ **done**
    (`get_schedules` / `create_schedule` / `enable_schedule` / `disable_schedule` / `delete_schedule`
    on lights, sockets, blinds and thermostats, and a diagnostic *Schedules* sensor per load (used slots, the slots
    as an attribute), `schedules.py`; an astro schedule first sends Home Assistant's home location to the node's
    Location Setup Server, as the app sends the phone's; not yet tried on a real device). Services and one sensor
    instead of a switch per slot: the slots are the node's, 16 per load element, and nothing about them is in the
    export. Open: whether a Set is answered and the open astro bound (31:00) are inferred from the app, not seen on
    air. ~~Thresholds for sockets~~ **done** (`set_threshold` / `delete_threshold` and a diagnostic
    *Switch-on* / *Switch-off threshold* sensor per metering socket, `thresholds.py`; the `0x5004` / `0x5005`
    Admin properties plus the wiring of `CreateThreshold` as captured on air — the socket's OnOff Client on the
    meter element subscribes and publishes to that element's group, the loads' `0x0527:1013` and OnOff servers
    subscribe to it, `MeshConfigurator.set_threshold_devices`; the app's unwiring on disable / delete,
    `MeshConfigurator.unwire_threshold`; not yet tried on a real socket).
14. ~~Gateway sync~~ **done**: every export the configurator rewrites is handed to the gateway the way
    the app does (`POST config {"data": {"project_file": …}}`, `MeshConfigurator._upload`), a failed upload raises
    the `gateway_sync_failed` repair (per entry since review 2; a rejected token raises
    `gateway_token_rejected` instead) and `junghome_ble.sync_gateway` retries; and the other way round, an entry
    set up from the gateway fetches the gateway's export by itself when a node of the mesh it does not know
    advertises, stores it and reloads (`coordinator._refresh_export_from_gateway` — the Gold `dynamic-devices`
    rule). Both need an entry set up from the gateway (host, token, pinned certificate); the fetch direction has
    existed since the gateway config flow. ~~Reading `0xC001–0xC003` over the mesh instead of the flow~~ **done**
    (an unreachable gateway, or one presenting another certificate, is looked up on the mesh —
    `JungHomeHub.async_follow_gateway` reads `0xC002` / `0xC003` and the entry follows a new address before one
    retry; since review 2 (S1/S2) a certificate reported there is never adopted but raises the
    `gateway_certificate_changed` repair, and a pin not vouched for by the gateway node is checked against `0xC003`
    before the gateway is first used; the token is never taken, it is the app's).
    Background: the gateway's REST API (firmware 2.1.3 / API 1.5.0, as used by `ernetas/junghome`) has
    `GET /api/junghome/project/junghome` (the JUNG HOME project), **`GET /api/junghome/project/cdb` (the mesh CDB —
    with NetKey/AppKey/DevKeys!)**, `PATCH /api/junghome/project` (fw 1.5.0+) and `POST /api/junghome/config`
    (api-server routes `/jung_home_project`, `/bt_mesh_project`, `/cdb`, `/functions/:id/datapoints/:id`,
    `/scenes/:id`, `/groups`, `/states/:state_id` in the firmware; `docs/cross-repo-analysis.md` §1.4); the gateway
    advertises itself over mDNS as `_junghome._tcp` with a `serial=` TXT record. The token must be treated like the
    keys themselves.

### Step 2 — completeness (devices the user does not own yet)
15. ~~**Cover** platform for 2-channel actuators as blinds/shutters/awnings (Generic Level/Delta on position + slat
    elements; setup and expert parameters `0x1101–0x110B`; reference run)~~ **done** (`cover.py`, Move Set
    open/close/stop, Level Set position/tilt, class from `0x1104`; parameters + reference-run button as config
    entities) — **unverified on hardware**.
16. ~~**Climate** platform for the room thermostat (set-point via Generic Level `pct = (°C − 5)/25·100`, current
    temperature `0x004F` ×0.5 °C, presets/HVAC mode `0x120B`, boost `0x120D`, auto/manual `0x1246`, set-points
    `0x1203–0x1205`, parameters)~~ **done** (`climate.py`; boost / automatic operation as config switches) —
    **unverified on hardware**.
17. ~~Detectors: motion/occupancy `binary_sensor`, illuminance `sensor`, forced-off select, PIR parameters~~ **done**
    (`binary_sensor.py`, illuminance in `sensor.py`, detector page of the config entities) — **unverified on
    hardware**; open: walking-test switch (`0x6001`/`0x6003`, needs a dedicated entity with the 1 s poll).
18. ~~Battery wall transmitters / binary-input pucks: battery sensor~~ **done** (`Generic Battery Get` right after a
    key event, `sensor.py`) — **unverified on hardware**; the node's settings are read the same way, right after a
    key press, and it gets no Health entities (review 2, P4); a key action or parameter change keeps it awake with
    the app's 6 s Admin Get `0x5001` and reports a silent node as asleep (review 3, W4 / F24, `keep_awake.py`) —
    unverified on air.
19. ~~Per-device availability like the app (3 timeouts → unavailable; any message → available)~~ **done**
    (`coordinator._missed_answer` / `_heard_from`; only state and keep-alive Gets count, a node that
    missed one is asked again after 60 s, battery nodes are never asked; combines with the *Node heartbeats* option).
20. ~~Central-function group entities~~ **done** (*All lights* `0xFEF5`, *All sockets* `0xFEF8`, *All
    blinds* `0xFEF6` + slats `0xFEF7`, *All thermostats* `0xFEF9`, one unacknowledged message each,
    `entity.JungHomeCentralEntity`; lights and sockets not yet tried on the installation, blinds and thermostats
    **unverified on hardware** — no such device here). Per room too (*All lights / sockets / blinds / thermostats in
    <room>*, the app's area sheet: a dim level and a blind stop as one message to the room address, on / off,
    positions and set-points one unacknowledged message per member; `JungHomeHub.room_command` / `room_level`). ~~The sensor-server publication switch~~ **done**
    (*Sensor values for IoT systems* per node with a Sensor Server, `MeshConfigurator.set_sensor_publication`).
    Not done, deliberately: the legacy SIG scheduler — no `1206` server on any node here, current firmware has the
    JH Scheduler on every load, and its timer scenes / `meta.timer` rows could not be tested against anything.

### Candidates from the device side (`hidden-features.md` §7)

Reading the nodes' own property lists and the SIG / Config states the app never touches added, in value order: the
~~**socket energy sensor**~~ (done, step 8 above), ~~**heartbeat-based availability**~~ (done: the *Node heartbeats*
option), ~~a **default transition time** per load~~ (not worth it: the DALI insert ignores it, `hidden-features.md`
§7.3), ~~**Health** identify / fault diagnostics~~ (done: *Identify*, *Fault*, *Clear faults* per mains node), the
DALI inserts' **hotel / night / presentation dimming** properties, `key_toggle_enable`, ~~writing HA's **home
location** to every node for astro schedules~~ (broadcast after Time Set on every connection,
`coordinator._send_location`), and — separately — firmware updates over the Silabs OTA GATT service.

**Not worth doing** (review 4, report 6 §1, and the parity ledger's review of the same rows; each line is the decision
record a declined ledger row cites, with its evidence):

- *Light LC and Light HSL*: no JUNG product hosts a Light LC (`130F`/`1310`) or Light HSL (`1307`) server; the
  gateway's HSL client points at colour products that do not exist (`hidden-features.md` §1).
- *The legacy SIG Scheduler*: step 20 above.
- *Sensor Cadence, Settings, Series and Column*: the meter answers all four Gets with the property id only; its Sensor
  Setup Server holds no cadence, the ~65 s rhythm is firmware (`hidden-features.md` §3), and no Column or Series
  Status was ever seen on air.
- *Present input power / current `0x0052` / `0x0057`*: both read 0 on a loaded socket (`hidden-features.md` §2); the
  socket measures the load side, which HA reads (`0x0081`, `0x005C`, `0x005D`).
- *Generic Default Transition Time*: the DALI insert ignores it (§7.3 of `hidden-features.md`); a transition carried
  in each Set (review 4, brief 31) is the planned way to fade, not a per-node default.
- *Virtual addresses*: no app flow and no export uses a Label UUID; publications and subscriptions stay on group
  addresses.
- *Friend and Low Power Node*: every node reports both features unsupported (`hidden-features.md` §1).
- *SAR tuning*: the nodes implement the SAR Configuration Server (`hidden-features.md` §4), but the app and the
  gateway live with its defaults, and no segmented exchange here is known to suffer from them.
- *Health Period*: faults are read once per connection; nothing would consume a faster fault publication.
- *Output / Input OOB authentication and OOB public keys*: JUNG devices have neither display nor keypad and offer No
  OOB only; the app never uses them (Static OOB, where a device offers it, is review 4 brief 40).
- *Proxy filter address lists*: HA keeps the empty reject list (receive everything) for a link's whole life and
  commissions through that link, so it never adds addresses to the filter (`jhmesh/commission.py` `NOT_COVERED`).

## 3b. TODO from the Bluetooth recheck (`bluetooth-recheck.md`)

What the full sweep of every Bluetooth layer could not do, or left half-done — with what it would take.

**Needs someone at home (the app or a finger on a device):**
- [ ] Capture the app's sessions: provisioning (PB-GATT `1827`), the configure-device sequence, a settings change
  per Parameters-tab property, a "connect key to room", a scene store — `mesh_sniff.py capture` in the room, the
  app on the phone; the GATT side additionally needs the connection-following capture below.
- [x] Rocker (KeyMode 6, layout 5) `0x5012` codes 0–3: **captured** — top = `01` pushed_up, bottom = `00`
  pushed_down, held top = `03` then `04` released; HA's `side` attribute is right (`poc-gatt-proxy.md`).
- [ ] Toggle the gateway integration's *status LED* switch while capturing (`0x5013` via opcode `0x11`).
- [x] Does the *Identify* button blink the LED? **Yes** (bedroom-door push-button `0297`). The
  button moved to the device whose LED it is (a push-button's buttons device, the socket) — the user looked for it
  there first.
- [ ] The stray JUNG 1-gang push-button `30:FB:10:60:A3:82` advertising without a mesh near the living-room door:
  a spare / unpaired insert, or a neighbour's?

**Infrastructure to check (HA side):**
- [x] ESPHome proxy **`livingroom-msr2-c80da4` ("Living room Apollo") registered as a Bluetooth scanner but never
  scanned** (`scanning: False`, no detections since HA start). A power cycle changed nothing; the cause was the
  Apollo firmware's own *Bluetooth Proxy* switch in HA (`switch.living_room_living_room_apollo_bluetooth_proxy`),
  off — switched on, scanning within seconds (52 devices in the first half minute).

**Tooling not finished:**
- [x] `mesh_sniff.py capture --follow <MAC>` — implemented and unit-tested (ATT → proxy SAR → network / beacon /
  proxy-config / PB-GATT provisioning, `sniffer.md` "Following a connection").
  - [ ] **Catch a real connection with it**: three attempts saw no `CONNECT_IND` (the Mac and the ESPHome proxy
    HA connects through are out of the dongle's reach on the server). Put the dongle on a USB extension in the room
    of the phone / proxy, or bring a phone to the server, then reload HA (it reconnects) or use the app.
- [x] Open our LINKTYPE_NORDIC_BLE pcap in Wireshark and check its `btmesh` dissector decrypts it with the keys:
  **yes** (TShark 4.2.5, — 2036/2036 Network PDUs; the key table wants `0x` prefixes and an 8-digit IV
  index, `sniffer.md` "Wireshark").
- [x] A long unattended capture (`capture --dedupe 2`, systemd user unit on the HA host) to catch the IV Update /
  Key Refresh when they come: `tools/mesh-sniff.service` (`sniffer.md` "Unattended capture"), **running on
  `sniffhost`**.

**Firmware corners (`hidden-features.md` §10):**
- [x] Health Fault array `0x81` (every device) / `0x80` (13 of 27): *registered* historical faults, cleared by
  Health Fault Clear, not re-registered; every test id is accepted and reports nothing. Meaning still unknown —
  nothing in the app / gateway code touches them. `health FFFF` surveys all nodes in one message.
  - [x] **Watched** `0148` / `01A4` (cleared): `0x81` did not come back in 2.5 h; a self-reboot (`0133`) and a known
    **mains loss** (boiler socket `0172`, breaker off 10 s) registered nothing — neither fault is a
    power or restart record, their meaning stays unknown. (Sequence-number high bytes count boots, per element.)
  - [x] HA shows the register: a *Fault* problem binary sensor per node (`ha-integration.md`), read by
    unicast Get once per connection — all 29 nodes answer (the group survey had missed three); live: 26 with
    `0x81`, 12 of them also `0x80`, the gateway and the two cleared WC nodes clean. A *Clear faults* button per
    node clears and reads back (verified live on the bedroom-door push-button `0297`: `0x81` → empty).
- [ ] The DIS vendor channel `2f98a382` (write) / `a0dc3a44` (notify): purpose unknown; probing means writing to an
  unknown command interface — only on a device we can afford to lose.
- [x] LBC property `5014` = a per-device **timestamp** on the meter element (`Timestamp7`, 2024-10-03 14:46:46 /
  2024-10-12 22:40:15 on the two sockets); `1FFF` is an empty placeholder everywhere.
  - [x] Asked: the sockets were installed around those dates — `5014` is the **commissioning timestamp**, now the
    socket's *Installed* diagnostic sensor (`sensor.py`).
- [x] `0x000E server_state_publish_request`: **works on dimmers** — the DALI insert publishes CTL / Lightness /
  OnOff / Level Status at once on `01`; switch inserts and sockets ignore every value. (A one-message refresh HA
  could use for dimmers; not adopted.)
- [x] Scene Action Setup Status decoded and verified on nine nodes (`vendor_models.py`, `scene-actions`): the
  element's action per scene (switch on/off, lightness + colour temperature …) — the state the export lacks.
- [x] JH Scheduler statuses: every sub-command's empty-slot layout verified, decoders / builders written (`sched`);
  a filled slot has never been seen (no schedules here).
- [x] Remote Provisioning / SAR / Private Beacon / Large Composition Data on the gateway: unanswered with the final
  Mesh 1.1 opcodes because the gateway's host process never initialises those models (SD dump, `lbc-gw-bt-tunnel`).
- [x] Phantom room subscriptions on Scene Server / Setup: the nodes **accept** the Subscription Add (verified and
  reverted) — the export's entries are app-side bookkeeping the app never sent.
- [x] Heartbeat Subscription as a topology probe: `config hops <counter> <origin>` (1 hop WC ↔ WC, 2 hops WC ↔ far
  socket; everything restored). **Full matrix run** (`config hopmatrix`, 812 pairs, 10.5 min):
  83 % at 1 hop, 17 % at 2, nothing farther — `hop-matrix.md`.
- [ ] Devices this installation lacks (blinds, RTR, detectors, battery transmitters, 2-channel pucks): every
  `prop lists`, state and acknowledgement question above is open for them.

**Quality scale (`quality_scale.yaml`), the one Bronze rule still `todo` — `dependency-transparency` (below; `brands` is
done: the icon ships in `custom_components/junghome_ble/brand/`):**
- [x] `jhmesh` builds as sdist + wheel from `pyproject.toml` (PEP 639 licence metadata, setuptools pinned;
  `MANIFEST.in` keeps the HA tests out of the sdist): ci.yml `library` builds both, runs `twine check --strict` and
  tests the wheel installed in a clean Python 3.13 (every line and branch) on every push, and `release.yml` publishes
  those very files to PyPI by trusted publishing on every `vX.Y.Z` tag (job `pypi`).
- [ ] **User:** on pypi.org, *Your account → Publishing* (<https://pypi.org/manage/account/publishing/>) → add a
  pending publisher: project `jhmesh`, owner `ernetas`, repository `junghome-bt-mesh`, workflow `release.yml`,
  environment `pypi`; then set the repository variable `PUBLISH_PYPI` to `true` (the `pypi` job is skipped without
  it) and tag the next version — the tag publishes, and the first upload makes the pending publisher the project's
  own. A pending publisher does not reserve the name (anyone may register `jhmesh` first, which voids it). 1.0.0 was
  released as the GitHub release alone (what HACS installs), before any of this.
- [ ] After the first publication: `manifest.json` `requirements: ["jhmesh==X.Y.Z"]`, absolute `jhmesh` imports in
  the integration and tests (`pip install -e .` for the venv), drop the bundled copy from the HA package. The rule
  also says "public repository", which holds since 1.0.0.

**Release notes:** `release.yml` publishes the version's CHANGELOG.md section as the release body (no generated
notes), so what users must know goes there — the move of the sequence-number store to one file per mesh
(`.storage/junghome_ble.seq.<mesh uuid>`) is in 1.0.0 *Upgrading*.

**Going public — the one checklist (everything that flips or is owed when the repository stops being private):**
- [ ] `dependency-transparency` (`quality_scale.yaml`): the rule's "public repository" clause is met by the act
  itself; the rest of that item (trusted publisher on pypi.org, first tagged publication, `requirements` entry) is the
  list above — reword the comment once those are done.
- [ ] HACS `hacsjson` exemption: nothing to do — `ci.yml` computes the ignore list from
  `github.event.repository.visibility`, so the validator gates by itself on the first public run (check the `validate`
  job then; `hacs.json` has to pass it).
- [x] Brands: the JUNG HOME brand images (as home-assistant/brands publishes them for `custom_integrations/junghome`)
  ship in `custom_components/junghome_ble/brand/`, which Home Assistant 2026.3+ and the HACS `brands` validator read;
  the brands repository no longer takes custom integrations. `quality_scale.yaml` `brands: done`; the HACS `brands`
  validator gates. With `dependency-transparency` done and a reauth step for the gateway token
  (`reauthentication-flow`, Silver) `manifest.json` can claim `platinum` again.
- [ ] HACS: submit the repository to the HACS default repository list (https://hacs.xyz/docs/publish/include —
  the `validate` job is that list's own check), or document the custom-repository install in `ha-integration.md`
  as the way in.
- [ ] `docs/network-topology.md`: decided — keep the document, key material redacted (no bits of any
  live key; the report now prints `[redacted]`), but it still carries the real node EUI-64s / MACs, the phone's
  provisioner UUID and the room names of the maintainer's installation. The test fixtures no longer share any of
  them (regenerated the same day with fake EUI-64s over the IANA documentation-block MACs and a fixed fake
  provisioner UUID — `README.md`'s Tests paragraph, `docs/ha-integration.md`'s developer notes; verified none of
  the 31 real identifiers in this document appear anywhere under `tests/`), so nothing here blocks on the
  fixtures any more: either regenerate this document from a faked dump before going public or accept that these
  identifiers are public (they are not secrets — nothing authenticates on a MAC — but they are personal).
- [ ] `README.md` layout line for `ios/` and the "never publish" notes stay: the backup itself is git-ignored and was
  never tracked (`git log --all -- ios android` is empty).

## 4. Verified on air vs. still to capture

Verified (`poc-gatt-proxy.md`): OnOff/CTL/Sensor statuses and their quirks, vendor property Get/Status framing,
InsertId and version encodings, key events (`0x5012`, codes 4/5/6 from single keys), segmented TX/RX. Settled by the
gateway firmware instead of a capture (`cross-repo-analysis.md` §1): the full `0x5012` event set incl. rocker codes
0–3, the status LED property `0x5013`, the doubling of every publication, the RTR's OnOff server (heating output),
the detector sensor properties `0x004D`/`0x0055`, product id `0x0D` = blinds actuator mini, the 2000–6000 K clamp
being the gateway's. Still needs a capture or a device we do not own (`cross-repo-analysis.md` §8,
`control-and-state.md` §5, `device-settings.md` §13):

The instrument for all of these is the nRF sniffer on the HA host with `tools/mesh_sniff.py`
(`sniffer.md`): passive, key-free capture of every PDU on air, decoded offline or live with the export's keys. Its
first captures settled the TTL question (**5**, the gateway's too), the socket-value delivery (**publications every
~65 s plus polls**), the 2000–6000 K CTL range (**the device answers it**, `016A`), and refined the doubling
(**statuses yes, sensor publications no**); it also confirmed the "no unicast reply to a state-changing Set" rule
from outside the network.

- ~~`0x5012` codes 0–3 on a KeyMode-6 **rocker** element: which half is top / bottom~~ — captured, `up` = top.
- Whether `Scene Recall` / `OnOff Set` from keys are doubled too (same TID, fresh SEQ) — decides the TID dedupe.
- A simultaneous ms-resolution capture (`mesh_sniff.py decode --json` + the gateway WebSocket) to pin the copy
  spacing and the gateway's synthetic release timer.
- A real RTR's composition (which element hosts the OnOff server; the numeric ids behind the gateway's
  `RTR_SCHEDULER_ENABLE` / `RTR_HVACMODE_DISPLAY` — firmware says `0x1246` / `0x120B`).
- The status-LED write on air (`0x5013` via opcode `0x11`) while toggling it from the gateway.
- **Every step-1 setting the app can change**: perform it once in the app while the sniffer runs and compare with
  what `mesh_config.py` / `config_messages.py` would send — the cheapest verification there is before HA writes a
  setting itself.
- Unchanged from before: unsolicited vendor Admin statuses, blind movement reporting, RTR set-point/temperature
  publications, battery level byte, energy units, the `0x0001` lock bit order (the gateway's value map favours the
  app's *encoder* order, `properties.md` §5.1), and how vendor Sets are acknowledged.

## 5. Corrections fed back into the android docs
- `properties.md`: SchedulerEnabled is `0x1246` (not 0x1236), DropOfTempState is `0x1212` (not 0x121A); InsertId wire
  order and version byte order (from the on-air tests); LED mode byte = night mode.
- `vendor-models.md`: opcode `0x10` confirmed as User Property Set Unack (button events).
- From the gateway firmware (`cross-repo-analysis.md`): `properties.md` §1.6 `0x5012`/`0x5013`, §1.8 LED
  triples start at `0xA000`, §1.10 firmware-only property ids, §4 product-id names (`0x0D` = blinds actuator mini);
  `network-logic.md` §6.1 gateway subscribes *and* polls; `control-and-state.md` RTR OnOff server, detector sensor
  ids, CT clamp; `poc-gatt-proxy.md` full key-event table, doubling policy, IV-update forecast.
