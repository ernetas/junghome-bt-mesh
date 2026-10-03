# On-air verification sweep

What review 4 brief 30 asks the maintainer to check on the installation, in one sitting: every behaviour the code,
`docs/ha-integration.md`, the unreleased `CHANGELOG.md` section and the parity ledger still call *unverified on air*
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

- Debug logging for the integration (*Settings → Devices & services → JUNG HOME → Enable debug logging*, or
  `logger: logs: custom_components.junghome_ble: debug`); *Download diagnostics* at the end of each group.
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
- the connection of the key used in D2: its event entity's `connection`, `connection_address`, `connection_name`,
  its *Key mode* sensor, and `prop get <key el> key_mode`;
- which room each light used in D3 sits in.

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
| [B1](#b1--ctl-temperature-set-airaccess8264) | CTL Temperature Set (`air:access:8264`) | DALI TW light | light colour | — |
| [B2](#b2--commands-confirmed-by-their-status-d32-and-hold-to-dim) | D32 status matching, hold-to-dim | dimmer, DALI, socket | load states | — |
| [B3](#b3--a-colour-temperature-changed-elsewhere) | CTL Temperature Status from elsewhere | DALI TW light, app | light colour | — |
| [B4](#b4--gateway-mode-key-events-and-every-hold-ends) | Key events with the entity disabled; hold reasons | gateway-mode key | nothing kept | **yes** |
| [B5](#b5--holds-of-a-rocker-wired-to-a-dimmer) | Holds of a rocker wired to a dimmer | such a rocker | dimmer level | **yes** |
| [B6](#b6--link-loss-grace-re-send-short-links) | Link-loss grace, re-send, short-link penalty | ≥ 2 proxy nodes | nothing kept | — |
| [B7](#b7--a-plan-to-an-unreachable-device-is-refused) | Plan refused for an unreachable device | a light on its own breaker | power of one light | — |
| [B8](#b8--transitions-probe-f4-1) | Transitions probe: which loads fade | DALI, dimmer, switch insert, a scene | load states | **yes** |
| [B9](#b9--homeassistantupdate_entity-reads-the-device) | *update entity* reads the device | a light, a push-button, the app | a setting, restored | — |
| [C1](#c1--tunable-white-range-and-the-setup-states-msgop826b) | Colour-temperature range (`msg:op:826b`), setup states | DALI TW light, dimmer | settings, restored | — |
| [C2](#c2--device-lock-lock-operation-f4) | Device lock *Lock operation* (F4) | push-button | setting, restored | **yes** |
| [C3](#c3--lock-function-of-a-load-0x0009) | Lock function of a light (`0x0009`) | a light + its key | timed lock | **yes** |
| [C4](#c4--mini-actuator-inputs-f10) | Mini-actuator inputs (F10) | an input with a contact | setting, restored | **yes** |
| [C5](#c5--schedules) | Schedules, node clock and location | a light | a schedule slot, freed | — |
| [D1](#d1--socket-thresholds-netuccreatethreshold-togglethreshold-deletethreshold) | Thresholds create / disable / delete | socket + harmless load, a light | wiring, removed | — |
| [D2](#d2--key--scene-f15) | Key → scene (F15) | push-button key, harmless scene | key wiring, restored | **yes** |
| [D3](#d3--rooms-and-scenes-allocated-from-the-top) | Rooms and scenes allocated from the top | a light, the app | room / scene, removed | — |
| [D4](#d4--delete_unused_scenes-and-an-app-scene) | `delete_unused_scenes` and an app scene | the app | app scene, removed | — |
| [D5](#d5--sensor-values-for-iot-systems) | *Sensor values for IoT systems* | metering socket | publication, restored | — |
| [D6](#d6--both-sides-changed-merge-optional) | Both-changed merge (optional) | gateway, the app, a firewall rule | room names, restored | — |
| [D7](#d7--configuration-changes-without-a-reload) | Configuration changes without a reload | a light, a key | a room and a key, undone | — |
| [E1](#e1--gateway-re-authentication) | Gateway re-authentication | gateway, the app | the gateway token | — |
| [E2](#e2--backup-and-restore) | Backup and restore | HA backups | **2^20 sequence numbers** | — |
| [E3](#e3--a-new-unicast-address-starts-220-in) | New address starts 2^20 in | a free address | **2^20 numbers, an address used** | — |
| [E4](#e4--a-key-renewal-in-the-app-decision) | Key renewal in the app (decision) | the app | **the network key** | — |
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
- **Markers:** `custom_components/junghome_ble/coordinator.py::JungHomeHub._after_connect`,
  `custom_components/junghome_ble/coordinator.py::JungHomeHub._connect_step`,
  `custom_components/junghome_ble/const.py::CONNECT_STEP_FRESH`,
  `custom_components/junghome_ble/coordinator.py::JungHomeHub._send_location`.

### A3 · Software version once per start, property reads queued once

- **Checks:** review-4 R4-5 — each node's software version (*Generic Manufacturer Property Get* `0x001A`) is asked
  once per start, not on every link; a property read is queued once, not once per link.
- **Needs / safety / do:** A2's two links.
- **Capture:** `--src <ha> --grep '001A|software'`: the Gets on the first link only (the gateway polls `0x001A` too —
  filter on `<ha>`). On either link, no LBC property Get to the same element and property twice.
- **Pass:** no `0x001A` Get from `<ha>` on the second link (unless a node restarted in between), no duplicate reads.
- **Markers:** `custom_components/junghome_ble/config_entities.py::PropertyReader.schedule_version`,
  `custom_components/junghome_ble/config_entities.py::PropertyReader`.

### A4 · Passive checks over every capture

- **Checks:** that the sweep's own traffic trips none of the new guards: own PDUs relayed back are not taken for
  another client (review-4 S I2), proxy configuration PDUs pass the header and replay checks (P4-7), a disconnect
  never hangs (R4-11).
- **Pass:** at the end of the sitting the diagnostics show `address_shared` empty, `proxy_config_dropped` 0, no
  *Another client uses Home Assistant's JUNG HOME address* repair, and the log no *disconnect … timed out* warning.
  Provoking them is not possible here (see F).
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
- **Markers:** `custom_components/junghome_ble/coordinator.py::JungHomeHub._dim_hold`.

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
- **Markers:** `custom_components/junghome_ble/coordinator.py::JungHomeHub._drop_link`,
  `custom_components/junghome_ble/coordinator.py::JungHomeHub._load_command`,
  `custom_components/junghome_ble/coordinator.py::JungHomeHub._judge_link`.

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
- **Markers:** `custom_components/junghome_ble/mesh_config.py::MeshConfigurator._send`.

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
  `custom_components/junghome_ble/config_entities.py::DEVICE_LOCK_ENABLED` (bits 1 and 2 here; 3 and 4 are a
  thermostat's, see F).

### C3 · Lock function of a load (`0x0009`)

- **Checks:** the *Lock* switch and *Lock time limit* of a light (docs: *not yet tried on a real device*); what Home
  Assistant shows while a load ignores it (input for brief 35).
- **Needs:** `<light>` and the key that drives it; a person. **Safety:** a timed lock that ends by itself.
- **Do:** enable `<light>`'s *Lock* switch and *Lock time limit*; set the limit to 60 s; turn *Lock* on. Have the key
  pressed; switch `<light>` from Home Assistant. Wait 70 s.
- **Capture:** `--src <ha> --grep '0009|enforced'`: the Admin Set `02 01 3c 00` to `<light el>`, and the read-back
  about 65 s later.
- **Pass:** the light keeps its state against the key and Home Assistant while locked (note what the light entity
  and the action reported); the switch turns off by itself after the read-back; the key works again.
- **Markers:** the docs' lock-function paragraph (the code markers are the blinds', see F).

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
  `junghome_ble.get_schedules` on `<light>`; then `delete_schedule` for both slots. Enable the *Schedules* sensor.
- **Pass:** the light switches on at the set minute (the node's clock is right); the sunset slot's
  `effective_time` matches today's sunset at home within a few minutes (the node got the location); the sensor shows
  the slots and then none; nothing remains after the deletes.
- **Markers:** `custom_components/junghome_ble/sensor.py::<module>` (the *Schedules* sensor part).

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
- **Markers:** `custom_components/junghome_ble/mesh_config.py::MeshConfigurator._plan_scene_link`,
  `custom_components/junghome_ble/mesh_config.py::MeshConfigurator._write_scene_config`,
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
- **Markers:** `custom_components/junghome_ble/mesh_config.py::MeshConfigurator.delete_unused_scenes`.

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
- **Markers:** `custom_components/junghome_ble/mesh_config.py::MeshConfigurator.set_sensor_publication`.

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
- **Markers:** `custom_components/junghome_ble/mesh_config.py::MeshConfigurator._adopt`,
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
- **Markers:** `custom_components/junghome_ble/model_update.py::<module>`.

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
  2^20 numbers ahead.
- **Needs:** Home Assistant's backups. **Stays changed:** 2^20 of the 2^24 numbers of the current IV index are spent
  (one sixteenth); everything else changed since the backup is rolled back by the restore, not by the integration.
- **Do:** take a backup (*Settings → System → Backups*), note the *Sequence numbers used*; switch a light a few
  times; restore the backup.
- **Pass:** the backup completes (the log shows the records marked, at most a 10 s wait); after the restore the log
  warns that the record was restored from a backup and continues 2^20 past it, *Sequence numbers used* jumps by
  about 2^20, and the lights answer at once. With the Supervisor, the same through its backup.
- **Markers:** `custom_components/junghome_ble/backup.py::<module>`.

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

## F · Not checkable here

| Why | Markers |
|---|---|
| **Energy puck** (none): its meter, counters, reset, energy history | `custom_components/junghome_ble/jhmesh/devices.py::meter_element`, `custom_components/junghome_ble/jhmesh/devices.py::Light`, `custom_components/junghome_ble/coordinator.py::SENSOR_READINGS`, `custom_components/junghome_ble/coordinator.py::PROPERTY_POWER_ON_TIME`, `custom_components/junghome_ble/coordinator.py::JungHomeHub.reset_consumption`, `custom_components/junghome_ble/button.py::<module>`, `custom_components/junghome_ble/energy_history.py::<module>`, `custom_components/junghome_ble/sensor.py::<module>` (puck part) |
| **Blinds** (none): cover, lock function select, wind alarm, reference run, *All blinds*, scenes, *update entity* | `custom_components/junghome_ble/cover.py::JungHomeCover._update_read`, `custom_components/junghome_ble/cover.py::<module>`, `custom_components/junghome_ble/cover.py::JungHomeAllBlinds`, `custom_components/junghome_ble/const.py::COVER_LEVEL_OPEN`, `custom_components/junghome_ble/select.py::JungHomeLockFunction`, `custom_components/junghome_ble/binary_sensor.py::JungHomeWindAlarm`, `custom_components/junghome_ble/binary_sensor.py::JungHomeReferenceRun`, `custom_components/junghome_ble/services.py::_store_scene` |
| **Room thermostats** (none): climate, window, *All thermostats*, key lock bits 3 / 4, *update entity* | `custom_components/junghome_ble/climate.py::JungHomeClimate._update_read`, `custom_components/junghome_ble/climate.py::<module>`, `custom_components/junghome_ble/climate.py::JungHomeAllThermostats`, `custom_components/junghome_ble/binary_sensor.py::JungHomeRtrWindow`, `ui:vm:roomtemperatureviewmodel.changemode` |
| **Detectors** (none): walking test, illuminance, continuous on / off, *update entity* | `custom_components/junghome_ble/sensor.py::JungHomeDetectorIlluminance._read_now`, `custom_components/junghome_ble/switch.py::JungHomeWalkingTest`, `custom_components/junghome_ble/sensor.py::JungHomeDetectorIlluminance`, `custom_components/junghome_ble/sensor.py::JungHomeForcedOff`, `custom_components/junghome_ble/const.py::DETECTOR_PROPERTY_PRESENCE` |
| **Battery nodes** (none): keep-awake, how long a transmitter stays awake | `custom_components/junghome_ble/keep_awake.py::<module>` |
| **Mesh 1.1 privacy**: the installation's devices do not use it | `custom_components/junghome_ble/config_flow.py::proxy_in_range`, `custom_components/junghome_ble/jhmesh/client.py::classify_proxy_advert`, `custom_components/junghome_ble/jhmesh/client.py::ProxyClient._parse_beacon` |
| **A spare device** (none): `add_device`, `remove_device`, `reset_pending_device`, the vault, its key refresh (D2, D11, D15, D20, W4-7) | `custom_components/junghome_ble/onboard.py::<module>`, `custom_components/junghome_ble/onboard.py::_keep_key`, `custom_components/junghome_ble/onboard.py::async_reset_pending_device`, `custom_components/junghome_ble/services.py::_reset_pending_device`, `custom_components/junghome_ble/jhmesh/provisioning.py::<module>`, `mgmt:flow:checkformissingdevices`, `custom_components/junghome_ble/jhmesh/provisioning.py::provision`, `custom_components/junghome_ble/mesh_config.py::MeshConfigurator.remove_node`, `custom_components/junghome_ble/mesh_config.py::MeshConfigurator._reset_unconfirmed`, `custom_components/junghome_ble/vault_refresh.py::<module>`, `custom_components/junghome_ble/jhmesh/client.py::ProxyClient.request_config`, `custom_components/junghome_ble/strings.json::issues.pending_device.description`, `custom_components/junghome_ble/strings.json::issues.vault_unwritable.description`, `custom_components/junghome_ble/strings.json::issues.vault_key_refresh_lagging.description`, `custom_components/junghome_ble/strings.json::services.reset_pending_device.description`, `msg:op:8016`, `msg:op:8045`, `net:alloc:element-group` |
| **A spare app install**: the provisioner identity option | `custom_components/junghome_ble/strings.json::options.step.init.data.provisioner_identity` |
| **Another client on Home Assistant's address** (S I2): the CLI refuses Home Assistant's address by design, and a client starting below Home Assistant's counter would not even be detected; provoking it means two clients on one address, i.e. reused nonces. *Decision for the maintainer:* leave it simulated (the docs' *to check it, run `tools/mesh_poc.py` with `--source` set to Home Assistant's address* cannot be followed as written) | `custom_components/junghome_ble/strings.json::issues.address_shared.fix_flow.step.confirm.description`, `custom_components/junghome_ble/strings.json::issues.address_shared_again.fix_flow.step.confirm.description` |
| **Home Assistant ahead of the mesh's IV index**: never happens on its own; not to be provoked | `custom_components/junghome_ble/strings.json::issues.iv_index_ahead.fix_flow.step.confirm.description` |

## G · Already seen on air

Review 3's on-air round (its plan's Status, *Phase 3*) saw `start_dim`, `stop_dim` and `step_dim` dim, stop and step
a dimmer (a −40 % step exact, the gateway agreeing to ±1), yet the code, the action descriptions and the docs still
call them untried. After B2 re-confirms them on the current build, the second pass can drop these markers:
`custom_components/junghome_ble/light.py::<module>`,
`custom_components/junghome_ble/light.py::JungHomeLight.async_start_dim`,
`custom_components/junghome_ble/light.py::JungHomeLight.async_stop_dim`,
`custom_components/junghome_ble/light.py::JungHomeLight.async_step_dim`,
`custom_components/junghome_ble/const.py::DIM_MOVE_TRANSITION`,
`custom_components/junghome_ble/strings.json::services.start_dim.description`,
`custom_components/junghome_ble/strings.json::services.stop_dim.description`,
`custom_components/junghome_ble/strings.json::services.step_dim.description` (and `translations/en.json`), and the
docs' *Hold-to-dim* sentence.

## Results

Fill in a private copy; the second pass of brief 30 takes it from there (flip each passing row to `implemented`
citing the session and sequence number, open a regression test for each failure, remove the markers).

| Item | Result (pass / fail / skipped / not checkable) | Session, sequence numbers | Notes |
|---|---|---|---|
| A1 | | | |
| A2 | | | |
| A3 | | | |
| A4 | | | |
| B1 | | | |
| B2 | | | |
| B3 | | | |
| B4 | | | |
| B5 | | | |
| B6 | | | |
| B7 | | | |
| C1 | | | |
| C2 | | | |
| C3 | | | |
| C4 | | | |
| C5 | | | |
| D1 | | | |
| D2 | | | |
| D3 | | | |
| D4 | | | |
| D5 | | | |
| D6 | | | |
| E1 | | | |
| E2 | | | |
| E3 | | | |
| E4 | | | |
