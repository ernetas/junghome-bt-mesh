# Proof of concept: controlling JUNG HOME over Bluetooth only (GATT proxy client)

Host: macOS (CoreBluetooth via bleak 3.0.2), no gateway involved.
Code: `jhmesh/` (Python: mesh crypto, network/transport/access PDUs incl. segmentation, proxy protocol, bleak client,
CDB/device model; ≈600 lines at the time of this test, since grown and moved from `tools/jhmesh/`) and the CLI
`tools/mesh_poc.py`.

## Result

**It works.** With nothing but the keys from the app's `MeshNetwork.json`, a laptop connected to a random JUNG node's
Mesh Proxy service can read state, switch loads, and passively decode *all* mesh traffic (including the gateway's own
requests and the vendor property exchanges). Total round trip for a Set including status confirmation ≈ 1 s.

```
$ .venv/bin/python tools/mesh_poc.py scan            # 28 nodes advertise our Network ID
$ .venv/bin/python tools/mesh_poc.py get WC          # → 0148 OFF, 01A4 OFF   (group Get, 2 replies)
$ .venv/bin/python tools/mesh_poc.py blink 0148      # WC light on 2 s, then restored
$ .venv/bin/python tools/mesh_poc.py prop 0149 user 0x5003   # KeyMode = 5 (Switch)
$ .venv/bin/python tools/mesh_poc.py listen --seconds 60     # decrypted live traffic
```

## CLI reference (`tools/mesh_poc.py`)

All commands take `--cdb <export>` (default: the iOS app container's `MeshNetwork.json`; a `JungHome.json` share
export works too) and `--source <hex>` (default `7FFF`, one sequence store per address in
`tools/.jhmesh_state_<ADDR>.json` — never the integration's `0D00`; with `--ha-storage <config>/.storage` none
the integration's store of the mesh holds a counter for). Addresses, property ids and model ids are hex
without a prefix; groups may be given by name; property names are the identifiers of `jhmesh/properties.py`
(`prop list` shows them). `tools/cli_ops.py` holds the link-free helpers (tested in `tests/test_cli.py`).

| Command | What it does |
|---|---|
| `scan [--adv]` | proxies of our network, strongest first; `--adv` adds local name, manufacturer data and service UUIDs (to settle whether provisioned JUNG nodes advertise anything vendor-specific — `docs/ha-integration.md` Known limitations) |
| `listen [--seconds N] [--src ADDR] [--dst ADDR]` | prints every decoded message to stdout as `HH:MM:SS.mmm SRC→DST ttl= seq= [key] <decoded>` (millisecond resolution; vendor property values decoded by the codec catalogue, e.g. `key_event=KeyEvent(counter=3, event='pushed_up')`); the client's own log (the `RX` / `TX` lines are DEBUG) is silenced unless `-v` |
| `get` / `set` / `blink` / `lightness` / `ctl` / `ctlget` / `scene` | as before (OnOff, Lightness, CTL, Scene) |
| `ctlrange <addr>` | `Light CTL Temperature Range Get` (0x8262) → Status (min/max K) |
| `prop get <addr> <name\|id> [--server …]` | property Get through the catalogue's hosting server (LBC admin / manufacturer / user, SIG admin / manufacturer / user, Sensor), reply decoded as `name=value (access=N raw=hex)`; `--server` overrides (e.g. `user` for KeyMode, which the gateway reads that way); `--pad N` pads the Get to force segmentation |
| `prop set <addr> <name\|id> <value> [--unack] [--access N] [--server …] [--yes]` | property Set with the codec's text form (`on`/`off`, numbers, enum names, `a,b` flags, `2.2.0.2` versions, `hex:0102` raw bytes); the access byte defaults to what the app sends for that property (3, `1` for `dim_mode`); a group target (every member takes the Set) is refused without `--yes` |
| `prop list <product id> [--all]` | the properties the app uses on a product (id, name, server, access, element, codec, unit, range, minimum firmware); `--all` adds firmware-only ids |
| `config get-composition <node>` | Composition Data page 0, one line per element |
| `config publication <node> <element> <model> [<group> --yes]` | Model Publication Set to the group (Get without a group) |
| `config subscribe` / `unsubscribe <node> <element> <model> <group> --yes`, `config subscriptions <node> <element> <model>` | Model Subscription Add / Delete / Get |
| `config bind` / `unbind <node> <element> <model> --yes` | Model App Bind / Unbind of AppKey 0 |
| `config audit <node>` | the node's Configuration Server (relay, network transmit, TTL, beacon, proxy, every model's publication, subscriptions and AppKeys) against the export, Gets only |
| `export write <file> [--out PATH] [--strict]` | loads the export with `jhmesh.export.ProjectFile` and renders it again unchanged; prints `identical` or the diff (what the writer would alter: layout, key order, Base64 payload); `--out` writes the rendering next to the original, never over it |

