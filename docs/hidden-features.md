# What the devices can do that the JUNG HOME app never uses

The app is one client of the mesh; the devices implement more than it shows. This is what the nodes of the user's
installation (30 nodes, firmware 2.2.0.x) really expose, established from three sources:

1. the **composition data** every node carries (the models in the export, confirmed on air with
   `Config Composition Data Get`),
2. the **property lists** the devices' six property servers hand out when asked (LBC "Properties Get"
   `C0/C6/CC 27 05` and SIG `Generic … Properties Get` — opcodes the app never sends; the LBC ones were only
   inferred from the gateway firmware until now, `cross-repo-analysis.md` §1.3 — verified: every node answered),
3. **read-only probes** of the SIG model states the app has no screen for (`tools/mesh_poc.py prop lists`,
   `prop get … --server sig_*`, and the scratch scripts behind the numbers below).

"Not used by the app" means: nothing in the normal UI. The app's hidden debug screens (`gap-analysis/network-features.md`
§11) reach a few of these (Relay / Network Transmit Set, Sensor Cadence, Time Role, Scene Register Get); they are
noted where relevant. Nothing here was *written*; every value below comes from a Get.

## 1. The composition: models per element

| element | models (SIG unless `0527:`) |
|---|---|
| **primary** (loc `0001`, every node) | Config Server `0000`, Health Server `0002`, **SAR Configuration Server `000E` (Mesh 1.1)**, Time Server + Setup `1200/1201`, Generic Location Server + Setup `100E/100F`, the three SIG property servers `1011/1012/1013`, the three LBC property servers `0527:1011/1012/1013`, JH Scheduler `0527:1016`, Scene Action Setup `0527:1017`; **loads** add Generic OnOff `1000`, **Default Transition Time `1004`**, **Power OnOff + Setup `1006/1007`**, Scene + Setup `1203/1204`; dimmers/DALI add Generic Level `1002`, Light Lightness + Setup `1300/1301`, Light CTL + Setup `1303/1304` (CTL Temperature `1306` on element 1) |
| **key** (loc `0040`–`0043`) | OnOff / Level / Lightness / CTL / Scene *clients* `1001/1003/1302/1305/1205`, LBC Property Client `0527:1015`, the six property servers |
| **vendor-only** (loc `0044`, push-buttons) | the four LBC models only — the app's `aux` element; its Manufacturer / User servers hold exactly **wind_alert_active `A200` / wind_alert_priority `A201`** (read on `014A`), i.e. this is the wind-alert element of a blinds insert, present on every push-button whatever the insert |
| **meter** (socket loc `0040`) | **Sensor Server + Setup `1100/1101`**, SIG property servers `1011/1012/1013`, LBC servers, OnOff client, Scene client |
| **gateway** (`00DC`, pid `0B`) | **Remote Provisioning Server `0004` (Mesh 1.1)**, **BLOB Transfer Server + Client `1400/1401` and Firmware Update Server `1402` (Mesh DFU)**, Sensor Client `1102`, Generic Battery Client `100D`, **Light HSL Client `1309`**, Property Client `1015`, Time Server, … |
| **phone** (provisioner `0001`) | Config / Health / Remote Provisioning / Private Beacon / SAR / DTT / Power OnOff / Location / Battery / Time / Scheduler *clients* — Nordic's library registers them; the app uses a fraction (`transport-provisioning.md` §1.2: Mesh 1.1 names only) |

Features: every device node `relay=1 proxy=1 friend=2 lowPower=2` (relay and proxy on, friendship unsupported), the
gateway `proxy=2 relay=2` (not supported — it is a plain node with its own radio). No SIG Scheduler Server (`1206`)
anywhere: the app checks for it but the devices only have the vendor JH Scheduler. No Light HSL server anywhere
either — the gateway's HSL *client* points at colour products that do not exist yet.

## 2. The property lists — and the energy counters nobody exposes

`tools/mesh_poc.py prop lists <addr>` (one Get per server, 3 s each). Ids in **bold** have no place in the app's UI.

**Push-button primary element (`0148`, 1-gang, insert = switch; `0232`, 2-gang, insert = DALI)** — LBC Admin:
device_lock `0001`, button_layout `5001`, LED triples `A000`–`A005`, automatic_dst `000F`, on/off delay
`1001/1002`, timed_on_duration `1007`, prewarning `100A`, manual_off_enable `100B`, invert_output `100C`,
switch_blocking_time `100D`, enforced_output `0009`; the DALI insert adds **hotel_dimm_value `1008` (= 51)**,
**night_dimm_value `1011` (= 51)**, **basic_light_function_enable `1009` (= 0)**, **presentation_mode_enable
`1012` (8 bytes `00 6f00 2008 000000`, RW)**, **presentation_mode_time `1013` (`b400 0020 08000000`)**,
dim_to_warm `100E`, **server_state_publish_request `000E` (empty)** and an unnamed **`1FFF`** (empty; a Get is
answered with the id alone, on-air sweep A7). The
switch insert's list ends in `1008` repeated five times — a firmware quirk (fixed-size list, unused slots carry
the last id). LBC Manufacturer (identical on every node): insert_id `0002`, secure_element_version `0003`,
bootloader_version `0004`, the schema versions `000A–000C`, **current / all-time runtime stats `0F01/0F02`**
(a Get is answered with the id alone everywhere it was asked, where listed too: nothing counts, §13). LBC User = Admin ∪ Manufacturer (read view). SIG Manufacturer / User: hardware_revision
`0010` (`"10000000"`), software_version `001A`, **date_of_manufacture `000C`** (SIG *Device Date of Manufacture*,
uint24 days since 1970: the socket `0172` reads `2022-09-22`), manufacturer_name `0011` (`Albrecht Jung GmbH &
Co.KG`). SIG Admin: empty on push-buttons.

**Key element (`0149`)** — LBC Admin/User: key_mode `5003`, key_property_mode `5006`, key_property_value_up/down
`5007/5008`, key_scene_config `5002`, **key_rtr_temp_step_size `500A` (`1e05`)**, **transmission_settings `0F00`
(`0100`)**, **key_toggle_enable `500C` (= 1)**. No manufacturer properties; the SIG servers do not answer on key
elements at all.

