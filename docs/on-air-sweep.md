# On-air verification sweep

What review 4 brief 30 asks the maintainer to check on the installation, in one sitting: every behaviour the code,
`docs/ha-integration.md` (with the user guide `docs/user/` and the developer notes `docs/dev/` that grew out of
it), the unreleased `CHANGELOG.md` section and the parity ledger still call *unverified on air*
(or *not yet tried on a real device*) and that this installation can settle, with the steps, what to capture, what
counts as a pass, the hardware it needs and whether it is safe to undo. The rest is listed under
[Not checkable here](#f--not-checkable-here) with the reason.

The groups run from harmless to invasive. **Do them in order and stop wherever time runs out**: everything in A–C is
undone by the end of its own item; D rewires keys, rooms and scenes and undoes it; **E changes sequence numbers,
addresses and credentials and cannot be fully taken back** — each of its items says what stays changed.

Nothing here was run: the agent that wrote this list has no access to the installation. The results go to the
second pass of brief 30 (flip ledger rows, remove markers, open regression tests for failures).

## 0 · Before you start

### What the installation has

Lights (switch actuators, mini actuators with two inputs E1 / E2, dimmers, one tunable-white DALI light), metering
sockets, push-buttons (1-gang and 2-gang), the JUNG HOME Gateway. **No** energy puck, blind, room thermostat,
detector or battery node. Also needed: the nRF Sniffer dongle on the capture host (`docs/sniffer.md`), the JUNG HOME
app on a phone, Home Assistant with its Bluetooth adapter or ESPHome proxy, and for a few items a second person to
press keys.

### Placeholders

Nothing below names a real address, MAC or file of the installation; fill these in from Home Assistant on the day.

| Placeholder | What it is | Where to read it |
|---|---|---|
| `<ha>` | Home Assistant's unicast address (default `0D00`) | the integration's options / diagnostics |
| `<export.json>` | the export Home Assistant uses, copied to the workstation | never into the repository |
| `<capture host>`, `<nordic extcap>` | the machine with the sniffer, Nordic's `extcap` directory there | `docs/sniffer.md` |
| `<session>` | a name per capture, e.g. `sweep-b1` | you choose; results cite it |
| `<tw light>`, `<tw el0>`, `<tw el1>` | the DALI tunable-white light's entity, its CTL element and its temperature element | entity attribute `mesh_address`; el1 is the element after el0 |
| `<light>`, `<dimmer>` | another light / a dimmer entity, `<light el>` its element | attribute `mesh_address` |
| `<socket>`, `<socket el>`, `<meter el>` | a metering socket's switch entity, its load element, its meter element | attributes, *Power* sensor |
| `<key>`, `<key el>` | a push-button key's event entity and element | event entity attributes `mesh_address`, `connection` |
| `<input>`, `<input el>` | a mini actuator input's event entity (*Input E1*) and element | as `<key>` |
| `<mini el>` | a mini actuator's output (its primary element) | the output's light entity, attribute `mesh_address` |
| `<node>` | a node's primary address | device page |
| `<scene>` | a scene whose recall is harmless (it only switches lights) | scene entity attribute `scene_number` |

### Capture and decode

The dongle has one user: stop the unattended capture first and start it again at the end.

```
# on <capture host> (no key there)
systemctl --user stop mesh-sniff
python3 mesh_sniff.py capture --api <nordic extcap> --seconds 900 --ndjson <session>.ndjson
systemctl --user start mesh-sniff                  # at the end of the sitting

# on the workstation (the keys stay here)
scp <capture host>:<session>.ndjson .
.venv/bin/python tools/mesh_sniff.py decode --export <export.json> <session>.ndjson --json <session>.decoded.ndjson
.venv/bin/python tools/mesh_sniff.py decode --export <export.json> <session>.ndjson --src <ha>        # what HA sent
.venv/bin/python tools/mesh_sniff.py decode --export <export.json> <session>.ndjson --grep 'Scene (Get|Status)'
```

The live pipe of `docs/sniffer.md` works too. `--grep` is a regular expression over the decoded text; each line
shows `SRC→DST ttl= seq=`, and the `--json` records carry `src`, `dst`, `seq`, `opcode` and `params` (hex). A result
cites the capture by **session name and sequence number** (`sweep-b1`, `<ha>` seq `0012A4`), never by a date.

**Captures hold installation data.** Keep the NDJSON, pcap and decoded files out of the repository; a regression
test re-encodes the bytes with the fixture keys. The *Generic Location Global Set* every link sends carries Home
Assistant's home coordinates: never paste its parameters anywhere.

### Home Assistant side

- Debug logging for the integration (*Settings → Devices & services → JUNG HOME Bluetooth Mesh → Enable debug
  logging*, or `logger: logs: custom_components.junghome_ble: debug`); *Download diagnostics* at the end of each group.
- *Developer tools → Events*, listening to `junghome_ble_button_action` and `junghome_ble_scene_recalled`, for the
  key items.
- Enable, for the duration, the disabled-by-default entities an item names; disable them again afterwards.
- Actions run from *Developer tools → Actions* in YAML mode (the snippets below), as an administrator.

### Note what you will restore

Before group C and D, write down (privately, not in the repository):

- the DALI light's colour-temperature range: `.venv/bin/python tools/mesh_poc.py --cdb <export.json> ctlrange <tw el0>`;
- the device lock word of the push-button used in C2: `tools/mesh_poc.py --cdb <export.json> prop get <node> device_lock`;
- the edge evaluation of the input used in C4: `tools/mesh_poc.py --cdb <export.json> prop get <input el> input_edge_detection`;
- both thresholds of the socket used in D1 (its *Switch-on* / *Switch-off threshold* sensors with their attributes,
  or `prop get <socket el> turn_on_threshold` / `turn_off_threshold`);
- the connection of the key used in D2 (and of the key and the input used in D9, and of the key used in D10): its
  event entity's `connection`, `connection_address`, `connection_name`, its *Key mode* sensor, and
  `prop get <key el> key_mode`;
- which room each light used in D3 sits in;
- every value A7 reads (the hex `prop get` prints), before C6 changes any of them.

The CLI runs as its own mesh client: keep its default `--source` (`7FFF`) or another free address, never Home
Assistant's, and pass `--ha-storage <HA config>/.storage` where that directory is reachable so a slip is refused.

### What is still open, before and after

`tools/on_air.py` lists every marker with the symbol holding it; `--uncovered docs/on-air-sweep.md` lists the
markers outside the docs that this checklist does not cite (none when it was written). Run it again after the second
pass removed the markers of the checks that passed.

## Summary

| Item | What | Needs | Changes | Person |
|---|---|---|---|---|
| [A1](#a1--scene-registers-read-after-a-connection) | Scene Get after a connection (`msg:op:8241`) | any | nothing | — |
| [A2](#a2--connect-time-order-and-the-15-minute-freshness) | Connect-time order, 15-minute freshness, location broadcast | any | nothing | — |
| [A3](#a3--software-version-once-per-start-property-reads-queued-once) | Software version once per start, reads queued once | any | nothing | — |
| [A4](#a4--passive-checks-over-every-capture) | Own echoes, proxy configuration checks, bounded disconnect | any | nothing | — |
| [A5](#a5--inserts-and-key-layouts-from-the-adverts-f4-12) | Inserts and key layouts from the adverts | push-buttons | nothing | — |
| [A6](#a6--node-clocks-time-zones-and-stored-locations-f4-8) | Node clocks, zones, locations; the clock repair | any | nothing | — |
| [A7](#a7--firmware-only-properties-read-only-f4-3-f4-9-f4-10-f4-11) | Firmware-only properties, read only | push-buttons, a mini, a socket | nothing | — |
| [A8](#a8--keys-held-and-friend-in-the-audit-f4-15) | Audit: keys held and Friend (`msg:op:8001`, `8042`, `800f`) | any mains node | nothing | — |
| [A9](#a9--firmware-entities-f4-18-u4-11) | *Firmware* `update` entities | any node | nothing | — |
| [A10](#a10--gateway-discovery-u4-8) | The gateway's mDNS card, the gateway form prefilled | gateway | nothing | — |
| [A11](#a11--jung-only-discovery-and-the-mesh-uuid-as-unique-id-m10) | The entry's unique id migrated to the mesh UUID; no discovery card for the configured mesh | any | nothing | — |
| [A12](#a12--the-mesh-topology-picture-u4-14) | The *Mesh topology* image against the installation | any; *Node heartbeats* for the bands | nothing | — |
| [A13](#a13--reads-behind-the-refresh-a-link-attached-all-the-way-review-5) | Property reads behind each link's refresh; a link up once attached; heartbeats as link traffic; re-reads on a link that holds | any; *Node heartbeats* for the watchdog | nothing | — |
| [B1](#b1--ctl-temperature-set-airaccess8264) | CTL Temperature Set (`air:access:8264`) | DALI TW light | light colour | — |
| [B2](#b2--commands-confirmed-by-their-status-d32-and-hold-to-dim) | D32 status matching, hold-to-dim | dimmer, DALI, socket | load states | — |
| [B3](#b3--a-colour-temperature-changed-elsewhere) | CTL Temperature Status from elsewhere | DALI TW light, app | light colour | — |
| [B4](#b4--gateway-mode-key-events-and-every-hold-ends) | Key events with the entity disabled; hold reasons | gateway-mode key | nothing kept | **yes** |
| [B5](#b5--holds-of-a-rocker-wired-to-a-dimmer) | Holds of a rocker wired to a dimmer | such a rocker | dimmer level | **yes** |
| [B6](#b6--link-loss-grace-re-send-short-links) | Link-loss grace, re-send, short-link penalty | ≥ 2 proxy nodes | nothing kept | — |
| [B7](#b7--a-plan-to-an-unreachable-device-is-refused) | Plan refused for an unreachable device | a light on its own breaker | power of one light | — |
| [B8](#b8--transitions-probe-f4-1) | Transitions probe: which loads fade | DALI, dimmer, switch insert, a scene | load states | **yes** |
| [B9](#b9--homeassistantupdate_entity-reads-the-device) | *update entity* reads the device | a light, a push-button, the app | a setting, restored | — |
| [B10](#b10--locate_node-node-identity-f4-15) | `locate_node`: Node Identity on, then off (`msg:op:8047`) | any mains node, a BLE scanner | nothing kept | — |
| [B11](#b11--mesh-health-a-breaker-off-u4-7) | *Mesh connection*, *Unreachable devices*, *Mesh overview*, the offline blueprint | a light on its own breaker | power of one light | **yes** |
| [B12](#b12--blueprints-with-a-person-at-the-keys-u4-3) | Key blueprints on a real key | gateway-mode key, a dimmer | load states | **yes** |
| [B13](#b13--double-click-per-key-u4-19) | *Keys that wait for a double click*: one key waits, another does not | two gateway-mode keys | an option, set back | **yes** |
| [C1](#c1--tunable-white-range-and-the-setup-states-msgop826b) | Colour-temperature range (`msg:op:826b`), setup states | DALI TW light, dimmer | settings, restored | — |
| [C2](#c2--device-lock-lock-operation-f4) | Device lock *Lock operation* (F4) | push-button | setting, restored | **yes** |
| [C3](#c3--lock-function-of-a-load-0x0009-and-locked-loads-f4-2) | Lock function of a light (`0x0009`), locked loads (F4-2) | a light + its key, a dimmer, the app | locks, undone | **yes** |
| [C4](#c4--mini-actuator-inputs-f10) | Mini-actuator inputs (F10) | an input with a contact | setting, restored | **yes** |
| [C5](#c5--schedules) | Schedules, node clock and location | a light | a schedule slot, freed | — |
| [C6](#c6--firmware-only-properties-set-and-restored-brief-36) | Firmware-only properties, set and restored | DALI insert, socket meter, a key, a light | settings, restored | **yes** |
| [C7](#c7--repairs-that-fix-u4-5) | Repairs that fix: gateway sync, a device name, a fetch | gateway, a light, a firewall rule | a device name, restored | — |
| [C8](#c8--following-the-app-u4-6) | Following the app: a rename in the app reaches Home Assistant | gateway, the app, a light | a device name, restored | **yes** |
| [C9](#c9--rooms-to-areas-u4-2) | *Change which area each room's devices go to*: devices move, a hand-placed one stays | the integration's devices | areas, set back | — |
| [C10](#c10--hotel-function-entities-written-from-home-assistant-brief-73) | Hotel function, its brightness and the night-light brightness from Home Assistant | DALI TW light | settings, restored | — |
| [D1](#d1--socket-thresholds-netuccreatethreshold-togglethreshold-deletethreshold) | Thresholds create / disable / delete | socket + harmless load, a light | wiring, removed | — |
| [D2](#d2--key--scene-f15) | Key → scene (F15) | push-button key, harmless scene | key wiring, restored | **yes** |
| [D3](#d3--rooms-and-scenes-allocated-from-the-top) | Rooms and scenes allocated from the top | a light, the app | room / scene, removed | — |
| [D4](#d4--delete_unused_scenes-and-an-app-scene) | `delete_unused_scenes` and an app scene | the app | app scene, removed | — |
| [D5](#d5--sensor-values-for-iot-systems) | *Sensor values for IoT systems* | metering socket | publication, restored | — |
| [D6](#d6--both-sides-changed-merge-optional) | Both-changed merge (optional) | gateway, the app, a firewall rule | room names, restored | — |
| [D7](#d7--configuration-changes-without-a-reload) | Configuration changes without a reload | a light, a key | a room and a key, undone | — |
| [D8](#d8--several-rooms-per-light-leaving-a-room-f4-5) | Several rooms per light, leaving a room | a light, two rooms | rooms, set back | — |
| [D9](#d9--key-connections-colour-temperature-lock-function-property-users-brief-38) | Key → colour temperature, lock function, property users: the app captured first | spare key, DALI TW light, a light, socket, mini actuator, the app | key wiring and locks, restored | **yes** |
| [D10](#d10--a-key-that-only-talks-to-home-assistant-with-a-blueprint) | A key that only talks to Home Assistant, with a blueprint | a key, a light | a room and a key, undone | **yes** |
| [D11](#d11--areas-follow-a-room-change-u4-2) | *Move devices along when their JUNG room changes* after `set_room` | a light, two rooms | a room and areas, set back | — |
| [D12](#d12--dry-runs-and-the-answer-of-a-real-assign_key-w-i3-w-i6) | Dry runs; the answer of a real `assign_key` | a key, a light | a key connection, restored | — |
| [D13](#d13--pre-flight-reconcile-before-a-destructive-plan-w-i4) | Pre-flight reads stop a plan the nodes no longer match | a key, a light, the app | a key in the app, set back | — |
| [E1](#e1--gateway-re-authentication) | Gateway re-authentication | gateway, the app | the gateway token | — |
| [E2](#e2--backup-and-restore) | Backup and restore | HA backups | **2^20 sequence numbers** | — |
| [E3](#e3--a-new-unicast-address-starts-220-in) | New address starts 2^20 in | a free address | **2^20 numbers, an address used** | — |
| [E4](#e4--a-key-renewal-in-the-app-decision) | Key renewal in the app (decision) | the app | **the network key** | — |
| [E5](#e5--approve-a-gateway-api-client-f4-17) | `approve_gateway_client` | gateway, a second API client | **an approved client** | — |
| [E6](#e6--home-assistant-starts-an-iv-update-p-i-11) | `start_iv_update`: Home Assistant starts an IV Update (decision) | any proxy node | **the IV index, for good** | — |
| [E7](#e7--download-the-export-and-import-it-into-the-app-u4-17) | `download_export`: the signed link, and the file imported into the app | the app (a spare phone, or the app's project kept first) | **the app's project**, unless on a spare phone | **yes** |
| [F](#f--not-checkable-here) | Not checkable here | — | — | — |
| [G](#g--already-seen-on-air) | Seen on air before; markers to drop | — | — | — |

## A · Watch only

Nothing on any device changes. One capture can serve A1–A4: start it, restart Home Assistant, wait until *Link state*
says connected and the state refresh is through (a few minutes), then do the link drop of A2 within 15 minutes.

### A1 · Scene registers read after a connection

- **Checks:** `msg:op:8241` — after a connection every element holding a scene register (each address in the
  export's `scenes[].addresses`) answers Home Assistant's *Scene Get* with a 3-byte *Scene Status*; the scene
  entities' `active_members` follow. Also the per-member *Scenes* sensor (diagnostic, off by default).
- **Needs:** any scene with members. **Safety:** read-only.
- **Do:** optional first: recall `<scene>` from the app, so a current scene is known. Start the capture, restart Home
  Assistant, wait for the refresh. Enable the *Scenes* sensor of one member.
- **Capture:** `decode … --grep 'Scene (Get|Status)'`: a *Scene Get* from `<ha>` to each such element, each answered
  `Scene Status status=0 current=<n>` (parameters `00 <current u16 LE>`, opcode `5E`).
- **Pass:** every element of `scenes[].addresses` answered; `<scene>`'s entity lists, in `active_members`, the members
  whose current scene it is; the *Scenes* sensor lists the scenes of its member.
- **Markers:** `msg:op:8241`, `custom_components/junghome_ble/sensor.py::<module>` (the *Scenes* sensor part).

### A2 · Connect-time order and the 15-minute freshness

- **Checks:** review-4 R I-5 — Time Set and the home location go out first on every link, right after the proxy
  filter, then the state refresh, then the scene actions, fault registers and current scenes; those last three are
  not read again by a link that comes within 15 minutes of their last complete round after a link that lasted a
  minute. The location broadcast itself (`_send_location`).
- **Needs:** at least one proxy node; a way to end the link without restarting Home Assistant (a reload makes a new
  hub, which reads everything again): restart the ESPHome Bluetooth proxy Home Assistant uses, or with a local
  adapter `bluetoothctl disconnect <the proxy node's MAC>` on the HA host (the *Proxy node* sensor names the node).
- **Safety:** read-only; the entities ride through on the link-loss grace.
- **Do:** after A1's link has lasted over a minute, end the link once; let it come back.
- **Capture:** `--src <ha>`: on each link, *Time Set* and *Generic Location Global Set Unack* (both to `FFFF`) come
  before the first state Get; on the first link the *Scene Action* reads (JUNG vendor), *Health Fault Get*s and *Scene
  Get*s follow the state refresh; on the second link the Time Set, location, state Gets and energy poll appear again,
  the scene-action, fault and *Scene Get* rounds do not.
- **Pass:** both orders as above. Do not copy the location's parameters into any note.
- **Markers:** `custom_components/junghome_ble/hub/refresh.py::Refresh.after_connect`,
  `custom_components/junghome_ble/hub/refresh.py::Refresh.connect_step`,
  `custom_components/junghome_ble/hub/refresh.py::CONNECT_STEP_FRESH`,
  `custom_components/junghome_ble/hub/clock.py::Clock.send_location`.

### A3 · Software version once per start, property reads queued once

- **Checks:** review-4 R4-5 — each node's software version (*Generic Manufacturer Property Get* `0x001A`) is asked
  once per start, not on every link; a property read is queued once, not once per link.
- **Needs / safety / do:** A2's two links.
- **Capture:** `--src <ha> --grep '001A|software'`: the Gets on the first link only (the gateway polls `0x001A` too —
  filter on `<ha>`). On either link, no LBC property Get to the same element and property twice.
- **Pass:** no `0x001A` Get from `<ha>` on the second link (unless a node restarted in between), no duplicate reads.
- **Markers:** `custom_components/junghome_ble/properties/reader.py::PropertyReader.schedule_version`,
  `custom_components/junghome_ble/properties/reader.py::PropertyReader`.

### A4 · Passive checks over every capture

- **Checks:** that the sweep's own traffic trips none of the new guards: own PDUs relayed back are not taken for
  another client (review-4 S I2), proxy configuration PDUs pass the header and replay checks (P4-7), a disconnect
  never hangs (R4-11); and, as a regression check, that segmented messages to Home Assistant are still acknowledged
  (review-4 P I-8: the SAR Acknowledgment timer of Mesh Protocol 1.1 §3.5.3.4) — any segmented status a node sends
  Home Assistant (the connect-time Composition Data or a long property status) exercises it.
- **Pass:** at the end of the sitting the diagnostics show `address_shared` empty, `proxy_config_dropped` 0, no
  *Another client uses Home Assistant's JUNG HOME address* repair, and the log no *disconnect … timed out* warning.
  In the capture, each segmented message to `<ha>` is followed by Home Assistant's Segment Acknowledgment with every
  segment's bit set, and the node does not send the message again. Provoking the guards is not possible here (see F).
- **Markers:** `custom_components/junghome_ble/jhmesh/client.py::ProxyClient._disconnect` (passive only).

### A5 · Inserts and key layouts from the adverts (F4-12)

- **Checks:** review-4 F4-12 — each push-button's insert and key layout as the node advertises them, the InsertId /
  ButtonLayout Gets asked only of a push-button nothing told about, the device models and the keys' `position`
  attribute, and whether the *insert does not match the export* repair fires on this installation.
- **Needs:** the push-buttons. **Safety:** read-only (two Gets per push-button the export and adverts say nothing
  about, once).
- **Do:** `tools/mesh_poc.py scan` and compare each push-button's advertised function and layout with the export's
  InsertId (`docs/hidden-features.md` §8 records the 2-gang `028A` advertising function 6, an extension); in Home
  Assistant look at the push-button node devices' *model*, the buttons devices' *model* and an event entity's
  `position`; open *Settings → Repairs*.
- **Capture:** `--src <ha> --grep 'InsertId|ButtonLayout'` after a restart: Gets only to push-buttons without an
  export InsertId and without an advert, each once.
- **Pass:** the models name the inserts and layouts the app shows; positions match the keys; the repair appears only
  for a push-button whose advert really differs from the export (then the export or the device is out of date).
- **Markers:** `custom_components/junghome_ble/inserts.py::<module>`,
  `custom_components/junghome_ble/jhmesh/devices.py::key_position`,
  `custom_components/junghome_ble/strings.json::issues.insert_mismatch.description`,
  `custom_components/junghome_ble/translations/en.json::issues.insert_mismatch.description`,
  `net:uc:observecontrolswitchkeyassignment`, `prod:insert-type:generic-insert`, `prod:insert-type:no-insert`,
  `prod:insert-type:not-supported`, `prod:insert-type:unknown`, `prop:0x0002`.

### A6 · Node clocks, time zones and stored locations (F4-8)

- **Checks:** review-4 F4-8 — every node answers Home Assistant's *Time Set* broadcast with a *Time Status*, and after
  the daily Time Set each mains node answers *Time Get*, *Time Zone Get* and *Generic Location Global Get* (five nodes
  at a time, no battery node); the *Clock offset* sensors, the diagnostics' `clocks` and the absence of the *JUNG HOME
  devices with a wrong clock* repair follow; the repair's fix.
- **Needs:** any mains nodes. **Safety:** read-only (Gets; the fix only sends Time Set again).
- **Do:** first a probe outside Home Assistant: a *Time Get* (`8237`, `messages.time_get()`) to two or three nodes
  from a short script over `jhmesh` with the CLI's own address, as the probe of `docs/hidden-features.md` §9 did (the
  CLI has no Time Get command), and compare with the workstation's clock. Then enable the *Clock offset* sensor of a
  few nodes, restart Home Assistant and wait for the link: the answers to the connect-time Time Set fill them. Leave
  Home Assistant running over a day for the daily read (it follows the daily Time Set, a day after the start).
  Optional, to see the repair: the same script sends one node a *Time Set* ten minutes off
  (`messages.time_set(now + 10 min)`), the next daily read finds it; **Submit** the repair and see it clear.
- **Capture:** `--src <ha> --grep 'Time|Location'`: the *Time Set* to `FFFF` answered by a *Time Status* from each
  node (some lost to collisions is expected); after the daily Time Set, *Time Get*, *Time Zone Get* and *Generic
  Location Global Get* to each mains node, each answered (`5D`, `823D`, `40`). Do not copy the location's parameters.
- **Pass:** the sensors show a second or less; the diagnostics' `clocks` list every answering node with `has_time:
  true`, the zone Home Assistant sent and `location: home`; no repair; a node set off raises it naming that node, and
  **Submit** clears it.
- **Markers:** `custom_components/junghome_ble/node_clocks.py::<module>`,
  `custom_components/junghome_ble/hub/clock.py::Clock._send_time_and_read_clocks`,
  `custom_components/junghome_ble/repairs.py::<module>`,
  `custom_components/junghome_ble/sensor.py::JungHomeNodeDiagnostic`,
  `custom_components/junghome_ble/strings.json::issues.node_clock_wrong.fix_flow.step.confirm.description`,
  `air:access:5d`, `msg:op:5d`, `msg:op:8237`, `msg:op:823b`, `msg:op:40`, `msg:op:8225`.
### A7 · Firmware-only properties, read only (F4-3, F4-9, F4-10, F4-11)

- **Checks:** review-4 brief 36, its read-only half — what the firmware-only ids hold on this installation's devices
  before anything is written: transmission settings `0x0F00`, the runtime statistics `0x0F01` / `0x0F02` (do they
  count?), key toggle enable `0x500C`, the DALI insert's hotel / basic-light / night / presentation ids `0x1008`,
  `0x1009`, `0x1011`–`0x1013`; whether a load with a run-on time reports the time left in its OnOff Status (the
  *Switches off at* sensor); an LED's colour bytes. Home Assistant exposes none of these ids and writes none.
- **Needs:** a push-button with a switch insert (`<node>` = its primary, `<light el>` its load), the DALI insert
  (`<tw el0>`), `<dimmer el>`, `<key el>`, `<mini el>`, `<input el>`, `<socket el>` and `<meter el>`.
  **Safety:** read-only (Gets; the run-on step is an ordinary key press).
- **Do:** with `tools/mesh_poc.py --cdb <export.json> listen` or the sniffer running alongside; write every value
  down privately (C6 restores them):
  ```
  tools/mesh_poc.py --cdb <export.json> prop lists <tw el0>                  # which ids each server holds
  tools/mesh_poc.py --cdb <export.json> prop lists <key el>
  tools/mesh_poc.py --cdb <export.json> prop lists <meter el>
  # runtime statistics (Manufacturer server), twice, ten minutes apart: do they move?
  tools/mesh_poc.py --cdb <export.json> prop get <node> current_runtime_stats
  tools/mesh_poc.py --cdb <export.json> prop get <node> all_time_runtime_stats
  tools/mesh_poc.py --cdb <export.json> prop get <mini el> current_runtime_stats
  tools/mesh_poc.py --cdb <export.json> prop get <mini el> all_time_runtime_stats
  tools/mesh_poc.py --cdb <export.json> prop get <socket el> current_runtime_stats
  tools/mesh_poc.py --cdb <export.json> prop get <meter el> current_runtime_stats
  # transmission settings (seen as 0100) and key toggle enable (seen as 01)
  tools/mesh_poc.py --cdb <export.json> prop get <key el> transmission_settings
  tools/mesh_poc.py --cdb <export.json> prop get <input el> transmission_settings
  tools/mesh_poc.py --cdb <export.json> prop get <meter el> transmission_settings
  tools/mesh_poc.py --cdb <export.json> prop get <key el> key_toggle_enable
  tools/mesh_poc.py --cdb <export.json> prop get <input el> key_toggle_enable
  # hotel / basic light / night / presentation, on the DALI insert, a dimmer and a switch insert
  tools/mesh_poc.py --cdb <export.json> prop get <tw el0> hotel_dimm_value
  tools/mesh_poc.py --cdb <export.json> prop get <tw el0> basic_light_function_enable
  tools/mesh_poc.py --cdb <export.json> prop get <tw el0> night_dimm_value
  tools/mesh_poc.py --cdb <export.json> prop get <tw el0> presentation_mode_enable
  tools/mesh_poc.py --cdb <export.json> prop get <tw el0> presentation_mode_time
  tools/mesh_poc.py --cdb <export.json> prop get <dimmer el> hotel_dimm_value
  tools/mesh_poc.py --cdb <export.json> prop get <light el> hotel_dimm_value
  # run-on time and the remaining time: only on a light whose run-on time is already set (non-zero)
  tools/mesh_poc.py --cdb <export.json> prop get <light el> timed_on_duration
  tools/mesh_poc.py --cdb <export.json> get <light el>                      # right after its key switched it on
  # LED colour bytes [r][g][b][mode]
  tools/mesh_poc.py --cdb <export.json> prop get <node> led1_mode_on
  tools/mesh_poc.py --cdb <export.json> prop get <node> led1_mode_off
  ```
  In Home Assistant, enable the *Switches off at* sensor of the light used for the run-on step.
- **Capture:** the CLI output; `listen` for anything a Get triggers besides its Status (none expected).
- **Pass:** every Get answered or answered with the property id alone (the element does not have it, as the switch
  insert for `0x1008`); the runtime statistics either move between the two reads (a counter: note by how much) or
  not. For the run-on step: `get` prints `target=OFF remaining=…` while the run-on time runs (then *Switches off at*
  shows that moment), or the short form (then the sensor stays unknown for good and can go). Nothing passes or fails
  the code here: the readings feed C6 and `docs/hidden-features.md` §13.
- **Markers:** `custom_components/junghome_ble/coordinator.py::JungHomeHub._on_onoff_status`,
  `custom_components/junghome_ble/sensor.py::JungHomeSwitchOffAt`.

### A8 · Keys held and Friend in the audit (F4-15)

- **Checks:** review-4 brief 41 — `audit_network` asks each mains node *NetKey Get*, *AppKey Get* (NetKey 0) and
  *Friend Get* besides its other Gets, and every node answers: a NetKey List and an AppKey List of indexes, a Friend
  Status (2, not supported, expected on every node: `docs/hidden-features.md` §1).
- **Needs:** any mains node. **Safety:** read-only (Gets).
- **Do:** `tools/mesh_poc.py --cdb <export.json> config audit <node>` on a light and a socket node; then in Home
  Assistant `audit_network` with `device` set to the same light.
- **Capture:** `--src <ha> --grep 'NetKey|AppKey|Friend'`: one *NetKey Get*, one *AppKey Get netkey=0* and one
  *Friend Get* per node, each answered (`NetKey List netkeys=[0]`, `AppKey List Success: netkey=0 appkeys=[0]`,
  `Friend Status not supported`). The lists carry indexes only; nothing to keep private.
- **Pass:** the CLI prints `net_keys 0  app_keys 0`; the action's `keys` read `export` = `node` = `[0]` for both lists,
  `settings.friend` `{export: 2, node: 2}`, and no `keys_*` finding.
- **Markers:** `msg:op:8001`, `msg:op:8002`, `msg:op:8042`, `msg:op:8043`, `msg:op:800f`, `msg:op:8011`,
  `custom_components/junghome_ble/jhmesh/audit.py::<module>`.

### A9 · Firmware entities (F4-18, U4-11)

- **Checks:** review-4 brief 41 — each node's *Firmware* `update` entity shows the version the device page shows and
  the one the app bundles for the product, and offers no install.
- **Needs:** any node. **Safety:** read-only (nothing is sent: the version is the one already read).
- **Do:** enable the *Firmware* entity of a push-button, a socket and a mini actuator; open each.
- **Capture:** none.
- **Pass:** installed and latest version both shown (`2.2.0.2` on a push-button, `2.2.0.1` on a socket or mini
  actuator), *Up-to-date*, no *Install* button; the release summary names the JUNG HOME app.
- **Markers:** none in the code (the comparison is offline); the docs' *Firmware* section ends on it.

### A10 · Gateway discovery (U4-8)

- **Checks:** review-4 U4-8 (brief 50) — the gateway's mDNS announcement (`_junghome._tcp`, its shape already seen on
  air) starts the integration's zeroconf flow: no card when the entry names the gateway by the announced address or
  host name, else one *JUNG HOME Gateway &lt;address&gt;* card whose confirmation opens the gateway form with that
  address filled in; nothing goes to the gateway before that form is submitted. Review-5 U5-2 (brief 82): no card
  either for a gateway the entry knows by its recorded serial number or by the mesh it serves (the announced `mac` is a
  node of the export), and a gateway entry follows its gateway to a new IP address.
- **Needs:** the gateway entry, the gateway on the LAN. **Safety:** nothing is submitted (submitting would ask the
  gateway for another token); the card is left as it is.
- **Do:**
  1. Note the address the entry uses: *Reconfigure* names it in *Fetch it again from the gateway at …*; close the
     menu. Optionally compare with the gateway's announcement from a LAN host (`avahi-browse -rt _junghome._tcp`).
  2. Restart Home Assistant, wait a minute, open *Settings → Devices & services → Discovered*.
  3. If a *JUNG HOME Gateway* card is there: **Add**, read the confirmation (*JUNG HOME Gateway found*), **Submit**,
     check the *Gateway address* field and open *Advanced*; then close the dialog without submitting.
- **Capture:** none (mDNS, not Bluetooth); the debug log of `custom_components.junghome_ble.config_flow`.
- **Pass:** an entry naming the announced IPv4 address or the `junghome-….local` name: no card. An entry naming
  `junghome.local`: one card (not one per announcement), the form shows the announced address and `0D00` under
  *Advanced*, and the log has no *certificate pinned* line for it — unless the announced `mac` (step 1's
  `avahi-browse`) is the MAC of a node of the export (the gateway's node device shows its MAC as its serial number):
  then no card, and the entry's diagnostics show `gateway_serial` (redacted) in its data after the restart. Note
  which of the two it was: whether the gateway announces its mesh node's MAC is what this settles. Finishing a card
  would end with *already configured*; not needed. Optional, when the router can do it: give the gateway another IP
  address (a DHCP reservation), restart it, and check that no card appears and that *Reconfigure* names the new
  address (an entry that names the gateway by IP address).
- **Markers:** `custom_components/junghome_ble/config_flow.py::JungHomeConfigFlow.async_step_zeroconf_confirm`,
  `custom_components/junghome_ble/config_flow.py::JungHomeConfigFlow._configured_gateway`,
  `custom_components/junghome_ble/config_flow.py::_follow_gateway`,
  `custom_components/junghome_ble/strings.json::config.step.zeroconf_confirm.description`.

### A11 · JUNG-only discovery and the mesh UUID as unique id (M10)

- **Checks:** review-4 H I-7, H I-9 (brief 69, decision M10) — the first start after the upgrade migrates the entry
  (version 1.3): its unique id, the Network ID until 1.1.0, becomes the mesh UUID in lower case; Bluetooth discovery
  matches only proxies with JUNG manufacturer data (0x0527) and shows no card for the configured mesh, which it
  recognises by its keys and nodes, not by the unique id.
- **Needs:** the configured entry, the JUNG proxies in range. **Safety:** read-only (`core.config_entries` is only
  read, never edited; nothing is sent to the mesh).
- **Do:**
  1. Before the upgrade, note the entry's `unique_id`, `version` and `minor_version` in
     `<config>/.storage/core.config_entries` (read only).
  2. Upgrade, restart Home Assistant, wait until the entry is loaded and a few minutes for the proxies' adverts.
  3. Read `core.config_entries` again; open *Settings → Devices & services → Discovered*.
- **Capture:** none (Home Assistant's own adverts are enough); the log of `custom_components.junghome_ble` at INFO.
- **Pass:** the `unique_id` is the export's `meshUUID` in lower case with its dashes, `minor_version` is 3, and the
  log has no *keeps its unique id* line; no *Bluetooth Mesh* card is under *Discovered* (a card for another JUNG
  mesh, a neighbour's, would be right); the entry loaded as before.
- **Markers:** `custom_components/junghome_ble/__init__.py::async_migrate_entry`,
  `custom_components/junghome_ble/config_flow.py::async_migrate_unique_id`,
  `custom_components/junghome_ble/config_flow.py::JungHomeConfigFlow.async_step_bluetooth`.

### A12 · The *Mesh topology* picture (U4-14)

- **Checks:** review-4 brief 75 — the *Mesh topology* image draws the installation as Home Assistant hears it: the
  node of the link next to Home Assistant, the other nodes in the band of their heartbeat hops, the right state and
  features per node; it does not redraw for a heartbeat that changed nothing.
- **Needs:** any proxy node; for the hop bands the *Node heartbeats* option on (otherwise every node is under *Hops
  not known*, which is a pass too). **Safety:** read-only.
- **Do:** put the user guide's picture-entity card on a dashboard (or open the entity); wait a few minutes after the
  link is up; download the diagnostics. Look at it in the browser's light and dark mode.
- **Capture:** none; the diagnostics' `topology`, `link.proxy_node` and `heartbeats`.
- **Pass:** the node with the thick border is the *Proxy node* sensor's and `link.proxy_node`; every node sits in the
  band of its `heartbeats.nodes.<unicast>.hops` (the same as `topology`); the names and areas are the devices'; the
  relay / proxy letters are the export's features; the picture reads in both modes; the entity's state (when it last
  changed) stays put over ten minutes with nothing changing on the mesh.
- **Markers:** `custom_components/junghome_ble/image.py::JungHomeMeshTopology`,
  `custom_components/junghome_ble/mesh_topology.py::<module>`.

### A13 · Reads behind the refresh, a link attached all the way (review 5)

- **Checks:** review-5 brief 81 — on every link the config entities' property reads start only once the state
  refresh is through (R5-3), also on a link that comes while reads are still queued; a send waiting for a link goes
  out once the proxy filter is written, not while the link is still set up (R5-5); a Heartbeat counts as traffic for
  the link watchdog, so a mesh that only beats pays no keep-alive Get; a link that holds past three hours reads the
  config values again without a reconnect.
- **Needs:** any proxy node; for the watchdog part the *Node heartbeats* option on and a quiet stretch (night, no
  gateway polling); for the re-read a link that lasts over three hours. **Safety:** read-only.
- **Do:** as A2: end the link once while property reads are still going out after the first link (within a minute
  of the start), let it come back; leave the link up overnight.
- **Capture:** `--src <ha>`: on each link the state Gets (OnOff, Lightness, CTL, Level, the meters' Sensor Gets) and
  only after the last of them the LBC property Gets (`C2 / C8 / CE 27 05`); overnight, with heartbeats on, no
  keep-alive OnOff Get from `<ha>` while the nodes beat; about every three hours the property Gets again on the same
  link.
- **Pass:** no vendor property Get from `<ha>` before the link's last state Get answered or timed out, on either link;
  no Get from `<ha>` before its Set Filter Type on a link; the overnight capture as above. **Fail:** note the link's
  `refresh` seconds from the diagnostics' link history.
- **Markers:** `custom_components/junghome_ble/properties/reader.py::PropertyReader._wait_for_refresh`,
  `custom_components/junghome_ble/properties/reader.py::PropertyReader.reread_tick`,
  `custom_components/junghome_ble/hub/link.py::LinkManager.link_up`,
  `custom_components/junghome_ble/coordinator.py::JungHomeHub._on_control`.

## B · Momentary control

Loads switch or dim and are set back by hand; nothing persists on a device.

### B1 · CTL Temperature Set (`air:access:8264`)

- **Needs:** the DALI tunable-white light. **Safety:** the light changes colour; set it back.
- **Do:** with `<tw light>` on, change only its colour temperature in Home Assistant, e.g. 3000 K → 4000 K, then back.
- **Capture:** `--src <ha> --grep 'CTL Temperature Set'`: *Light CTL Temperature Set* to `<tw el1>` with 7 parameter
  bytes `[temperature u16 LE][delta UV 00 00][tid][transition 00][delay 00]` (4000 K = `a0 0f 00 00 <tid> 00 00`).
  Compare the last two bytes with the gateway's own `0x8264` in the earlier capture the ledger row names (or one
  the gateway sends in this session).
- **Pass:** that form; the light changes at once (no fade); `<tw el1>`'s *CTL Temperature Status* (`0x8266`) or
  `<tw el0>`'s *CTL Status* (`0x8260`) reports the new temperature, and the entity shows it.
- **Markers:** `air:access:8264`.

### B2 · Commands confirmed by their status (D32), and hold-to-dim

- **Checks:** review-4 D32 — a Status answers a light, socket or set-point command only when it shows the requested
  state (present, or target while transitioning, within the load's 1 % / 100 K step). On air the risk is the other
  way round: a load whose Status rounds differently would make every such command re-sent and fail.
  `ui:uc:communicatewithdevice`'s deliberate differences (hold-to-dim sent without waiting).
- **Needs:** a dimmer, the DALI light, a socket. **Safety:** the loads change; set them back.
- **Do:** dim `<dimmer>` to 37 %, 1 %, 99 %; set `<tw light>` to 2730 K and 3150 K at 45 %; switch `<socket>` off and
  on. Then `junghome_ble.start_dim` (`direction: up`, `speed: 20`) on `<dimmer>`, `stop_dim` after two seconds,
  `step_dim` with `step: -40`.
- **Capture:** `--src <ha>`: each command is one Set (same TID repeated only on a lost answer), answered within the
  first attempt; the dimming actions are a *Generic Move Set*, a Move Set 0 and a *Generic Delta Set*.
- **Pass:** every action returns at once without error, one Set per action, entities show the values; the debug log
  has no "sent again" for these commands. Hold-to-dim dims, stops and steps as review 3 saw it.
- **Markers:** `custom_components/junghome_ble/coordinator.py::JungHomeHub._load_command` (the D32 part),
  `ui:uc:communicatewithdevice`; for hold-to-dim see [G](#g--already-seen-on-air).

### B3 · A colour temperature changed elsewhere

- **Checks:** a colour temperature changed outside Home Assistant reaches the entity through the *CTL Temperature
  Status* only, which is taken defensively (temperature only).
- **Needs:** the DALI light, the app. **Safety:** the light changes colour.
- **Do:** with `<tw light>` on, change only its colour temperature in the app; then once more through the gateway's
  own control if you use it.
- **Capture:** `--grep 'CTL Temperature'`: the app's / gateway's Set and the light's status(es).
- **Pass:** the entity shows the new temperature within seconds; its brightness and on / off do not move.
- **Markers:** `custom_components/junghome_ble/coordinator.py::JungHomeHub._on_ctl_temperature_status`.

### B4 · Gateway-mode key events, and every hold ends

- **Checks:** review-4 D24 / H4-2 — the `junghome_ble_button_action` event and device triggers keep working with the
  key's event entity disabled; R4-7 / decision M11 — a hold ends with `reason` `timeout` (30 s), `stopped` (reload)
  or `link_lost`; a late release ends nothing twice.
- **Needs:** `<key>` linked to the gateway (attribute `connection: gateway`); a person at the key.
- **Safety:** nothing changes on the mesh; disable and re-enable an entity.
- **Do:**
  1. Make a throw-away automation: device trigger *click* of `<key>` → a persistent notification. Disable the event
     entity `<key>`. Click the key: the automation fires; the logbook names the device and key; the bus event has no
     `entity_id`. Re-enable the entity; delete the automation.
  2. Hold the key for 40 s: `hold_start`, then at 30 s `hold_end` with `reason: timeout`; the release at 40 s fires
     nothing more.
  3. Hold the key and reload the integration meanwhile: `hold_end` with `reason: stopped`.
  4. Hold the key and end the link as in A2: `hold_end` with `reason: link_lost`.
- **Capture:** the key's vendor gesture messages (`0x5012`) to the gateway's group, published twice each.
- **Pass:** as described, each `hold_end` exactly once.
- **Markers:** none in the code (the docs' *Every hold ends* paragraph); the CHANGELOG bullets of D24 and R4-7.

### B5 · Holds of a rocker wired to a dimmer

- **Checks:** review-3 F12 / R4-7 — `hold_start` / `hold_end` with `direction`, derived from the Level client's
  *Generic Move Set* (Move 0 ends it) or *Delta Set*s (ending 1.5 s after the last, `DIM_HOLD_QUIET`).
- **Needs:** a rocker wired directly to a dimmer (`connection: device` or `room` with a dimmer, key mode `light`);
  a person. **Safety:** the dimmer's level changes; set it back.
- **Do:** hold the upper half 3 s, release; hold the lower half 3 s, release; one short press.
- **Capture:** `--src <key el>`: which messages a held rocker really sends (Move or Delta, and how often).
- **Pass:** each hold gives one `hold_start` (`direction: up` / `down`) and one `hold_end` without `reason`, plus the
  `dim` events; the short press gives no hold. Record which form the rocker uses: the derivation was written from the
  SIG semantics only.
- **Markers:** `custom_components/junghome_ble/hub/gestures.py::ButtonGestures.dim_hold`.

### B6 · Link-loss grace, re-send, short links

- **Checks:** review-4 D14 / R4-3 — entities stay available 20 s whatever ends the link, and a command meanwhile waits
  for the next link; R I-11 — a load command whose link ended while it was out is sent once more on the next link;
  D13 / R4-1 — three links lost within a minute each pass the node over for two minutes in favour of another.
- **Needs:** two or more proxy nodes in range; the link-ending method of A2. **Safety:** nothing persists.
- **Do:**
  1. Grace: end the link, watch `<light>`: it stays available; switch it during the outage — the command waits and is
     applied on the next link.
  2. Re-send: run a toggle of `<light>` and end the link within the same second (a script that does both), a few
     times.
  3. Short links: end the link right after each of three consecutive connections.
- **Capture / log:** step 2 — the debug log line *The link changed during a command to …; sending it again on the
  next one*, and the same TID twice on air; step 3 — the diagnostics' link history (the last 20 links, why each
  ended) and the *Proxy node* sensor.
- **Pass:** 1 — never unavailable while the link is back within 20 s, the command applied once; 2 — the light ends
  in the commanded state, the action does not fail; 3 — after the third short link the next one goes to another
  node, and the first node is used again after two minutes (or at once when it is the only one in range). A link
  Home Assistant drops itself (the watchdog, the *devices ignore Home Assistant* repair) cannot be provoked; if the
  link history shows one during the sitting, check the grace held there too.
- **Markers:** `custom_components/junghome_ble/hub/link.py::LinkManager.drop_link`,
  `custom_components/junghome_ble/coordinator.py::JungHomeHub._load_command`,
  `custom_components/junghome_ble/hub/link.py::LinkManager._judge_link`.

### B7 · A plan to an unreachable device is refused

- **Checks:** review-4 W I5 — a configuration change with a message to a device Home Assistant counts as unreachable
  is refused before anything is sent.
- **Needs:** `<light>` on a breaker you may switch off (a mini actuator behind its own circuit), with no proxy role
  you depend on. **Safety:** one light without power for a few minutes; nothing is written if the check holds.
- **Do:** switch the breaker off; switch `<light>` in Home Assistant until it is unavailable; run
  `junghome_ble.set_room` for `<light>` with another existing room. Switch the breaker on; wait until it answers.
- **Capture:** `--src <ha> --dst <light el>`: no Config message to the light after the action.
- **Pass:** the action fails at once with *Not sent: … count as unreachable*, naming the device; the export is
  unchanged (no reload).
- **Markers:** `custom_components/junghome_ble/configurator/executor.py::PlanExecutor.send`.

### B8 · Transitions probe (F4-1)

- **Checks:** review-4 F4-1 — which JUNG loads fade when a Set carries a transition time (neither the app nor the
  gateway ever sends one). Home Assistant's transition support is built but switched off until this runs:
  `light.TRANSITION_KINDS` is empty and `scene.SCENE_TRANSITIONS` is off (`docs/hidden-features.md` §11).
- **Needs:** the DALI tunable-white light, a dimmer insert, a switch insert, a harmless scene; a person watching the
  light. **Safety:** the loads change; set them back.
- **Do:** with `tools/mesh_poc.py listen` running, on each of the three loads:
  `tools/mesh_poc.py lightness <element> 6553 --transition 3`, then `... lightness <element> 65535 --transition 3`;
  `tools/mesh_poc.py ctl <element> 65535 2700 --transition 3` (DALI only); `tools/mesh_poc.py set <element> off
  --transition 3`, then `... on --transition 3`; `tools/mesh_poc.py scene FFFF <scene> --transition 3`. Only if a
  Lightness transition is ignored: `tools/mesh_poc.py delta <element> -16384 --transition 3`.
- **Capture:** each status: does it carry `target=… remaining=…`, does a final status follow at the end, or is the
  Set unanswered (a timeout means that kind stays out).
- **Pass:** per kind, the light fades over about 3 s and the statuses show the target and remaining time. Write the
  results into `docs/hidden-features.md` §11, then list the kinds that fade in `TRANSITION_KINDS` (and turn
  `SCENE_TRANSITIONS` on if the scene faded). Key-scene transitions (`0x5002`) stay a separate decision.
- **Markers:** `custom_components/junghome_ble/light.py::TRANSITION_KINDS`,
  `custom_components/junghome_ble/scene.py::SCENE_TRANSITIONS`,
  `custom_components/junghome_ble/coordinator.py::JungHomeHub._reread_after_transition`,
  `custom_components/junghome_ble/coordinator.py::JungHomeHub.set_onoff`,
  `custom_components/junghome_ble/coordinator.py::JungHomeHub.central_command`,
  `custom_components/junghome_ble/coordinator.py::JungHomeHub.set_ctl_temperature`,
  `custom_components/junghome_ble/coordinator.py::JungHomeHub.recall_scene`, `msg:op:8202`, `msg:op:824c`,
  `msg:op:825e`, `msg:op:8264`, `msg:op:8242`, `msg:op:8243`.

### B9 · `homeassistant.update_entity` reads the device

- **Checks:** review-4 H4-10 / F4-7 — *update entity* sends a fresh Get (a light's state, a parameter's property)
  instead of showing the cached value; at most one request per value and device every 2 s.
- **Needs:** a light, a push-button, the app. **Safety:** a setting changes in the app and is set back.
- **Do:** change a push-button's LED colour (or a light's run-on time) in the app; in Home Assistant run
  `homeassistant.update_entity` on that entity; then on a light whose state you changed with its own key.
- **Capture:** `--src <ha>`: one Get per update, none for a second update within 2 s.
- **Pass:** the entity shows the app's new value at once; the light its real state. Set the value back.
- **Markers:** none of its own on these loads (the code is marked only for the hardware in [F](#f--not-checkable-here)).

### B10 · `locate_node`: Node Identity (F4-15)

- **Checks:** review-4 brief 41 — `locate_node` sends a *Node Identity Set* (running) with the node's device key,
  the node answers *Node Identity Status running* and advertises its Node Identity instead of the Network ID, and
  the Set off follows after `duration`.
- **Needs:** a mains node (`<node>`), a Bluetooth scanner near it (Home Assistant's Bluetooth advertisement monitor,
  or a phone app). **Safety:** reversible: the node stops by itself after 60 s.
- **Do:** `locate_node` with `device` the node's device and `duration: 30`, response on; watch the scanner for the
  node's MAC; wait a minute.
- **Capture:** `--src <ha> --grep 'Node Identity'`: the Set (`running`), its Status from `<node>`, the Set off 30 s
  later and its Status (`stopped`).
- **Pass:** the response `{node: <node>, seconds: 30}`; the scanner shows the node's Mesh Proxy service data change
  from the Network ID (type 0) to a Node Identity (type 1) and back; Home Assistant's link is unaffected.
- **Markers:** `msg:op:8047`, `msg:op:8048`, `msg:op:8046`,
  `custom_components/junghome_ble/coordinator.py::JungHomeHub.async_locate`,
  `custom_components/junghome_ble/actions/devices.py::_locate_node`,
  `custom_components/junghome_ble/strings.json::services.locate_node.description`.

### B11 · Mesh health: a breaker off (U4-7)

- **Checks:** review-4 brief 45 — *Unreachable devices* counts a mains device that lost power and names it, the
  *Mesh overview* row says `reachable: false`, both go back when it is heard again; *Mesh connection* follows the
  link; each row's `scanner` names the adapter or proxy that hears the node; the *device offline* blueprint over
  *Unreachable devices* notifies of both.
- **Needs:** `<light>` on a breaker you may switch off (as in [B7](#b7--a-plan-to-an-unreachable-device-is-refused);
  the two can share the sitting), a person at the breaker. **Safety:** one light without power for a few minutes.
- **Do:** make an automation from the *JUNG HOME device offline* blueprint with *Offline for at least* 1 and *Notify
  when back* on; note *Unreachable devices* (0) and the *Mesh overview* attribute `nodes`; switch the breaker off;
  switch `<light>` in Home Assistant until it is unavailable (or, with *Node heartbeats* on, wait four minutes); after
  two minutes switch the breaker on and wait until the light answers. Optionally switch the Bluetooth proxy off for a
  minute and on again. Delete the automation (the blueprint may stay).
- **Capture:** none needed; the entities' history and the log's *did not answer a request* / *is reachable again*.
- **Pass:** *Unreachable devices* goes to 1 with the light's node device in `devices`, back to 0 when it answers;
  its `nodes` row shows `reachable: false` within a minute and `true` again; `scanner` names your adapter or proxy;
  with the proxy off, *Mesh connection* goes off after 20 s and *Unreachable devices* unavailable, both back with it.
  The blueprint's notification names the light's node device a minute after it is listed, and a second one when it
  answers; the proxy off and on reports neither.
- **Markers:** `custom_components/junghome_ble/binary_sensor.py::JungHomeMeshConnection`,
  `custom_components/junghome_ble/sensor.py::JungHomeUnreachableDevices`,
  `custom_components/junghome_ble/sensor.py::JungHomeMeshOverview`,
  `blueprints/automation/junghome_ble/device_offline_notify.yaml::blueprint.description`.
### B12 · Blueprints with a person at the keys (U4-3)

- **Checks:** review-4 brief 46 — the key blueprints do on a real key what `tests/test_blueprints.py` drives them
  through: a click on a half switches, a single key's click toggles, a held half dims in steps every 0.35 s until
  released, a JUNG light dims while a key is held and stops on release, each gesture runs only its own action; with
  a key wired to a load, `press_on` / `press_off` and its derived holds (B5) do the same.
- **Needs:** `<key>` linked to the gateway (a rocker: its events carry `side`), a `<dimmer>` the key does not drive;
  optionally a rocker wired to a dimmer (as in B5) and another `<light>`; a person at the keys.
  **Safety:** lights switch and dim; the automations are deleted at the end; nothing changes on the mesh.
- **Do:**
  1. Import the three key blueprints with the user guide's *Import* links (or copy the files of
     `blueprints/automation/junghome_ble/` into `<config>/blueprints/automation/junghome_ble/`).
  2. *Key switches and dims lights* with `<key>` and `<dimmer>`, its double-click action a persistent
     notification: click the upper half, then the lower; double-click; hold the upper half 3 s and release; the
     lower half the same; hold a half 15 s (the steps stop after 30, about 10 s).
  3. Disable that automation. *Key dims a JUNG light* with `<key>` and `<dimmer>`: hold the upper half 3 s, release;
     the lower half the same.
  4. Disable it. *Key runs up to six actions* with `<key>`, each gesture a persistent notification naming it: each
     of the six gestures once.
  5. Optional: step 2 again with the rocker wired to a dimmer and `<light>`.
  6. Delete the three automations (the blueprints may stay).
- **Capture:** not needed for the pass; `--src <ha>` shows the Lightness and Generic Move Sets if a step goes wrong.
- **Pass:** each gesture does what the blueprint's description says; no dimming step after the `hold_end` (the
  automation's trace shows the loop ended); the JUNG light starts and stops with the hold; each of the six gestures
  runs only its own notification (a double press also runs the click's, unless the key is picked under *Keys that
  wait for a double click* or the option *Report clicks only once a double click is ruled out* is on).
- **Markers:** `blueprints/automation/junghome_ble/rocker_light_control.yaml::blueprint.description`,
  `blueprints/automation/junghome_ble/rocker_dim_jung_light.yaml::blueprint.description`,
  `blueprints/automation/junghome_ble/rocker_scene_selector.yaml::blueprint.description` (their sentence on a key connected to an empty room
  with D10).

### B13 · Double click per key (U4-19)

- **Checks:** review-4 brief 77 — a key picked under *Keys that wait for a double click* reports a single press as
  one `click` half a second late and a double press as `double_click` only; a key not picked reports its `click` at
  once and a double press as `click`, then `double_click`; both pressed at the same time keep their own way.
- **Needs:** two keys linked to the gateway (`connection: gateway`), `<key1>` and `<key2>` (two halves of one rocker
  are one key: take keys of two elements); a person at the keys.
- **Safety:** nothing changes on the mesh; the option is set back at the end.
- **Do:**
  1. *Configure* the integration: *Report clicks only once a double click is ruled out* off, *Keys that wait for a
     double click* = `<key1>` only. Check the list: it names the keys linked to the gateway by their entity names,
     no key wired to a load. `<key1>`'s event entity shows `waits_for_double_click: true`, `<key2>`'s `false`.
  2. Watch the events (*Developer tools → Events*, listen to `junghome_ble_button_action`). Press `<key1>` once,
     then double-press it; the same with `<key2>`.
  3. Double-press both keys at about the same time (one hand each).
  4. Press `<key2>` four times quickly, then, after a pause, three times (review-5 R5-1).
  5. *Configure* again and empty the list (or set back what step 1 found).
- **Capture:** not needed; the key's vendor gesture messages (`0x5012`) if an event is missing.
- **Pass:** `<key1>`: one `click` about half a second after the single press, `double_click` alone for the double
  press; `<key2>`: `click` at once, then `click` + `double_click` for the double press; step 3 gives `<key1>`
  `double_click` alone and `<key2>` `click` + `double_click`; step 4 gives `click`, `double_click`, `click`,
  `double_click` for the four presses and `click`, `double_click`, `click` for the three — never two `double_click`s
  in a row.
- **Markers:** `custom_components/junghome_ble/hub/gestures.py::<module>`,
  `custom_components/junghome_ble/strings.json::options.step.init.data_description.double_click_keys`; the docs'
  per-key sentences and the CHANGELOG bullet of U4-19.

## C · Settings, changed and set back

Each item writes a device setting and restores the value noted in [0](#note-what-you-will-restore).

### C1 · Tunable-white range and the setup states (`msg:op:826b`)

- **Checks:** `msg:op:826b` / `prod:param:lamp:tunable-white-range` — the expected outcome on this DALI insert is
  *not applied*; the setup-state parameters of dimmers and the DALI light, *not yet tried on a real device* in the
  docs.
- **Needs:** the DALI light, a dimmer. **Safety:** settings; each is set back.
- **Do:**
  1. Enable `<tw light>`'s *Minimum colour temperature* entity (expert, disabled by default); set it to 2100 K.
  2. Should the light have applied it, set it back to 2000 K.
  3. On `<dimmer>`: *Minimum brightness* +1 % and back; *Behaviour after mains return* to another value and back
     (expert, disabled by default); on `<tw light>`: *Switch-on colour temperature* +100 K and back.
- **Capture:** `--src <ha> --grep 'CTL Temperature Range|Lightness Range|OnPowerUp|CTL Default'`: an acknowledged
  *Light CTL Temperature Range Set* to `<tw el0>` with parameters `34 08 70 17` (2100..6000 K); then either a *Range
  Status* (`0x8263`) or Home Assistant's read-back *Range Get* (`0x8262`).
- **Pass:** either outcome decides the row: **(a)** no `0x8263`, the read-back still 2000..6000 K, and the change fails
  with *not applied* (what the earlier probe predicts); or **(b)** the light answers and applies it: its
  `min_color_temp_kelvin` becomes 2100, and after step 2 `ctlrange <tw el0>` reads 2000..6000 again. The other
  setup states read back what was written and end at their old values.
- **Markers:** `msg:op:826b`, `prod:param:lamp:tunable-white-range`.

### C2 · Device lock *Lock operation* (F4)

- **Checks:** review-3 F4 — the device lock word `0x0001`, read-modify-write per bit; which bit is *operation* (the
  encoder says bit 2, the app's decoder disagrees; the app's own writes confirmed bits 1 and 2 on air).
- **Needs:** a push-button whose key drives something harmless; a person to try it.
  **Safety:** the key stops working until unlocked. Do not touch *Lock factory reset*.
- **Do:** turn on the push-button's *Lock operation* switch; have the key pressed; turn it off; have it pressed again.
- **Capture:** `--src <ha> --grep '0001|device_lock'`: an Admin Property Set of `0x0001` with the noted word plus
  bit 2, then the word as noted.
- **Pass:** while locked, a press sends nothing (no publication from `<key el>`) and its load stays; after the
  unlock the key works; `prop get <node> device_lock` reads the noted word again. If the key still works while
  locked, record the word the device holds: the bit order is then the decoder's.
- **Markers:** `custom_components/junghome_ble/switch.py::JungHomeDeviceLockFlag`,
  `custom_components/junghome_ble/properties/targets.py::DEVICE_LOCK_ENABLED` (bits 1 and 2 here; 3 and 4 are a
  thermostat's, see F).

### C3 · Lock function of a load (`0x0009`), and locked loads (F4-2)

- **Checks:** the *Lock* switch and *Lock time limit* of a light (docs: *not yet tried on a real device*); review-4
  F4-2 (brief 35): what a locked load answers to an OnOff / Lightness Set and whether a lock set in the app reaches
  anyone but the app (`docs/hidden-features.md` §12); the lights' and sockets' `locked` / `lock_until` attributes,
  the refusal of a command to a locked load, one lock Get per load and link (shared with an enabled *Lock* switch),
  and that a locked load never goes unavailable for ignoring a command.
- **Needs:** `<light>` and the key that drives it, `<dimmer>`, the app; a person. **Safety:** each lock is undone in
  the item (a timed one ends by itself); the loads keep their states while locked.
- **Do:**
  1. The probe, with `tools/mesh_poc.py listen` running in a second shell (or the live sniffer pipe), on `<light el>`
     and then `<dimmer el>`:

     ```
     tools/mesh_poc.py prop get <light el> enforced_output                  # unlocked: command 00
     tools/mesh_poc.py prop set <light el> enforced_output hex:02010000     # lock the current state, no limit
     tools/mesh_poc.py set <light el> on                                    # the state it is not in (or off)
     tools/mesh_poc.py prop set <light el> enforced_output hex:00010000     # unlock
     tools/mesh_poc.py prop set <dimmer el> enforced_output hex:02010000
     tools/mesh_poc.py lightness <dimmer el> 30000                          # a level it is not at
     tools/mesh_poc.py set <dimmer el> on                                   # (or off)
     tools/mesh_poc.py prop set <dimmer el> enforced_output hex:00010000
     ```

     Then lock `<light>` in the app (device page, *Lock*) and unlock it again, watching `listen`.
  2. In Home Assistant: enable `<light>`'s *Lock* switch and *Lock time limit*; set the limit to 60 s; turn *Lock* on.
     `<light>`'s attributes show `locked: true` and `lock_until` about 60 s ahead. Have the key pressed; switch
     `<light>` from Home Assistant. Wait 70 s, then switch it again.
  3. Lock `<light>` in the app (no time limit), wait a minute, and switch it from Home Assistant twice (Home
     Assistant does not know the lock yet). Unlock it in the app; after 15 s switch it from Home Assistant.
  4. Disable the *Lock* switch and *Lock time limit* again.
- **Capture:** the probe's `listen` output: per Set, a Status (to the CLI or to the element group) and the state it
  shows, or the CLI's timeout; any `0x0009` Status while the app locks. `--src <ha> --grep '0009|enforced'`: one
  Admin Get of `0x0009` per light and socket after each connection (one, not two, for `<light>` with its *Lock* switch
  enabled), the Admin Set `02 01 3c 00` to `<light el>`, the read-back about 65 s later; in step 3 what each command
  sent (an OnOff Set, then a Get of `0x0009`).
- **Pass:** the light keeps its state against the key and Home Assistant while locked; in step 2 the action fails
  at once with *… is locked … keeps its state until it is unlocked* and sends no Set; after the time limit the
  command works and the switch has turned off by itself; in step 3 the first command fails with that message (a
  load that answers with its old state: the lock is read after it), the second is refused at once, and the one
  after the unlock works; the key works again. Write the probe's answers into `docs/hidden-features.md` §12. If the
  locked load stays silent instead, step 3's first command fails with *did not answer* and `<light>` goes
  unavailable until it is heard from (only a lock already known spares it, `Liveness.missed_answer`): note it —
  the decision is then whether to read the lock after every unanswered command (an unreachable load's action would
  take one more read to fail).
- **Markers:** `custom_components/junghome_ble/config_entities.py::LoadLock`,
  `custom_components/junghome_ble/hub/liveness.py::Liveness.missed_answer`, `ui:state:lockfunctioncapability`,
  the docs' lock-function paragraph and *Locked loads* (the cover's lock markers are the blinds', see F).

### C4 · Mini-actuator inputs (F10)

- **Checks:** review-3 F10 — what an input really sends, the *Input state* binary sensor, the input's *Edge
  evaluation* / *Rising edge* / *Falling edge*; optionally `mgmt:flow:configureminiactuatorconnection` (an input
  wired with `assign_key`).
- **Needs:** a mini actuator input with a switch or contact wired to it (the export shows the inputs, not whether
  anything is connected) and a person to operate it. If no input is wired, record *not checkable here*.
- **Safety:** settings, set back; the optional step rewires the input (D-class) and is undone.
- **Do:**
  1. Enable `<input>`'s *Input state* sensor. Operate the contact: closed, open, closed (pause a second between).
  2. Enable *Edge evaluation*, *Rising edge*, *Falling edge*. Turn edge evaluation on with *Switch on* / *Switch
     off*, operate the contact; then off (the app's key mode), operate it again. Restore the noted byte.
  3. Optional: `junghome_ble.assign_key` with `key_entity: <input>`, `target_entity: <light>`; operate the contact;
     restore the input's noted connection (or `clear_key` when it had none).
- **Capture:** `--src <input el>`: what each edge sends (a *Generic OnOff Set* in key mode light / switch?), to which
  address.
- **Pass:** the event entity reports each change; *Input state* follows the contact's level in both modes (which of
  on / off means closed is the contact's matter; note it for the docs); the edge settings read back what was written
  and end at the noted value; in step 3 the light follows the input.
- **Markers:** `custom_components/junghome_ble/binary_sensor.py::JungHomeInputState`,
  `custom_components/junghome_ble/binary_sensor.py::<module>` (the inputs paragraph),
  `mgmt:flow:configureminiactuatorconnection`.

### C5 · Schedules

- **Checks:** the schedule actions and *Schedules* sensor (docs: *not yet tried on a real device*); that the nodes'
  clocks (Time Set) and the location broadcast (A2) are right.
- **Needs:** `<light>`, off. **Safety:** one free schedule slot used, then freed; nothing in the export.
- **Do:**
  ```yaml
  action: junghome_ble.create_schedule
  target: { entity_id: <light> }
  data: { trigger: time, time: "<now + 3 min>", action: "on" }
  response_variable: created
  ```
  Wait for the time; then `create_schedule` with `trigger: sunset`, `enabled: false`; then
  `junghome_ble.get_schedules` on `<light>`; then `update_schedule` on the sunset slot (`slot` as created) with
  `trigger: time`, `time: "<now + 3 min>"`, `action: "on"` after switching `<light>` off again (review-4 F4-6), and
  `get_schedules` once more; then `delete_schedule` for both slots. Enable the *Schedules* sensor.
- **Pass:** the light switches on at the set minute (the node's clock is right), and again at the updated slot's
  minute; the sunset slot's `effective_time` matches today's sunset at home within a few minutes (the node got the
  location); after the update the same slot number lists the timed schedule, enabled, and no other slot changed; the
  sensor shows the slots and then none; nothing remains after the deletes.
- **Markers:** `custom_components/junghome_ble/sensor.py::<module>` (the *Schedules* sensor part),
  `custom_components/junghome_ble/schedules.py::Scheduler.update`,
  `custom_components/junghome_ble/actions/schedules.py::_update_schedule`,
  `custom_components/junghome_ble/strings.json::services.update_schedule.description`.

### C6 · Firmware-only properties, set and restored (brief 36)

- **Checks:** review-4 brief 36 step 1 — what the firmware-only ids do, so that only settled ones become entities
  (`config_entities.FIRMWARE_ENTITIES`, empty until then) and only settled ones get a codec.
- **Needs:** someone at home watching the loads; A7's values noted; `tools/mesh_poc.py --cdb <export.json> listen`
  (or the sniffer) running throughout. **Safety:** every value goes back to what A7 read in the same session.
  **Never enable presentation mode** (`presentation_mode_enable`) unattended: it may switch loads on its own; leave
  it as read.
- **Do:** (`<…as read>` = the hex A7 noted)
  1. Meter rhythm: `prop set <meter el> transmission_settings hex:0000`, then `hex:0200`, then `hex:0101`, each
     followed by `listen --src <meter el> --seconds 300` (the Sensor Status rhythm is about 65 s today); restore
     `prop set <meter el> transmission_settings hex:<as read>` and see the rhythm come back.
  2. Key toggling: `prop set <key el> key_toggle_enable hex:00`; press the key several times (does a single key
     stop toggling, or does nothing change?); restore `hex:<as read>`.
  3. DALI insert / dimmer: `prop set <tw el0> basic_light_function_enable hex:01`, switch `<tw light>` off from
     Home Assistant: does it stay at the hotel value (`hotel_dimm_value`) instead of off? Restore `hex:<as read>`.
     `prop set <tw el0> night_dimm_value hex:<another value>`, switch the light on (after dark if night means the
     clock), compare the level; restore. `prop set <tw el0> presentation_mode_time hex:<as read, first two bytes
     changed>`, `prop get` both presentation ids; restore.
  4. Run-on remaining time: `prop set <light el> timed_on_duration 20`, `set <light el> on`, then `get <light el>`
     within the 20 s: is there `target=OFF remaining=…`? Watch *Switches off at*; restore `timed_on_duration` to the
     value as read.
  5. LED: `prop set <node> led1_mode_on hex:<r><g><b><mode as read>` with a colour outside the app's palette (each
     channel 0..100, e.g. `32143c`): does the LED show it, does the read-back keep it? Restore `hex:<as read>`.
- **Capture:** the CLI output and `listen`; per step, what the load or LED did.
- **Pass:** each value read back as written and restored at the end. Write every outcome into
  `docs/hidden-features.md` §13 and `docs/android/properties.md` §1.10; then, per id that is settled: a codec in
  `jhmesh/properties.py` (number for a percentage, switch for an enable), its id in `FIRMWARE_ENTITIES`, *Switches
  off at* kept or removed by step 4, an RGB light per LED state only if step 5 worked.
- **Markers:** `custom_components/junghome_ble/coordinator.py::JungHomeHub._on_onoff_status`,
  `custom_components/junghome_ble/sensor.py::JungHomeSwitchOffAt`.

### C7 · Repairs that fix (U4-5)

- **Checks:** review-4 U4-5 (brief 44) — the repairs that offer their fix: *JUNG HOME export not handed to the
  gateway* hands the export to the gateway on **Submit**, *Device name not passed on to the JUNG HOME app* asks for
  another name, *JUNG HOME devices missing from the export* on a gateway entry fetches the export again; each repair's
  *Learn more* opens its entry on the user guide's maintenance page.
- **Needs:** a gateway entry; a firewall rule that blocks Home Assistant from the gateway for a minute (as D6); a
  light; optionally a device added in the app. **Safety:** the gateway receives the export Home Assistant already
  holds; the device name is set back; nothing new goes on air.
- **Do:**
  1. Block the gateway and run *Sync gateway* (`junghome_ble.sync_gateway`): the action fails and the repair
     appears. Open it and follow *Learn more*. Unblock the gateway, then **Submit** the repair.
  2. Rename `<light>`'s device in Home Assistant to `50% off`: the device-name repair appears. Open it, enter
     `100%` (refused on the form), then the light's old name, and **Submit**.
  3. Optional, only when a device is added in the app anyway: if *JUNG HOME devices missing from the export* stays
     after Home Assistant's own fetch, open it and **Submit**.
- **Pass:** 1: the repair clears and the log says *Handed the mesh export to the gateway*; 2: the repair clears and
  the app shows the old name; 3: the entry is set up again from the gateway's export (the replaced one kept as
  `.pre-reconfigure`), the device's entities appear and the repair clears. The links open the right headings.
- **Markers:** `custom_components/junghome_ble/repairs.py::GatewaySyncFlow`, `custom_components/junghome_ble/repairs.py::DeviceNameFlow`, `custom_components/junghome_ble/repairs.py::NewExportFlow`,
  `custom_components/junghome_ble/strings.json::issues.gateway_sync_failed.fix_flow.step.confirm.description`,
  `custom_components/junghome_ble/strings.json::issues.device_name_rejected.fix_flow.step.name.description`,
  `custom_components/junghome_ble/strings.json::issues.unknown_nodes_gateway.fix_flow.step.gateway_refetch.description`.

### C8 · Following the app (U4-6)

- **Checks:** review-4 U4-6 (brief 48, decision M12) — the phone running the app is heard on the mesh as a source
  that is neither Home Assistant nor a device; a gateway entry fetches the gateway's export about 3 minutes after the
  phone went quiet (one GET for a burst, at most one per 15 minutes) and follows a changed export in place; the
  *Fetch export from gateway* button does it at once; a file entry raises *The JUNG HOME app changed the
  installation* when the phone configures a device, which needs the proxy to forward the phone's device-key Config
  messages to Home Assistant.
- **Needs:** a gateway entry, `<light>`, **a person with the phone and the app** at home. The file-entry notice
  cannot be raised here (one entry per mesh, and this one is from the gateway): step 4 checks the traffic it depends
  on. Enable debug logging for `custom_components.junghome_ble.app_follow`. **Safety:** a device name changes in the
  app and is set back.
- **Do:**
  1. With the app open near a proxy node, rename `<light>` in the app, then leave the app alone.
  2. Watch the log: *Fetching the gateway's export: the app was used* about 3 minutes after the last phone message,
     then *following it*. Check `<light>`'s device name in Home Assistant.
  3. Rename it back in the app; this time press **Fetch export from gateway** on the gateway's device right after the
     app saved.
  4. File-entry part, from the capture: during step 1 or 3, does any Config message (`--grep 'Config'`) from the
     phone's address reach Home Assistant's link (`--dst` any, seen by the proxy Home Assistant is connected to)?
     Change a key connection or a room in the app if a rename alone sends none, and set it back.
- **Capture:** `--src <phone>` (the phone's address: the export's provisioner node without a product id) for the
  traffic Home Assistant counts as the phone's, and the HTTPS request in the log.
- **Pass:** steps 2 and 3 show the new name without Reconfigure and without *unavailable* in `<light>`'s history,
  one GET each; step 4 shows the phone's Config messages arriving (else the file-entry notice cannot trigger: note
  it, and the repair's markers stay).
- **Markers:** `custom_components/junghome_ble/app_follow.py::<module>`,
  `custom_components/junghome_ble/strings.json::options.step.init.data_description.follow_app`,
  `custom_components/junghome_ble/strings.json::issues.app_changed.fix_flow.step.upload.description`,
  `custom_components/junghome_ble/strings.json::issues.app_changed.fix_flow.step.gateway_refetch.description` (shown
  only for an entry reconfigured to the gateway while the notice stood, which Reconfigure clears).

### C9 · Rooms to areas (U4-2)

- **Checks:** review-4 U4-2 (brief 47) — *Reconfigure → Change which area each room's devices go to* moves the
  devices still in the area the previous choice gave them (keys and node devices included, which had none before
  1.1.0) and leaves a device placed by hand where it is; Home Assistant side only, nothing goes on air.
- **Needs:** the running entry; a room R (note the area its devices are in). **Safety:** areas only; set back in
  step 4.
- **Do:**
  1. Note the area of R's light devices, their *Push-buttons* devices and node devices; move one light device of R
     to another area by hand (note it).
  2. Create an area *Sweep area*. *Reconfigure → Change which area each room's devices go to*: the form shows each
     room with its current area (an area named, or aliased, like the room); pick *Sweep area* for R, **Submit**.
  3. The result names how many devices moved. Check R's device pages.
  4. Undo: the same step with R's old area; move the hand-placed device back; delete *Sweep area*.
- **Pass:** every device of R in step 3 is in *Sweep area* but the one placed by hand; the count matches; after step 4
  everything is where it was (keys and nodes now in R's area). The entry reloads after each submit, as for any
  options change.
- **Markers:** `custom_components/junghome_ble/config_flow.py::JungHomeConfigFlow._async_areas_option`.

### C10 · Hotel function entities written from Home Assistant (brief 73)

- **Checks:** review-4 brief 73 — the DALI insert's *Hotel function* switch (`basic_light_function_enable`,
  `0x1009`), *Hotel function brightness* (`hotel_dimm_value`, `0x1008`) and *Night-light brightness*
  (`night_dimm_value`, `0x1011`), written from Home Assistant rather than the CLI (C6 settled their layouts).
- **Needs:** the DALI tunable-white light `<tw light>`; the three entities enabled on its device. **Safety:** each
  value goes back to what the entity showed first.
- **Do:**
  1. Note the three entities' states. Set *Hotel function brightness* to 30 %, turn *Hotel function* on, switch
     `<tw light>` off from Home Assistant: it should stay on, dimmed to about 30 %.
  2. Turn *Hotel function* off, switch the light off (it goes off), set the brightness back to its first value.
  3. Set *Night-light brightness* to another value and back (its effect needs the dark and a person; skip if not).
- **Capture:** `--src <ha>` and `--dst <ha>` over the sitting.
- **Pass:** each write is a vendor Property Set answered by a Status with the written byte (30 % = `0x4D`), the
  entity shows the read-back, and step 1's off leaves the light at that level.
- **Markers:** `prod:unused-string:device_parameter_hotel_function`,
  `prod:unused-string:device_parameter_hotel_lightness`,
  `prod:unused-string:device_parameter_night_light_lightness`.

## D · Rewiring, undone in the sitting

**These change the network's wiring and the export, upload it to the gateway and reload the entry.** Each ends with the
starting state restored; the app shows Home Assistant's changes only once it takes the gateway's export.

### D1 · Socket thresholds (`net:uc:createthreshold`, `togglethreshold`, `deletethreshold`)

- **Needs:** `<socket>` with a harmless load whose power can be made to stay low (a lamp switched off at its own
  switch), and `<light>` as the target; preferably a socket with no threshold yet. Enable its *Switch-on* /
  *Switch-off threshold* sensors.
- **Safety:** `<light>` switches by itself while wired; removed at the end. A socket that had thresholds gets them
  back with `set_threshold` and the noted values.
- **Do:**
  1. Create:
     ```yaml
     action: junghome_ble.set_threshold
     target: { entity_id: <socket> }
     data: { threshold: switch_off, power: 5, duration: 30, devices: [<light>] }
     ```
     Turn `<light>` on, then make the socket's power stay under 5 W (the lamp off): `<light>` turns off after 30 s.
  2. Disable: `set_threshold` with `threshold: switch_off`, `enabled: false` (the other threshold stays cleared).
     Repeat the low-power test: `<light>` stays on.
  3. Delete: `junghome_ble.delete_threshold` on `<socket>`.
- **Capture:** `--grep 'Subscription|Publication|5004|5005|threshold'`, compared with the app's sequences in the app
  settings capture the ledger rows cite:
  - create: the threshold Set first; then Subscription Add and Publication Set (TTL 255) of `<meter el>`'s OnOff
    Client `0x1001` to its element group; then on `<light el>` Subscription Add of `0x0527:1013`, then of `0x1000`;
    every Config Status *Success*;
  - disable: the threshold Set inactive; `<light el>`'s Subscription Delete of `0x1000` and `0x0527:1013`;
    `<meter el>`'s `0x1001` Publication Set `0000` (TTL 0), then its group again (TTL 255); every Status *Success*;
  - delete: `0x5004` Set to `81 00 00 00 FF FF FF 00` (and `0x5005` alike); `0x1001` Publication Set `0000` then
    its group (TTL 255); every target subscription to the group deleted.
- **Pass:** those sequences (Home Assistant's documented differences: no KeyMode 5 write, both thresholds cleared,
  targets unsubscribed before the publication reset) and the behaviour in steps 1–2; the sensors follow; after step 3
  `config audit <socket node>` and `config audit <light node>` show no difference from the export.
- **Markers:** `net:uc:createthreshold`, `net:uc:togglethreshold`, `net:uc:deletethreshold`,
  `custom_components/junghome_ble/sensor.py::<module>` (the threshold sensors).

### D2 · Key → scene (F15)

- **Checks:** review-3 F15 — `assign_key` with `scene`: the key's connections cleared, its Scene Client publishing to
  `FFFF`, KeyModeSceneConfig `0x5002` written and confirmed, key mode 2; the key then recalls the scene.
- **Needs:** a key whose wiring is easy to restore — best one with no function (`connection: none`) or linked to
  the gateway — and `<scene>`; a person. Enable the key's *Key mode* sensor.
- **Safety:** **rewires a real key.** Restore it in step 3.
- **Do:**
  1. `junghome_ble.assign_key` with `key_entity: <key>`, `scene: <scene>` (leave `mode` empty).
  2. Press the key.
  3. Restore: `clear_key` for a key that had no function; `assign_key` with `target_device: <the gateway device>` for
     a gateway key; otherwise `assign_key` back to the noted target and mode (or set it in the app).
- **Capture:** `--grep 'Publication|0x5002|5002|5003|Scene Recall'`: the clearing steps, Publication Set of
  `<key el>`'s Scene Client (`0x1205`) to `FFFF`, the Admin Set `0x5002` `[scene u16 LE][00 00 00 00]` and its
  status, the KeyMode Set `0x5003` = 2; on the press a *Scene Recall* from `<key el>` to `FFFF` with `<scene>`'s
  number.
- **Pass:** that sequence; on the press the scene's members switch, the key's `scene` event and
  `junghome_ble_scene_recalled` (with `source`) fire, *Key mode* shows `scene` and the entity's `connection` is
  `scene` with `connection_scene`; after step 3 the noted connection is back (`prop get <key el> key_mode` and the
  attributes). Brief 31 and 38 wait for this result.
- **Markers:** `custom_components/junghome_ble/configurator/wiring.py::plan_scene_link`,
  `custom_components/junghome_ble/configurator/rooms.py::Keys._write_scene_config`,
  `custom_components/junghome_ble/strings.json::services.assign_key.fields.scene.description`,
  `custom_components/junghome_ble/sensor.py::<module>` (the *Key mode* sensor).

### D3 · Rooms and scenes allocated from the top

- **Checks:** `net:alloc:room-group`, `net:alloc:scene-number` (review-4 W4-2) — Home Assistant's new rooms and scenes
  take the highest free group address / scene number of the app's range, the app's the lowest.
- **Needs:** `<light>` and `<dimmer>` you may move between rooms; the app.
- **Safety:** a room and a scene on each side, removed again; each light goes back to its noted room.
- **Do:**
  1. `junghome_ble.create_room` `name: Sweep HA`; `set_room` `<light>` → `Sweep HA`.
  2. In the app: a room `Sweep app`, `<dimmer>` into it.
  3. `junghome_ble.create_scene` `name: Sweep HA scene`; `store_scene` with `<light>`. In the app: a scene with
     `<dimmer>`.
  4. Undo: `set_room` `<light>` back; `delete_room` `Sweep HA`; `delete_scene` `Sweep HA scene`; in the app move
     `<dimmer>` back and delete its room and scene.
- **Capture:** the Subscription Adds of steps 1–2 show each room's group address; the *Scene Store*s of step 3 each
  scene's number.
- **Pass:** Home Assistant's room and scene sit at the top of the app's ranges (the first free from the end: `C64B`,
  `6553` on an untouched range), the app's at its lowest free ones, and they differ; after step 4 `audit_network`
  reports no difference for the two lights.
- **Markers:** `net:alloc:room-group`, `net:alloc:scene-number`.

### D4 · `delete_unused_scenes` and an app scene

- **Checks:** review-4 W4-3 / decision M3 — a dry run by default, on the gateway's current export, never listing a
  scene the app made since Home Assistant last loaded the export.
- **Needs:** a gateway entry; the app. **Safety:** the dry run deletes nothing; the app scene is removed at the end.
- **Do:** in the app make a scene with one light; then `junghome_ble.delete_unused_scenes` without fields (with
  *Response* on); then delete the scene in the app.
- **Capture:** `--src <ha> --grep 'Scene'`: Scene Register Gets only, no *Scene Delete*.
- **Pass:** the response does not list the app scene's number; nothing deleted; the entry took over the gateway's
  export first (log), which has the scene.
- **Markers:** `custom_components/junghome_ble/configurator/scenes.py::Scenes.delete_unused_scenes`.

### D5 · *Sensor values for IoT systems*

- **Checks:** review-4 W4-6 — the switch shows the node's publication state and a change sends the *Publication Set*
  even where the export already agrees.
- **Needs:** a metering socket; enable its *Sensor values for IoT systems* switch (shown with a gateway in the
  project and software 1.3.0.0 or later). **Safety:** the socket stops (or starts) publishing its values to the
  gateway until set back.
- **Do:** note the switch's state; flip it; flip it back. Optional, the W4-6 case itself: flip it in the app first,
  then back from Home Assistant before the entry took over the app's export.
- **Capture:** `--grep 'Publication'`: a *Config Model Publication Set* of each Sensor Server (`0x1100`) of the node:
  off `0000`, on its element group with TTL 255; the Publication Gets of the read on the next link.
- **Pass:** *Success* statuses; after the reload the switch shows what the node answers; the socket's power still
  reaches Home Assistant and, once back on, the gateway.
- **Markers:** `custom_components/junghome_ble/configurator/thresholds.py::Thresholds.set_sensor_publication`.

### D6 · Both-sides-changed merge (optional)

- **Checks:** review-4 S4-4 / S4-6 / D16 / D17 — a change made while Home Assistant's file and the app's upload both
  changed is merged; a key's `meta` rows stay single; a conflict raises *The JUNG HOME app overrode a change*;
  *unverified against the iOS app's import*.
- **Needs:** a gateway entry, the app, and a temporary firewall rule on the HA host that blocks the gateway's
  address. **Safety:** room names change and are set back; the gateway sees the merged export.
- **Do:** block the gateway; `rename_room` room X in Home Assistant (the upload fails and is retried: expect *export not
  handed to the gateway*); in the app rename room Y; unblock; `junghome_ble.sync_gateway`. For the conflict, repeat
  with both sides renaming the same room. Rename the rooms back. If an iOS app is at hand, let it take the gateway's
  export and check it opens and shows both renames.
- **Pass:** the merged export holds both changes (`<export>.pre-adopt` kept), the upload succeeds, no duplicate `meta`
  rows for one element; the conflict run raises the repair naming the room; the app's import works.
- **Markers:** `custom_components/junghome_ble/configurator/store.py::ExportStore._adopt`,
  `custom_components/junghome_ble/jhmesh/merge.py::<module>`.

### D7 · Configuration changes without a reload

- **Checks:** review-4 D23 / H4-1 — `create_room`, `set_room`, `rename_room`, `assign_key` and the scene actions update
  the running hub in place: no entity passes through `unavailable` / `unknown`, the Bluetooth link stays up, nothing
  is read over the mesh again.
- **Needs:** a light, a push-button key. **Safety:** a room and a key's wiring change; undo both in the sitting.
- **Do:** `junghome_ble.create_room` (a throw-away name, `create: true` where asked), `set_room` a light into it,
  `rename_room` it, `assign_key` a rocker to that light; then undo: `set_room` the light back, restore the key's old
  connection, `delete_room` the throw-away room.
- **Capture:** the light's history in Home Assistant, the *Link state* sensor (enable it if needed) and
  `--src <ha>`.
- **Pass:** the light's history shows no gap or `unavailable`; the link does not drop; only the Config messages of the
  change go out, no wave of state Gets. A change that cannot be followed in place reloads (debug log: the reason).
- **Also (review-5 U5-7, brief 82):** open the options, pick a key under *Keys that wait for a double click* and save,
  then remove it again and save; save the form once more without changing anything. **Pass:** no entity passes
  through `unavailable`, the *Link state* sensor stays `connected`, the key's event entity shows
  `waits_for_double_click: true` at once (and `false` after), and a click of that key arrives half a second late
  while it is picked.
- **Markers:** `custom_components/junghome_ble/model_update.py::<module>`,
  `custom_components/junghome_ble/coordinator.py::JungHomeHub.async_options_updated`.

### D8 · Several rooms per light, leaving a room (F4-5)

- **Checks:** review-4 F4-5 / W I12 — `add_to_room` puts a light into a second room without leaving its first;
  `remove_from_room` takes it out of one room only, and refuses a light a room-linked key drives unless `force`.
- **Needs:** `<light>` in a room (note it: room R1) that no key is linked to, a second room R2 (any other room; or
  `create: true` with a throw-away name); optionally the app. **Safety:** `<light>`'s rooms change; set back in step 4.
- **Do:**
  1. `junghome_ble.add_to_room` with `<light>`, `room: R2`. Its `rooms` attribute lists R1 and R2.
  2. Turn *All lights in R1* off, `<light>` on, then *All lights in R2* off: `<light>` goes off with each room; both
     room entities list it under `members`.
  3. `junghome_ble.remove_from_room` with `<light>`, `room: R1`; switch *All lights in R1*: `<light>` no longer
     follows. Optional, the refusal: `remove_from_room` of a light a room-linked key drives, without `force` — the
     error names the key and nothing goes out.
  4. Undo: `add_to_room` `<light>` → R1, `remove_from_room` `<light>` → R2 (and `delete_room` a throw-away room).
- **Capture:** `--src <ha> --grep 'Subscription'`: step 1 *Config Model Subscription Add* of R2's address on
  `<light el>`'s OnOff `0x1000` and Level `0x1002` servers (plus each R2-linked key's group), no Subscription Delete;
  step 3 *Subscription Delete* of R1's address on every model of `<light el>` that carried it, nothing for R2; every
  Config Status *Success*.
- **Pass:** those messages and behaviour; after steps 1, 3 and 4 `tools/mesh_poc.py config audit <node>` (or
  `audit_network`) reports no difference from the export; the history of `<light>` shows no `unavailable`. With the
  app, after it takes the gateway's export following step 1: note whether it shows `<light>` in both rooms (*the
  app's view of several rooms per device written by Home Assistant* — unverified).
- **Markers:** `custom_components/junghome_ble/configurator/rooms.py::Rooms.add_to_rooms`,
  `custom_components/junghome_ble/configurator/rooms.py::Rooms.remove_from_rooms`,
  `custom_components/junghome_ble/strings.json::services.add_to_room.description`,
  `custom_components/junghome_ble/strings.json::services.remove_from_room.description`,
  `net:uc:adddevicetogroups`, `net:uc:deletedevicefromgroups`.

### D9 · Key connections: colour temperature, lock function, property users (brief 38)

- **Checks:** review-4 F4-4 — `assign_key` with `target_element: color_temperature` (the Level client alone to the
  tunable-white light's temperature element) and with `mode: lock` (key mode *property*: `0x5006` / `0x5007` /
  `0x5008` on the key, the vendor client publishing to the load's element group), the property-user wiring of a
  mini-actuator target, and the event entity's `connection: lock` read back from a key. All of it was written from
  the app's code alone: **capture the app first** (step 1), then compare Home Assistant's sequences with it.
- **Needs:** a spare push-button key (best one with no function or linked to the gateway), `<tw light>`, `<light>`,
  `<socket>`, `<mini el>` and `<input>`; the app; **a person at home pressing keys**. Enable the key's *Key mode*
  sensor.
- **Safety:** **rewires a real key** — a wrong plan leaves it dead until it is reassigned or cleared — and locks a
  light and a socket. Note the key's connection first (as for D2); restore it in step 4; unlock every load in the
  app or with its *Lock* switch if a lock outlives the sitting.
- **Do:**
  1. *Capture the app* (`--seconds 900`, session `sweep-d9-app`): in the app's connection screen of the spare key,
     connect it in turn to (a) `<tw light>`'s colour temperature, (b) the lock function of `<light>` with a time
     limit (note it), (c) the lock function of `<socket>` without one, (d) `<socket>` from `<input>` (the app's
     *Switching* category: a mini-actuator input to a property user), and (e) `<mini el>` from the key. After each,
     press the key's up and down sides once and note what the load does. Then `mesh_sniff.py decode --json`; keep
     only the decoded sequences of each connection, with addresses mapped to the fixture's (`tests/fixtures`) and no
     key material, as the test data of `tests/test_mesh_config.py` (`test_assign_key_to_a_tw_lights_colour_temperature`,
     `test_assign_key_to_lock_a_light_sends_the_apps_locking_sequence`,
     `test_a_lock_link_to_a_socket_wires_the_property_user_publications`,
     `test_assign_key_to_a_mini_actuator_wires_both_channels_property_users`). Note in particular whether the app
     sends `ResetKeySetPropertyMode` inside its lock link, which values it writes to `0x5008`, and whether its
     *Get* of `0x0009` goes to the load.
  2. *Home Assistant* (session `sweep-d9-ha`), the same connections:
     ```yaml
     action: junghome_ble.assign_key
     data: { key_entity: <key>, target_entity: <tw light>, target_element: color_temperature }
     ```
     then `target_entity: <light>`, `mode: lock`, `lock_seconds: <the app's limit>`; `target_entity: <socket>`,
     `mode: lock`; `key_entity: <input>`, `target_entity: <socket>`; `target_entity: <light on <mini el>>`. Press the
     key after each as in step 1; after each lock link look at the key's event entity.
  3. Restart Home Assistant once while the key holds a lock link: the event entity asks the key for `0x5006` /
     `0x5007` again (`--src <ha> --grep '5006|5007'`).
  4. Restore: the key's and the input's noted connections (`clear_key`, `assign_key` back, or the app), every load
     unlocked.
- **Capture:** `--grep 'Publication|Subscription|5003|5006|5007|5008|0x0009'` in both sessions, side by side.
- **Pass:** Home Assistant's Config messages match the app's (Home Assistant's documented order: new wiring first,
  the clear after, the vendor writes once the Config plan was accepted); the `0x5006` / `0x5007` / `0x5008` values
  and KeyMode `3` match; the key changes the colour temperature (a), locks and unlocks the light and socket with the
  noted time limit (b, c; the loads' `locked` attribute follows), the input switches the socket (d); the event entity
  says `connection: lock` with `connection_lock_seconds` after (b) and (c) and again after the restart; after step 4
  `config audit` of the key's node shows no difference from the export. A difference in step 1 against the tests is
  a bug to fix before the markers go.
- **Markers:** `custom_components/junghome_ble/configurator/rooms.py::Keys.assign_key`,
  `custom_components/junghome_ble/configurator/wiring.py::plan_device_link`,
  `custom_components/junghome_ble/configurator/wiring.py::pick_target_element`,
  `custom_components/junghome_ble/configurator/rooms.py::Keys._write_lock_function`,
  `custom_components/junghome_ble/configurator/rooms.py::Keys._request_lock`,
  `custom_components/junghome_ble/configurator/wiring.py::TARGET_COLOR_TEMPERATURE`,
  `custom_components/junghome_ble/jhmesh/properties.py::key_lock_values`,
  `custom_components/junghome_ble/jhmesh/properties.py::key_lock`,
  `custom_components/junghome_ble/jhmesh/devices.py::KeyConnection`,
  `custom_components/junghome_ble/event.py::<module>`,
  `custom_components/junghome_ble/event.py::connection_attributes`,
  `custom_components/junghome_ble/strings.json::services.assign_key.fields.mode.description`,
  `custom_components/junghome_ble/strings.json::services.assign_key.fields.target_element.description`,
  `custom_components/junghome_ble/strings.json::services.assign_key.fields.lock_seconds.description`,
  `custom_components/junghome_ble/strings.json::selector.key_mode.options.lock`,
  `custom_components/junghome_ble/strings.json::selector.target_element.options.color_temperature`,
  `net:uc:setlockingfunctionconnection`, `net:uc:requestlockfunctionforcontrolkeyselection`,
  `net:enum:deviceconnection.element` (its LIGHT_TEMPERATURE half), `net:uc:configurepublicationsforpropertyuser`,
  `prod:key-mode:property`. The slat element needs a blind ([F](#f--not-checkable-here)).

### D10 · A key that only talks to Home Assistant, with a blueprint

- **Checks:** the user guide's recipe *A key that only talks to Home Assistant* (review-4 brief 43) and the key
  blueprints on such a key (brief 46): connected to an empty room, the key reports `press_on` / `press_off` and,
  held, `dim` with `hold_start` / `hold_end`, and switches nothing by itself.
- **Needs:** a key `<key>` whose connection you noted (see *Note what you will restore*), a `<light>`; a person.
  **Safety:** creates a room and rewires the key; both undone at the end.
- **Do:**
  1. `create_room` with the name `Home Assistant`, then `assign_key` with `key_entity: <key>`,
     `room: Home Assistant`, `mode: light`, as in the guide.
  2. An automation from *Key switches and dims lights* with `<key>` and `<light>`: press the upper half, the lower
     half; hold each half 3 s.
  3. Delete the automation; connect the key back to what it did before with `assign_key` (or `clear_key` if it had
     no function); `delete_room` `Home Assistant`.
- **Capture:** `--src <key el>`: what the key sends to the room's group (Generic OnOff Set, Level Move or Delta).
- **Pass:** the presses switch `<light>` on and off, the holds dim it up and down and stop on release; nothing else
  switches (the room is empty); afterwards the key does its old job again and the room is gone.
- **Markers:** none in the code (the guide's recipe); the key blueprints' sentence on it, cited under B12.

### D11 · Areas follow a room change (U4-2)

- **Checks:** review-4 U4-2 (brief 47) — with *Move devices along when their JUNG room changes* on, `set_room`
  moves the light's device, its node device and its keys' device to the new room's area, unless one was placed by
  hand; off, nothing moves.
- **Needs:** a `<light>` in room R1 whose devices are in R1's area, a second room R2. **Safety:** `<light>`'s room
  and the devices' areas change; set back in step 4.
- **Do:**
  1. *Configure*: switch *Move devices along when their JUNG room changes* on (the entry reloads).
  2. Move the *Push-buttons* device next to `<light>` to another area by hand (note it).
  3. `junghome_ble.set_room` with `<light>`, `room: R2`.
  4. Undo: `set_room` `<light>` → R1; move the *Push-buttons* device back; switch the option off again.
- **Pass:** after step 3 `<light>`'s device and its node device are in R2's area, the *Push-buttons* device stays
  where it was put; after step 4's `set_room` they are back in R1's area (with the option still on); the light
  never went `unavailable`.
- **Markers:** `custom_components/junghome_ble/model_update.py::async_sync_areas`,
  `custom_components/junghome_ble/strings.json::options.step.init.data_description.sync_areas`.

### D12 · Dry runs, and the answer of a real `assign_key` (W I3, W I6)

- **Checks:** a dry run sends nothing and lists what the real run then sends (review-4 brief 49); a real
  `assign_key` answers `applied` / `total` / `recorded` / `nodes` as counted on air, and leaves a logbook line.
- **Needs:** a key `<key>` whose connection you noted (see *Note what you will restore*), a `<light>`.
  **Safety:** the dry runs change nothing; the real run rewires the key, set back at the end.
- **Do:**
  1. Start a capture. *Developer tools → Actions*, *Return response* on: `assign_key` with `key_entity: <key>`,
     `target_entity: <light>`, `dry_run: true`; then `set_room` with `<light>`, a room it is not in and
     `dry_run: true`; `remove_device` with `<light>`'s device and `dry_run: true` (no `confirm`).
  2. The same `assign_key` without `dry_run`. Note the response; open the logbook.
  3. Connect the key back to what it did before (`assign_key`, or `clear_key` if it had no function).
- **Capture:** `--src <ha>` over the sitting.
- **Pass:** during step 1 the capture shows nothing from `<ha>` to any device but the usual reads, and the export's
  timestamp and the gateway's *Last export upload* do not move; in step 2 the Config messages from `<ha>` are those
  the dry run listed, in that order, followed by the KeyMode write; the response's `applied` equals `total`, equals
  the Config messages seen, `recorded` is true and `nodes` names the key's device; the logbook shows *… now drives
  …; N messages* with the same N.
- **Markers:** `custom_components/junghome_ble/configurator/executor.py::PlanExecutor.plan_response`.

### D13 · Pre-flight reconcile before a destructive plan (W I4)

- **Checks:** review-4 brief 70 — before a plan that removes or replaces what the export says a node holds, Home
  Assistant reads it from the nodes and stops on a difference; a dry run lists the differences under `preflight`.
- **Needs:** a key `<key>` wired to a `<light>`, the app. **Safety:** step 1 only reads; step 2 changes the key in
  the app and sets it back there.
- **Do:**
  1. `clear_key` with `key_entity: <key>`, `dry_run: true`, *Return response* on: the capture shows Model
     Subscription / Publication Gets from `<ha>` and no Set; `preflight.differences` is empty.
  2. In the app, connect `<key>` to another light (do not export). Repeat step 1: `preflight.differences` names the
     key's element, the Get and both addresses. Run `clear_key` without `dry_run`: it fails with *… the mesh export
     says …* and nothing is written. Connect the key back in the app.
  3. Review 5 (decision M17): `remove_from_room` of a `<light>` a key's room link drives, with `force: true` and
     `dry_run: true`: the Gets still go out and `preflight` is answered; with `skip_preflight: true` as well, no Get
     and no `preflight`. Then switch a `<light>`'s supply off until its entity is unavailable and run
     `set_room` on it with `dry_run: true`: the answer's `reachability.unreachable` names it; switch it back on.
- **Capture:** `--src <ha>` over the sitting.
- **Pass:** as described; no Config Set from `<ha>` in any step.
- **Markers:** `custom_components/junghome_ble/configurator/executor.py::PlanExecutor.preflight`,
  `custom_components/junghome_ble/configurator/store.py::ExportStore.dry_run`, `custom_components/junghome_ble/configurator/store.py::ExportStore.reachability`, `custom_components/junghome_ble/strings.json::services.remove_from_room.fields.skip_preflight.description`, `custom_components/junghome_ble/strings.json::services.delete_scene.fields.skip_preflight.description`, `custom_components/junghome_ble/strings.json::services.remove_device.fields.skip_preflight.description`, `custom_components/junghome_ble/strings.json::services.set_room.fields.force.description`, `custom_components/junghome_ble/strings.json::services.delete_room.fields.force.description`, `custom_components/junghome_ble/strings.json::services.assign_key.fields.force.description`, `custom_components/junghome_ble/strings.json::services.clear_key.fields.force.description`, `custom_components/junghome_ble/strings.json::services.remove_from_scene.fields.force.description`, `custom_components/junghome_ble/strings.json::services.set_threshold.fields.force.description`, `custom_components/junghome_ble/strings.json::services.delete_threshold.fields.force.description`, `custom_components/junghome_ble/translations/en.json::services.set_room.fields.force.description`, `custom_components/junghome_ble/translations/en.json::services.delete_room.fields.force.description`, `custom_components/junghome_ble/translations/en.json::services.assign_key.fields.force.description`, `custom_components/junghome_ble/translations/en.json::services.clear_key.fields.force.description`, `custom_components/junghome_ble/translations/en.json::services.remove_from_scene.fields.force.description`, `custom_components/junghome_ble/translations/en.json::services.set_threshold.fields.force.description`, `custom_components/junghome_ble/translations/en.json::services.delete_threshold.fields.force.description`.

## E · Credentials, sequence numbers and keys

**Not fully reversible.** Each item says what stays changed. Leave them for last, and skip any you would rather not
spend.

### E1 · Gateway re-authentication

- **Checks:** review-4 H I-2 — the re-authentication flow (`reauthentication-flow` in `quality_scale.yaml`).
- **Needs:** a gateway entry; the app. **Stays changed:** Home Assistant holds a new gateway token (the old one is
  revoked); nothing else.
- **Do:** in the app remove Home Assistant under *Settings → Gateway → Access permissions*; make Home Assistant ask
  the gateway (a change, `sync_gateway`, or wait for the status poll with the gateway status entities on); follow
  the re-authentication card once with the password empty (approve the request in the app within three minutes).
  Then `sync_gateway`.
- **Pass:** the repair *JUNG HOME Gateway no longer accepts Home Assistant* and the re-authentication card appear once;
  after the approval the repair clears, the entry is not reloaded, the export is not fetched again, and the sync
  uploads.
- **Markers:** `custom_components/junghome_ble/quality_scale.yaml::rules.reauthentication-flow.comment`.

### E2 · Backup and restore

- **Checks:** review-4 D5 — a backup marks every sequence-number record; a start that finds a mark it did not set skips
  ahead. Review-5 S5-1 — the skip is twice the address's measured send rate over the backup's age, at least 2^20; one
  that would pass the end of the sequence space sends nothing and raises *Restored backup too old*.
- **Needs:** Home Assistant's backups. **Stays changed:** 2^20 of the 2^24 numbers of the current IV index are spent
  (one sixteenth; more for a backup older than *minimum_covers_days*); everything else changed since the backup is
  rolled back by the restore, not by the integration.
- **Do:** download the diagnostics and note `local.send_rates` of `<ha>` (numbers a day, how many days the minimum
  covers); take a backup (*Settings → System → Backups*), note the *Sequence numbers used*; switch a light a few
  times; restore the backup.
- **Pass:** the backup completes (the log shows the records marked, at most a 10 s wait); after the restore the log
  warns that the record was restored from a backup, names its age in days and continues 2^20 past it (the age is well
  under *minimum_covers_days*), *Sequence numbers used* jumps by about 2^20, and the lights answer at once; no
  *Restored backup too old* repair. With the Supervisor, the same through its backup. The refusal itself (a backup
  older than *restore_covers_days*) is not checked here: it would leave Home Assistant mute until an IV Update.
- **Markers:** `custom_components/junghome_ble/backup.py::<module>`,
  `custom_components/junghome_ble/seq_store.py::SendRate`,
  `custom_components/junghome_ble/seq_store.py::_async_skip_restored_record`,
  `custom_components/junghome_ble/strings.json::issues.restore_too_old.description`.

### E3 · A new unicast address starts 2^20 in

- **Checks:** review-4 S I5 — an address without a record starts 2^20 numbers in when something says it may have sent
  before (here: the store knows Home Assistant's other address).
- **Needs:** a free address: no node's, not excluded, outside every provisioner's range, never used by the CLI (no
  `tools/.jhmesh_state_<ADDR>.json`), not `7FFF`. **Stays changed:** the new address has a record, 2^20 of its numbers
  spent; changing back continues the old address's counter.
- **Do:** *Reconfigure* → the new address; switch a light; *Reconfigure* back to `<ha>`; switch it again.
- **Pass:** the log says *Address XXXX has no sequence-number record, but …*; the devices answer at once on the new
  address and again on `<ha>` (no *devices ignore Home Assistant* repair).
- **Markers:** none in the code; the docs' *Changing the address* paragraph and the CHANGELOG bullet.

### E4 · A key renewal in the app (decision)

- **Checks:** review-4 D4 — the key refresh followed only on proof (`msg:op:8017`, the proxy's beacon); H4-4 —
  discovery recognises the mesh mid refresh and after; H I-6 — an old export gets *the network's keys were renewed
  after the export was made*.
- **Needs:** the maintainer's decision to renew the network key in the app; it re-keys every device. **Stays
  changed:** the network key. Run a capture through the whole renewal.
- **Do:** renew in the app; watch the log (each step names its proof) and *Settings → Devices & services*
  (no new *Bluetooth Mesh network* card); afterwards set up a test entry from an export taken before the renewal.
- **Pass:** Home Assistant moves at each proven phase and keeps working after Phase 3; no discovery card; the old
  export gives `export_keys_stale`. The devices Home Assistant added itself (D11) cannot be checked: there are none.
- **Markers:** `msg:op:8017`, `custom_components/junghome_ble/jhmesh/keyrefresh.py::<module>`,
  `custom_components/junghome_ble/jhmesh/keyrefresh.py::KeyRefreshFollower.beacon`,
  `custom_components/junghome_ble/coordinator.py::KnownMesh`,
  `custom_components/junghome_ble/coordinator.py::async_release_network_id`,
  `custom_components/junghome_ble/config_flow.py::mesh_proxies_without_match`.

### E5 · Approve a gateway API client (F4-17)

- **Checks:** review-4 brief 41 — `approve_gateway_client` lists the requests waiting at the gateway and approves
  the one named with `POST config {"data": {"api_client_accept": <name>}}` over the pinned connection.
- **Needs:** a gateway entry; a second API client that asks the gateway for access (the gateway integration being
  set up without a password, or a `POST /api/junghome/register` from a script on the LAN). **Stays changed:** that
  client holds a token for the gateway's whole API; revoke it in the app afterwards if it was only for the test
  (*Access permissions*; note that the app's reset revokes Home Assistant's token too, which E1 then renews).
- **Do:** start the client's request; within its three minutes run `approve_gateway_client` without `client`
  (response on), then with the name it listed.
- **Capture:** none on air (HTTPS); Home Assistant's log at INFO.
- **Pass:** the first call answers the name under `waiting`; the second `{approved: <name>, waiting: []}`, the client
  receives its token, the *Access requests* sensor drops to 0; the log names the client and never the token. A
  misspelt name is refused and approves nothing.
- **Markers:** `net:http:post-config:permissionsdto`,
  `custom_components/junghome_ble/gateway_api.py::JungHomeGatewayApi.approve_client`,
  `custom_components/junghome_ble/actions/audit.py::_approve_gateway_client`,
  `custom_components/junghome_ble/strings.json::services.approve_gateway_client.description`.

### E6 · Home Assistant starts an IV Update (P I-11)

- **Checks:** review-4 P I-11 — `start_iv_update` has Home Assistant move to the next IV index in *IV Update in
  Progress* and send its proxy the Secure Network beacon of it; Mesh Protocol 1.1 §6.7 has the proxy process it like
  any other beacon and beacon the new state back (the confirmation), and the mesh carry it on (§3.11.5, §3.9.4);
  96 to 144 hours later the mesh, and Home Assistant, are back in normal operation under the new index.
- **Needs:** the maintainer's decision. **Cannot be undone:** the IV index only goes up, for every device of the mesh
  and for the app and the gateway. Best done when the *sequence numbers running low* repair is open anyway; otherwise
  it spends nothing but moves the index once (`force: true`). Not within 96 hours of an IV change.
- **Do:** start the unattended capture (`docs/sniffer.md`) and keep it running for the whole update (about six days);
  note the *IV index* sensor; run `junghome_ble.start_iv_update` with `confirm: true` (and `force: true` unless the
  repair is open), response on — or, when the *sequence numbers running low* repair is open, repair it and confirm
  (review-5, brief 82: the same start, the same refusals); keep Home Assistant running. During the update switch a light from Home Assistant, the
  app and a rocker once a day; after it, the same.
- **Capture:** Home Assistant's beacon to its proxy (on the GATT link: Home Assistant's log at `jhmesh.trace: info`,
  `beacon sent: iv_index=<n+1> iv_update=True`), the proxy's beacon back (`beacon: iv_index=<n+1> iv_update=True`),
  the Secure Network beacons on the air (which nodes beacon `<n+1>` with the IV Update flag, and when), the IVI bit
  of the nodes' traffic, and later the beacons of normal operation (flag clear) and the first PDUs under `<n+1>`.
- **Pass:** the answer names IV index `<n+1>`, transmit index `<n>`, `confirmed: false`; within seconds the
  diagnostics' `local.iv_update.confirmed` turns true; the capture shows the proxy and then the other nodes beaconing
  `<n+1>` in progress; every load keeps answering Home Assistant, the app and the rockers throughout; 96 hours on
  Home Assistant logs *IV Update completed: back to Normal Operation*, its sequence numbers start over (*Sequence
  numbers used* near 0) and the devices still answer; by 144 hours every node beacons `<n+1>` with the flag clear, the
  *sequence numbers running low* repair is gone and the *Mesh sequence numbers used* sensor starts low. **Fail:**
  `confirmed` stays false (the proxy did not take the beacon: Home Assistant stays in progress and keeps transmitting
  under `<n>`, which the mesh accepts, until it gives the update up 144 hours after the start) — note the proxy node,
  its firmware and whether the capture shows the beacon reaching it.
- **Not taken (review-5 P5-3):** when `confirmed` stays false, leave it: 144 hours after the start Home Assistant
  logs *IV Update to IV index `<n+1>` given up*, is back at `<n>` in normal operation (*IV index* sensor, attribute
  `waiting_for_mesh_since` gone), raises the repair *JUNG HOME mesh did not take the IV Update* and stops its beacons;
  `local.iv_update.abandoned` is `not_taken`. Or end it earlier with `junghome_ble.abort_iv_update` (`confirm: true`):
  the same without the repair, `abandoned: aborted`. Either way the devices keep answering Home Assistant throughout
  (it never sent under `<n+1>`). An abort after `confirmed` turned true is refused (`abort_iv_update_taken`).
- **Late confirmation (review-5 P5-1):** if the proxy's beacon back came only after the start (a proxy within its own
  96 hours takes it later), the return to normal operation comes 96 hours after `local.iv_update.confirmed_at`, not
  after `started_at`.
- **Key refresh (review-5 P5-2):** not to be provoked; should the app run a key refresh during the update, the trace
  log's `beacon sent: … key_refresh=True` appears only while Home Assistant follows its Phase 2, and the app's refresh
  completes as usual.
- **Markers:** `custom_components/junghome_ble/actions/iv_update.py::<module>`,
  `custom_components/junghome_ble/actions/iv_update.py::_start_iv_update`,
  `custom_components/junghome_ble/actions/iv_update.py::_abort_iv_update`,
  `custom_components/junghome_ble/actions/iv_update.py::async_start_iv_update`,
  `custom_components/junghome_ble/repairs.py::StartIVUpdateFlow`,
  `custom_components/junghome_ble/jhmesh/client.py::ProxyClient.start_iv_update`,
  `custom_components/junghome_ble/jhmesh/client.py::ProxyClient.abort_iv_update`,
  `custom_components/junghome_ble/jhmesh/state.py::<module>`,
  `custom_components/junghome_ble/jhmesh/state.py::LocalState.start_iv_update`,
  `custom_components/junghome_ble/jhmesh/state.py::LocalState.complete_iv_update`,
  `custom_components/junghome_ble/jhmesh/state.py::LocalState.iv_update_overdue`,
  `custom_components/junghome_ble/jhmesh/state.py::LocalState.abandon_iv_update`,
  `custom_components/junghome_ble/jhmesh/state.py::LocalState.abort_iv_update`,
  `custom_components/junghome_ble/hub/issues.py::Issues.report_iv_update_not_taken`,
  `custom_components/junghome_ble/services.py::<module>` (its `start_iv_update` sentence),
  `custom_components/junghome_ble/strings.json::issues.iv_update_not_taken.description` (and every translation),
  `custom_components/junghome_ble/strings.json::issues.sequence_space_low.fix_flow.step.confirm.description` (and
  every translation),
  `custom_components/junghome_ble/strings.json::services.start_iv_update.description`,
  `custom_components/junghome_ble/strings.json::services.abort_iv_update.description`,
  `mgmt:api:meshnetwork.setivindex`.

### E7 · Download the export and import it into the app (U4-17)

- **Checks:** review-4 brief 76 — `download_export` answers a path signed for five minutes for the administrator who
  asked; the view serves the export on disk as `JungHome.json` to that administrator only; and the JUNG HOME app
  imports the file Home Assistant wrote, with the rooms, scenes and key connections Home Assistant changed.
- **Needs:** an administrator's browser; the app on a spare phone, or the production app after its own project was
  kept (*Project → Share via file*, the file stored safely). **Stays changed:** on the production phone the app's
  project is replaced by Home Assistant's file (import the kept file again to undo); nothing on a spare phone. Nothing
  is sent on the mesh; the download itself changes nothing.
- **Do:** *Developer tools → Actions → Download export*, *Return response* on; open the answered path after Home
  Assistant's address within five minutes; open it again after five minutes; open it from a private window while
  still valid (no login). Then import the downloaded file into the app and look at a room, a scene and a key
  connection Home Assistant changed earlier (D7 or D12 leave some); switch a light from the app.
- **Capture:** none on air; Home Assistant's log at INFO.
- **Pass:** the browser saves `JungHome.json`, byte for byte the file on the host; after five minutes the link answers
  401; the log says a link was made, for whom, and who downloaded it, never the link. The app imports the file
  without an error, shows Home Assistant's changes, and controls the devices. **Fail:** the app refuses the file or
  loses devices — note the app version and the error, and keep the file private. Delete the downloaded file afterwards.
- **Markers:** `custom_components/junghome_ble/actions/download.py::<module>`,
  `custom_components/junghome_ble/actions/download.py::_download_export`,
  `custom_components/junghome_ble/strings.json::services.download_export.description`.

## F · Not checkable here

| Why | Markers |
|---|---|
| **Energy puck** (none): its meter, counters, reset, energy history | `custom_components/junghome_ble/jhmesh/devices.py::meter_element`, `custom_components/junghome_ble/jhmesh/devices.py::Light`, `custom_components/junghome_ble/hub/energy.py::SENSOR_READINGS`, `custom_components/junghome_ble/hub/energy.py::PROPERTY_POWER_ON_TIME`, `custom_components/junghome_ble/hub/energy.py::Energy.reset_consumption`, `custom_components/junghome_ble/button.py::<module>`, `custom_components/junghome_ble/energy_history.py::<module>`, `custom_components/junghome_ble/sensor.py::<module>` (puck part) |
| **Blinds** (none): cover, lock function select, wind alarm, reference run, *All blinds*, scenes, *update entity*, a key on a blind's slats (`assign_key` `target_element: slat`, brief 38); the end positions, the slat refusal and the parameter gates (F4-16) | `custom_components/junghome_ble/strings.json::selector.target_element.options.slat`, `custom_components/junghome_ble/cover.py::JungHomeCover._update_read`, `custom_components/junghome_ble/cover.py::<module>`, `custom_components/junghome_ble/cover.py::JungHomeAllBlinds`, `custom_components/junghome_ble/const.py::COVER_LEVEL_OPEN`, `custom_components/junghome_ble/select.py::JungHomeLockFunction`, `custom_components/junghome_ble/binary_sensor.py::JungHomeWindAlarm`, `custom_components/junghome_ble/binary_sensor.py::JungHomeReferenceRun`, `custom_components/junghome_ble/actions/scenes.py::_store_scene`, `custom_components/junghome_ble/cover.py::JungHomeCover.async_open_cover`, `custom_components/junghome_ble/cover.py::JungHomeCover.async_close_cover`, `custom_components/junghome_ble/cover.py::JungHomeCover._set_slats`, `custom_components/junghome_ble/config_entities.py::VALUE_GATES`, `custom_components/junghome_ble/number.py::MODE_MINIMUMS`, `custom_components/junghome_ble/select.py::OFFERED_OPTIONS`, `prod:param:blind:move-blind-position-on-power`, `prod:param:blind:move-slat-position-on-power`, `prod:param:blind:on-power-up-behavior`, `prod:param:blind:slat-reversal-time`, `prod:param:blind:slat-ventilation-position`, `prop:0x1103`, `ui:vm:blindsviewmodel.blindsclosing`, `ui:vm:blindsviewmodel.blindsopening`, `ui:vm:blindsviewmodel.updateslatlevel` |
| **Room thermostats** (none): climate, window, *All thermostats*, key lock bits 3 / 4, *update entity*; the thermostat links and their gates, the boost poll and refusal, `0x1249`, key mode 4 (F4-16) | `custom_components/junghome_ble/climate.py::JungHomeClimate._update_read`, `custom_components/junghome_ble/climate.py::<module>`, `custom_components/junghome_ble/climate.py::JungHomeAllThermostats`, `custom_components/junghome_ble/binary_sensor.py::JungHomeRtrWindow`, `ui:vm:roomtemperatureviewmodel.changemode`, `custom_components/junghome_ble/climate.py::JungHomeClimate.extra_state_attributes`, `custom_components/junghome_ble/climate.py::JungHomeClimate._poll_boost`, `custom_components/junghome_ble/climate.py::JungHomeClimate.async_set_temperature`, `custom_components/junghome_ble/binary_sensor.py::JungHomeRtrSchedulerStatus`, `custom_components/junghome_ble/properties/reader.py::PROPERTY_SCHEDULER_ENABLED`, `custom_components/junghome_ble/config_entities.py::THERMOSTAT_GATED`, `custom_components/junghome_ble/jhmesh/devices.py::thermostat_links`, `custom_components/junghome_ble/configurator/wiring.py::KEY_MODE_CLIENTS`, `custom_components/junghome_ble/configurator/wiring.py::derive_mode`, `custom_components/junghome_ble/strings.json::services.assign_key.description`, `custom_components/junghome_ble/strings.json::services.assign_key.fields.mode.description`, `net:uc:getconnecteddevices`, `net:uc:isanyrtrdeviceconnected`, `net:uc:observertrconnectionmode`, `prod:key-mode:rtr`, `prod:param-gate:rtr-connection`, `prop:0x1249`, `ui:vm:devicesviewmodel.isactiondisabled`, `ui:vm:roomtemperatureviewmodel.requestboostfunction`, `ui:vm:roomtemperatureviewmodel.updateboostmode` |
| **Detectors** (none): walking test, illuminance, continuous on / off, *update entity*; the detector as a key source, the relay's continuous on / off, the PIR detents and 5 lx steps (F4-16); a detector's sensors in the presence blueprint (U4-3) | `blueprints/automation/junghome_ble/presence_lighting.yaml::blueprint.description`, `custom_components/junghome_ble/sensor.py::JungHomeDetectorIlluminance._read_now`, `custom_components/junghome_ble/switch.py::JungHomeWalkingTest`, `custom_components/junghome_ble/sensor.py::JungHomeDetectorIlluminance`, `custom_components/junghome_ble/sensor.py::JungHomeForcedOff`, `custom_components/junghome_ble/binary_sensor.py::DETECTOR_PROPERTY_PRESENCE`, `custom_components/junghome_ble/mesh_config.py::MeshConfigurator.assign_key`, `custom_components/junghome_ble/actions/keys.py::_resolve_key`, `prod:param:detector:switch-on-brightness`, `prod:ui:detector_connection_title`, `ui:ctl:detectorsensorviewmodel.pirsensitivity`, `ui:state:detectorforcedoffcapability` |
| **Battery nodes** (none): keep-awake, how long a transmitter stays awake, the sleep-mode sensor (F4-16); a wall transmitter's keys and node in the area of the room its keys switch (U4-2) | `custom_components/junghome_ble/device_info.py::gang_room`, `custom_components/junghome_ble/keep_awake.py::<module>`, `custom_components/junghome_ble/sensor.py::JungHomeSleepMode`, `ui:state:sleepmode` |
| **Mesh 1.1 privacy**: the installation's devices do not use it | `custom_components/junghome_ble/config_flow.py::proxy_in_range`, `custom_components/junghome_ble/jhmesh/client.py::classify_proxy_advert`, `custom_components/junghome_ble/jhmesh/client.py::ProxyClient._parse_beacon` |
| **A spare device** (none): `add_device`, `remove_device`, `reset_pending_device`, the vault, its key refresh (D2, D11, D15, D20, W4-7) | `custom_components/junghome_ble/onboard.py::<module>`, `custom_components/junghome_ble/onboard.py::_keep_key`, `custom_components/junghome_ble/onboard.py::async_reset_pending_device`, `custom_components/junghome_ble/actions/devices.py::_reset_pending_device`, `custom_components/junghome_ble/jhmesh/provisioning.py::<module>`, `mgmt:flow:checkformissingdevices`, `custom_components/junghome_ble/jhmesh/provisioning.py::provision`, `custom_components/junghome_ble/configurator/nodes.py::Nodes.remove_node`, `custom_components/junghome_ble/configurator/nodes.py::Nodes._reset_unconfirmed`, `custom_components/junghome_ble/vault_refresh.py::<module>`, `custom_components/junghome_ble/jhmesh/client.py::ProxyClient.request_config`, `custom_components/junghome_ble/strings.json::issues.pending_device.description`, `custom_components/junghome_ble/strings.json::issues.vault_unwritable.description`, `custom_components/junghome_ble/strings.json::issues.vault_unwritable_recorded.description`, `custom_components/junghome_ble/configurator/nodes.py::Nodes.record_node`, `custom_components/junghome_ble/strings.json::issues.vault_key_refresh_lagging.description`, `custom_components/junghome_ble/strings.json::services.reset_pending_device.description`, `msg:op:8016`, `msg:op:8045`, `net:alloc:element-group`; the commissioning by the app's rules from the node's own composition, the InsertId check, Time Set, the 30 s budget and the reset of a device `add_device` could not finish (brief 40: F4-13): `custom_components/junghome_ble/jhmesh/commission.py::<module>`, `custom_components/junghome_ble/onboard.py::_commission`, `custom_components/junghome_ble/onboard.py::_reset_new_node`, `custom_components/junghome_ble/strings.json::services.add_device.description`, `mgmt:cfgop:02`, `mgmt:err:configuredeviceerror.nodenotconfigured`, `mgmt:flow:deviceprovisioning`, `mgmt:flow:devicesetupprogress`, `mgmt:setup:provisioningaborting`, `mgmt:setup:settime`, `net:uc:connecttodevicetypegroup`, `net:uc:createelementconnectiongroups`, `net:alloc:element-group`, `mgmt:flow:removedevice` |
| **A two-channel insert of older firmware** (none: every node here has the Scene Action Setup server `0x0527:1017`): `store_scene` on its second channel is refused as the app refuses it (review-5 F5-2) | `custom_components/junghome_ble/configurator/scenes.py::Scenes._store_one`, `ui:msg:series_timer_scene_not_available_title` |
| **A spare device that offers Static OOB or the HMAC-SHA256 algorithm** (no JUNG device is known to; they offer No OOB with algorithm 0): the authenticated provisioning methods (brief 40: P4-8) — run `add_device` with its `static_oob` value and capture the Start (`02 01 00 01 00 00` for HMAC with Static OOB) | `mgmt:provauth:01`, `mgmt:provalg:fips_p_256_elliptic_curve` |
| **PP2 pucks** (none; the *Time keeper* switch only exists in a project with one): the time keeper's publication to `FEFF`, Time Role Set, the repair (brief 40: F4-14) | `custom_components/junghome_ble/switch.py::JungHomeTimeKeeper`, `custom_components/junghome_ble/configurator/nodes.py::Nodes.set_time_keeper`, `custom_components/junghome_ble/hub/issues.py::Issues.report_time_keeper`, `custom_components/junghome_ble/jhmesh/messages.py::time_role_set`, `custom_components/junghome_ble/strings.json::issues.time_keeper_missing.description`, `msg:op:8239` |
| **A spare app install**: the provisioner identity option; and the app write-backs (review-4 F4-6), shaped after the Android decompile while this installation runs the iOS app. *Open item for the maintainer:* store a scene with Home Assistant on a dimmer and the DALI light (`store_scene`), take the export (`export_network`, `flavour: share`), import it into a spare app install (never the production one: the import replaces everything) and check that the scene lists both members with the stored brightness and colour temperature; with a spare device too, check that a device `add_device` added shows its insert and key layout without the app asking it again, and that one `remove_device` removed leaves nothing behind | `custom_components/junghome_ble/strings.json::options.step.init.data.provisioner_identity`, `custom_components/junghome_ble/jhmesh/export.py::ProjectFile.set_scene_info`, `custom_components/junghome_ble/configurator/scenes.py::Scenes.store_scenes`, `custom_components/junghome_ble/jhmesh/export.py::ProjectFile.clone_property_rows`, `custom_components/junghome_ble/jhmesh/export.py::ProjectFile.exclude_node`, `net:export:meta.sceneinfo`, `net:export:meta.actuatorexports`, `net:export:meta.buttonlayoutexports`, `mgmt:flow:removedevice` |
| **Another client on Home Assistant's address** (S I2): the CLI refuses Home Assistant's address by design, and a client starting below Home Assistant's counter would not even be detected; provoking it means two clients on one address, i.e. reused nonces. *Decision for the maintainer:* leave it simulated (the docs' *to check it, run `tools/mesh_poc.py` with `--source` set to Home Assistant's address* cannot be followed as written) | `custom_components/junghome_ble/strings.json::issues.address_shared.fix_flow.step.confirm.description`, `custom_components/junghome_ble/strings.json::issues.address_shared_again.fix_flow.step.confirm.description` |
| **Repairs whose cause is not to be provoked here** (brief 44, U4-5): a node on Home Assistant's address, a key renewal Home Assistant did not follow, devices missing from an export set up from a file. The flows run in `tests/test_repairs.py` | `custom_components/junghome_ble/repairs.py::FreeAddressFlow`, `custom_components/junghome_ble/strings.json::issues.address_in_use.fix_flow.step.confirm.description`, `custom_components/junghome_ble/strings.json::issues.key_refresh.fix_flow.step.gateway_refetch.description`, `custom_components/junghome_ble/strings.json::issues.key_refresh.fix_flow.step.upload.description`, `custom_components/junghome_ble/strings.json::issues.export_stale.fix_flow.step.gateway_refetch.description`, `custom_components/junghome_ble/strings.json::issues.export_stale.fix_flow.step.upload.description`, `custom_components/junghome_ble/strings.json::issues.unknown_nodes.fix_flow.step.gateway_refetch.description`, `custom_components/junghome_ble/strings.json::issues.unknown_nodes.fix_flow.step.upload.description`, `custom_components/junghome_ble/strings.json::issues.unknown_nodes_gateway.fix_flow.step.upload.description` |
| **Home Assistant ahead of the mesh's IV index**: never happens on its own; not to be provoked | `custom_components/junghome_ble/strings.json::issues.iv_index_ahead.fix_flow.step.confirm.description` |
| **Which node starts an IV Update** (Mesh Protocol 1.1 §3.11.5, Mesh Profile §3.10.5: a node at risk of running out starts one itself): needs a sender to run low; not to be provoked. The unattended capture (`docs/sniffer.md`) records the update when it comes. Home Assistant starting one is E6 | `custom_components/junghome_ble/hub/issues.py::Issues.check_sequence_space` |

## G · Already seen on air

Review 3's on-air round (its plan's Status, *Phase 3*) saw `start_dim`, `stop_dim` and `step_dim` dim, stop and step
a dimmer (a −40 % step exact, the gateway agreeing to ±1). Their markers were dropped after review 4's wave 12:
`custom_components/junghome_ble/light.py::<module>` (its hold-to-dim paragraph),
`custom_components/junghome_ble/light.py::JungHomeLight.async_start_dim`,
`custom_components/junghome_ble/light.py::JungHomeLight.async_stop_dim`,
`custom_components/junghome_ble/light.py::JungHomeLight.async_step_dim`,
`custom_components/junghome_ble/light.py::DIM_MOVE_TRANSITION`,
`custom_components/junghome_ble/strings.json::services.start_dim.description`,
`custom_components/junghome_ble/strings.json::services.stop_dim.description`,
`custom_components/junghome_ble/strings.json::services.step_dim.description` (and every translation), and the
docs' *Hold-to-dim* sentence. B2 still runs them on the current build, where the commands are sent without waiting
for an answer (D32, `ui:uc:communicatewithdevice`); a tunable-white channel has not been dimmed this way.

## Results

Fill in a private copy; the second pass of brief 30 takes it from there (flip each passing row to `implemented`
citing the session and sequence number, open a regression test for each failure, remove the markers).

### Groups A–D, remote, CLI only

The maintainer's limits for this sweep: groups A–D only, everything set back in the same sitting; **no Home
Assistant API** (no token, no change to its configuration, no debug logging); nothing that needs a person at a key,
the app or a breaker; group E not run. The tools were the mesh CLI as its own client (`7FFF`, with `--ha-storage`,
connected through proxy nodes other than the one Home Assistant used), the unattended sniffer's capture, Home
Assistant's log at its configured INFO level and read-only views of its `.storage` (config entry, device registry,
repairs registry, sequence store). Hence most items that check Home Assistant's own behaviour are *needs HA access*;
where Home Assistant's own traffic in the capture settles part of an item, that part is recorded.

- **Sessions.** Both are stretches of the unattended capture (`docs/sniffer.md`), decoded on the capture host with the
  export Home Assistant uses. `sweep-ha`: Home Assistant's start on the build of this commit (the installed
  integration was identical to it) and its first link. `sweep-cli`: the CLI's steps (source `7FFF`). Home Assistant
  was restarted once more during the sweep, by someone else, on a newer working build; nothing below relies on that
  run.
- **Installation as found.** No dimmer insert (the DALI insert stood in where an item allows it); four DALI
  tunable-white lights; both metering sockets power running appliances (so no socket was switched); the entry is set
  up from an export file, not from the gateway. One push-button in the mesh is missing from the export (Home
  Assistant warns about it at start and holds *unknown_nodes*); it answers the Time Set like the others.
- **Restored.** Every value a step changed was written back and read back with the CLI in the same sitting: the
  DALI light's colour-temperature range, lock, *basic light function*, Lightness Last and colour temperature; the
  switch insert's lock, run-on time and LED colour; the meter's transmission settings; every load's on / off state.

| Item | Result (pass / fail / skipped / not checkable) | Session, sequence numbers | Notes |
|---|---|---|---|
| A1 | pass (capture part); the entity part needs HA access | `sweep-ha`: `<ha>` Scene Get `01BF01`–`01BF09` | After the scene-action and fault rounds, a *Scene Get* (`8241`, no parameters) went to every element of `scenes[].addresses` (eight; the one to Home Assistant's own proxy node is not repeated on the advertising bearer, its answer is) and every one answered *Scene Status* (`5E`, 3 bytes `00 <current u16 LE>`, status 0). `active_members` and the *Scenes* sensor: needs HA access. |
| A2 | partial: first link pass; second link not run | `sweep-ha`: `<ha>` Time Set `01BCFA`, location `01BCFB`, first state Get `01BCFC`, first *Scene Action Setup Get* `01BE77`, first *Health Fault Get* `01BEDC`, first *Scene Get* `01BF01` | First link in the specified order: *Time Set* and *Generic Location Global Set Unack* (both to `FFFF`) are the first two messages Home Assistant sends on the link; then the state Gets (OnOff, CTL, Sensor) with the property reads and the heartbeat publication Sets; then scene actions, fault registers, scene registers. The second link (the 15-minute freshness) needs Home Assistant's link ended, which the limits of this sweep left out (`bluetoothctl` on the HA host), and the link never dropped by itself during the sweep. |
| A3 | partial: first link pass; second link not run | `sweep-ha`: `<ha>` *Generic Manufacturer Property Get* `0x001A` from `01BD0B` | Each node got one software-version Get on the first link (every node but Home Assistant's own proxy node seen on air, for the reason in A1). As far as the capture shows, every repeated property Get to the same element and id was either the next attempt about 3 s after the first (Home Assistant had not received the answer through its proxy; for 13 of them no answer was heard on air either) or a read-back after a Set from the installation's own LED automation, never a second queue entry; the gateway answers `0x001A` with the property id alone and was asked twice that way. The second link: as A2. |
| A4 | partial | — | Over the sweep: no *Another client uses Home Assistant's JUNG HOME address* repair in the repairs registry, no *disconnect … timed out* warning and no lost link in the log, the CLI's own traffic notwithstanding. `address_shared` and `proxy_config_dropped` in the diagnostics: needs HA access. |
| A5 | partial | CLI `scan --adv` in `sweep-cli`; `sweep-ha`: no *InsertId* / *ButtonLayout* Get from `<ha>` | The JUNG adverts of 16 nodes caught in one scan; every one matched the export's InsertId and layout except one 1-gang push-button (`<node>`), exported as a switch insert (function 0) and advertising function 6 (extension insert). Home Assistant raised *insert_mismatch* at start (in the repairs registry), as the pass expects for a real difference; its device model for that node names the export's insert. Every push-button has an export InsertId, and no InsertId / ButtonLayout Get went out (expected: none). The device registry's models name the inserts and the buttons' layouts; whether they match what the app shows needs a person, the keys' `position` needs HA access. |
| A6 | partial | `sweep-ha`: `<ha>` Time Set `01BCFA`; `sweep-cli`: CLI *Time Get* (`8237`) to three nodes | 27 of the 29 nodes' *Time Status* to `<ha>` heard within seconds of the connect-time Time Set (two not heard), plus the node missing from the export. The probe (a short script over `jhmesh`, CLI address): each node answered *Time Status* with the zone Home Assistant sent and a clock 0.44–0.47 s behind the capture host's at reception (the round trip included). The daily read (Time Get, Time Zone Get, Location Get) needs a day of uptime and did not come; the *Clock offset* sensors, `clocks` and the repair need HA access; the node set ten minutes off was not done. |
| A7 | readings taken (nothing to pass or fail) | `sweep-cli` | Property lists: the DALI insert's primary serves admin `0x1008`, `0x1009`, `0x1011`–`0x1013`, `0x0009`, `0x100E`, `0x000E` and an id `0x1FFF` unknown to the catalogue (a Get returns the id alone), and its manufacturer server lists `0x0F01` / `0x0F02`; a key element serves `5003`, `5006`–`5008`, `5002`, `500A`, `0F00`, `500C` (no manufacturer ids, SIG servers silent); the meter element serves `5004`, `5005`, `0F00`, `5010`, `5011`, `5014` and the SIG energy ids. Runtime statistics `0x0F01` / `0x0F02`: the id alone on `<node>`, `<mini el>`, `<socket el>`, `<meter el>` and `<tw el0>` (even where listed): nothing that counts. `transmission_settings` `0100` on `<key el>`, `<input el>`, `<meter el>`; `key_toggle_enable` `01` on `<key el>` and `<input el>`. `<tw el0>`: hotel `33`, basic light `00`, night `33`, `presentation_mode_enable` `006f002008000000`, `presentation_mode_time` `b400002008000000` (8 bytes each). `<light el>` (switch insert): hotel the id alone. No light has a run-on time (Home Assistant's start reads `0x1007` 0 s everywhere), so the run-on step moved to C6 step 4. LED `[r][g][b][mode]`: on `04640000`, off `641b0000`. No dimmer: hardware not present. |
| A8 | partial | `sweep-cli`: CLI *Friend Get* `004261` → *Friend Status* not supported; *NetKey Get* `004262` → `[0]`; *AppKey Get* `004263` → `[0]` | `config audit` of a light node and a socket node printed `net_keys 0  app_keys 0`; on air one *NetKey Get*, one *AppKey Get netkey=0* and one *Friend Get* per run, the lists `[0]`, the light node's Friend Status *not supported*. The CLI printed `friend ?` (its link through a distant proxy lost several answers in both audits). `audit_network` and its `keys` / `settings.friend`: needs HA access. Besides the known phantom Scene Server / Scene Setup Server entries of `docs/hidden-features.md` §9, the audit of the light node found its key element's Light Lightness Client and Light CTL Client (`1302`, `1305`) subscribed to the element group the export does not list there. |
| A9 | needs HA access | — | The device registry's software versions are `2.2.0.2` on the push-buttons, `2.2.0.1` on the sockets and mini actuators. |
| A10 | needs HA access | — | The discovered-flows list is not readable without the API; this entry is set up from a file. |
| B1 | needs HA access | — | No *CTL Temperature Set* from `<ha>` during the sweep. |
| B2 | needs HA access | — | No load command from `<ha>` during the matching run; the dimmer and hold-to-dim steps: hardware not present (no dimmer). |
| B3 | needs a person | — | The app's change; the entity side needs HA access. |
| B4 | needs a person | — | Also needs HA access. |
| B5 | hardware not present | — | No dimmer; it needs a person too. |
| B6 | needs HA access | — | Entity availability and the link history; ending the link was left out (A2). |
| B7 | needs a person | — | A breaker. |
| B8 | partial: statuses only | `sweep-cli`: `<tw el0>` *Lightness Set* `0068E7`, `006AE9`, *CTL Set* `006CEB`, *OnOff Set* off `006EED`, on `0070EF`; `<light el>` on `0078F7`, off `007AF9` | Per kind, with 3 s: **DALI Lightness** fades: the answer carries target and remaining (2.8 s; the target rounded to the DALI's step, 6553 → 6425), the element group gets statuses about every 100 ms over the last half second and a final one at the end, and upwards to 65535 the same with a final status after 3 s. **DALI CTL Set**: answered at once with the new lightness and temperature, no target, nothing after: the transition is not reported. **DALI OnOff off**: a short Status (on), then *on, target off, remaining 0*, and off after about 3 s; **on**: at once (full level within 0.4 s, no transition). **Switch insert on**: at once; **off**: answered *on, target off, remaining 2.8 s*, off after 3 s (the relay waits the transition out). Whether anything visibly fades needs a person; the scene and the dimmer were not run (a recall to all nodes is not harmless here; no dimmer). Restored and read back. |
| B9 | needs HA access | — | Also needs the app. |
| B10 | needs HA access | — | `locate_node` is a Home Assistant action. |
| B11 | needs a person | — | A breaker. |
| B12 | needs a person | — | |
| C1 | partial: the probe, outcome (b) | `sweep-cli`: *Light CTL Temperature Range Set* `00A11A` → *Range Status* `19040D` (group), `19040E`; restore Set `00A31D` → `190412` | The CLI sent Home Assistant's message (acknowledged `826B`, parameters `34 08 70 17`, 2100..6000 K) to `<tw el0>`: answered at once by a *Range Status* (`8263`) status 0 with 2100..6000 K, to the sender and to the element group, and the Range Get read 2100..6000. **The DALI insert applies the range**: outcome (b), against the earlier probe's prediction (a). Set back to 2000..6000 and read back. Home Assistant's own Set from the entity, `min_color_temp_kelvin`, the DALI light's switch-on colour temperature: needs HA access; the dimmer's setup states: hardware not present. |
| C2 | needs a person | — | Also needs HA access. |
| C3 | partial: the probe without the app | `sweep-cli`: lock `00A722` (`<light el>`), `00BD3D` (`<tw el0>`); Sets while locked `00AB28` → Status `0E04C6`, `00BF40` → `190433`, `00C142` → `19043C`; refused unlock `00AF2C`; unlock `00B534`, `00C544` | **Lock** `02 01 00 00`: the Admin Property Status to the sender carries 8 bytes (`02 01 0000` and the 4-byte value), and **the load publishes an LBC *User* Property Status of `0x0009` to its element group**, twice; so a lock does reach others than the sender. **While locked**, an OnOff Set (switch insert, DALI) and a Lightness Set (DALI) are each answered with a Status showing the unchanged state (off / present 0), to the sender, and the load publishes its `0x0009` status to the group again; it keeps its state. This is the *answers with its old state* branch of §12, not silence. **Unlock with priority 0** (`00 00 00 00`) is refused: a Status with the property id alone, the lock stays. The app's unlock `00 01 00 00` works, and the read-back is `00 00 0000 …` (priority back to 0), exactly the value read before the probe. The app's lock and steps 2–4: needs a person / needs HA access; the dimmer: hardware not present (DALI insert used). |
| C4 | needs a person | — | Whether an input is wired is unknown from here. |
| C5 | needs HA access | — | |
| C6 | partial: steps 1, 3 (basic light), 4 and 5 (read-back); everything restored | `sweep-cli`: meter `0F00` Sets `010D8B`, `010F8D`, `01118F`, restore `011391`; basic light `00ED6D`, OFF `00F171` → CTL Status `190466`; run-on `00D757`, ON `00D959` → `0E04F3`, Get `00DB5B` → `0E04F5`; LED `00E565`, restore `00E969` | **1, meter rhythm:** `0000` is answered `0100`, `0101` is answered `0100`, `0200` is kept (read back `0200`); with each, over four minutes, the meter kept its rhythm (power, voltage and current about every 65 s, plus statuses on change), so no setting showed an effect in that window. Restored `0100` (read back), rhythm unchanged afterwards. **2, key toggling:** needs a person. **3:** with `basic_light_function_enable` `01`, an OFF Set leaves `<tw light>` on: the OnOff Status stays on and the CTL Status shows lightness 13107 (20 %), i.e. the hotel value `0x33` read as 51/255. Restored `00`; that OFF had also moved the light's Lightness Last to 13107 and its temperature to 2700 K, both set back (Last `ffff`, 2000 K, off; read back). The night value and the presentation ids were left as read (daytime; the night level needs the dark and a person). **4, run-on:** with `timed_on_duration` 20 s on `<light el>`, the ON Set's Status and a Get during the run-on are the short form (`present=ON`, no target or remaining time), and the light published OFF by itself about 20 s later: this firmware does not report the remaining time, so *Switches off at* stays unknown (the sensor can go). Restored 0 s. **5, LED:** `32143c00` (outside the app's palette) accepted and read back unchanged; whether the LED shows it needs a person. Restored `04640000` (read back). |
| C7 | needs HA access | — | This entry is set up from a file, not from the gateway. |
| C8 | needs a person | — | The phone and the app. |
| C9 | needs HA access | — | A reconfigure flow. |
| D1 | needs HA access | — | Also: both metering sockets power running appliances, so no harmless load. |
| D2 | needs HA access | — | Also needs a person at the key. |
| D3 | needs HA access | — | Also needs the app. |
| D4 | needs HA access | — | Also needs the app. |
| D5 | needs HA access | — | |
| D6 | needs HA access | — | Also needs the app and a gateway entry. |
| D7 | needs HA access | — | |
| D8 | needs HA access | — | |
| D9 | needs a person | — | The app's capture first; then needs HA access. |
| D10 | needs a person | — | Also needs HA access. |
| D11 | needs HA access | — | |
| D12 | needs HA access | — | |
| E1 | group E, excluded by the maintainer | — | |
| E2 | group E, excluded by the maintainer | — | |
| E3 | group E, excluded by the maintainer | — | |
| E4 | group E, excluded by the maintainer | — | |
| E5 | group E, excluded by the maintainer | — | |