`config …` messages are device-key encrypted (`ProxyClient.send_config` / `request_config`); `<node>` may be any
element of the node, the primary unicast is used. Every write goes to the *mesh only* — the app's export file is not
touched (the HA integration's actions do that through `ProjectFile`) — so each one needs `--yes` (`--dry-run` prints
the message and connects to nothing) and ends with *export not updated — run `config audit <node>`*.

Logging: the per-message `RX …` / `TX …` lines of `jhmesh.client` are DEBUG, so they only appear with `-v` (which
also switches the CLI's own log to DEBUG); `listen` prints its decoded messages to stdout regardless. Secret
properties are never printed in clear — the gateway's API token `0xC001` (`PropertySpec(secret=True)`) reads
`gateway_api_token=<redacted>` in `describe()` / `prop get` output, whatever the bytes.

Capture recipes for the open questions in `docs/cross-repo-analysis.md` §8:

```
$ tools/mesh_poc.py listen --seconds 300 --src 0293        # rocker in KeyMode 6: press top / bottom → KEY_EVT codes 0–3
$ tools/mesh_poc.py listen --seconds 120                   # keys: are Scene Recall / OnOff Set doubled? gateway Sets: TTL, opcode
$ tools/mesh_poc.py ctlrange 016A                          # 0x8262 on a DALI node (also 0210 / 0232 / 026E)
$ tools/mesh_poc.py listen --seconds 600 | tee capture-$(date +%H%M%S).log   # next to G's tools/ws-capture/capture_ws.py
$ tools/mesh_poc.py config get-composition <rtr>           # real RTR composition (OnOff server present?)
$ tools/mesh_poc.py prop get <rtr> 1246 --server admin     # LBC_PROP_RTR_HVACMODE_DISPLAY_ID / 120B scheduler enable
$ tools/mesh_poc.py listen --seconds 120 --src 0173        # socket sensor element: 15 s poll answers vs publications
$ tools/mesh_poc.py listen --seconds 60 --dst 0149         # toggle G's status_led switch meanwhile → 0x5013 via opcode 0x11
$ tools/mesh_poc.py prop set 0148 6004 2c01 --server manufacturer   # Manufacturer Set framing (access byte or not)
$ tools/mesh_poc.py prop get 0148 device_lock              # 0x0001 bit order; prop set <blind> position 50 for the 0/255 quirk
$ tools/mesh_poc.py export write JungHome.json             # byte parity of export.py with a real Android share export
```

## How it works (what a Home Assistant integration would do)

1. **Identity**: we act as an extra node with our own unicast address (outside every provisioner's allocated range
   — the phone's is `0001–0CCC`, a second app user's starts right above it — and outside `networkExclusions`) and our
   own 24-bit sequence number. The CLI defaults to `0x7FFF`, the address the app hands out last, and refuses one the
   export does not leave free (`--source`, one store per address in `tools/.jhmesh_state_<ADDR>.json`); the HA
   integration defaults to `0x0D00` with its own store. The test in this document ran as `0x0D00`, before the integration existed. **Never let two
   clients share an address**: replay protection is per source address + SeqAuth, so the nodes silently drop every
   message of whichever client's counter is behind. No provisioning needed — nodes accept any source that has the
   NetKey/AppKey.
2. **Discovery**: scan for service data of `0x1828` (Mesh Proxy). Type `0x00` + `k3(NetKey)` = our Network ID
   (`dd384ecdb58ee53c` for this network) identifies every proxy node of *this* network; type `0x01` (Node Identity) is
   matched with `e(IdentityKey, 0^6 ‖ random ‖ address)`.
3. **Connect** to the strongest node, subscribe to Mesh Proxy Data Out (`2ADE`), write to Data In (`2ADD`,
   write-without-response, Proxy SAR framing at MTU-3; the nodes negotiate MTU 247).
4. The proxy immediately sends a **Secure Network Beacon** (authenticated with `BeaconKey`; IV index 0, no update,
   no key refresh) — this is where IV index changes must be picked up.
5. **Proxy filter**: send Proxy Config *Set Filter Type = blacklist* (encrypted with the proxy nonce, TTL 0, DST
   `0000`). The node answers *Filter Status: blacklist, size 0* → we now receive every Network PDU the node hears,
   i.e. the whole flat, because all JUNG nodes are relays.
6. **Send**: access PDU → AES-CCM with AppKey (application nonce) → unsegmented lower transport
   (`0x40|AID`) → network PDU (AES-CCM with EncryptionKey, header obfuscated with PrivacyKey, TTL 5) → proxy frame.
7. **Receive**: proxy reassembly → network decrypt (NID match, IVI → IV index) → lower transport (unsegmented,
   or segmented with reassembly + Segment Acknowledgement) → upper transport decrypt with AppKey (AKF=1) or with the
   DevKey of src/dst (AKF=0, config messages — we have all DevKeys) → replay check per source → decode opcode.

All crypto (s1, k1–k4, network encrypt/obfuscate, app-layer nonce) is verified against the Mesh Profile 1.0.1 sample
data (§8.1, §8.3.1). The library is transport-agnostic: in HA it would run unchanged over an ESPHome Bluetooth proxy
(`bluetooth_proxy: active: true`) because HA's bleak backend spans proxies.

## Findings from the live traffic

### JUNG firmware quirks
- **No unicast reply to a state-changing acknowledged Set.** `Generic OnOff Set` (acked, with or without an explicit
  transition time) that changes the state produces only the model's *publication* of the new status to the element's
  group (`0148→C061 present=ON`), sent **twice** ≈1 s apart with different sequence numbers. The unicast
  `Generic OnOff Status` to the sender is only sent when nothing changes (a Get, or a retransmitted Set with the same
  TID, which the server treats as a duplicate). Verified with an 8 s single-attempt wait: nothing arrives.
  → A controller must accept a status *from* the target element regardless of destination as the acknowledgement.
  Nordic's request matcher in the app only checks source + opcode, which is why the app works.
  *Confirmed from outside the network with the nRF sniffer (`sniffer.md`): HA's `Set ON` to `0148`
  produced two group publications and no unicast reply; a second `Set ON` while already on produced two
  publications **and** a unicast `OnOff Status` to HA, 200 ms after the Set.*
- **State publications are sent twice** — the doubling above is not specific to OnOff statuses: model status
  publications and button events (`0x5012`) come as two copies, the second 0.9–2.3 s later with a fresh SEQ and
  the same payload/TID/counter. It is *not* mesh publish-retransmission: the CDB has retransmit count 0 on every
  publication, and the gateway firmware has no dedupe either (only scene statuses are debounced 1 s) — it processes
  both copies (`docs/cross-repo-analysis.md` §1.2). A controller must dedupe on `(src, TID)` for SIG messages and
  `(src, counter)` for `0x5012`. **Not** doubled: the sockets' `Sensor Status` publications (40 of them in the
  sniffer baseline, none repeated) — those come as three separate statuses (power, current, voltage)
  100–200 ms apart, every ~65 s and on change (`sniffer.md`).