**Mini actuator (`0133`, pid `04`)** — the primary element lists what the switch insert does (incl. the `1008`
padding), its **input elements (`0134`/`0135`)** the key set plus `input_edge_detection 5009` (app: "edge
evaluation"); their Manufacturer server is empty and the User / SIG servers do not answer. The **CTL temperature
element** of a DALI insert (`0233`) has no property servers at all. The **gateway (`00DC`)**: Admin `key_event
5012`, Manufacturer `insert_id 0002`, `gateway_api_status/token/ip/fingerprint C000–C003`, `key_status_led 5013`;
its Sensor Server answers no descriptor.

**Socket main element (`0172`)** — as the push-button plus **total_off_on_cycles `100F` (= 118)** and
**power_on_cycles `1010` (= 79)** (LBC Admin, u32), LED triple `A000–A002`; SIG Admin: power_on_time `006D`.

**Socket meter element (`0173`) — the energy block:**

| server | ids | values |
|---|---|---|
| LBC Admin | turn_on_threshold `5004`, turn_off_threshold `5005`, transmission_settings `0F00` | `0F00` = `0100` |
| LBC Manufacturer | daily_energy_chart `5010`, monthly_energy_chart `5011`, **unknown `5014`** | `5014` = `7c00 0a03 0e2e2e` (7 B, identical across reads 45 s and 90 min apart — a static record, not a clock; chart metadata?) |
| **SIG Admin** | **`006A` total_energy** (RW: the app's "reset consumption" writes 0) | **210 040 Wh** |
| **SIG Manufacturer** | **`0072` precise_total_energy** (lifetime, RO), **`000D` energy_since_turn_on** | **210 198 Wh** (210 199 ten minutes later, load 3.7 W), 1 009 Wh |
| SIG User | `0072`, `006A`, `000D` | as above |
| Sensor Descriptor | `0052` present_input_power, `005C` output current, `005D` output voltage, `0057` present_input_current, `0081` active_power — all `tol=0 func=3 (RMS) interval=1.1^(0x50−64)=4.6 s` | `0052` / `0057` read 0 while `0081` = 3.7 W |

So **JUNG sockets do count energy** — the earlier "0x006A absent from every server" (`poc-gatt-proxy.md`,
`ha-integration.md`) came from asking the socket's *main* element; the counters sit on the meter element, exactly
where the Sensor Server is. `0x006A` is what the app shows and resets, `0x0072` the counter nothing resets: the
one to feed HA's Energy dashboard (`roadmap.md` step 8, re-opened). `0x0052/0x0057` are the gateway firmware's
`PresentDeviceInputPower` / `PresentInputCurrent` (`btmesh_sensor_ids.js`); they read 0 here, so the socket
measures only the load side.

## 3. SIG model states the app has no screen for

Read from `0148` (switch insert), `0232` (DALI) and `0173` (meter); all answered unless noted.

| state | value | what it would give |
|---|---|---|
| **Generic Default Transition Time** (`1004`, `0148`/`0232`) | 0 (no fade) | a device-side default fade for every Set without a transition — in theory. Written: the switch insert `0148` stores a `DTT Set` (5 s, read back, reset), the DALI insert `0232` ignores it (stays 0), so the fade idea is dead on the devices that could fade. |
| Generic OnPowerUp (`0148`) | 2 = restore | app-exposed (S5 "behaviour after mains return") |
| Light Lightness Default / Range / Last (`0232`) | `0xFFFF` (switch on at 100 %; *use last value* would be Default `0x0000`, Mesh Model spec, `gap-analysis/device-settings.md` §4.2) / 3084–65535 / `0xFFFF` | app-exposed (min brightness, switch-on brightness, "use last value") |
| Light CTL Default (`0232`) | l = `0xFFFF`, 2700 K, ΔUV 0 | app-exposed (default colour temperature) |
| **Scene Register** (`0148`) | scenes `[1, 5]` stored, current 1 | per-node view of which scenes it holds — the app keeps its own list and never asks (debug screen only) |
| **Health Attention / Period** (`0148`) | 0 / 0 | `Health Attention Set` = "identify me" — the app uses attention only in the provisioning invite. Written: `Attention Set 10` answered `10`, the timer read `7` three seconds later and `0` after eleven — now the *Identify* button of every node in HA. **The LED does blink** (the user watched the bedroom-door push-button `0297`; the node answered `Attention Status 10`). |
| **Health Fault Status** (`0148`, `0172`) | test 0, company `0527`, faults `[0x81]` | every node reports vendor fault `0x81`; meaning unknown (`0x80`–`0xFF` are vendor-specific codes). Worth watching across nodes / after a mains loss. |
| **Sensor Cadence / Settings / Series / Column** (`0173`) | status carries the property id only | the Sensor Setup Server is there but holds **no cadence**: the ~65 s publication rhythm is firmware, not configurable (`sniffer.md`) |
| Time (`0148`) | correct to the second (local zone offset as set in HA, TAI−UTC 37 s) | HA sends Time Set daily; **Time Role** = 3 (client). The app's "time keeper" switch is Time Role 2 + a publication to `FEFF` |
| Generic Location Global (`0148`) | not configured | set by the app only when an astro schedule is created, with the phone's GPS; HA could set `zone.home` once for every node |
| Generic Location Local, Generic Battery (`0148`) | no answer | not served (no Battery Server on mains devices) |

## 4. Config Server (device key) — everything the app leaves at provisioning defaults

Read from `0148` with `Config … Get`:

| state | value | hidden capability |
|---|---|---|
| **Heartbeat Publication / Subscription** | disabled (dst `0000`) | per-node liveness without polling: a Heartbeat every 30–60 s to a group tells HA a node is alive and how many hops away (the gateway polls instead; the app has no heartbeat code at all) |
| Node Identity | stopped | `Config Node Identity Set` makes one node advertise its identity for a while — a way to find *which* physical device an address is, without the app |
| Friend | not supported (2) | — |
| Network Transmit | count 2 (= 3 transmissions), 100 ms | debug screen only; the ~16 copies per PDU the sniffer sees are every relay in range repeating each PDU three times |
| Relay | on, retransmit 2 × 90 ms | debug screen only; switching relay off on well-placed nodes would thin the air |
| Composition page 1 | returns page 0 | only page 0 exists |
| **SAR Receiver** (Mesh 1.1) | `43 11 02` | the nodes implement the 1.1 SAR Configuration Server (segmentation timers); SAR Transmitter Get / Large Composition Data Get went unanswered |

Also standard and unused by the app's UI: `Config Beacon Set`, `GATT Proxy Set` (debug screens), **Key Refresh**
(a manual button in the debug screen only — `transport-provisioning.md` §4.2), `Node Reset`, `Default TTL`.

## 5. Firmware property ids the devices did not list

The gateway firmware's catalogue (`properties.md` §1.10) names ids for hotel / night / presentation modes (found on
the DALI insert above), **scene escape `1300–1303`**, **wind alert `A200/A201`**, **blinds step / wind-alert enable
`1109/110C`**, RTR cooling / holiday / floor-max `1202/1206/1207/120E`, detector constant-light control
`6018–601F` and night light `6020`, `BATTERY_CHANGED A100`, `LPN_STATE_TIMEOUT 0010`. None of these appeared in the
lists of the devices probed (push-buttons with switch / DALI inserts, a metering socket) — they belong to blinds,
thermostats, detectors and battery transmitters this installation does not have. `prop lists` on such a device is
the way to see which of them are real.

## 6. Outside the mesh: the GATT services every node exposes

Next to the Mesh Proxy service (`0x1828`) every JUNG node advertises and serves, to anyone who connects and without
any mesh key: the **Silicon Labs OTA service** (`1d14d6ee-…`, control `f7bf3564-…`, data `984227f3-…`) the app
uses for firmware updates, a Device Information Service and the storage-schema characteristic `946A8BF1-…`
(`transport-provisioning.md` §5.2). The images are signed and encrypted GBL files, so nothing but JUNG firmware can
be installed — but a connection can put a node into OTA mode (write `0x00` to control) and read its versions
without being part of the network. For us it means firmware updates *could* be driven from HA with the images
bundled in the APK (`android/…/assets/updates/`), and that version reads need no mesh traffic.

## 7. What to do with it (candidates, in value order)

1. ~~**Energy sensor** from `0x0072` (+ `0x006A`, `0x000D` as diagnostics)~~ **done the same day**: polled with the
   power-on hours, `total_increasing` kWh for the Energy dashboard (`roadmap.md` step 8, `hub.energy.COUNTER_READS`).
2. ~~**Heartbeat-based availability**~~ **done the same day** as the *Node heartbeats* option (`ha-integration.md`
   "Options"): Heartbeat Publication Set to every mains node (64 s period, to HA's address, TTL 5, persisted in the
   node), a node is dead after 3½ minutes without a beat or any message, its entities go unavailable; switching
   the option off sends the disabling Set. On air: `0148` accepted the Set (status Success), beat every
   4 s as asked (test period), `features=0003`, and stopped when the count ran out.
3. ~~**Default Transition Time**~~ **not worth it**: tested — the switch insert `0148` accepts `DTT Set`
   (5 s stored and read back, reset to 0 afterwards) but the DALI insert `0232`, the one that could fade, *ignores*
   it (status stays 0). JUNG dims with its own ramps; no entity.
4. **Health**: ~~an "identify" button~~ **done** (`button.<node>_identify`, `Health Attention Set` 10 s; the node's
   attention timer was seen counting 10 → 7 → 0 on `0148`). The fault array is a diagnostic *problem* binary
   sensor per node (`binary_sensor.<node>_fault`, read once per connection) with a *Clear faults*
   button beside it (Health Fault Clear + read-back); §10 for what is known about the codes — the meaning still
   is not.
5. **Hotel / night / presentation dimming** (`1008/1011/1009/1012/1013`) on dimmer/DALI inserts as expert config
   entities once the semantics are known (values suggest a percentage and a duration; the app's unused strings
   `device_parameter_hotel_*`, `night_light_*`, `presentation_mode` describe them, `device-settings.md` §13.10).
   **Done for the hotel function** (review-4 briefs 36 and 73, §13): the read-only reads were `on-air-sweep.md` A7,
   the supervised set-and-restore probe C6, the writes from Home Assistant's entities C10; the presentation mode is
   still not settled, so not exposed and never written by Home Assistant.
6. **key_toggle_enable `500C`** and `transmission_settings 0F00` as expert entities after a capture of what the
   app does not do with them — **probe pending** like item 5 (§13; the runtime statistics `0F01/0F02` with them).
   ~~**`5014`**~~ settled: the commissioning moment, the socket's *Installed* sensor (§10); **`1FFF`** is an empty
   placeholder on every server (§10). `0x000E server_state_publish_request`: no effect on switch inserts and sockets
   (§9), a one-message refresh of every light state on dimmers (§10) — settled, not needed as an entity (Home
   Assistant reads the states itself).
7. ~~**Location**: write HA's home coordinates to every Location Setup Server once (astro schedules without a phone).~~
   **done**: broadcast after every connection right after Time Set (`coordinator._send_location`, one
   unacknowledged Generic Location Global Set to all nodes — every node has the Location Setup Server on its
   primary element); `create_schedule` also sends it to the node before an astro schedule, as the app does.
8. **Firmware update from HA** over the Silabs OTA service with the APK's images — feasible, but a separate,
   careful project (re-provisioning after storage-schema changes, `transport-provisioning.md` §5.2 step 1).

## 8. On air outside the mesh PDUs — what a node tells anyone (sniffer + one GATT connection)

Advertising survey (`adv_survey.py`, `jung_adv.py` on the capture host; 60 s + 180 s):

- The proxy advertisement of every node is a connectable `ADV_IND` with Flags `06`, the 16-bit UUID list `1828`
  and service data `1828` type `00` + Network ID (~28 per node per second across the three channels). No local
  name in it — the name (`JUNG push-button 2gang`) is in the scan response.
- **It is sent from the node's public MAC address** (no random address: the advertiser address of every proxy
  advert equals the MAC the export encodes in the node UUID). Only the mesh Network PDUs (`0x2A`) and beacons
  (`0x2B`) use the random non-resolvable addresses the mesh spec asks for.
