# Gap analysis — runtime control and state: JUNG HOME app vs `junghome_ble`

Scope: everything a user can *do* on a device tile / detail page of the JUNG HOME Android app
2.2.0 and every piece of live *state* those screens show, the mesh messages behind each, and whether the Home
Assistant integration (`custom_components/junghome_ble/`) already covers it. Configuration parameters (delays,
LED colours, key assignments, thresholds *editing*, timers, scenes *editing*) are out of scope except where the
app reads them at runtime to render state.

Sources: `android/jadx-out/sources/` (paths below are relative to it; `simple-mode:` = the
`--decompilation-mode simple` output used in `docs/android/network-logic.md`), `android/jadx-out/resources/res/values/strings.xml`,
and our code `custom_components/junghome_ble/{coordinator,light,switch,sensor,event,scene}.py`,
`jhmesh/{messages,devices,client}.py`. This document builds on `docs/android/network-logic.md` (§4 control paths,
§5 state, §5.4 requests, §6.2 time), `docs/android/properties.md` (§1 property tables, §2 enums),
`docs/android/vendor-models.md` and `docs/poc-gatt-proxy.md`; facts already established there are referenced,
not re-derived.

## 0. Legend and conventions

**Our status** column:

| Value | Meaning |
|---|---|
| `done` | implemented in the integration **and** verified on air (`docs/poc-gatt-proxy.md`) |
| `done*` | implemented in the integration, not yet exercised on air |
| `lib` | a builder or decoder exists in `jhmesh/messages.py` (or the CLI), but nothing in the integration uses it for this purpose |
| `todo` | nothing exists |
| `n/a` | the app has no runtime control/state for this either |

Message notation: `Opcode (hex) [params]`, `→ element` = destination unicast (which element of the node), `ack`/`unack`.
Vendor opcodes are written as wire bytes (`C2 27 05` = LBC Admin Property Get, CID 0x0527); their payloads are
`[propertyId u16 LE][…]` (Get), `[propertyId u16 LE][userAccess u8 = 3][value]` (Admin Set) and
`[propertyId u16 LE][userAccess u8][value]` (all Status), see `properties.md` §0.2. "Admin Get/Set 0xNNNN" below
always means the LBC vendor Admin property server (`0x0527:1011`) on the node's primary element unless stated.

Level ↔ percent (blinds, slats, RTR set-point): `level = round(−32768 + pct/100·65535)` and
`pct = round((level + 32768)·100/65535)` (`network-logic.md` §4.1/§5.2). Lightness ↔ percent: `lightness = round(pct/100·65535)`.
CTL temperature ↔ percent: `K = round((2000 + 8000·pct/100)/100)·100`, `pct = (K − 2000)/8000·100`.

## 1. Cross-cutting behaviour

### 1.1 How the app sends controls (recap of `network-logic.md` §4.1)

* Every runtime control is an **acknowledged** SIG message to the **load element's unicast** (the element hosting the
  server model), `waitForStatus = true` → 3 attempts × 3 s, matched on *source element + status opcode* only (the
  JUNG firmware answers a state-changing Set with a *publication* to the element group instead of a unicast reply,
  `poc-gatt-proxy.md` "firmware quirks"). Room and "all devices" functions are **unacknowledged** to a group address
  (`network-logic.md` §4.4).
* No transition time, no delay, TTL 5, AppKey 0, one global TID counter.
* If the proxy link is down the app fails the request immediately (`DeviceNotReachable`) — except for battery
  transmitters, whose requests are queued (`network-logic.md` §4.1).

### 1.2 How the app learns state (recap of §5.1–5.3, with additions)

* Statuses are consumed regardless of whether they were solicited. Each SIG resolver stores the value under the
  **source element address**. The resolvers use the **present** field of every status
  (`app/ui/timer/threshold/devices/d.java:356` OnOff `getPresentState()`, `resolver/L0.java:146-150` present lightness,
  `resolver/E0.java:202-213` present lightness/temperature); only `GenericLevelStatus` keeps present **and** target
  (`resolver/Q.java:147-148`). Our coordinator prefers the *target* field when the long form is present
  (`coordinator.py:265,269,275`) — identical in practice because JUNG never sends transitions, but a dimmer that is
  being faded by a rocker hold reports present ≠ target; the app shows present.
* The `SensorStatus` resolver only looks at the **first** marshalled property of a status
  (`resolver/M1.java:28-31`, `kotlin.collections.v.e0` = firstOrNull) and only for `0x004F` (RTR temperature,
  `F7/c.java:17`) and `0x0081` (active power, `F7/b.java`); scaling in `F7/a.java:35,47`
  (`raw_LE × 10^-1` W, `raw_LE × 2^-1` °C clamped 5..30). The multi-property socket status
  (`0x0081, 0x005D, 0x005C`) seen on air is therefore parsed by the app for power only; we parse all three
  (`coordinator.py:280-289`).
* Reachability: a device becomes *unreachable* as soon as one request exhausts its attempts (a request timeout
  sets its failed-message counter to 3 at once, `MeshMessengerImpl$handleError$1.java:47-68`; other errors add 1) and
  is marked reachable again by any status from it (`network-logic.md` §5.1). Battery devices are additionally unreachable as soon as one
  request failed (`Y7/AbstractC0916e.java:314-316,361-363`: `N1() = isBattery && failedMessagesCounter > 0`,
  `S1() = failed < 3 && !N1()`). Tiles show "No connection" (`strings.xml device_not_reachable_title`) via
  `I6/c.java:49,125-132`. Ours (`coordinator._missed_answer`): the app's rule — one request asked with
  the full budget (3 x 3 s) and left unanswered marks the node unreachable at once — over the connect-time state Gets
  and every load command (acknowledged and waited for like the app's, `JungHomeHub._load_command`; a command's miss
  counts once the link watchdog's probe got an answer through the proxy, so a proxy that stopped forwarding marks no
  node; blind / dimmer movements — Move and Delta Set — stay fire-and-forget, see §5); a node heard from
  while it was asked stays reachable, the one-attempt link keep-alive is re-asked instead of counted, vendor property
  reads do not count, battery nodes are never marked, and an unreachable node is re-probed every 5 min.

### 1.3 Published vs polled — who tells us what

Every "supported server" model of a load element (Generic OnOff, Generic Level, Light Lightness, Light CTL,
Light CTL Temperature, Scene, LBC User Property, LBC Admin Property — **not** Sensor Server, **not** Generic
Battery) is configured to publish to the element's own group (`network-logic.md` §1.4), so with the proxy's empty
black-list filter we see:

| Arrives by publication (on change, no polling needed) | Must be polled (nothing is ever published) |
|---|---|
| Generic OnOff Status, Light Lightness Status, Light CTL Status / CTL Temperature Status of every load (verified on air for OnOff/CTL) | Generic Battery Status of wall transmitters / battery puck (Generic Battery Server is not in the publication list; the app polls it only on the configuration page, §2.9) |
| Generic Level Status of blind position / slat / RTR set-point elements (configured, expected, **not yet verified on air**) | LBC **Manufacturer** property values (detector brightness `0x6004`, PIR state `0x6005`, versions) — the Manufacturer server is not wired for publication |
| Sensor Status of sockets (power/voltage/current) **when** "sensor values for gateway" is enabled on the device (`network-logic.md` §6.1; on in this installation, `poc-gatt-proxy.md`) and, by the same mechanism, of detectors and RTRs (property set unknown, see open questions) | SIG Generic Admin/Manufacturer properties (`0x006A` energy, `0x006D` power-on time, `0x001A` version) |
| LBC User Property Status (`D1 27 05`) publications: InsertId / KeyMode changes, button events `0x5012` (verified on air) | Vendor Admin properties read for state: lock state `0x0009`, blind mode `0x1104`, RTR modes/boost/auto-manu `0x120B/0x120D/0x1246`, detector forced-off `0x6016`, energy charts `0x5010/0x5011` — unless the LBC Admin server publishes unsolicited statuses on change, which the app code cannot tell (open question) |
| Scene Register Status (published by the Scene Server) | Sensor values of devices where "sensor values for gateway" is off |

### 1.4 App start, page open and periodic traffic (all of it)