- Buttons are polled by the gateway with `LBC **User** Property Get 0x5003 (KeyMode)` and answered with
  `access=3 value=06 (Gateway)`; so KeyMode is readable through the *User* property server too (the Android code
  writes it through the Admin server). Reading it on a *load* element returns a status with an empty value.
- The gateway (`00DC`) polls loads with `Generic OnOff Get` and sends `Generic OnOff Set` with explicit
  `transition=0 delay=0`. It sends with **TTL 5** like every node (`defaultTTL 5` in the CDB) — the "TTL 2" this
  capture first recorded was the copy that had crossed three relays before reaching our proxy node; the nRF sniffer
  sees the originals (`sniffer.md`). (Firmware: the poll runs every 15 s, `device_state_poll_interval_sec` — on air
  one request every ~12 s round-robin over the elements — in addition to the gateway's client models being
  subscribed to every element group; the Sets are Silicon Labs `generic_client_set` with flags=1 = acknowledged,
  `docs/cross-repo-analysis.md` §1.1.)

### Sensor publications (sockets, element loc `0040`, e.g. `0173→C001`, `0175→C007`)
`Sensor Status` with marshalled SIG device properties, published on change:

| property | meaning | encoding observed |
|---|---|---|
| `0x0081` | Active Power Loadside | uint24 LE, **0.1 W** (`a00100` = 41.6 W; `ff0000` = 25.5 W; `510000` = 8.1 W) |
| `0x005D` | Present Output Voltage | uint16 LE, **1 V** (`e400` = 228 V, `dd00` = 221 V) — not the SIG 1/64 V scaling |
| `0x005C` | Present Output Current | uint16 LE, **0.01 A** (`1e00` = 0.30 A, `0500` = 0.05 A) |

