# Passive sniffing with a Nordic nRF Sniffer for Bluetooth LE

A BLE sniffer dongle next to the installation records the mesh's **advertising bearer** — every Network PDU as its
originator sent it — without taking part in the network. This complements the GATT-proxy view of
`tools/mesh_poc.py listen` (`poc-gatt-proxy.md`), which only shows what one proxy node forwards *after* its network
cache dropped the relay copies and with the TTL the last relay left, and which needs a mesh address, a sequence
counter and one of the node's proxy slots.

| | proxy `listen` | sniffer `capture` + `decode` |
|---|---|---|
| sees | what one node forwards over GATT | every PDU on air in radio range, every relay / network-transmit copy |
| TTL | after the relays | as sent (originals identified by the highest TTL) |
| timing | when the notification arrives | sniffer µs clock + host clock, RSSI, channel |
| cost | a mesh address, sequence numbers, a proxy slot | none — no key on the capture host, nothing transmitted |
| decrypts | live, in `jhmesh` | offline or live over a pipe, in `jhmesh` (same `describe()` / codecs) |
| GATT sessions | its own only | not yet (`--follow` is a possible extension, see below) |

## Hardware and setup

- nRF52840 dongle with Nordic's **nRF Sniffer for Bluetooth LE 4.1.1** firmware (USB `1915:522a`, appears as
  `/dev/ttyACM0`), on the Home Assistant host (`sniffhost` in the examples below); the capture user needs access
  to the serial device (the group that owns it, `uucp` or `dialout` depending on the distribution).
- Nordic's extcap package unpacked in `~/nrfsniff/extcap` (`nrf_sniffer_ble.py`, `SnifferAPI/`; the `--api`
  default of `tools/mesh_sniff.py`) with a venv in `~/nrfsniff/venv` (`pyserial`, `psutil`): the layout
  `tools/mesh-sniff.service` expects.
- Two quirks of Nordic's package: the extcap wants its interface named `<port>-<version>` (`/dev/ttyACM0-4.1.1`),
  and `SnifferAPI/Filelock.py` writes `/var/lock/LCK..ttyACM0`, which is root-only on Arch Linux, the distribution
  of the host it was found on (`PermissionError`). `tools/mesh_sniff.py` drives `SnifferAPI` directly and disables that lock (the dongle has one
  user).
- The sniffer listens to **one advertising channel at a time** and hops 37 → 38 → 39. A mesh node sends each PDU on
  all three channels, `networkTransmit.count` (3 here) times, and every relay repeats it, so a PDU is heard many
  times (mean 16, max 29 copies in the baseline); still, individual copies are missed, and a node whose original was
  missed shows up only through its relays (lower TTL). Pin the sniffer to one channel with `--channels 37` when the
  exact copy timing matters more than coverage.

## The tool: `tools/mesh_sniff.py`

Two halves that may run on different machines. The **capture** half imports only Nordic's `SnifferAPI` (plus the
stdlib) and writes one JSON line per mesh AD structure — `0x2A` Network PDU, `0x2B` Mesh Beacon, `0x29` PB-ADV —
with the host time, the sniffer's µs timestamp, channel, RSSI, the (rotating, non-resolvable) advertising address
and the raw bytes. **No key is needed or present on the capture host.** The **decode** half runs where the export
lives and turns those lines (or a Nordic pcap) into decrypted, described messages with `jhmesh.sniffer.MeshDecoder`.

```
# live view; the script itself is streamed to the sniffer host, nothing is installed there
ssh sniffhost '~/nrfsniff/venv/bin/python -W ignore - capture --api ~/nrfsniff/extcap --ndjson -' \
    < tools/mesh_sniff.py \
  | .venv/bin/python tools/mesh_sniff.py decode --export JungHome.json -

# record on the sniffer host (NDJSON for us, pcap for Wireshark), decode later
python3 mesh_sniff.py capture --api ~/nrfsniff/extcap --seconds 600 --ndjson base.ndjson --pcap base.pcap
.venv/bin/python tools/mesh_sniff.py decode --export JungHome.json base.ndjson --json base.decoded.ndjson
.venv/bin/python tools/mesh_sniff.py decode --export JungHome.json base.pcap --src 0148 --copies
```