| When | Message(s) | Source | Ours |
|---|---|---|---|
| App start, after the proxy connected | **`Time Set` (0x5C) → `0xFFFF`, unack**, 10-byte TAI time (`[TAI seconds u40][subsecond u8][uncertainty u8][authority 1 bit ‖ TAI−UTC delta 15 bits][zone offset u8]`, LE bit-packed; app: TAI epoch 2000-01-01, TAI−UTC delta 37 s encoded as `delta + 255`, zone offset encoded as `minutes/15 + 64`, subSecond = ms/256 (`W7/c.java:91-94`, Nordic `TimeSet.java:22-31`) | simple-mode `LoadInitialDeviceState$work$2:263-266`; `network-logic.md` §5.4/§6.2 | `todo` (no builder; only matters for devices with timers / astro schedules) |
| App start | Manufacturer Property Get `C8 27 05` `0xC001/0xC002/0xC003` to each gateway (REST credentials) | `LoadInitialDeviceState$work$2:186-193` | `n/a` |
| App start | `LoadStateForDevices(favourites only)` (`M1()` = isFavorite filter, `:199-206`); the Devices tab and room pages call it for every listed device (`app/ui/home/devices/DevicesViewModel$observeDevices$1.java:193`, `groupDetail/GroupDetailsViewModel`) | simple-mode `LoadInitialDeviceState$work$2:199-206` | `done` (we refresh every load at connect) |
| `LoadStateForDevices` | reachable devices, chunks of 5, 300 ms between chunks, 3 retries; per device **only if the node has no cached value**: `Generic OnOff Get` (0x8201) → OnOff element (`LoadStateForDevices.java:355-359`, guard `I1().k()` = OnOff cached, `:352`); blinds `Generic Level Get` (0x8205) → blind element (`:305-310`, guard `l()`); RTR `Sensor Get` (0x8231) `[0x004F]` (`:255-260`, guard `d()`). Nothing for detectors, control switches, gateway | `network-logic.md` §5.4 | `done` for OnOff (we also send Lightness/CTL Get, `coordinator.py:236-243`); `todo` Level Get / Sensor Get 0x004F |
| Any device detail page open | `Admin Get 0x0009` (lock state) for lockable devices; `Generic OnOff Get` if state missing | `network-logic.md` §5.4 | `todo` |
| Control-switch / mini-actuator detail page (battery devices only) | **keep-alive**: acknowledged `Admin Get 0x5001` (ButtonLayout) every **6 s** while no other BLE process runs, until the page closes; on timeout emit *DeviceNotAwake* ("Press the button to wake it", `strings.xml control_switch_sleep_mode_text`) and retry after 1 s | simple-mode `KeepLowPowerDeviceAwake$work$1:89-124,164-175`; `KeepLowPowerDeviceAwake.java:81-84`; `$work$2.java` | `todo` (only needed to *configure* or read battery; keys work while asleep) |
| RTR detail page | on open: `Sensor Get [0x004F]`, `Generic Level Get`, `Admin Get 0x120B` (or 0x1203/04/05 on FW < 2.2.0.0), `Admin Get 0x1246`; then **`Admin Get 0x120D` (boost) every 5 s** while the page is open | §2.7 | `todo` |
| Socket consumption page | every 5 s: SIG Admin Property Get `0x006D`, `0x006A`; `Sensor Get [0x0081]` | §2.4 | `todo` (we read the sensor once at connect) |
| Detector "walking test" active | Manufacturer Get `0x6005` every 1 s, auto-off after 300 s | §2.8 | `todo` |
| Detector parameter page ("Current brightness") | Manufacturer Get `0x6004` every 5 s | §2.8 | `todo` |
| Gateway | REST `GET /api/junghome/config` every 5 s | `network-logic.md` §6.1 | `n/a` |

## 2. Per device kind

Common to every lamp / socket / mini-actuator output page (`app/ui/devices/DeviceDetailsViewModel`, `d.java`):
header power button = `ToggleDevice`; "Device not reachable" label when `!S1()` (`app/ui/devices/d.java:461-469`);
header lock icon when `P1()` (wind icon if priority 0xFF, `d.java:472-480`); on the first device emission an
**`Admin Get 0x0009`** for the lock state (`DeviceDetailsViewModel$observeDevice$1.java:159-171`); a "controlled by
a thermostat" card (local DB only, `ObserveRtrConnectionMode`) that hides the toggle when the output is bound to an
RTR (`LampDetailActivity.java:222-241`). The lock function itself is in §2.11.

### 2.1 Switch lamp (`SwitchLampDevice`, `Y7/q0.java`; switch insert, mini-actuator / puck outputs, 2-channel actuator as two switches)

| Control / state shown in the app | Mesh message(s) | Our status | HA representation |
|---|---|---|---|
| Tile: bulb icon on/off, "Switched on/off" text, toggle button (hidden while locked), skeleton until the first OnOff is known, lock badge (`I6/a.java:75-86`, `I6/c.java:69-79,162-191`, `app/utils/g.java:674-676`) | state **`Generic OnOff Status` (0x8204)** `[present u8][target u8][remaining u8]`, app uses **present** (`app/ui/timer/threshold/devices/d.java:356`); toggle **`Generic OnOff Set` (0x8202)** `[onoff][tid]`, ack → OnOff element, new state = OFF if current is ON *or unknown* (`ToggleDevice.java:111-113,199`; then `Admin Get 0x0009` if the lock state is unknown and the node has no Scene Action Setup server, `:124-139`) | `done` (`coordinator.py:263-266,342-343`; we send `transition=0`, the app sends none) | `light` (`onoff`) — as today. For sockets-like loads on pucks a `switch` would be more honest, but the app itself renders every switch insert as a lamp |
| Detail page: illustrated on/off icon + "Switched on/off" text (read-only), `Generic OnOff Get` if the node has no cached state (`SimpleSwitchViewModel$requestGenericOnOffIfMissing$2$1.java:73-85`); "Permanent On \| Off" lock card with a *time-limit picker* (H:MM:SS, max 4 h 59 min) — the picker is the lock's time limit, **not** a timed-on (`SimpleSwitchFragment$onCreateView$1$1$1$1$1$1.java:52-60`, `e6/c.java:102-139`); no remaining time is shown | `Generic OnOff Get` (0x8201) → OnOff element; lock: §2.11 | `done` (Get in `_refresh_all`) | – |
| "Continuous ON/OFF activated" banner + controls disabled when the attached detector insert has forced the output (`LampDetailActivity.java:162-219`) | `Admin Get 0x6016` to the *detector* element of the same node when the lamp page opens (`LampViewModel$_forceOffMode$3.java:70-96`), `u8` 0 INACTIVE / 2 OFF / 3 ON | `todo` (only for detector-driven loads) | see §2.8 |
| Room / "all lamps" on-off | `Generic OnOff Set Unack` (0x8203) → `0xFEF5`, or per-device unicast unack inside a room (`LampCentralFunctionsViewModel.java:42,59`, `UpdateDeviceTypeGroup.java:301,350`) | **done** (*All lights*) | not needed |

### 2.2 Dim lamp (`DimLampDevice`, `Y7/f0.java`; dimmer insert, puck dimmer)

| Control / state shown in the app | Mesh message(s) | Our status | HA representation |
|---|---|---|---|
| Tile: as switch lamp (on = OnOff present state; the Lightness resolver also sets OnOff = lightness ≠ 0, `resolver/L0.java:146-150`) | `Generic OnOff Status` / `Light Lightness Status` (0x824E) `[present u16][target u16][remaining]`, app uses **present** → `pct = round(l·100/65535)` | `done` (`coordinator.py:267-271` uses target when present) | `light` (`brightness`) — as today |
| Detail header "*N* % brightness" (`strings.xml:446`), slider 0..100 % with the device's **lightness range** as bounds, − / + buttons (±1 %) (`lamp/elements/dim/d.java:80-101,213-268`, `controls/DimSlider.java:365`) | page open: **`Light Lightness Range Get` (0x8257)** → status 0x8258 `[status u8][min u16][max u16]` (`p234v7/L.java:29`, `resolver/J0.java`) and **`Light Lightness Get` (0x824B)**, both ack (`dim/e.java:65-67`); control: slider sampled every 500 ms → **`Light Lightness Set` (0x824C)** `[lightness u16 LE][tid]`, ack → Lightness element (`DimViewModel.java:85-114`, `dim/e.java:57`, `p234v7/J.java:45`); ± = Set(pct ± 1). No Generic Level / Delta / Move for lamps | `done*` (`set_lightness`, `light_lightness_get`; range not read) | `light` brightness; TODO: clamp to the range status (dimmer minimum) |
| Toggle / lock / forced-off / room functions | as §2.1 (`Light Lightness Set Unack` → `0xFEF5` for "dim all", `UpdateDeviceTypeGroup.java:108`) | as §2.1 | |

### 2.3 Tunable-white (DALI) lamp (`TunableWhiteLampDevice`, `Y7/t0.java`; DALI insert, puck DALI)

Composition: element 1 (loc `0001`) = `1300 Lightness` + `1303 CTL` servers; element 2 (also loc `0001`) =
`1002 Generic Level` + `1306 CTL Temperature` server (`docs/ios-app-data.md`). The app addresses the CTL server
element; the second element answers `Generic Level Status = −32768` when off (seen on air, `poc-gatt-proxy.md`).

| Control / state shown in the app | Mesh message(s) | Our status | HA representation |
|---|---|---|---|
| Dim page as §2.2 (brightness via `Light Lightness Set`) | as §2.2 | `done*` | `light` (`color_temp` mode with brightness) — as today |
| Colour-temperature wheel "*N* K" (rounded to 100 K), ± buttons (±1 %), bounded by the device's **CTL temperature range** (`lamp/elements/tunableWhite/d.java:84-103,222-246`, `controls/TunableWhiteWheel.java:254-259`; wheel attrs 2000..10000 K, `layout/fragment_tunable_white.xml:18-19`) | page open: **`Light CTL Temperature Range Get` (0x8262)** → status 0x8263 `[status][min K u16][max K u16]` (`p234v7/U0.java:29`, `resolver/c2.java:120-139`) and **`Light CTL Get` (0x825D)**, both ack (`tunableWhite/e.java:49-50`); control: debounced 500 ms → **`Light CTL Set` (0x825E)** `[lightness u16 = current lightness][temperature K u16][deltaUV s16 = 0][tid]`, ack (`TunableWhiteViewModel.java:121`, `p234v7/T0.java:39`); `Light CTL Temperature Set` (0x8264) is never used. State: **`Light CTL Status` (0x8260)** `[lightness u16][temp u16][target l][target t][remaining]` present values, `pct = (K−2000)/8000·100` (`resolver/E0.java:202-204`) | `done` (CTL Set/Get/Status on air; `coordinator.py:272-279,348-349`); range `todo` | `light` `color_temp_kelvin`; TODO read 0x8262 at start and use `[min,max]` instead of the fixed 2000–6000 K (`const.py:18-19` — a clamp copied from the gateway middleware, not a device limit; open question 8) |
| "All lamps" | `Generic OnOff Set Unack` / `Light Lightness Set Unack` → `0xFEF5` | `todo` | not needed |

### 2.4 Socket — switch socket (0x000C) and measuring socket (0x0003); measuring puck 0x0010 outputs are `MeasureLampDevice` (`Y7/n0.java`) with the same consumption page

| Control / state shown in the app | Mesh message(s) | Our status | HA representation |
|---|---|---|---|
| Tile: socket icon on/off, "Switched on/off", toggle, lock badge (`I6/a.java:118-128`, `app/utils/g.java:280`) | as §2.1 (`Generic OnOff Set/Status`) | `done` (`switch.py`) | `switch` (`outlet`) — as today |
| Detail page: on/off illustration + text, lock card (§2.11), "controlled by thermostat" card | `Generic OnOff Get` if unknown, `Admin Get 0x0009` | `done` / `todo` (lock) | – |
| **Consumption page** (measuring devices): current power (W, 2 dp), total consumption (Wh; kWh on the configuration page = raw/1000), operating hours, electricity cost (price from the local DB), on/off state (`socket/consumption/c.java:134-174,279,512`, `layout/view_total_consumption_info.xml:75`) | **every 5 s while the page is open** (`PowerConsumptionViewModel$observeDevice$1.java:144-146`, `FlowOperatorsKt$startRepeatingSuspendableJob$1.java:103`), sequential, all ack: **SIG `Generic Admin Property Get` (0x822D)** `[6D 00]` (0x006D power-on time) → **`Generic Admin Property Status` (0x4A)** `[pid u16][access u8][value LE]` = hours (uint24 on air, `poc-gatt-proxy.md`); **0x822D `[6A 00]`** (0x006A energy) → `0x4A`, LE integer, Wh (`resolver/T.java:44`, `Y1.java:85`; builders `p234v7/C2364i0.java:34`, `P0.java:33`); **`Sensor Get` (0x8231) `[81 00]`** → `Sensor Status` 0x0081 `LE × 0.1 W` (`p234v7/C2347a.java:30`, `F7/a.java:35`) | power/voltage/current `done` (publication + one `Sensor Get` at connect, `coordinator.py:241,280-289`); energy & hours `done` (`hub.energy.COUNTER_READS`: hours from the main element, the energy counters `0x0072`/`0x006A`/`0x000D` from the socket's *meter* element — the app's `[6A 00]` Get goes there too; `hidden-features.md` §2) | `sensor` energy (`energy`, kWh = Wh/1000, `total_increasing`), `sensor` operating time (`duration`, h, diagnostic) — polled (a few minutes is plenty; nothing publishes them) |
| Consumption chart, 24 h / 31 days (`socket/consumption/c.java:954-1047`) | **LBC User Property Get `CE 27 05`** `[10 50]` 0x5010 daily / `[11 50]` 0x5011 monthly, on page open and on spinner change (`$requestConsumptionData$1.java:61`, `p234v7/U.java:39-44`); status `D1 27 05`: 0x5010 = array of `u16 big-endian` samples ×0.1, entry *i* = *i* hours ago, `0xFFFF` = invalid; 0x5011 = `u24` BE ×0.1 per day (`J7/b.java:85-133`); segmented replies | `done*` (`energy_history.py`: read after a gap and imported into the *Energy* sensor's hourly statistics when they fit the lifetime counter; the chart semantics are unverified on air) | not an entity; imported into HA long-term statistics |
| Reset counters (configuration page) | SIG `Generic Admin Property Set` (0x48) `[pid][access 3][int32 LE 0]` (`p234v7/P0.java:49`, `C2364i0.java:50`) | `todo` | optional `button` "Reset energy counter" |
| Thresholds (turn-on / turn-off at *x* W for *t* s) | `Admin Get/Set 0x5004 / 0x5005` `[0081 u16 LE][time u16 LE s][value u24 LE ×0.1 W][active u8]` (`p234v7/L0.java:39-59`); **no "triggered" status exists** — `active` is the enable flag written by the app, edited only on the timer-profiles screen (`ToggleThreshold`) | `todo` | configuration only; skip (HA automations on the power sensor do the same) |
| "All sockets" | `Generic OnOff Set Unack` → `0xFEF8` | `todo` | not needed |

Publication: the socket's Sensor Server (element loc `0040`) publishes `Sensor Status` with `0x0081`, `0x005D`
(1 V), `0x005C` (0.01 A) on change when "sensor values for gateway" is on (verified on air). `0x006A` / `0x006D`
are never published.

### 2.5 Mini actuator (0x0004 switch mini, 0x000D blinds mini, 0x0010–0x0016 pucks) — the *input side* device (`MiniActuatorDevice.java`)

The app's "mini actuator" device is the button/binary-input side (`compatibles.g` key modes, `p108j8.d` button
layout, `i` input edge detection); its **outputs are separate load devices** (§2.1–2.3, §2.6) created from the
InsertId (`network-logic.md` §5.5). Runtime state on the tile: none (`I6/d.java:13-36`, `A(device, "")`).

| Control / state shown in the app | Mesh message(s) | Our status | HA representation |
|---|---|---|---|
| Tile: puck / mini icon, name, reachability; no state, no action | none | `n/a` | node device (already) |
| Detail page: battery icon in the header (only meaningful for the battery puck 0x0016, §2.9), sleep-mode fragment for the battery puck, configuration fragment otherwise (`MiniActuatorDetailActivity.java:168-171`, `resolver/e2.java:309-328`) | on load: `Generic Battery Get` (0x8223), `Admin Get 0x5009` (input edge detection), `0x5003` (key mode) per input, `0x5006/0x5007` property links (`configuration/MiniActuatorConfigurationViewModel$request*$1.java:78-81`); battery puck: 6 s keep-alive (§1.4) | `todo` (battery), `n/a` (config) | `sensor` battery for 0x0016 (§2.9) |
| Binary inputs (locations `0040`/`0041`) | what the input *sends*: the SIG client messages of its key mode (OnOff Set / Level / Scene Recall) to the load's element group or the room group; `0x5012` vendor events when in Gateway mode (§2.9) | `done*` (`event.py`, untested for pucks) | `event` per input (already) |
| Outputs (relay, dimmer, DALI, blind) | see §2.1 / §2.2 / §2.3 / §2.6 — identical messages to the output element | as those sections | `light` / `cover` |

### 2.6 2-channel actuator as blinds / shutter / awning (`BlindDevice`, `Y7/C0892b.java`)

Created for every load location whose node InsertId is `5 Blind` (push-button with blinds insert), and for
PID 0x000D "blinds actuator mini" / 0x0013 "puck blinds actuator" (`network-logic.md` §5.5). Two elements
(`Y7/C0892b.java:2298-2299`): the **position element** = the node's first `0x1002 Generic Level Server`
(`:2410-2419`), the **slat element** = the `0x1002` server on the *last* element of the device range
(`:2478-2509`, `c2()` `:1976-1982`). Vendor Admin properties (lock, mode, reference run) go to the node's first
LBC Admin Property Server element (`:2378-2387`). Device-type groups `0xFEF6` (position) / `0xFEF7` (slats).

Encoding used throughout: **JUNG percent = closedness**, `0 % = level −32768 = "Open"`, `100 % = level +32767 =
"Closed"`; `pct = clamp(round((level + 32768)·100/65535))` (`io/grpc/u.java:361-366`, `models/a.java:22`). Nothing
is inverted for shutters or awnings (`Y7/C0892b.java:1967-1969`; `elements/c.java:129-134`) — the only runtime
difference between the three operation modes is that the slat slider exists for `BLINDS` (0) only.

| Control / state shown in the app | Mesh message(s) | Our status | HA representation |
|---|---|---|---|
| Tile: "Closed" (present level = 32767), "Open" (present = −32768), otherwise "To *N*% Closed" with the **present** percent; skeleton until the first level is known; lock badge (wind icon if priority 0xFF); no moving indicator, no slat (`I6/a.java:40-64`, `strings.xml:91-93`, `adapter/a.java:338-348`) | state from **`Generic Level Status` (0x8208)** `[present s16 LE][target s16 LE][remaining u8]` of the position element; the app stores present % and target % (target := present when the short 2-byte form arrives) and ignores the remaining-time byte (`resolver/Q.java:146-148`; Nordic `GenericLevelStatus.java:69-77`) | `done*` (`cover.py`: `current_cover_position`/`is_closed`/`is_opening`/`is_closing` from the position element's Level Status) | `cover` with `device_class` from 0x1104 (`blind`/`shutter`/`awning`), `current_cover_position = 100 − pct(present)`, `is_closed = present == 32767`, `is_opening/is_closing` = target < / > present while they differ (the app's two-tone slider shows exactly this) |
| Detail page: position slider (thumb = target % if known else present %, shaded area = present %), "%d %" text (target preferred), arrows ▲ ▼, Stop, slat slider (BLINDS only, disabled while the blind is at 0 % = fully open, `elements/c.java:85-117,596-599`); all controls disabled while unreachable, while the three page-open Gets are pending, **while the lock state is unknown**, or while locked (`c.java:589-606`) | page open: **`Generic Level Get` (0x8205)** → position element and → slat element (ack), plus **`Admin Get 0x1104`** (`C2 27 05 [04 11]`, ack) and **`Admin Get 0x0009`** (`BlindsViewModel$observeCompatibles$1.java:243-292`, `DeviceDetailsViewModel$observeDevice$1.java:163-171`) | `done*` (position + slat Level Get and 0x1104 Get on page open; the 0x0009 lock Get is not sent — lock state row below is still `todo`) | see above; `0x1104` status value `u8` 0 BLINDS / 1 SHUTTER / 3 AWNING decides the device class and whether tilt is offered |
| ▲ open / ▼ close / ■ stop (one message per tap; skipped when already at 0 % / 100 %) | **`Generic Delta Set` (0x8209)** `[delta s32 LE][tid]`, delta **−1 = open (up)**, **+1 = close (down)**, **0 = stop**, ack, → position element (`BlindsViewModel$blindsOpening$1.java:74-77`, `$blindsClosing$1.java:74-77`, `$stopBlinds$1.java:71-73`, `p085h8/j.java:14,33,52`; always the acked opcode, `p234v7/H.java:38`); no `Generic Move Set` exists in the app | `done*` (`cover.py`: `open_cover`/`close_cover`/`stop_cover`; uses `Generic Move Set` rather than the app's `Delta Set`, functionally equivalent) | `open_cover` / `close_cover` / `stop_cover` (alternatively `Generic Level Set` −32768 / 32767 — the app uses Delta so that the actuator runs its move-time profile; keep Delta) |
| Position slider (sent on release only, `slider/Blinds.java:307-316`) | **`Generic Level Set` (0x8206)** `[level s16 LE][tid]`, no transition, ack, → position element (`$updateBlindLevel$1.java:82-84`, `p234v7/C2394y.java:41-46`) | `done*` (`cover.py:async_set_cover_position`) | `set_cover_position(p)` → level of `pct = 100 − p` |
| Slat slider (BLINDS only, sent on release, `slider/SlatSlider.java:297-304`) | `Generic Level Set` → slat element (`$updateSlatLevel$1.java:91-93`) | `done*` (`cover.py:async_set_cover_tilt_position`/`async_open_cover_tilt`/`async_close_cover_tilt`) | `set_cover_tilt_position`; `current_cover_tilt_position = 100 − slat pct` (0 % = slats open); `open_cover_tilt` / `close_cover_tilt` = level −32768 / 32767 on the slat element |
| Lock functions on the page: lock (keep position, timed), lock-out protection, **wind alarm** start/stop; state text "Blind moves to 0%" / "Hold hanging position" / "Keep current state"; wind badge on the tile | **Admin Set 0x0009** (`C3 27 05 [09 00][03][cmd][prio][time u16 LE][value…]`, ack): lock = `02 01 <s>`; lock-out = `02 FE <s>`; **wind alarm = `01 FF 00 00 00 00`** (cmd 1 = enforce value, priority 0xFF, no time limit, value = level 0 = open); unlock = `00` + previous fields (`LockFunctionViewModelDelegate$*.java`, `C8/a.java:80-92`, §2.11). State from **Admin Status 0x0009**: locked = cmd ∉ {0, 0xFF}; wind alarm = locked ∧ priority 0xFF (`C8/a.java:120-138`) | `todo` | `switch` "Operation lock" (config) + `binary_sensor` "Wind alarm" (`problem`/`safety`), optional `button` "Start wind alarm"; while locked the `cover` should reject commands like the app disables its controls |
| Reference run ("calibration") | Admin **Set** 0x110D `[01]`, then the app just waits `BlindMoveUpDownTime (0x1102, s) + 10 s`; nothing is read back (`parameter/ReferenceRunLoadingViewModel$observeDevice$1.java:83-105`, `$waitForReferenceRun$1.java:61-76`) | `n/a` | not an entity (configuration) |
| "All blinds" / room functions: open (0 %), close (100 %), position, stop, all slats | `Generic Level Set Unack` (0x8207) → `0xFEF6` (position) / `0xFEF7` (slats); stop = `Generic Delta Set` 0 → `0xFEF6` (sent unacknowledged, `UpdateDeviceTypeGroup.java:123-126,151-153,189-190`); inside a room: per-device unicast unack, stop → room group (`BlindCentralFunctionsViewModel.java:34-56`, `p125l6/c.java:174-189,277-300`) | **done** (*All blinds*, `cover.JungHomeAllBlinds`; unverified on hardware) | not needed (HA cover groups); the `0xFEF6/0xFEF7` unack fan-out is a cheap optimisation for "all blinds" automations |

Publication: the position and slat `Generic Level Server`s are wired to publish to their element group like every
supported server (`network-logic.md` §1.4; the real export shows `1002` servers publishing to their element
group, `docs/network-topology.md:577,597`), so `Generic Level Status` will arrive unsolicited — but the app does
not depend on it (no polling while the page is open; the page shows the value carried by the Set's acknowledgement
and re-reads on the next open). Whether the firmware publishes at movement start (with target ≠ present /
remaining time) or only at the end is **not derivable** (open question 2). Note that the app's tile is only
re-rendered when the *present* percent or the lock state changes (`adapter/a.java:338-348`).

### 2.7 Room thermostat (RTR, PID 0x000A, `domain/item/devices/RtrDevice.java`)

Elements (demo composition `RtrDevice.java:1034-1048`; lookups `:2148-2241`): **admin element** = first
LBC Admin Property Server (primary); **level element** = first `0x1002 Generic Level Server` = the set-point;
**sensor element** = first `0x1100 Sensor Server` (primary + 2 in the demo layout), which also carries the RTR's
`0x1001 Generic OnOff Client` used to switch bound heating actuators (`:2257-2271`). Device-type group `0xFEF9`.
The RTR has **no lock function, no OnOff server, no heating-demand / valve-state property** *as far as the app
model goes*: the only way the app can know a heating actuator is on is that actuator's own OnOff status
(`RtrDevice.java:67`; §8 of the RTR analysis — `RtrDevice` does not implement `E8.a`; the `0x120A` valve output and
`0x1208` heating optimisation are parameters, `strings.xml:617-643`). (Firmware / gateway: a real RTR **does** expose a
`0x1000 Generic OnOff Server`, and its state follows the heating output — the PI controller's ~15-minute PWM — which
the gateway integration reads as the RTR's `switch` and which gives `hvac_action` directly; the reduced-wiring list
in `network-logic.md` §1.4 names that server too. `docs/cross-repo-analysis.md` D9; needs the composition of a real
RTR to pin the element, §8 there.)

Errata for `properties.md` §1.5 found on the way: `SchedulerEnabledProperty` is **0x1246** (4678, `p056e8/r.java:2340`),
not 0x1236; `DropOfTempStateProperty` is **0x1212** (4626, `r.java:2040`), not 0x121A; `RtrSchedulerFunctionStatus`
0x1249 (4681) is correct.

| Control / state shown in the app | Mesh message(s) | Our status | HA representation |
|---|---|---|---|
| Tile: "Currently *x.x* °C" (or "-"), skeleton until the first temperature, "No connection"; nothing else (`I6/a.java:103-117`, `adapter/a.java:349-355`, `strings.xml:1313`) | **`Sensor Status` (0x52)** from the sensor element, first marshalled property must be **`0x004F` Present Ambient Temperature**: value = `signed LE raw × 0.5 °C`, clamped 5..30 (`F7/c.java:78-81`, `F7/a.java:42-53`; 1-byte Temperature 8 format) | `lib` (`sensor_values()` unmarshals; no 0x004F consumer) | `climate.current_temperature` (+ optional `sensor` temperature) |
| Detail page: target temperature arc slider 5..30 °C in 0.5 °C steps, − / + buttons, "Target"/"Current" label toggle (`roomtemperature/a.java:42-52`, `controls/RoomTemperatureSlider.java:571-583`, `shared/slider/b.java:172`) | state: **`Generic Level Status` (0x8208)** from the level element → `pct = clamp(round((level+32768)·100/65535))`, **`t = 5 + 25·pct/100`** (target preferred over present, `p046d9/b.java:36-44`; 0.25 °C resolution). Control: slider samples every 500 ms / ± 0.5 → **`Generic Level Set` (0x8206)** `[level s16 LE][tid]`, `pct = round((t−5)/25·100)`, `level = clamp(round(−32768 + pct/100·65535))`, ack → level element (`RoomTemperatureViewModel.java:221`, `$updateTargetTemperature$1$1.java:70-72`, `p256x9/c.java:584-586`, `models/a.java:37-45`) | `done*` (`climate.py`: `target_temperature` from the level element, `async_set_temperature`) | `climate.target_temperature`, `min_temp 5`, `max_temp 30`, `target_temperature_step 0.5`; `set_temperature` → Level Set |
| Mode buttons **Frost / ECO / Comfort** (`device_detail_rtr_operation_mode_*`); the active one is highlighted | FW ≥ 2.2.0.0 (SIG property 0x001A): state from **Admin Status 0x120B** `u8` 0 NONE / 1 COMFORT / 2 ECO / 3 FREEZE; control **Admin Set 0x120B** `[0B 12][03][mode]`, ack → admin element (`p234v7/C2391w0.java:39`, `$changeMode$1.java:71-73`, `resolver/C1901b0.java:291-308`). Older FW: no property — the app shows the mode whose preset (0x1203/0x1204/0x1205, `int16 LE °C×100`) equals the current target exactly, and "selecting a mode" writes that preset as the target via Level Set (`$observeCompatible$1.java:93-121`, `RoomTemperatureViewModel.java:242-255`) | `done*` (`climate.py`: `preset_mode` from 0x120B on FW ≥ 2.2.0.0, falling back to the preset-matches-target-temperature derivation on older firmware, like the app; `async_set_preset_mode`) | `climate.preset_mode` ∈ {`comfort`, `eco`, `frost`} (HA `PRESET_COMFORT`, `PRESET_ECO`, plus `frost` as a custom preset or HA's `away`); the preset temperatures 0x1203/04/05 as `number` entities (config) |
| **Auto / Manu** label on the slider ("Automatic operation: the temperature changes between Comfort and ECO modes" = the RTR's own heating profile / scheduler) | state **Admin Status 0x1246** `u8` 1 AUTO / 0 MANU (`$observeCompatible$1.java:123`, `RtrDevice.java:683-702`; `0x1249 RtrSchedulerFunctionStatus` updates the same capability if it ever arrives, `resolver/C1952s1.java:76`); control **Admin Set 0x1246** `[46 12][03][01|00]`, ack (`$switchOperationMode$1.java:66-86`, `p234v7/C0.java:38`) | `todo` | `climate.hvac_mode` `auto` (scheduler on) / `heat` (manual) — or a `switch` "Automatic operation"; there is no "off" |
| **Boost** start / stop ("heats at full power for 5 minutes"); while active the slider and mode buttons are disabled (`roomtemperature/g.java:637-668`) | state **Admin Status 0x120D** `u8` 1/0, **polled every 5 s while the page is open** (`$observeCompatible$1.java:170-172`, `FlowOperatorsKt.h`, `$requestBoostFunction$1.java:71-74`); control **Admin Set 0x120D** `[0D 12][03][01|00]`, ack (`$updateBoostMode$1.java:70-72`, `p234v7/C2369l.java:39-44`) | `todo` | `climate.preset_mode = boost` (HA `PRESET_BOOST`) or a `switch` "Boost"; poll 0x120D while boost is on to catch the automatic end after 5 min |
| Page open | one-shot: `Sensor Get` (0x8231) `[4F 00]` → sensor element; `Generic Level Get` → level element; `Admin Get 0x120B` (or 0x1203/04/05 on old FW); `Admin Get 0x1246`; then the 5 s boost poll (`$observeCompatible$1.java:166-172`, `$requestTemperature$1.java:97-119`, `$requestRtrModes$1.java:187-248`, `$requestSchedulerEnabledValue$1.java:75-78`). The home screen sends only `Sensor Get 0x004F` (`LoadStateForDevices.java:255-260`). **No periodic re-read of the temperature.** | `todo` | `_refresh_all`: the same five Gets per RTR |
| Preset temperatures page (Comfort / Eco / Frost values) | `Admin Get/Set 0x1203 / 0x1204 / 0x1205`, `int16 LE` in 0.01 °C (`p234v7/y0.java:41-48,67-83`, `X8/a.java:56-104`) | `todo` | `number` × 3 (config category, 5..30 °C step 0.5) |
| Connected heating actuators list (with remove button) | local DB: devices whose `0x1000` server subscribes to the RTR's OnOff-client publish address; removal = config unsubscribe + `Admin Set 0x1014 = 0` on the **actuator** (`GetConnectedDevices$work$1.java:108-134`, `RemoveMultiConnection$work$1.java:221-224`) | `n/a` | not runtime; could be shown as a device attribute (which lights/sockets this RTR drives) |
| Heating demand / valve open / window-open / presence / key lock | **not shown by the app**: it models no OnOff server on the RTR; `0x1212 DropOfTempState`, `0x121C/121D/121F KeyLock*` are declared but their parsers are `TODO()` stubs and `RtrDevice` has no field for them (`app/ui/timer/threshold/devices/d.java:327-330`, `RtrDevice.java:73-257`). (Firmware: the RTR's own `0x1000 Generic OnOff Server` tracks the heating output — D9 above; the firmware's ids for drop-of-temperature state / enable are `0x1225` / `0x1220`, `properties.md` §1.5 note.) | `todo` | `hvac_action` = `heating` / `idle` from the RTR's own OnOff status (`Generic OnOff Get` to that element in `_refresh_all`, plus its group publication); fallback: the bound actuator's OnOff state (the RTR's `Generic OnOff Set` to its actuators is visible on air but the app has no resolver for Set messages, `MeshNotificationChannel.java:1489-1492`) |
| "All RTRs" central function / room | `Generic Level Set Unack` → `0xFEF9` with `pct = int((t−5)/25·100)` (truncated, `UpdateDeviceTypeGroup.java:262-270`); per-device unicast unack inside a room (`p265y6/a.java:47-69`) | **done** (*All thermostats*, unverified on hardware) | not needed |

Publication: the RTR's Sensor Server publishes to its element group only when "sensor values for gateway" is
on (§1.3); the level element (set-point) and the admin server publish to the element group by the standard
wiring, so a wheel turn on the device *should* produce an unsolicited `Generic Level Status`, and a local mode
change *may* produce an `Admin Status 0x120B` — the app would consume both (`resolver/P1.java:50-57`) but never
relies on it (it re-reads on page open and polls boost). Verification = open question 3.

### 2.8 Motion detectors (PID 0x0007 1.1 m, 0x0008 2.2 m) and presence detector (0x0009)

Domain: `Y7/AbstractC0915d.java` (base), `p012a8/a.java` (motion, 2 PIR segments), `p012a8/b.java` (ceiling
presence, 3 PIR segments). The **detector element is the node's highest element**; every `0x60xx` property
capability is addressed there (Admin server `p012a8/a.java:1533-1559`, Manufacturer server `:1560-1590`,
capabilities `:2272-2456`). The detector's relay output(s) are *separate* load devices created from the insert
(§2.1) — the detector drives them itself through its `0x1001 Generic OnOff Client` on the detector element,
publishing to the load's element group or a room group (`AbstractC0915d.java:108-150`,
`ConfigureDetector$work$2.java:74-97`), i.e. the same wiring as a rocker → load link. The detector has **no lock
function, no OnOff server and no Sensor Status the app parses**.

| Control / state shown in the app | Mesh message(s) | Our status | HA representation |
|---|---|---|---|
| Tile: icon + "Motion detector" / "Ceiling presence detector", reachability — **static**, no detected indicator, no lux, no toggle (`I6/a.java:87-101`, `strings.xml:1088,1231`) | none | `n/a` | – |
| Detail page: activation-area diagram with per-segment **PIR sensitivity** (0/25/50/75/100 %), drag to change (`detectors/controls/DetectorView.java:456-478`, `detectors/c.java:108-118`) | page open: `Admin Get 0x6008`, `0x6009` (+ `0x600A` presence) sequentially (`DetectorSensorViewModel$requestSensitiveConfigurations$1.java:64-86`); status `u8` raw 0..255 (`resolver/A0.java:88-102`); write on release `Admin Set 0x600x [raw]` with `raw = round(pct/100·255)`, ack (`G6/b.java:159-167`, `p234v7/C2362h0.java:50-55`) | `todo` | configuration — optional `number` × 2/3 (config category, %). Low priority |
| **Walking test** ("Start / Stop walking test"; indicator icon while running; ends automatically after 5 min, `strings.xml:552-555`) | start: `Admin Set 0x6001 [01]` then `Admin Set 0x6003 [01]`, both ack; stop / after **300 000 ms** / page destroyed: `0x6001 [00]`, `0x6003 [00]` (`EnablePresenceTest.java:134-181`, `EnablePresenceTest$work$2$1$1.java:44`, `DisablePresenceTest.java:118-137`, `DetectorSensorViewModel.java:117-123`); page open reads `Admin Get 0x6001` (`$observeDevice$1.java:222-237`); status `u8` bool | `todo` | `switch` "Walking test" (config/diagnostic) that also auto-resets after 5 min |
| **Segment "triggered" highlight during the walking test** — the only "motion detected" indicator in the whole app | **`C8 27 05` Manufacturer Get `[05 60]` (0x6005 PresenceControlPir) every 1000 ms while the test flag is true** (`$observeDevice$1.java:148-160`, `FlowOperatorsKt$startRepeatingJob$1.java:65-72`); status `CB 27 05 [05 60][access][u8]`, bitfield **bit0 = PIR A, bit1 = PIR B, bit2 = PIR C** (`DetectorView.java:323-368`; the A-branch condition is rendered inverted by jadx, so the polarity of bit 0 needs an on-air check) | `todo` | `binary_sensor` motion / occupancy (device class `motion` for 0x0007/0x0008, `occupancy` for 0x0009) if `0x6005` reflects PIR activity **outside** the walking test too (open question 5); poll interval to be chosen (1 s is what the app does during the test). If not, motion can only be inferred from the driven load's OnOff status |
| "What should the detector control?" card (target load / room / function) | local DB + cached Config Model Publication status; changes go through the connection flows (`network-logic.md` §2) | `n/a` | device attribute (target of the OnOff client publication) |
| **Current brightness** ("Light situation", lux, "Update happens every 5 s") on the *parameter* page | **`C8 27 05` Manufacturer Get `[04 60]` (0x6004 PresenceBrightness) every 5 s while the page is open** (`providers/C1857m.java:59-72`, `providers/C1861q.java:44-47`, `C1866w.java:37-41`; builder `p234v7/C2366j0.java:28-31`); status `CB 27 05 [04 60][access][u16 LE]` lux (`resolver/A0.java:103-106`). Never delivered as a Sensor Status | `lib` (`vendor_property_get("manufacturer", 0x6004)` builds it; no decoder/entity) | `sensor` illuminance (`lx`, `measurement`), polled (e.g. every 30–60 s, faster while occupancy is on) |
| **Forced ON / OFF** ("Continuous ON/OFF activated") — read-only, shown on the *load's* lamp page with a hint how to release it on the hardware (slider switch / buttons, `strings.xml:423-429`); lamp controls disabled while active (`lamp/LampDetailActivity.java:162-219`) | `Admin Get 0x6016` to the detector element once when the lamp page opens (`lamp/LampViewModel$_forceOffMode$3.java:70-96`); status `u8` **0 INACTIVE, 2 OFF, 3 ON** (`ForcedOffMode.java:22-24`). A Set builder exists (`p234v7/r.java:34-49`) but no UI writes it | `todo` | `sensor` enum `auto/off/on` (diagnostic) on the detector; writing `0x6016` from HA (a `select`) is untested — the app treats it as hardware-set |
| Day mode 0x6015, operation mode 0x6006 (0 GUARD "mounting location", 1 PRESENCE "room brightness"), site 0x6017 (7 indoor / 112 outdoor), switch-on brightness threshold 0x600F (u16 lux), repetition time 0x6021 (set to 50 at configuration) | `Admin Get/Set`, parameter page only | `todo` | configuration — optional `switch`/`select`/`number` (config category); `0x600F` as `number` lx is the one users may want |
| Load timed-on: the detector's lamps get `TimedOnDuration 0x1007 = 120 000 ms` and `ManualOffEnable 0x100B = 1` at configuration (`ConfigureDetector.java:227-277`); **no remaining-time countdown anywhere** | `Admin Set 0x1007 [u32 LE ms]` on the load | `n/a` | the load's `light` (already) — `0x1007` as `number` (config) |
| Sensor Server `0x1100` on the detector | the app never parses its Sensor Status; the developer screen only names `PRESENT_ILLUMINANCE` (0x0055) and `PRESENCE_DETECTED` (0x004D) for detectors (`app/ui/meshnetwork/SensorSettingsViewModel.java:846`, Nordic `DeviceProperty.java:90,98`); publication to the element group is configured by "sensor values for gateway" (§1.3) | `lib` (`sensor_values()`) | if the detector publishes `0x004D` / `0x0055` (open question 5) these become the `binary_sensor` occupancy and `sensor` illuminance without any polling — capture first |

### 2.9 Battery wall transmitters (PID 0x0005 / 0x0006) and binary-input pucks (0x0015 mains, 0x0016 battery)

Domain classes: `Z7/i.java` (1-gang), `Z7/j.java` (2-gang) — both `ControlSwitchDevice` subclasses with the
`p108j8.b` *BatteryCapable* interface (`properties.md` §3.4); the battery puck is `p023b8/c.java`
(`MiniActuatorWallTransmitterDevice`, also `p108j8.b`). `Q1()` = "is battery device" is exactly these three
classes (`Y7/AbstractC0916e.java:335-337`). The mains binary-input puck 0x0015 is a `MiniActuatorHardwiredDevice`
(`p023b8/b.java`), i.e. behaves like the mini actuator inputs of §2.5 (no battery, no sleep mode).

There is **no runtime control** on these devices; the app only configures them (§2 of `network-logic.md`) and
shows a little state:

| Control / state shown in the app | Mesh message(s) | Our status | HA representation |
|---|---|---|---|
| Tile: name + button layout text ("Rocker / Rocker" …) — no live state (`I6/a.java:66-71`, `app/utils/g.java:154-188`); "No connection" once a request to it failed (`N1()`, §1.2) | none at runtime (layout from Admin property 0x5001, cached) | `n/a` | – (layout could become a device attribute) |
| Detail page header: **battery icon** unknown / medium / empty / critical (`app/utils/g.java:126-152`, `ControlSwitchDetailActivity.java:155-158`, `MiniActuatorDetailActivity.java:169`); *unknown* when the device is unreachable | **`Generic Battery Get` (0x8223)** → primary element, ack, sent when the configuration fragment opens (`controlSwitch/configuration/ControlSwitchConfigurationViewModel.java:278-288`, `CommunicateWithDevice … REQUEST, waitForStatus=false`); status **`Generic Battery Status` (0x8224)** `[level u8 (0..100, 0xFF unknown)][time-to-discharge u24][time-to-charge u24][flags u8]`; the app maps only **flags bits 3:2 = Battery Indicator** to `BatteryStatusIndicator` (0 CRITICAL, 1 LOW, 2 GOOD, 3 UNKNOWN; `app/ui/timer/threshold/devices/d.java:247-267`, Nordic `GenericBatteryStatus.java:135-137,165-176`) and ignores the level byte | `todo` (no builder, no decoder) | `sensor` battery: if the level byte ≠ 0xFF use it as `%` (device class `battery`, diagnostic); otherwise an enum `sensor` (`critical/low/good/unknown`) from the indicator bits. Only obtainable while the device is awake (below) |
| Detail page banner "Power saving mode — press the button to wake it" (`strings.xml control_switch_sleep_mode_*`, `ControlSwitchDetailActivity.java:143-150`; shown for every `Q1()` device) | **keep-alive**: while the page is open, acknowledged **`C2 27 05` Admin Get `[01 50]` (0x5001 ButtonLayout)** every 6 s; success → *DeviceAwake*, timeout → *DeviceNotAwake* + retry after 1 s (simple-mode `KeepLowPowerDeviceAwake$work$1:89-124`; `KeepLowPowerDeviceAwake.java:81-84`; `$work$2.java:44`) | `todo` | not needed for runtime: the keys work while the device sleeps (`strings.xml control_switch_sleep_mode_dialog_text`). Useful only to read the battery: the transmitter is awake right after it sent a key event, so send `Generic Battery Get` within the first second after a `0x5012`/OnOff/Scene message from one of its elements (open question: how long it stays awake) |
| Key presses (what the button *sends*) | key mode Gateway (6): vendor **`D0 27 05` User Property Set Unack** `[12 50][counter u8][event u8]` to the gateway's element group, `05` click / `06` hold-start / `04` hold-end, each published twice; key modes Light/Switch/Move/Scene/RTR: the SIG client messages of `network-logic.md` §2.1 (OnOff Set, Level/Delta/Move Set, Scene Recall) to the load's element group or the button's own group | `done` (0x5012 events), `done*` (direct SIG messages; `coordinator.py:290-296`) | `event` (device class `button`), already in `event.py`. The battery puck's two binary inputs (locations 0x40/0x41) fit the same entity |
| Sleep/awake state | derived from the keep-alive result only | `todo` | optional `binary_sensor` "awake"? Not recommended (only meaningful while polling) |

Everything else the app shows for these devices (layout, key assignments, LED colours, device-key locks) is
configuration read/written through Admin properties 0x5001–0x5008, 0xA0xx, 0x0001 (`properties.md` §1.6/§1.8/§1.2)
and needs the keep-alive to succeed.

### 2.10 Gateway node (PID 0x000B)

| Control / state shown in the app | Mesh message(s) | Our status | HA representation |
|---|---|---|---|
| Tile: icon + name only, no state, no action (`I6/a.java:72-74`, `app/utils/g.java:283`) | none | `n/a` | device only (already: "listed as a device, no entities") |
| Gateway detail pages (connection, network, log, system, update, sensor-values) | all REST over `https://<ip>/api/junghome/…`; the only mesh traffic is the read of `0xC001` token / `0xC002` IP / `0xC003` TLS fingerprint with **`C8 27 05` Manufacturer Property Get** at app start and on HTTP 401/404/503 (`network-logic.md` §6.1) | `lib` (`vendor_property_get("manufacturer", …)` exists) | none recommended; the gateway IP (`0xC002`) could be a diagnostic attribute, the token must never be exposed |
| "Sensor values for gateway" toggle per device | configuration (`ConfigModelPublicationSet` of each Sensor Server to its element group), see §1.3 — this is what makes socket / detector / RTR sensor statuses visible to us at all | `n/a` (we rely on it being on) | – |
| Buttons linked to the gateway | see §2.9: `0x5012` events on the gateway's element group (`C005` here) | `done` | `event` |

### 2.11 Lock function (every lamp, socket, mini-actuator output and blind — `E8.a` devices)

The "lock function" freezes an output against local and remote operation, optionally with a time limit; for
blinds it also carries the lock-out protection and the wind alarm. The app reads it once when a device page
opens (`DeviceDetailsViewModel$observeDevice$1.java:159-171`) and after a toggle when unknown
(`ToggleDevice.java:124-139`), and shows it on tiles and headers (§2.1).

| Control / state shown in the app | Mesh message(s) | Our status | HA representation |
|---|---|---|---|
| Tile / header lock badge (`ic_function_lock_closed`, or `ic_lock_wind` when priority 0xFF); all controls of the page disabled while locked; button "Lock / Unlock device", "Start / End wind alarm" | state: **`C2 27 05` Admin Get `[09 00]`** → **`C5 27 05` Status `[09 00][access][cmd u8][priority u8][time u16 LE s][value…]`** (`C8/a.java:20-64`); `locked = cmd ∉ {0, 0xFF}`; `wind alarm = locked ∧ priority == 0xFF`; `time 0` = no limit; `value` = enforced level/onoff bytes. Note the request matcher does **not** accept a User-property status as the reply for property 9 (`vendor-models.md` §5) | `done` (`switch.py` *Lock*, `lock_mode` / `lock_time_limit` attributes; unverified on air); no wind-alarm sensor | `switch` "Operation lock" (config category) with attributes `mode` (`keep_state` / `enforce_on` / `enforce_off` / `lockout` / `wind_alarm`), `time_limit_s`; `binary_sensor` "Wind alarm" (`problem`) on blinds |
| Lock (keep current state), with time limit *t* | **`C3 27 05` Admin Set `[09 00][03][02][01][t u16 LE]`**, ack (`LockFunctionViewModelDelegate$lockDevice$1.java:78-80`, `C8/a.java:80-93`, `D8/a.java:83-87`) | `done` (time from the *Lock time limit* number) | `switch.turn_on` |
| Unlock | **`[09 00][03][00][prio][time][value…]`** — cmd 0 with the previously read fields (`$unlockDevice$1.java:74-76`) | `done` | `switch.turn_off` |
| Lock-out protection (blinds), time *t* | `[09 00][03][02][FE][t u16 LE]` (`$startLockOutProtection$1.java:76-85`) | `done` (`select.py` *Lock function*, the load's *Lock time limit*; unverified on air) | `select`/`button` (optional) |
| Wind alarm start (blinds) | `[09 00][03][01][FF][00 00][00 00]` — enforce level 0 (open) and lock, no time limit (`$startWindAlarm$1.java:72-81`); end = unlock | `done` (`select.py` *Lock function*; `binary_sensor.py` *Wind alarm*; unverified on air) | `button` "Start wind alarm" / `switch` (optional) |
| Rocker "lock" key assignment (turn on/off and lock) | `[09 00][03][01][01][t][onoff u8]` written into the key's `KeySetPropertyValueUpOn/DownOff` (`C8/a.java:94-107`, `network-logic.md` §2.6) — configuration | `n/a` | – |

Whether a node publishes an unsolicited `0x0009` status when a rocker in property mode locks it is open
question 1; until verified, HA must poll (`_refresh_all` + after each command).


## 3. Publication vs polling — per entity summary

| Device / value | Source message | Unsolicited? | HA strategy |
|---|---|---|---|
| Lamp / socket / output on-off, brightness, colour temperature | OnOff / Lightness / CTL Status | **yes**, on change (verified) | push; one Get at connect (done) |
| Socket power, voltage, current | Sensor Status (loc `0040` element) | **yes**, on change, when "sensor values for gateway" is on (verified) | push; one Sensor Get at connect (done) |
| Socket energy total, operating hours | SIG Admin Property Status 0x4A | no | poll (minutes) |
| Blind position / slat | Generic Level Status | configured; cadence unverified | push + Get at connect and after each command |
| Blind mode (0x1104), lock state (0x0009) | LBC Admin Status | unknown (open q. 1) | Get at connect and after each command |
| RTR current temperature | Sensor Status 0x004F | expected when "sensor values for gateway" is on; unverified | push + Sensor Get at connect; fall back to polling |
| RTR set-point | Generic Level Status (set-point element) | configured; unverified | push + Get at connect |
| RTR mode / boost / auto-manu | LBC Admin Status 0x120B / 0x120D / 0x1246 | unknown | Get at connect; poll boost while active (app: 5 s) |
| Detector brightness (0x6004), PIR state (0x6005) | LBC Manufacturer Status | **no** (Manufacturer server not wired for publication) | poll |
| Detector SIG sensor properties (0x004D / 0x0055?) | Sensor Status | if published (open q. 5) | push |
| Detector forced-off (0x6016) | LBC Admin Status | unknown | poll rarely / on demand |
| Battery indicator | Generic Battery Status | **no** | Get right after a key event, else on demand |
| Button / input events | vendor 0x5012 (Gateway mode) or SIG client messages | **yes** | push (done) |
| Scene register | Scene Register Status | yes | not needed |

## 4. Prioritised TODO for the integration

Ordered by user value × certainty. "Builders/decoders" refer to `jhmesh/messages.py`; "hub" to
`custom_components/junghome_ble/coordinator.py`.

1. **Cover platform for blinds / shutters / awnings** (`cover.py`, new device kind `blind` in `jhmesh/devices.py`:
   a node whose InsertId is `5 Blind` / product 0x000D / 0x0013, position element = first `1002` Generic Level
   Server at location `0001`, slat element = last element of the node with a `1002` server).
   Builders: `generic_level_get()`, `generic_level_set(level, ack)`, `generic_delta_set(delta, ack)`.
   Decoder: `generic_level_status(p) → (present, target, remaining)` + percent conversion.
   Hub: `_refresh_all` sends `Generic Level Get` to both elements and `Admin Get 0x1104`; `_on_message` stores
   position/slat per element; `is_closed`, `is_opening/closing` from present vs target; commands per §2.6.
   HA: `cover` with device class from 0x1104 (`blind` / `shutter` / `awning`), `current_cover_position = 100 − pct`,
   `current_cover_tilt_position` from the slat element, `open/close/stop/set_position/set_tilt_position`.
2. **Climate platform for the room thermostat** (`climate.py`): entity per RTR node; set-point via
   `Generic Level Set` to the set-point element (`pct = (t − 5)/25·100`), current temperature from
   `Sensor Status 0x004F` (raw × 0.5 °C), presets from Admin `0x120B` (comfort/eco/frost; needs FW ≥ 2.2.0.0,
   else write the preset temperature as set-point), boost `0x120D` and auto/manu `0x1246` as `switch`es (or preset `boost` / hvac_mode `auto`).
   Builders: `vendor_property_set(kind, pid, value, access=3)`, `sensor_get(0x004F)` (exists).
   Decoders: LBC property status `(pid, access, value)` (exists only inside `describe()`), `sensor_values` (exists).
   Hub: `_refresh_all` adds `Sensor Get [0x004F]`, `Generic Level Get` (set-point element), `Admin Get 0x1203/04/05/120B/120D/1246`.
3. **Detectors**: `binary_sensor` motion/occupancy + `sensor` illuminance (lx) from Manufacturer properties
   `0x6005` / `0x6004` (poll; interval to be chosen — the app polls 0x6005 every 1 s only during the walking test),
   `switch` walking test (`0x6001`+`0x6003`), `select` forced-off (`0x6016`: auto/off/on). Their relay outputs are
   already lights. Check first (open question 5) whether the detector's Sensor Server publishes presence /
   ambient-light SIG properties — that would replace the polling.
4. **Lock function** (all lamps, sockets, blinds, mini-actuator outputs): decoder for `0x0009`
   `[cmd u8][priority u8][time u16 LE s][value…]` (`C8/a.java:20-64`), builder for lock / unlock / lock-out /
   wind-alarm payloads (§2.11); `switch` "Operation lock" (config category) + attributes (mode, priority, remaining),
   `binary_sensor` "Wind alarm" on blinds; `Admin Get 0x0009` for every lockable element in `_refresh_all`.
5. **Battery sensor** for wall transmitters / battery puck: `generic_battery_get()` builder, `0x8224` decoder;
   send the Get opportunistically right after a key event from the node (device awake), and on demand.
6. **Socket energy**: `sig_property_get(0x822D, 0x006A)` / `0x006D` builders and the `0x4A` decoder
   (`[pid u16][access u8][value LE]`); `sensor` energy (kWh, `total_increasing`, raw Wh / 1000) polled every few
   minutes, `sensor` power-on time (h, diagnostic). Optional: import the `0x5010` daily / `0x5011` monthly chart
   samples (LBC User Get, big-endian ×0.1) into long-term statistics.
7. **`Time Set` broadcast** (0x5C → 0xFFFF, unack) at connect and once a day, so device timers / astro schedules
   keep working when no phone or gateway sets the time.
8. ~~**Per-device availability**~~ **done** (§1.2). Like the app: mark an element unavailable after 3 request timeouts, available again
   on any message from it; battery devices unavailable after one timeout. Needs the hub to use `ProxyClient.request()`
   for state refreshes instead of fire-and-forget Gets.
9. Small fidelity items: use the *present* field of statuses like the app (`coordinator.py:265-277`); read
   `Light CTL Temperature Range Get` (0x8262) at start to set per-light min/max kelvin instead of the fixed
   2000–6000 K; `Light Lightness Range` for dimmer minimum; toggle semantics ("off if unknown") are irrelevant for HA.
10. Not worth an entity: consumption charts on the tile, thresholds (configuration), scheduler/timer editing,
    scene editing, central "all lamps/blinds" groups (HA has its own groups; sending to `0xFEF5..9` unack would be
    a cheap optimisation for room-wide actions).

## 5. Open questions (need an on-air capture or a device we do not own)

1. **Unsolicited vendor Admin property statuses.** The LBC Admin Property Server is wired to publish to the
   element group, but nothing in the app relies on it. Does a node publish `C5 27 05` when the lock state
   (`0x0009`), blind mode (`0x1104`), RTR HVAC mode / boost (`0x120B/0x120D`), detector forced-off (`0x6016`) change
   locally (e.g. RTR mode changed on its own display, wind alarm from a rocker)? If not, these must be polled.
   *Partly answered (`hidden-features.md` §9): a property changed by an Admin Set is **not** published
   (unicast Status only), and a 10-minute sniffer baseline of the idle mesh holds no vendor property status to any
   group at all — only Sensor Statuses. Local changes on devices this installation lacks (RTR, blinds, detectors)
   remain untested; plan on polling.*
2. **Blind movement reporting.** Does the position element publish `Generic Level Status` at movement start
   (target ≠ present, remaining time) and at the end, or only at the end? Does the slat element publish during
   the slat move? Is the `Generic Delta Set` direction semantic (−1 up / +1 down / 0 stop) also honoured when sent
   unacknowledged to a group (the app does so for "all blinds stop")?
3. **RTR publications.** Does the RTR publish `Sensor Status 0x004F` (cadence?) when "sensor values for gateway" is
   on, and `Generic Level Status` from the set-point element when the wheel is turned? ~~Is there any message that
   reveals heating demand or the valve output?~~ Answered by the gateway side: the RTR carries a `0x1000 Generic
   OnOff Server` whose state is the heating output (~15-min PWM of the PI controller; the gateway polls it every
   15 s like every OnOff server and it publishes to the element group like every server). The reduced-wiring list of
   `CreateElementConnectionGroups.g()` names that server (`network-logic.md` §1.4) while the app's RTR model only has
   an OnOff *Client*. Still open: which element hosts it (the composition of a real RTR would settle this) and
   whether it publishes on every PWM edge.
4. **Battery status.** Is the level byte of `Generic Battery Status` a real percentage on JUNG transmitters
   (the app ignores it and only uses the indicator flags)? How long does a wall transmitter stay awake after a
   key press (polling window)?
5. **Detector Sensor Server.** ~~Which SIG sensor properties does a motion / presence detector publish~~ —
   confirmed from the gateway side: detectors deliver **`PRESENCE_DETECTED` (`0x004D`, 1 byte 0/1)** and
   **`PRESENT_ILLUMINANCE` (`0x0055`, ÷100 lux on device FW > 1.4.0.0)** to the gateway API, which requests them with
   `Sensor Get` every 15 s and also hears the element-group publications (`docs/cross-repo-analysis.md` §1.4, D13;
   the app's developer screen names the same two ids, `app/ui/meshnetwork/SensorSettingsViewModel.java:846`).
   Still open: the publication cadence, and whether `0x6005 PresenceControlPir` reflects PIR activity outside the
   walking test (and is bit 0 "set = triggered" like bits 1–2). Unit of `0x6004` (assumed lux).
6. **Socket totals.** Unit of `0x006A TotalDeviceEnergyUse` (app divides by 1000 → assumed Wh) and the
   `0x5010/0x5011` chart scale (×0.1 of what?).
7. **Wind alarm value bytes** (`01 FF 00 00 00 00`): is the 2-byte value a Generic Level (0 = up) and is the same
   command accepted by lamps/sockets (the app shows the wind alarm only for blinds)?
8. **TW lamps**: the app's colour-temperature wheel spans 2000–10000 K by percent; do the DALI inserts answer
   `Light CTL Temperature Range Status` with a narrower range? Our fixed 2000–6000 K is **not** a device fact: it
   is the gateway middleware's own clamp (`ColorTemperatureState.js:60,79,94-103`, `range {min: 2000, max: 6000}`
   applied before every publish), while the gateway's datapoint catalogue lists 2000–10000 for `color_temperature`
   (`cdb_types_datapoints.json:63-67`). Read `0x8262` per light (`const.py:18-19`, D8).
