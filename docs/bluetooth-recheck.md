# Full Bluetooth recheck — every layer, what was verified, what is still open

Done (user away) against the live installation: 30 nodes (29 devices + gateway), firmware
2.2.0.1 / 2.2.0.2, keys from the app's share export of the same day. Instruments: the nRF sniffer on the HA host
(`sniffer.md`), bleak on a Mac in the flat, the CLI at address `0D03`, Home Assistant's own Bluetooth diagnostics
(three ESPHome proxies). Everything below is read-only or was written back unchanged; nothing was left modified.
Open items are collected in `roadmap.md` § "TODO from the Bluetooth recheck".

Legend: ✅ verified on air · 📄 from firmware / app code only · ❌ could not be done (see TODO) · ➖ not applicable.

## 1. Radio and advertising bearer

| Item | Status | Finding |
|---|---|---|
| Advertising PDU types in use | ✅ | `ADV_IND` (connectable proxy advert), `ADV_NONCONN_IND` (mesh PDUs, beacons, JUNG record), scan responses (name). No extended advertising, no coded PHY. |
| Proxy advert content | ✅ | Flags `06`, 16-bit UUID list `1828`, service data `1828` type `00` + Network ID; no name, no manufacturer data in it. ~28/s per node across the three channels (3 × transmit repeats). |
| Proxy advert address | ✅ | **The node's public MAC** (= the export's node UUID, EUI-64). Only Network PDUs and beacons use random non-resolvable addresses. |
| JUNG manufacturer record (`0xFF`, `0527`) | ✅ | `LBCAdvertisementData` type 3 every ~1.2 s from every device node (22 of 29 caught in 10 min; reception, not emission, varies with distance), same public MAC; not from the gateway. Decoded by `jhmesh.advert`; used by the hub (proxy naming, unknown-node repair). |
| Appearance (`0x19`) | ✅ | present on the JUNG-record adverts (`04C7`, `04C8`, `0780` seen) — not used. |
| Local name | ✅ | only in the **scan response** (`JUNG push-button 2gang`, `JUNG gateway`): bleak's active scan sees it, HA's passive ESPHome proxies never do (`name` = address in HA's history). |
| Network PDUs on air | ✅ | ~16 copies per PDU (relays × 3 repeats), TTL 5 originals, decrypt 100 % with the export keys, no foreign network in range. |
| Secure Network Beacons | ✅ | ~1 per 10 s network-wide (spec's adaptive rate), IV 0, no flags. |
| Unprovisioned Device Beacons / PB-ADV | ✅ | none in any capture (nothing is being provisioned). |
| Node Identity adverts | ✅ | none (all nodes stopped, Config Node Identity Get = 0); a node can be told to advertise it (not tested — write). |
| Other radios | ✅ | Daikin AC, Xiaomi `fe95`, an Apple iBeacon, ESPHome devices; **one JUNG 1-gang push-button `30:FB:10:60:A3:82` advertising the JUNG record with appearance but no mesh service (unprovisioned, or another network) at −70 dBm from the living-room-door proxy** — a spare / unpaired device? |

## 2. GATT (what anyone can connect to, no key)

| Item | Status | Finding |
|---|---|---|
| Connection | ✅ | no pairing / bonding, MTU 247, a node accepts **at least two concurrent GATT clients** (Mac + HA on `026E`, HA's link unaffected). |
| Services on devices | ✅ | Device Information `180A`, Silicon Labs OTA `1d14d6ee…` (control + data, write-only), Mesh Proxy `1828` (`2ADD` write-without-response, `2ADE` notify). No PB-GATT `1827` on provisioned nodes. GAP/GATT services hidden by macOS (not enumerable from the Mac). |
| DIS standard characteristics | ✅ | `2A29` `Albrecht Jung`, `2A23` System ID = EUI-64 (= node UUID / MAC), `2A27` hardware revision (`100`; `000` on the mini actuator), `2A28` software revision (`2.2.0.2` / `2.2.0.1`). |
| DIS JUNG characteristics | ✅ | `4c638383` product id, `83c49c8d` actuator function id, `19f789c3` secure_element_version, `8ec48c00` bootloader_version, `946a8bf1` schema versions (6 B; 8 B with a co-processor slot on the mini actuator) — the same values as the LBC Manufacturer properties `0002`/`0003`/`0004`/`000A–000C`. |
| Vendor channel `2f98a382` (write) / `a0dc3a44` (notify+read) | 📄❌ | present on every device (read of `a0dc3a44` times out; both readable on the gateway: 8 zero bytes / `00`); not in the app's UUID list; silent while the node's light was toggled and properties read. **Not probed with writes** (TODO, risky). |
| Gateway GATT | ✅ | DIS only + Mesh Proxy: product id 0, function id 9, secure element `10020100`, bootloader 2.4.0.2, software `0.0.0.0`, System ID zero, **no OTA service** (matches: no gateway update image). |
| OTA service behaviour | 📄 | write `00` to control, stream `.gbl` to data, write `03` — signed + encrypted images only (`transport-provisioning.md` §5.2). Not exercised. |

## 3. Mesh network / transport / proxy protocol

| Item | Status | Finding |
|---|---|---|
| Proxy filter | ✅ | blacklist + empty list forwards everything (our client); Filter Status names the proxy node; a proxy that drops the request (stale seq) is detected by the watchdog. |
| Segmentation / SAR | ✅ | segmented TX and RX with acks (2-segment property statuses, version reads); the gateway acks segment 1 with `block=1`, then both. SAR Configuration Server (Mesh 1.1) answers: Receiver `43 11 02`, Transmitter `71 72 11 03` (devices); the gateway does not answer SAR Gets. |
| Replay protection | ✅ | per-source SeqAuth on nodes (a burnt address is dropped silently — `0D00`); crpl 64 on devices, 720 on the gateway. |
| Heartbeats | ✅ | every node publishes when asked (Config Heartbeat Publication Set), 64 s, TTL 5, `features=0003`; all 29 reach HA (≤ 4 hops). Heartbeat Subscription works as a hop counter between two nodes (`config hops`; `hidden-features.md` §10). |
| Time | ✅ | all answering nodes within ±1 s (HA's daily Time Set), zone +180 min, TAI−UTC 37 s, Time Role client (3) on devices. |
| IV Update / Key Refresh | 📄 | IV 0, phase 0; IV Update following implemented and unit-tested, never seen on air (forecast: within 6–12 months) — a long passive capture would catch it. Key Refresh is followed from the provisioner's NetKey Update / Phase Set messages (unit-tested, never seen on air); a flagged beacon no known key authenticates raises the `key_refresh` repair hint (`ha-integration.md`). |

## 4. Models and states (application layer)

| Item | Status | Finding |
|---|---|---|
| Composition (all 30 nodes) | ✅ | matches the export; Mesh 1.1 SAR server on devices; the gateway also lists Remote Provisioning Server `0004`, BLOB `1400/1401`, Firmware Update `1402`, HSL client `1309`. |
| Property lists (LBC Admin / Manufacturer / User, SIG servers) | ✅ | `prop lists` on push-button, key, meter, socket, mini actuator, input, CTL and vendor-only elements and the gateway (`hidden-features.md` §2). |
| Codecs | ✅ | 99 / 99 listed property values decode and re-encode byte-identically (four nodes); 48 have no codec yet (`Raw`), `5014` unknown. |
| Property Sets (acknowledgement) | ✅ | Admin Set → unicast Status ~0.25 s, no publication; Set Unack works; the access byte is stored. SIG setup Sets: OnPowerUp / Lightness Default / CTL Default unicast, Lightness Range publication-only, **CTL Temperature Range Set unanswered** (`device-settings.md` §13 q.3). |
| State Sets | ✅ | OnOff Set: unicast reply only when nothing changed; statuses published twice (0.9–2.3 s); Sensor Status never doubled (`poc-gatt-proxy.md`). |
| SIG states read | ✅ | DTT, OnPowerUp, Lightness Default / Range / Last / Linear, CTL Default / Temperature Range, Scene Register (= export on 24/24 nodes), Scene Status, Health Attention / Period / Fault (registered vendor faults `0x81` on every device, `0x80` on 13 — historical, clearable, meaning unknown; `hidden-features.md` §10), Sensor Descriptor / Cadence / Settings / Series / Column, Time / Zone / TAI delta / Role, Location Global. Not served: Location Local, Battery (mains devices). |
| Config Server audit | ✅ | relay / network transmit / TTL / beacon / proxy identical on all 29 nodes and = the export; NetKey list `[0]`, AppKey list `[0]`, every model bound to AppKey 0 as exported; publications 52/52 = export; subscriptions = export **except Scene Server / Scene Setup Server** (export lists room + device-type groups the nodes do not hold — the nodes *accept* them when asked, the app never sent them; `hidden-features.md` §9–10). `config audit <node>` does this per node. |
| Vendor models | ✅ | JH Scheduler: every sub-command's status of an empty slot decoded (`sched`); Scene Action Setup: list + per-scene action decoded and verified on nine nodes (`scene-actions`: switch on/off, lightness + colour temperature); `0x000E` publish request makes a **dimmer** publish all four light states, switch inserts / sockets ignore it. |
| Remote Provisioning (gateway) | ✅ | Scan Capabilities / Scan / Link Get, SAR, Private Beacon, Large Composition Data, Models Metadata Gets (final Mesh 1.1 opcodes) all unanswered: the gateway's host process never initialises those models (`lbc-gw-bt-tunnel` in the SD dump), so the stack lists them and drops their messages. |
| Firmware Update models (gateway) | ➖ | not exercised (nothing to update with). |

## 5. Home Assistant's Bluetooth side

| Item | Status | Finding |
|---|---|---|
| Proxies HA hears the mesh through | ✅ | `livingroom-door-msr2-365f74` (24 nodes best, −39…−80 dBm) and `room-b-dk-msr2-5eac9d` (6 nodes); **`livingroom-msr2-c80da4` ("Living room Apollo 2") is registered as a scanner but `scanning: False`, never detected anything since HA started** — check that device. |
| What HA merges per node | ✅ | `service_data 1828` + `manufacturer_data 1319` on the same address (so `jhmesh.advert` works from HA's data), no name (passive). 30 JUNG devices in HA's history = 29 nodes + gateway; the stray push-button above too. |
| Discovery matcher | ✅ | `service_uuid 1828` finds every JUNG node (the gateway included); the flow checks the Network ID. Narrowing to `manufacturer_id 1319` considered and not needed. |

## 6. Sniffer coverage

| Item | Status | Finding |
|---|---|---|
| Advertising bearer capture + decode | ✅ | `tools/mesh_sniff.py` (NDJSON / pcap, live over ssh), 100 % decryption, copies collapsed, segments reassembled. |
| Wireshark | ✅ | our pcap is LINKTYPE_NORDIC_BLE as Nordic's extcap writes it; opening it in Wireshark was not verified in this recheck (no Wireshark on this Mac / docker down), but resolved the next day: TShark 4.2.5's `btmesh` dissector decrypts it with the keys, 2036/2036 Network PDUs (`roadmap.md` § "Tooling not finished", `sniffer.md` "Wireshark"). |
| Connection following (GATT capture) | 📄❌ | **implemented** (`capture --follow <MAC>`: L2CAP → ATT → proxy SAR → network / beacon / proxy-config / PB-GATT provisioning, unit-tested) but **no live connection caught yet**: three tries (Mac → `0148`, HA via ESPHome → `015E`) saw the node's adverts and no `CONNECT_IND` — the initiators are out of the dongle's reach. Move the dongle (USB extension) next to the phone / proxy (TODO). |