`decode` prints `HH:MM:SS.mmm chNN RSSI  SRC→DST ttl= seq= [key] <description>` for the first copy of every
message, collapses the further copies (`--copies` shows them indented with their channel, RSSI and TTL), reassembles
segmented messages (reported at the time of their first segment), prints beacons only when something changes
(`--beacons` for all), hides other networks' PDUs (`--foreign`), filters with `--src/--dst/--grep`, and ends with
a summary: records per kind (`access`, `copy`, `control`, `segment`, `beacon`, `unprovisioned`, `undecryptable`,
`foreign`, `pbadv`) and messages per source. Beacons are named by type: our network's Secure Network beacons
(`beacon iv= flags= auth=`) and Mesh Private beacons (`private beacon …`, opened with the private beacon key of
the export's NetKey), Unprovisioned Device beacons of devices waiting to be added (`unprovisioned device beacon
<UUID> oob=`, with the URI hash when there is one), and — among the foreign ones — private beacons no key of ours
opens (`private beacon (unknown network)`). `--json` writes every decoded record as NDJSON (time, channel, RSSI,
route, TTL, seq, key, opcode, params, text; the UUID and OOB of an unprovisioned device) for scripted analysis. The
IV index follows the authenticated beacons it sees, private ones included (`--iv` to start elsewhere).

`capture --dedupe SECONDS` folds byte-identical PDUs (a node's own network-transmit repeats) into one record with
a count, for long unattended recordings; relay copies differ in TTL and stay separate. `capture --pcap` writes the
LINKTYPE_NORDIC_BLE pcap Wireshark understands (Wireshark's `btmesh` dissector can decrypt it too once given the
keys — it does not know the JUNG vendor models, ours does); `decode` reads those pcaps as well
(`jhmesh.sniffer.read_pcap`, checked against the NDJSON of the same run: 2943/2943 records identical).

The decoder keeps **no replay list** on purpose: a passive observer wants to see retransmissions. It is covered by
`tests/jhmesh/test_sniffer.py` (100 %) and `tests/test_sniff_cli.py` (the capture half against a fake `SnifferAPI`).

## What the first captures established

All on the user's installation (30 nodes, IV index 0), keys from the app's share export of the same day.
Every Network PDU of the four captures (3,800+ records) decrypted; no foreign network in range.

**Confirmed / corrected in `poc-gatt-proxy.md`:**

- **Acknowledged Set → no unicast reply when the state changes, reply when it does not** — now seen from the
  outside, not through a proxy. HA (`0D02`) sent `Generic OnOff Set ON` to `0148`: the node published
  `OnOff Status` to its group `C061` **twice** (seq `0B0815`, `0B0816`, 2.1 s apart) and sent **nothing** to
  `0D02`. A second `Set ON` while already on: two publications again *and* a unicast `0148→0D02 OnOff Status`
  (seq `0B0818`, between the two publications, 200 ms after the Set). `Set OFF`: two publications, no reply. The
  gateway's `Set ON` to two already-lit WC lights (a scene) got unicast replies for the same reason.
- **Originals carry TTL 5** — the gateway's (`00DC`) too. The "TTL 2" of the earlier proxy capture was the copy that
  had crossed three relays before reaching our proxy node. Every node has `defaultTTL 5`, publications use TTL
  `255` (= default) in the CDB, `networkTransmit 3 × 100 ms`, `relayRetransmit 3 × 90 ms`.
- **The doubling is a *status* publication policy, not a general one.** OnOff statuses after a Set: always two
  (0.9–2.3 s apart, fresh SEQ, same payload). The sockets' `Sensor Status` publications (`0173→C001`,
  `0175→C007`): **never doubled** in 40 messages — three separate statuses (`0x0081` power, `0x005C` current,
  `0x005D` voltage) 100–200 ms apart, repeated every ~65 s and on change; the idle socket `0174` publishes only
  voltage. The CDB publish period is 0 for every model, so the 65 s cadence is firmware, not the model period.

**Gateway behaviour on air (`cross-repo-analysis.md` §1.1 / §8):**

- One request every ~12 s (median 12.2 s, p90 15 s) round-robin over 34 element targets; 58 of 59 requests were
  answered unicast within 2.5 s. Mix in 10 minutes: `LBC User Property Get 0x5003 KeyMode` (20; also
  `0x1014 rtr_operation_mode` on push-buttons), `Generic OnOff Get` (12), `Generic Manufacturer Property Get
  0x001A software_version` (6, the reply is 2 segments; the gateway acks the first segment with `block=1` and
  then both with `block=3`), `Sensor Get 0x0081 / 0x0052` (5), `Generic Level Get` (5), `Light Lightness Get` /
  `Light Lightness Range Get` (3 + 3), `Light CTL Temperature Get 0x8261` (3), `Generic Admin Property Get`,
  `Light CTL Temperature Range Get`.
- Socket values reach the gateway both ways: the sockets **publish** to their element groups every ~65 s / on
  change, and the gateway **polls** `Sensor Get` as well (property-qualified: `0x0081`, `0x0052`).
- Secure Network Beacons: 59 in 584 s ≈ one every 10 s network-wide — the spec's adaptive rate, not one per node.

**Home Assistant on air:** its 5-minute `power_on_time` poll of both sockets (`Generic Admin Property Get 0x006D`,
answered unicast within 50–250 ms), its Sets leaving the proxy node with TTL 4 (sent with 5).

## What it can settle next (`cross-repo-analysis.md` §8, `roadmap.md` §4)

The integration's own open checks — everything the code and docs still call *unverified on air* — are listed, with
the captures that settle them, in [`on-air-sweep.md`](on-air-sweep.md).

Passive, whenever the event happens:
- `0x5012` codes 0–3 from a KeyMode-6 **rocker** element (`0293`/`0297`): press top and bottom while capturing.
- Whether `Scene Recall` / `OnOff Set` from keys are doubled (same TID, fresh SEQ).
- The status-LED write (`0x5013` via opcode `0x11`) when the gateway integration toggles `status_led`.
- The IV Update and any Key Refresh when they come (the gateway's sequence forecast puts the IV update within
  6–12 months) — the unattended capture below records them.

With the app (the user does the action, the sniffer shows the exact messages — the ground truth for every
roadmap step 1 setting before we send it ourselves):
- `Manufacturer Property Set` framing (`0x6004`/`0x6005`: access byte or not).
- Every Parameters-tab setting: opcode, property id, value encoding, AppKey vs DevKey, how it is acknowledged.
- Rooms / connections / scenes: the Config Server sequence (`Subscription Add`, `Publication Set`, `Bind`) and the
  JH Scheduler / Scene Action Setup messages.

## Unattended capture (`tools/mesh-sniff.service`)

A systemd *user* unit for the sniffer host runs `capture --dedupe 2 --seconds 86400` in a loop (`Restart=always`),
one NDJSON file per day in `~/nrfsniff/captures/mesh-<date>-<time>.ndjson` — 24–31 MB a day with the repeats
folded (two full days; the raw rate on this installation is ~300 records/min, 2.7 MiB/h). No key is involved;
decoding happens here, on demand:

```
scp tools/mesh_sniff.py sniffhost:nrfsniff/ && scp tools/mesh-sniff.service sniffhost:.config/systemd/user/
ssh sniffhost 'systemctl --user daemon-reload && systemctl --user enable --now mesh-sniff.service'
ssh sniffhost cat nrfsniff/captures/mesh-<date>-0000.ndjson \
  | .venv/bin/python tools/mesh_sniff.py decode --export JungHome.json --beacons --grep 'IV|Key' -
```

The dongle has one user: `systemctl --user stop mesh-sniff` before an ad-hoc capture (`--follow`, a pcap for
Wireshark), `start` afterwards. The user session must linger (`loginctl enable-linger`).

Nordic's `SnifferAPI` also appends every packet to its own `/tmp/logs/capture.pcap` (rotated to `.1` at ~3 GB)
whatever the caller asks for; where `/tmp` is a tmpfs, a day of that held 5.6 GB of RAM.
`load_sniffer_api` replaces that writer with a no-op, so deploy the current `mesh_sniff.py` with the unit — an older
copy still fills `/tmp`.

## Wireshark

`capture --pcap` writes a LINKTYPE_NORDIC_BLE (272) pcap. **Verified** with TShark 4.2.5 (a
throwaway `alpine` container, `apk add tshark`): a 90 s capture — 42,607 BLE frames, 2,036 mesh Network PDUs and
12 beacons, the same counts as the NDJSON — decrypts completely once the keys are in Wireshark's
*BTMesh Network and Application keys* table: the file `btmesh_nw_keys` in Wireshark's personal configuration
folder (*About → Folders*), one line per AppKey,

```
"0x<NetKey 32 hex>","0x<AppKey 32 hex>","0x00000000"
```

(the `0x` prefix and the 8-digit IV index are mandatory — without them the table fails to load and every PDU stays
`Encrypted data and NetMIC`; TShark reports that only on stderr). Then `btmesh.src` / `btmesh.dst` / `btmesh.ttl`
/ `btmesh.seq`, `btmesh.model.opcode` for SIG messages and `btmesh.model.vendor` + `btmesh.model.vendor.opcode` +
`btmesh.model.parameters` for JUNG's (company `0x0527`, parameters raw — ours names them). The 90 s sample seen
through it: Sensor Status `0x0052` ×242, OnOff Status `0x8204` ×112, the gateway's `0x822B` property polls ×96,
vendor `0x14` / `0x17` ×75 / ×73. `btmesh_dev_keys` (`"0x<DevKey>","<unicast>"`) exists for config messages, not
tried. A quick filter session: `tshark -r x.pcap -Y btmesh.access.decrypted -T fields -e btmesh.src -e btmesh.dst
-e btmesh.model.opcode -e btmesh.model.vendor.opcode`.

## Following a connection (`capture --follow <MAC>`)

The nRF Sniffer can follow one advertiser into its GATT connection. `capture --follow 30:FB:10:A8:38:83` waits for
that node (JUNG nodes advertise from their public MAC) and asks the dongle to follow it; the LL data PDUs of the
connection are reassembled (L2CAP fragments → ATT PDUs) and written as `gatt` records (`dir` = `m2s` central → node
or `s2m`). `decode` turns the ATT writes to *Mesh Proxy Data In* and the notifications from *Data Out* into the
same decoded lines as on-air traffic, tagged `[gatt →node]` / `[gatt node→]` (proxy SAR reassembled, network PDUs
decrypted, beacons parsed), names the **proxy configuration** messages (`Set Filter Type blacklist`, `Filter Status
blacklist 0 addresses`, `Add Addresses [...]`) and the **PB-GATT provisioning** PDUs (Invite, Capabilities, Start,
Public Key, Confirmation, Random, Data, Complete, Failed — payloads as hex), and with `--gatt` every other ATT PDU
(MTU exchange, discovery). Fully unit-tested (`tests/jhmesh/test_sniffer.py`, `tests/test_sniff_cli.py`).

**Not yet seen working live**: three attempts — the Mac connecting to `0148` (two connections), HA
reconnecting through `015E` (twice) — recorded the followed node's advertisements but never a `CONNECT_IND`, so no
data PDUs. The sniffer needs to hear the *initiator's* connect request; the Mac and the ESPHome proxy that HA uses
are evidently out of the dongle's reach (it sits on the server), and in follow mode the dongle also reports many
CRC-failed packets. To try next: the dongle on a USB extension in the room where the phone / proxy is, or a phone
next to the server; then the app's provisioning and configure sequences become capturable end to end. Note that
the mesh *messages* of a GATT session are in the passive capture anyway (the proxy node re-sends them on the
advertising bearer); following adds the proxy configuration, provisioning and OTA traffic.