- Every 1.2 s (2.4–13 s on a few nodes) the same public address also sends an `ADV_NONCONN_IND` with a **JUNG
  manufacturer-specific structure** (`0xFF`, company `0527`): the `LBCAdvertisementData` **type-3 record** the
  app documents for *unprovisioned* devices only (`transport-provisioning.md` §2.3) — product id, actuator
  function id, button layout and the MAC again. 19 distinct records in 3 minutes, each matching a node of the
  export by MAC: `2705 03 0400 0000 0100 fac0d7c1c75c` = mini actuator `017D` (function 0, layout 1),
  `2705 03 0200 0600 0200 33214547b660` = 2-gang `028A` (function 6, layout 2), `2705 03 0300 0000 1400 …` = the
  metering socket `0172` (layout 0x14). Decoder: `jhmesh.advert`.
- Everything else was other people's radios: a Daikin AC, Xiaomi `fe95`, an Apple iBeacon, ESPHome names.

**Consequences, built the same day:** `JungHomeHub.node_for_address` maps every visible proxy to its node before
any message is exchanged (the *Proxy node* sensor and the diagnostics' `visible_proxies` name the node at once;
the Filter Status only confirms it), and a proxy advert carrying **our Network ID from a MAC the export does not
know** is a node added or re-provisioned after the export — reported once as the repair issue *JUNG HOME devices
missing from the export* with the product from the JUNG record (`ISSUE_UNKNOWN_NODES`; the `dynamic-devices` rule
as far as an export-based integration can go). HA's Bluetooth discovery could match `manufacturer_id` 1319 too,
but the `0x1828` matcher already finds every JUNG node and the flow verifies the Network ID, so it was left alone.