The app's turn-on/turn-off thresholds (`0x5004/0x5005`) reference `0x0081` with the same ×0.1 W scaling, which corroborates this.

### Vendor property reads (node `0148`, Push-button 1-gang, firmware 2.2.0.2)

| request | wire reply value | decoded |
|---|---|---|
| User `0x0002` InsertId | `00 00 02 00` | `[actuatorFunctionId u16 LE = 0 Switch][insertType u16 LE = 2 GenericInsert]` — **field order corrected** vs `docs/android/properties.md` (matches the iOS cache `{actuatorFunctionId:0, insertType:2}`) |
| Manufacturer `0x0003` SecureElementVersion | `0d 02 01 00` | **little-endian version** → 0.1.2.13 (= the SE image in the APK's update manifest) |
| Manufacturer `0x0004` BootloaderVersion | `00 00 04 02` | → 2.4.0.0 (= bootloader in the manifest) |
| Admin `0x1001` GeneralOnDelay | `00 00 00 00` | u32 ms = 0 (off) |
| User `0x5003` KeyMode on button element `0149` | `05` | Switch |

Status framing confirmed on air: `[propertyId u16 LE][userAccess u8][value…]` behind opcodes `C5/CB/D1 27 05`;
Get is `[propertyId u16 LE]` behind `C2/C8/CE 27 05`; `userAccess` came back as 1 (read-only) for manufacturer/user
properties and 3 for KeyMode.

### Button events (keys in "Gateway" key mode)
A button element in KeyMode 6 publishes, through its `0527:1015` client model, an
**`LBC User Property Set Unacknowledged`** (vendor opcode `0x10`, wire `D0 27 05`) to the gateway's element group
(`C005`), carrying property **`0x5012`** (`LBC_PROP_KEY_EVT_ID` in the gateway firmware) with a 2-byte value
`[counter u8][event u8]`. What was captured here came from **key** elements: node `01B9` has layout 0
(ONE_TOP_ONE_BOTTOM = two separate keys, `docs/network-topology.md:1121`), so `01B9` is key A and `01BA`
(location `0041`) key B — not the two halves of one rocker.

| event byte | meaning | evidence (sofa button `01B9`, key A) |
|---|---|---|
| `0x05` | click (short press) | `13 05` after one short press; a double press produced `16 05` + `17 05` within the same second (the log had 1 s resolution at the time; `listen` now prints milliseconds) |
| `0x06` | hold start | `14 06` 6 s later, when the key was held |
| `0x04` | hold end (release) | `15 04` 2 s later on release |

The full event set, from the gateway firmware's decoder
(`btmesh_property_service.js:184-228` in the JUNG HOME Gateway 2.1.3 middleware; `docs/cross-repo-analysis.md` §1.2):

| event | firmware label | side | gateway output (`buttonState`) | element type |
|---|---|---|---|---|
| 0 | `pushed_down` | bottom | `[1, 0]` — press, then a **synthesised release** | rocker (one element, two halves) |
| 1 | `pushed_up` | top | `[1, 0]` | rocker |
| 2 | `held_down` | bottom | `[1]` (release comes as event 4) | rocker |
| 3 | `held_up` | top | `[1]` | rocker |
| 4 | `released` | side = previous event's side | `[0]` | key and rocker |
| 5 | `pushed` (click) | **toggle** of the previous side ("for downwards compatibility") | `[1, 0]` | key (captured above) |
| 6 | `held` | toggle of the previous side | `[1]` | key (captured above) |

- `counter` increments once per event per element (persists across presses: `00…06` in the first capture, `13…17` here).
  The gateway ignores it (`Number(values[1])` only) and keeps **one** "previous side" field for the whole network,
  so the up/down label it attaches to key events 5/6 is meaningless for us; only rocker events 0–3 carry a real side.
  **Events 0–3 captured** on the two layout-5 rockers (`0298`/`0294`, the user pressing top and bottom
  in a known order): top half = `01` *pushed_up*, bottom half = `00` *pushed_down*, top half held = `03` *held_up*
  followed by `04` *released* on letting go — the firmware labels are the physical halves, as assumed. Both copies
  of a rocker event came 0.5–1.5 s apart (same counter, fresh SEQ). A rocker in key mode *Light* (`0299`, the
  bedroom door's right half) sends `Generic OnOff Set Unack` ON for the top half and OFF for the bottom one to its
  room group instead. Sequence numbers are **per element** (`0298` at `0502xx` while `0299` was at `05000x`), as
  the spec allows — the replay list keyed by source address is the right one.
- Every event is **published twice**, ~1 s apart, with fresh sequence numbers and the same counter (the general
  publication policy above) → dedupe on `(src, counter)`. The gateway does not dedupe: each copy of a click
  re-runs the `[1, 0]` press/release pair (two 200 ms delays between the two API writes, `:249-256`), which is why
  the gateway API shows one tap as **two** ≈0.4–0.5 s press/release pulses about a second apart, while a hold's
  second copy is a no-op (state already "pushed") and yields one pair. On key elements the two copies of a click
  even land on *alternating* sides in the gateway API because of the toggle in event 5.
- "Double press" is not a distinct code — an integration derives single/double from timing (two clicks within a
  short window; measure with a ms-resolution capture before fixing the threshold). The gateway does not derive
  double presses either; it forwards the device's own click / hold / release classification as press/release pulses.
- Property `0x5012` does not exist in the phone app; only the gateway consumes it. The second key of the same
  node is the next element (`01BA`, location `0041`) with the same format.
- The vendor-models doc listed opcode `0x10` as "unverified User Set Unack" — now confirmed on air.
- Companion property `0x5013 KEY_STATUS` (1 byte) is the key's status LED; the gateway drives it by sending a
  **User Property Status** (opcode `0x11`, wire `D1 27 05`) to the button element (`docs/android/properties.md` §1.6).

### SIG property traffic from the gateway (same capture)
- `Generic Manufacturer Property Get` (`82 2B`) `0x001A` → `Generic Manufacturer Property Status` (`0x46`)
  `1a00 01 "02020002"` — the Device Software Revision is an **8-char ASCII string** (segmented reply; our reassembly +
  Segment Ack path handled it).
- `Generic Admin Property Get` (`82 2D`) `0x006D` Total Device Power On Time from socket `0172` → `0x4A`
  `6d00 01 933a00` = 14 995 (hours).
- `Light CTL Temperature Get` (`82 61`) / `Generic Level Get` (`82 05`) polls to DALI channels; the dimmer's second
  element answers `Generic Level Status = -32768` when the channel is off.

### Other
- Proxy nodes negotiate MTU 247; one GATT connection is enough to see traffic from every node in the flat
  (relayed, TTL 1–4 on arrival).
- Sequence numbers seen: gateway `00DC` at `0xB0D6xx` (11.6 M of 16.7 M), phone at 0x1F3xx, nodes up to 0x25xxxx.
  The gateway's persisted counter was `0x9FC000` in one firmware dump and `0xA68000` in one seven weeks later (it is
  saved hourly, rounded up to a multiple of `0x4000`), i.e. **9–18 k messages/day**. The gateway firmware requests
  an IV Update once fewer than 128 × `0x4000` sequence numbers remain (≈ `0xE00000`, warnings from ≈ `0xBFFFFF`),
  so at the observed rate the update comes in **~6–12 months** from that reading (warnings in ~2–4 months), not
  "within a few years" as first estimated. The client must follow the beacon (`docs/cross-repo-analysis.md` §1.4,
  §3 first bug).
- The mesh never key-refreshed; `security: insecure` nodes accept everything as expected.

## What is still missing for a full integration
- ~~Button event PDU~~ — captured (property `0x5012`, above).
- ~~Segmented *sending*~~ — done (`jhmesh/client.py` `_send_segmented`, with Segment Ack handling; `ctlget --echo`
  exercises it on air).
- Model-level decoders for blinds (Generic Level on the 2-channel actuators), thermostat (RTR properties), detectors.
- ~~A reconnect loop that walks the proxy candidate list~~ — done (`jhmesh/standalone.py` `StandaloneLink` for the
  CLI, `bleak_retry_connector` in HA).
- ~~IV Update handling beyond "adopt new index"~~ — done, though never exercised on air (`jhmesh/client.py`
  `LocalState.apply_beacon`): Normal Operation → In Progress only for index + 1, transmitting with the old index until
  the update completes; a same-index "in progress" beacon after completion is ignored and a lower index is never
  adopted (the IV *downgrade* of `docs/cross-repo-analysis.md` §3 is fixed); IV Index Recovery up to 42 ahead; the
  sequence number restarts only when the transmit index rises; the proxy filter is re-sent after an IV change
  (§4 there) and the replay list keeps the previous index's entries. HA shows the IV index and the sequence space
  used (its own and the mesh's highest source) as diagnostic sensors and raises the `sequence_space_low` repair once
  a source passes `0xC00000` (`sensor.py` `MESH_DIAGNOSTICS`, `Issues.check_sequence_space`).
- Key refresh is still open: it is only detected (a Phase 2 beacon with the Key Refresh flag that our key cannot
  authenticate raises the `key_refresh` repair hint), not followed — the keys must be re-exported.
- ~~Mapping the CDB + `device_metadata.json` into HA entities~~ — done (`jhmesh/devices.py`; the app's own device
  split is: load elements loc `0001/0002`, button elements loc `0040+`, per-node InsertId → device class).