**The GATT database of a node** (bleak, no key; `0148`, `0293`): besides the Mesh Proxy service (`2ADD`/`2ADE`)
and the Silicon Labs OTA service (control `f7bf3564`, data `984227f3`, both write-only), the **Device Information
Service** holds, readable by anyone who connects: `2A29` Manufacturer Name `Albrecht Jung`, `2A23` System ID = the
EUI-64 = the node UUID / MAC, `2A27` Hardware Revision `100`, `2A28` Software Revision `2.2.0.2`, and five JUNG
characteristics that mirror LBC Manufacturer properties — `4c638383…` = product id (`0100` / `0200`),
`83c49c8d…` = actuator function id, `19f789c3…` = secure_element_version (`0d020100`), `8ec48c00…` =
bootloader_version (`00000402` = 2.4.0.0), `946a8bf1…` = the three schema versions (`0500 0200 0100`, the one the
app reads before an update) — plus a **write characteristic `2f98a382…` and a notify/read one `a0dc3a44…` the app
never touches** (its UUID list in `de/jung/common/c.java` has neither): a vendor command/response channel, silent
while the node's light was toggled and its properties read over the mesh. Not probed with writes.

Privacy / security notes for the record: a JUNG installation is trackable and inventoriable from the street — public
MACs, product types, insert functions and firmware versions, no key needed — and the OTA control characteristic
accepts a write from anyone (images are signed, so only JUNG firmware installs, but a node can be put into OTA
mode). The mesh traffic itself stays properly encrypted and address-randomised.

## 9. Remote-safe pass without the user at home: inventory, acknowledgements, audit

All read-only or idempotent (values written back unchanged), CLI address `0D03`, sniffer recording the air side.

**Inventory of the 30 nodes** (a one-off `inventory.py` script, one connection):
- Firmware: every push-button (pid 1 / 2) on **2.2.0.2**, every mini actuator (pid 4) and socket (pid 3) on
  **2.2.0.1**; the gateway has no SIG property server to ask.
- Clocks (`Time Get`): 27 nodes answered, all within **−0.8 … +0.9 s** of this Mac — HA's daily `Time Set` does its
  job; two nodes did not answer within 3 s.
- Scene registers (`Scene Register Get`): **24 of 24** answering load nodes hold exactly the scenes the export lists
  for their elements.
- Config Server (device key: Relay, Network Transmit, Default TTL, Beacon, GATT Proxy): **identical on all 29
  nodes** — relay on with 3 × 90 ms retransmit, network transmit 3 × 100 ms, TTL 5, beacon on, GATT proxy on —
  the gateway too, although the export's node entry says "proxy not supported" (and it does advertise `0x1828`).

**Codec round trip** over every property the Admin and Manufacturer servers of `0148`, `0172`/`0173`, `0232` and
`0133` list (`audit.py`): 99 values decode and re-encode to the same bytes, 48 have no codec yet (`Raw`), the two
energy charts are read-only by design, `5014` is the one unknown id; no decode error anywhere.

**Publication / subscription audit against the export** (`tools/mesh_poc.py config audit <node>`, new): all 52
publications match; 44 of 52 subscription lists match; **the 8 that differ are all Scene Server (`1203`) and Scene
Setup Server (`1204`) entries** — the export records room groups and the device-type group (`FEF5` / `FEF8`) on
them, the nodes hold only the element group on `1203` and nothing on `1204`. Phantom CDB entries, harmless in
practice: the app, the rockers and HA recall scenes to `0xFFFF` (`network-logic.md` §4.3), never to a room group.
Worth remembering when `export.py` mirrors the app's room wiring. The on-air sweep's audit of a light node (A8)
found the other direction too: its key element's Light Lightness Client and Light CTL Client (`1302`, `1305`)
subscribe to the load's element group, which the export does not list on them. Harmless as well: a client that
hears the load's statuses. The audit (`jhmesh.audit`, `audit_network`, `config audit`) therefore reports a client
model (Generic OnOff / Level / Default Transition Time / Power OnOff, Scene, Light Lightness / CTL / HSL client)
subscribed to the element group of a load on its own node — the address the export has the load's state servers
publish to — as a note of its own kind, `client_subscriptions`, kept apart from the findings and not counted; any
other extra subscription, on a client or not, stays `subscriptions_extra` (review-4 brief 72; unverified on air).

**How Sets are acknowledged** (`probe_sets.py`, `probe_sig_sets.py`; the matrix `device-settings.md` §13 q.3 was
waiting for): LBC Admin Property Set → unicast Status in ~0.25 s, no publication, value changed or not (except the
lock function `0x0009`, whose change a load also publishes to its element group, §12); Admin Set Unack (`C4`,
first use) applied silently; the Set's access byte is stored (userAccess 1 → 3). SIG: OnPowerUp Set, Lightness
Default Set, CTL Default Set → unicast Status; Lightness Range Set → group publication only (×2, like OnOff Set);
CTL Temperature Range Set → no answer, no publication, no change in this pass. **The on-air sweep (C1) found
otherwise**: the acknowledged Set `34 08 70 17` (2100..6000 K, Home Assistant's message) to the DALI insert's CTL
element was answered at once with a *Range Status* (`8263`, status 0, 2100..6000 K) to the sender and to the
element group, and the Range Get read the new range; set back to 2000..6000 K the same way. The DALI insert's range
is not fixed.

**`0x000E server_state_publish_request`** (Admin Set `01` to `0148`): acknowledged with an empty Admin Status,
**no publication followed** in the next 6 s — not the one-message refresh hoped for, at least not with that value.

## 10. Firmware corners: the roadmap's "left unexplained" list worked through

Every write below was reverted in the same run (Health publications, subscriptions, heartbeat settings back to
what HA had set); the scratch scripts were `corners.py`, `faults*.py`; the results are in the library
(`jhmesh/vendor_models.py`, health / heartbeat-subscription messages) and the CLI (`scene-actions`, `sched`,
`health`, `config hops`).

**Scene Action Setup (`0x0527:1017`) — decoded and verified on nine nodes.** The status is exactly what the app
code said (`android/vendor-models.md` §4.2): `scene u16` then `action code` + a 40-bit payload; scene 0 asks for
the **list** of scenes the element has an action for (`00 00 08 00 09 00 0A 00 0B 00 00 00` = scenes 8–11,
terminated by 0; a scene-less DALI insert answers `00 00 00 00`); an unknown scene answers with the bare scene
number (`FF FF` = no action). Read off the air: switch inserts store `01 01` / `01 00` (*switch on / off* — "WC
off" = off, "WC on" = on, exactly the app's scene names), the dimmer `016A` stores `03 FF FF D0 07 00` (*lightness
100 %, 2000 K*) for the ON scenes and `03 00 00 D0 07 00` for the OFF ones. A 2-byte scene number in the Get works as
well as the app's 4-byte one. `tools/mesh_poc.py scene-actions <element>` lists them with the app's scene names.
**This is the one piece of scene state the export does not hold** (the CDB's `scenes[]` only has the members).

**JH Scheduler (`0x0527:1016`) — empty-slot statuses of every sub-command.** Sub 0 (schedule) answers 8 zero-ish
bytes, sub 1 (action) 7, sub 2 (effective time) 8, sub 15 (list) 6 with the `centralScheduleId` echoed in byte 1
(`F0 01 …` for id 1) and 16 × 2-bit slot states all *available*; an **undefined sub-command (3) echoes its header
byte alone** (`30`) and a parameterless Get answers an empty status — so the "1-byte status" the app handles is the
node's "I do not know that command", not "slot empty". Full decoders / builders in `vendor_models.py`; nothing to
verify the *filled* layouts against here (no schedules on this installation) — the bit layout is the app's.

**Health faults `0x81` / `0x80` — historical, clearable, meaning still unknown.** A `health FFFF` survey (one
group Get, every node answers): the gateway reports **none**; every device reports vendor fault **`0x81`**, and 13
of 27 (`012E 0153 015E 017D 017F 018A 01A1 01A4 01C7 01F4 0208 0210 0247` — push-buttons and mini actuators alike,
no sockets) additionally **`0x80`**. Facts established on `0148` / `01A4`:
- they are *registered* faults, not current ones: a temporary Health Server publication to the CLI showed
  `Health Current Status … faults=[none]` every 2 s on `0148`; on `01A4` (faults `81 80`) no Current Status was
  published at all in 10 s at period 1–2 s — odd, noted;
- **Health Fault Clear removes them and they do not come back** (hours later still empty); `health <node> --clear`
  sends it and reads back with a Fault Get. An earlier note here said the acknowledged Clear is *not answered* by
  unicast — that observation was made while `messages.py` had the two Clear opcodes swapped: the "acknowledged"
  request it sent was `0x8030`, which is Health Fault Clear *Unacknowledged* per Mesh Profile §4.3.4.1, so no
  answer was the expected result, and the "unacknowledged" one `--clear` sent was really `0x802F`, the
  acknowledged Clear (the form seen to clear). Fixed (`0x802F` = Clear, `0x8030` = Clear
  Unacknowledged; `--clear` still sends `0x802F`); whether JUNG nodes answer the acknowledged Clear with a unicast
  Fault Status is **not observed yet**;
- **Health Fault Test accepts any test id** (0–5, 0x80, 0x81, 0xFF) and reports no fault; the id becomes the
  "most recent test" in later statuses; **test 0 leaves an explicit `0x00` (*no fault*) entry** in the array until
  the next Clear / other test;
- not caused by: Attention (identify), Heartbeat Publication Set (HA's), a ±1 h Time Set, replayed
  (stale-sequence) unicasts, GATT connections, OnOff toggling, the Health tests, our probes (`0148` had
  `81` only after most of them);
- no node rebooted during hours of probing (sequence numbers' high bytes unchanged — each element's sequence
  number jumps to the next `0x010000` block on boot, so the high byte counts boots), yet the two groups exist —
  the split is not from today. Hypothesis: `0x81` is registered at boot (power-on), `0x80` by a later event some
  nodes saw (a brownout on one phase? an over-temperature?); nothing in the app, the gateway middleware or the
  host tunnel touches Health faults, so only a reboot / mains event under observation can settle it.
  **Watch**: `0148` and `01A4` are clean now — if `0x81` reappears without a reboot, the boot theory is wrong.

**Property `5014` on the meter element = a stored timestamp**, per device: `7C 00 0A 03 0E 2E 2E` on socket
`0172`'s meter, `7C 00 0A 0C 16 28 0F` on `0174`'s — `[year-1900 u16][month][day][h][m][s]` → **2024-10-03
14:46:46** and **2024-10-12 22:40:15** (local time; the same bytes read again 45 s later, so not a clock). Served by
the Manufacturer *and* User servers (access 1), not by Admin; not present on the socket's load element nor on a
push-button. **The commissioning moment**: the user confirmed that the sockets went in around those
dates — so it is the socket's *Installed* sensor in HA now (diagnostic, read once per link). Codec `Timestamp7`,
name `meter_timestamp`. `1FFF` is
empty on every server of every node asked (a placeholder id); `5015`, `5016`, `1FFE`, `2000` do not exist.

**`0x000E server_state_publish_request` does work — on dimmers.** Admin Set `000E = 01` to the DALI insert `0232`
made it **publish all four light states at once** (Light CTL, Lightness, OnOff, Level Status to its group, each
twice as usual) within 0.1 s. The switch insert `0148` (values `00 02 03 FF`), the socket `0172` and its meter
element ignore it (Admin Status echo, nothing published). The read-back is always empty (a write-only trigger). So
it is the app's one-message refresh for a dimmer's state; HA could use it after connecting instead of four Gets
(not done — the Gets work for every product).

**Gateway Mesh 1.1 models are dead weight.** Remote Provisioning Scan Capabilities / Scan / Link Get, SAR
Transmitter / Receiver Get, Private Beacon, Private GATT Proxy, On-Demand Private Proxy, Large Composition Data and
Models Metadata Gets (the final 1.1 opcodes `804F`–`8077`) — all unanswered, with the device key, every reply from
`00DC` watched. The reason is in the SD dump: the gateway's host process `lbc-gw-bt-tunnel` (v1.2.21.1) initialises
only `node`, `vendor_model` (the LBC property servers / clients), `lbc_adv` (the JUNG advertising record) and DFU —
no `remote_provisioning_server_init`, no SAR server, nothing for the DFU models (`strings` of the binary; the
middleware issues 24 NCP commands, none of them). Silicon Labs' stack lists a model in the composition when it is
compiled in but drops its messages until the host initialises it. The devices' own firmware does initialise the
SAR server (they answer) and has no Private Beacon / Large Composition Data server (they do not).

**Phantom Scene Server subscriptions: the nodes take them, the app never sent them.** Subscription Add of the room
group `C00F` to `0148`'s Scene Server `1203` **and** Scene Setup Server `1204` → `Success`, the Get shows the address
(deleted again; the export's phantom entries are exactly these). So the firmware has room in those lists and the
export's room / device-type entries on `1203` / `1204` are app-side bookkeeping that was never written to the
device — `mesh_config.py` may one day sync them; harmless either way since scenes are recalled to all-nodes. The
OnOff server also took a fourth address (`C0FE`, removed again): the lists are not full.

**Heartbeat Subscription = a hop counter (topology probe).** `Config Heartbeat Subscription Set` (opcode `803B`,
`src, dst, PeriodLog`) on the counting node with `dst = FFFF` is accepted; the source is told to beat 4 × every 2 s
to all-nodes with InitTTL 127; the Subscription Status (`803C`) then reads `count_log=3 hops=min..max`. Measured:
`01A4 → 0148` **1 hop** (both in the WC), `0174 → 0148` and `0148 → 0174` **2 hops** (socket "Wine fridge" ↔
WC light), while the CLI's own proxy saw the same beats at 1–2 hops. `tools/mesh_poc.py config hops <counter>
<origin> [--beats N --period S]` does the whole dance and puts the origin's heartbeat publication back to what it
had (HA's `0D02 / 64 s`) and switches the counter's subscription off. **The full 29 × 28 matrix** (`config
hopmatrix`: each node beats in turn while all the others count, three config exchanges in flight; 812 pairs in
10.5 min) is in `hop-matrix.md`: 83 % of the pairs at 1 hop, the rest at 2, nothing farther —
one flat cell.

**Scene Store / Delete acknowledgements** (checked for the HA scene actions, `0148`): *Scene Store* is answered
**both** by a unicast Scene Register Status and by a publication of the same status to the element's group (`C061`),
0.1 s apart — the one state-changing Set seen so far that does reply by unicast; *Scene Action Setup Set* answers with a
unicast Scene Action Setup Status. The HA configurator still falls back to a Register / Action Get when nothing arrives.

**A node reboot, caught live (`0133`, switch actuator mini):** HA marked it unavailable after 224 s; a
direct probe showed it alive with its sequence number in the next `0x010000` block (`D4…` → `D5 0004`), an **empty
heartbeat publication** and the registered fault `0x81` — the same register it had before the reboot, so a reboot
clears the heartbeat setting (RAM) and registers nothing new (`0x80` is *not* "the node restarted"). HA now re-sends
the heartbeat configuration to a missing node every two minutes (`_reprobe_dead`) instead of waiting for the
six-hour renewal. Why the mini rebooted is unknown (a mains blip on its circuit?); the boot counter in the sequence
number is the way to spot such events. `0148` / `01A4` were still clean more than two hours after they were cleared — `0x81` is not
re-registered periodically either. **Mains loss registers nothing either**: the boiler socket `0172` (`[0x81]`)
had its breaker off for ten seconds and still holds `[0x81]`. So neither `0x80` nor `0x81` is a
power or restart record. What the power cut did show: on boot the socket's main element **publishes its OnOff
Status five times** in four seconds (`present=ON`, the power-on state) and the meter follows with its readings;
each element's sequence number moved to the next `0x010000` block on its own (`0172`: `…→0B0000`, its meter `0173`:
`25A5FA→260002`) — the boot counter is per element.

**Left open from the list**: the vendor GATT channel `2f98a382` / `a0dc3a44` (writes only with a device to spare),
and everything about devices this installation lacks.

## 11. Transitions on a Set (statuses probed)

Neither the app nor the gateway ever sends a transition time: the gateway's OnOff and CTL Temperature Sets carry
transition 0, every other Set none, and the nodes' Default Transition Time is 0 (§3; the DALI insert ignores a DTT
Set, §7.3). Whether a JUNG load fades a Set that carries one (Mesh Model §3.1.3: bits 7-6 the step of 100 ms, 1 s,
10 s or 10 min, bits 5-0 the steps 0..62), ignores the transition, or ignores the whole Set is **not known yet**
(review-4 F4-1). What Home Assistant does with the results below is decision M19 (review 5): the option *Fade
brightness changes*, off until a person watched a fade.

The probe, with someone watching the light and `tools/mesh_poc.py listen` (or the sniffer) running alongside, on the
DALI tunable-white insert, a dimmer insert and a switch insert:

    tools/mesh_poc.py lightness <element> 6553 --transition 3      # dim down over 3 s
    tools/mesh_poc.py lightness <element> 65535 --transition 3     # and back up
    tools/mesh_poc.py ctl <element> 65535 2700 --transition 3      # the tunable-white light
    tools/mesh_poc.py set <element> off --transition 3             # the switch insert; then on again
    tools/mesh_poc.py scene FFFF <scene> --transition 3            # a scene with these loads in it
    tools/mesh_poc.py delta <element> -16384 --transition 3        # only if a Lightness transition was ignored

For each: does the light fade, does the Status answering the Set carry the target and a remaining time (the CLI
prints `target=… remaining=…`), and does a final Status follow at the end of the fade? A Set that gets no answer at
all means the load ignores a Set with a transition: that kind must stay out of `const.TRANSITION_KINDS`. The
results go here.

**What the statuses showed** (on-air sweep B8, CLI only, 3 s each; nobody watched the lights, the dimmer insert and
the scene were not run — there is no dimmer here, and a recall to all nodes is not harmless):

| load, Set | answer | after it |
|---|---|---|
| DALI, Lightness Set down / up | target and remaining time (2.8 s), the target rounded to the DALI's step (6553 → 6425) | statuses to the element group about every 100 ms over the last half second, a final one at the end (after 3 s) |
| DALI, CTL Set | at once, the new lightness and temperature, no target | nothing: the transition is not reported (whether it fades is not known) |
| DALI, OnOff Set off | a short Status (on), then *on, target off, remaining 0* | off after about 3 s |
| DALI, OnOff Set on | at once | full level within 0.4 s: no transition |
| switch insert, OnOff Set on | at once | — |
| switch insert, OnOff Set off | *on, target off, remaining 2.8 s* | off after 3 s: the relay waits the transition out |

So every kind answers a Set with a transition; the DALI insert's Lightness Set reports a fade the way the Mesh
Model specification describes, the CTL Set and the switching on do not, and a switch insert delays its switching
off by the transition. **Decided (review 5, M19):** a transition only on a brightness change of a dimmer or DALI
light that is on, as a Lightness Set (`const.TRANSITION_KINDS`, `JungHomeHub.set_lightness`); never on a switch
insert, a CTL or CTL Temperature Set, an on or an off, or the *All lights* groups (whose Lightness Set also switches
the members that are off on); a scene recall only when every member of the scene is a dimmer or DALI light (a
switch insert or socket in it would only switch off late). Built behind the option *Fade brightness changes*, off
by default: whether anything visibly fades still needs a person (sweep B8's remaining step), and the dimmer insert
and a scene were not run.

A key in scene mode carries a transition of its own (KeyModeSceneConfig `0x5002`, `[scene u16][transition u32 ms]`;
the app writes 0, `mesh_config.py` too): writing one from `assign_key` waits for this probe to show that a recalled
scene fades, and for the key-scene check of the on-air sweep (review-4 brief 30).

## 12. A locked load and a Set (probed without the app)

A load locked by its lock function (`0x0009` EnforceOutput: the app's *Lock*, a key in lock mode, Home Assistant's
*Lock* switch) keeps its state against its keys, scenes and remote commands. Review-4 F4-2 asked what it **answers**
to an acknowledged Generic OnOff or Light Lightness Set while locked — a Status with its unchanged state, or nothing
at all — and whether a lock reaches anyone but the client that set it.

**What the probe showed** (on-air sweep C3, CLI only, on a switch insert and on the DALI insert; each unlocked again):

- **Lock** (Admin Set `02 01 00 00`): the Admin Property Status to the sender carries 8 bytes, `02 01 0000` and a
  4-byte value; and **the load publishes an LBC *User* Property Status of `0x0009` to its element group**, twice. A
  lock does reach others than the sender.
- **While locked**, an OnOff Set (switch insert, DALI) and a Lightness Set (DALI) are each answered, to the sender,
  with a Status showing the unchanged state (off / present 0), and the load publishes its `0x0009` Status to the
  group again. It keeps its state. No Set went unanswered.
- **Unlock with priority 0** (`00 00 00 00`) is refused: a Status with the property id alone, the lock stays.
  `00 01 00 00` (the app's unlock) works, and the read-back, `00 00 0000` and the value, is exactly what was read
  before the lock: an unlocked load reports priority 0.
- Not seen: a lock set in the app itself, a timed lock running out, a dimmer insert (none here).

Home Assistant reads every light's and socket's lock once per link and refuses commands to a load known to be locked
(`config_entities.LoadLock`). It takes the load's published `0x0009` Status like any vendor Status, so a lock set
by another client shows at once; a command a locked load answers with its old state counts for the Set when no
other request is out to the load (review-4 D32), so the entity reads the lock and reports the refusal — for a
brightness, the level must show (present, or the target of a fade), so a locked light that is on and answers with
its old level is refused too; the lock the load publishes on the refused Set counts as fresh, so no Get follows
it (review-4 brief 72, unverified on air); and an unlock
from Home Assistant never carries priority 0 (`LockFunctionEntity.async_unlock`: the plain unlock when no lock is
known, else the lock's own priority). Silence from a load known to be locked still does not mark it unreachable
(`Liveness.missed_answer`), for an answer lost on the air.

The probe, for a dimmer insert or a lock set in the app, with `tools/mesh_poc.py listen` (or the sniffer) running
alongside, each load unlocked again at the end:

    tools/mesh_poc.py prop get <element> enforced_output                  # unlocked: command 00
    tools/mesh_poc.py prop set <element> enforced_output hex:02010000     # lock the current state, no time limit
    tools/mesh_poc.py set <element> on                                    # the state it is not in (or off)
    tools/mesh_poc.py lightness <element> 30000                           # the dimmer / DALI insert only
    tools/mesh_poc.py prop set <element> enforced_output hex:00010000     # unlock

Then lock the same load in the app (device page, *Lock*) and unlock it again, watching `listen`: the `0x0009`
Status to the element group should appear as it did for the CLI's lock.

## 13. Firmware-only properties (probed in part)

Review-4 brief 36 (F4-3, F4-9, F4-10, F4-11). What is established, from §2, §9, §10 and the on-air sweep's A7 (read
only), C6 (set and restored, CLI only, nobody at the keys) and C10 (the hotel function's entities, written from Home
Assistant and restored):

| id | name (gateway firmware) | where it was read | value | settled? |
|---|---|---|---|---|
| `0x000E` | server_state_publish_request | every node | write-only trigger | **yes** (§10): a dimmer publishes all its light states; others ignore it. No entity: Home Assistant reads the states itself |
| `0x0F00` | transmission_settings | key elements, mini-actuator inputs, socket meter (LBC Admin) | `0100` | no — on the meter `0000` is answered `0100`, `0101` is answered `0100`, `0200` is kept; with each, over four minutes, the meter kept its rhythm (power, voltage and current about every 65 s, plus statuses on change), so no setting showed an effect |
| `0x0F01` / `0x0F02` | current / all-time runtime stats | every node's LBC Manufacturer server | the id alone everywhere asked (a push-button, a mini actuator, a socket and its meter, the DALI insert), where listed too | **yes**: nothing counts; no entity |
| `0x500C` | key_toggle_enable | key elements, mini-actuator inputs (LBC Admin) | `01` | no — whether `00` stops a single key toggling needs a person at the key |
| `0x1008` / `0x1011` | hotel / night dim value | DALI insert only (a switch insert answers `0x1008` with the id alone) | `33` / `33` | hotel: **yes**, a level in 1/255 (`0x33` = 51 = 20 %, below; 30 % written from Home Assistant left the light at 76/255); night: the layout yes (written from Home Assistant and read back), not when it applies — that needs the dark and a person |
| `0x1009` | basic_light_function_enable | DALI insert only | `00` | **yes**: with `01`, an OnOff Set off leaves the light on at the hotel value (OnOff Status on, CTL Status lightness 13107 = 20 %), and moves its Lightness Last there and its colour temperature to 2700 K; from Home Assistant too (C10: the switch on, `light.turn_off` left the light entity on at 76/255, the hotel value 30 %) |
| `0x1012` / `0x1013` | presentation mode enable / time | DALI insert only | 8 bytes each (`006f002008000000`, `b400002008000000`) | no — layout unknown, left as read; **never enabled unattended** |
| OnOff Status `[present][target][remaining]` | run-on time `0x1007` | a switch insert, run-on 20 s | the short form | **yes**: the On Set's Status and a Get during the run-on carry no target or remaining time, and the light publishes off by itself at the end; this firmware does not report the time left |
| `0xA0xx` | LED mode `[r][g][b][mode]`, 0..100 | push-buttons, sockets | on `04640000`, off `641b0000` | in part: `32143c00`, outside the app's palette, is accepted and read back unchanged; whether the LED shows it needs a person |

What Home Assistant does with them (review-4 brief 73): an allow-list, `properties/targets.py` `FIRMWARE_ENTITIES`,
of firmware-only ids a probe settled — only those become config entities, disabled by default, and only with a
codec. It holds the DALI insert's hotel function, on a push-button's tunable-white load only, named after the
app's declared but unshown strings: `0x1009` the switch *Hotel function* (`Bool`), `0x1008` the number *Hotel
function brightness* and `0x1011` the number *Night-light brightness* (`Percent`: one byte in 1/255, shown 0–100 %).
The sweep's C10 wrote all three from those entities and read them back, and with the switch on an off from Home
Assistant left the light lit at the hotel brightness, the light entity on, as the device reported; the night value's
effect is unverified on air. The rest stay `Raw` and unexposed: `0x0F00` (no effect seen and no
meaning in the app's notes), `0x500C` (no such setting in the app; its effect needs a person at the key), `0x1012` /
`0x1013` (layout unknown). The LED colour outside the palette gets no free-colour entity: the colour select keeps
the app's palette (and LED 2 following LED 1 while synchronised), and a second entity writing the same property
would fight it; such a colour shows as `unknown` there. The runtime statistics sit on the Manufacturer server in
the catalogue, where every node lists them; and every light and socket has a *Switches off at* sensor (disabled by
default, read-only) from the remaining time of an OnOff Status heading off. A run-on time
is not reported that way (above); a transition to off is (a switch insert switched off with a 3 s transition answers
*on, target off, remaining 2.8 s*, §11). So the sensor counts the run-on time itself (review-5 F5-9): a load whose
`0x1007` was read non-zero and that is seen switching on is off that long after it, started again by every on it
publishes (unverified on air).

The probe, in two halves of `on-air-sweep.md`: **A7** reads every id above (twice for the statistics, minutes apart)
and the OnOff Status of a light whose run-on time is set, right after its key switched it on — read-only, any time;
**C6** sets and restores them with someone at home (the meter's rhythm under `0000` / `0200` / `0101`, a key with
`500C = 00`, the basic-light enable and night value on the DALI insert, the presentation time field, a 20 s run-on
time, an LED colour outside the palette). What is still open: the key toggling, the night value, the presentation
ids, whether the LED shows a colour outside the palette, and the dimmer insert (none here). The outcomes are here and
in `android/properties.md` §1.10; the settled ids are entities (above).
