# JUNG HOME Android app — device-property catalogue

Source: `android/jadx-out/sources/` (jadx decompilation of the JUNG HOME Android app).
All paths below are relative to that directory; `file:line` cites the decompiled Java.

Scope: every Bluetooth-Mesh *device property* the app knows (SIG Generic Property IDs and
JUNG/"LBC" vendor property IDs), the byte layout of each value, the enums the values map
to, how the properties are combined into the "device" objects the app shows, and the
product-ID table. Message *framing* (opcodes, vendor-model status layout) is only summarised
where needed to read the tables; it is documented by the other agent.

---

## 0. How to read the tables (models, access, framing summary)

### 0.1 Property models the app talks to

| Model | ID | Used for | Get message | Set message | Status opcode (int → hex) |
|---|---|---|---|---|---|
| SIG Generic Manufacturer Property Server | `0x1012` (4114) | SIG props 16, 17, 26 | `GenericPropertyGet(opcode 33323=0x822B)` | `GenericManufacturerPropertySet` | 70 (`0x46`) |
| SIG Generic Admin Property Server | `0x1011` (4113) | SIG props 0x6A, 0x6D | `GenericPropertyGet(33325=0x822D)` | `GenericAdminPropertySet(access=3)` | 74 (`0x4A`) |
| SIG Generic User Property Server | `0x1013` (4115) | (debug screen only) | `GenericPropertyGet(33327=0x822F)` | `GenericUserPropertySet` | — |
| SIG Sensor Server | `0x1100` (4352) | sensor props 0x4F, 0x81 | `SensorGet(DeviceProperty)` | — (cadence/setting via Sensor Setup) | 82 (`0x52`) |
| LBC Generic Admin Property Server (vendor) | `0x0527:0x1011` (86446097) | almost all JUNG config props | `G7.a` opcode 337666 (`0x02`) | `G7.b` opcode 337667 (`0x03`) | 12920581 (`0xC52705`) |
| LBC Generic Manufacturer Property Server | `0x0527:0x1012` (86446098) | versions, presence brightness/PIR, gateway strings | `G7.c` 337672 (`0x08`) | `G7.d` 337673 (`0x09`) | 13313797 (`0xCB2705`) |
| LBC Generic User Property Server | `0x0527:0x1013` (86446099) | InsertId, energy charts | `G7.e` 337678 (`0x0E`) | `G7.f` 337679 (`0x0F`) | 13707013 (`0xD12705`) |

**Which element serves which SIG property (read off the devices, `docs/hidden-features.md` §2):** the
*primary* element's SIG Manufacturer / User servers hold the identity block — `0x000C` Device Date of Manufacture
(uint24 days since 1970: `39 4B 00` = 2022-09-22 on a socket), `0x0010` hardware revision, `0x0011` manufacturer
name, `0x001A` software revision — and the SIG Admin server of a socket `0x006D` power-on time. The socket's
**meter element** (location `0x0040`, the Sensor Server) holds the energy counters: SIG Admin `0x006A` total
energy (RW, the app's reset), SIG Manufacturer `0x0072` precise total energy and `0x000D` energy since turn-on
(User mirrors all three) — so `0x006A` is *not* absent, it was asked of the wrong element before. Its Sensor
Descriptor lists `0x0052` (`PresentDeviceInputPower` in the gateway firmware), `0x005C`, `0x005D`, `0x0057`
(`PresentInputCurrent`) and `0x0081`, all RMS with a 4.6 s update interval; `0x0052` / `0x0057` read 0 on a loaded
socket (load side only). SIG servers on *key* elements answer nothing.

Sources: model IDs `de/jung/junghome/domain/item/mesh/nodes/server/GenericAdminPropertyServer.java:18`,
`LBCGenericAdminPropertyServer.java:18`, `LBCGenericManufacturerPropertyServer.java:18`,
`LBCGenericUserPropertyServer.java:18`; SIG model constants in
`de/jung/junghome/domain/item/mesh/nodes/server/MeshModel.java:937,1529,3458`; message selection per model in
`de/jung/junghome/data/mesh/api/JungMeshApiImpl.java:250-268` (read) and `:312-326` (write);
vendor get/set builders `G7/a.java:24-31`, `G7/b.java:28-44`, `G7/c.java:20`, `G7/d.java:24-30`, `G7/e.java:20`;
status opcodes accepted by the resolver base `de/jung/junghome/data/mesh/resolver/AbstractC1972z0.java:43`.
Client-side models present on button elements: `0x1001` GenericOnOffClient, `0x1003` GenericLevelClient,
`0x1302` LightLightnessClient, `0x1305` LightCtlClient, `0x1205` SceneClient, `0x1102` SensorClient and the
vendor property client `0x0527:0x1015` (86446101) — `p076g8/a.java:67,166,267,368,469,570,671,772`.

### 0.2 Vendor property message payloads (as the app builds/parses them)

* **LBC Admin Get**: `[propertyId u16 LE][extra params…]` — extra params only used by
  AstroSchedulerRegister (1 byte register index) (`G7/a.java:25`, `B7/a.java:38-43`).
* **LBC Admin Set**: `[propertyId u16 LE][userAccess u8][value…]` (`G7/b.java:34-39`).
  The app always sends `userAccess = 3 (READ_AND_WRITE)` except DimMode which sends `1 (READ)` (`p234v7/C2386u.java:39`).
  On air (the app on the user's phone) DeviceLock 0x0001 and the delays 0x1001 / 0x1002 / 0x1007 go out
  with `1` too, although 2.2.0's builders say 3; the node keeps the byte and reports it in its Status.
* **LBC Manufacturer Get/Set** and **LBC User Get/Set**: `[propertyId u16 LE][value…]` (no access byte) (`G7/c.java:20`, `G7/d.java:25`, `G7/e.java:20`, `JungMeshApiImpl.java:318`).
* **All three LBC Status messages**: `[propertyId u16 LE][userAccess u8][value…]`
  (`AbstractC1972z0.java:46-51`). The property ID is resolved with `e8.r.a.a(id)` (`p056e8/r.java:13-32`).
* `UserAccess` enum: `READ=1, WRITE=2, READ_AND_WRITE=3, UNKNOWN=0` (`de/jung/junghome/domain/item/mesh/UserAccess.java:23-26`).
* Property ID is serialised little-endian everywhere (`p056e8/r.java:2446-2453`, `r.a()`).

**Endianness convention used by the resolvers.** Each status resolver gets the value bytes twice:
`f()` = a little-endian `ByteBuffer` over the raw bytes, and `e()` = a Nordic `BitReader` over the
**byte-reversed** value (`AbstractC1972z0.java:60-65`). `BitReader.getBits(n)` is MSB-first
(`no/nordicsemi/android/mesh/utils/BitReader.java:17-57`). So "`e().getBits(16)` first" means
"the *last* two bytes of the value read as a big-endian word" = the highest LE word. Where a property
is written with `BitWriter`/`k0(n)` (MSB-first) and then `n.Q()` (reverse) the result is again a
little-endian integer whose bit numbering is given in the tables below.

### 0.3 The debug `writeProperty` hex helper (`UtilsKt.i`)

`JungMeshApiImpl.q1(propertyId, elementAddress, valueHexString, userAccess, size, modelId)`
(`JungMeshApiImpl.java:290-345`) is only used by the developer "mesh model" screen
(`de/jung/junghome/app/ui/meshnetwork/model/MeshModelViewModel$writeProperty$1.java:68`) and the demo gateway.
`UtilsKt.i(size, hex)` (`de/jung/common/UtilsKt.java:153-179`) takes a **hex string typed by the user**,
left-pads it with `'0'` characters (`Q4/b.java:185-202` progression; when the string is shorter than
`size` hex chars it appends `4*(size-len)+1` zeros — an odd formula), splits it into 2-char chunks
(`kotlin/text/n.java:9-23`, `chunked(2)`) and parses each chunk as one byte. The bytes therefore go on the
wire **in the order typed (big-endian as typed)**; no numeric conversion happens. It is *not* the encoder used
by the normal configuration flows — those are the `MessageBuilder`s in `p234v7/`, `p245w7/`, `p254x7/`,
`p266y7/`, `A7/`, `B7/`, `C7/` (see §1).

---

## 1. Master property table

Legend: **Model** = which property server the app addresses (A = LBC Admin vendor, M = LBC
Manufacturer vendor, U = LBC User vendor, SIG-M/SIG-A = SIG manufacturer/admin property server,
SENS = SIG Sensor server). **R/W** = what the app does (R = only reads, RW = reads and writes,
W1 = write-only trigger). "TODO" = the app declares a resolver that reads 1 byte and then hits a
Kotlin `TODO()` (`throw null`), i.e. the value layout is unknown to the app itself.
Integer types are little-endian unless stated. "Devices" = which device classes carry the capability (§3.4).

### 1.1 SIG-defined property IDs (Bluetooth Mesh Device Properties)

| ID hex (dec) | Name in code (`p056e8/r.java`) | Model | R/W | Value layout | Devices |
|---|---|---|---|---|---|
| `0x0010` (16) | `ManufacturerHardwareRevisionProperty` (`r.b.a`, `:53`) | SIG-M | R | raw bytes, stored byte-reversed in `G8.a` (`p234v7/Q.java:29`, `resolver/U.java:75-77`, `resolver/T.java:25-30`) | all |
| `0x0011` (17) | `ManufacturerNameProperty` (`r.b.C0386b`, `:77`) | SIG-M | R | raw bytes (`p234v7/S.java:29`, `resolver/V.java:75-77`) | all |
| `0x001A` (26) | `ManufacturerSoftwareVersionProperty` (`r.b.c`, `:100`) | SIG-M | R | treated as an **ASCII digit string**: `String(bytes)` with NULs removed → split into 2-char groups → each group stripped of a leading `'0'` → joined with `.` (`resolver/X.java:346`, `app/ui/scheduler/n.java:257-260`, `data/repositories/MeshPropertyRepositoryImpl.java:479+`). E.g. `"02020002"` → `"2.2.0.2"`. Compared against `"2.2.0.0"` (RtrHvacMode support) and `"1.0.3.3"` (touch sensitivity) (`resolver/C1901b0.java:291-308,385-394`) | all |
| `0x006A` (106) | `TotalDeviceEnergyUse` (`r.b.d`, `:123`) | SIG-A | RW | read: LE integer (all bytes, `BigInteger(reversed)`), scale ×1 (`resolver/T.java:35-46`); app treats raw as Wh (`/1000` → kWh, `ConfigurationConsumptionViewModel.java:89-92`). Write (reset): `GenericAdminPropertySet(access=3, int32 LE)` (`p234v7/P0.java:37-50`) | measuring socket / measuring puck |
| `0x006D` (109) | `TotalDevicePowerOnTime` (`r.b.e`, `:146`) | SIG-A | RW | read: LE integer ×1 (`resolver/Y1.java:76-88`); write reset int32 LE (`p234v7/C2364i0.java:38-50`) | measuring socket |
| `0x004F` (79) | `PRESENT_AMBIENT_TEMPERATURE` (Nordic `DeviceProperty`) | SENS | R | `SensorStatus` raw LE int × 0.5 °C, clamped to 5..30 for UI (`F7/a.java:42-52`, builder `p234v7/C2349b.java:30`) | RTR |
| `0x0081` (129) | `ACTIVE_POWER_LOAD_SIDE` | SENS | R | `SensorStatus` raw LE int × 0.1 W (`F7/a.java:31-41`, builder `p234v7/C2347a.java:30`) | measuring socket / measuring puck / puck energy |

### 1.2 JUNG/LBC vendor property IDs — node / identification / security

| ID hex (dec) | Name in code | Model | R/W | Value layout | Devices |
|---|---|---|---|---|---|
| `0x0001` (1) | `DeviceKeyLockProperty` → capability *DeviceKeyConfiguration* (`r.c.C2076q`, `:1893`) | A | RW | `uint16 LE` bit-field of 5 flags. **Encoder** (`J8/C0541i.java:72-84`, MSB-first writer then byte-reverse): bit0=`isLocalFactoryResetLockActive`, bit1=`isFactoryResetWithTimeLimitActive`, bit2=`isLocalDevicesLockActive`, bit3=`isKeyLockActive`, bit4=`isConfigurationLockActive`, bits5-15=0. **Decoder** (`app/ui/project/gateway/detail/z.java:468-478`) assigns the bits in the opposite order (bit4→LocalFactoryResetLock … bit0→ConfigurationLock). Field names `V7/C0849f.java:63-83`. See §5. | control switches, sockets, detectors, RTR, mini actuators |
| `0x0002` (2) | `InsertIdProperty` → *ActuatorCapability* (`r.c.J`, `:600`) | U | R | 2 or 4 bytes. Parser (`resolver/C1922i0.java:105`): `actuatorFunctionId = uint16 LE` from the **last** two bytes; if the value is exactly 4 bytes (`bitsLeft()==32`, see BitReader quirk `BitReader.java:21-23`) the **first** two bytes are `insertType uint16 LE`, else insertType = Unknown. Set is `UnsupportedOperationException` (`p234v7/C2351c.java:34-38`). Enums §2.1, §2.2 | all (one per LBC-user-property element) |
| `0x0003` (3) | `SecureElementVersionProperty` (`r.c.z0`, `:2377`) | M | R | bytes → dotted decimal, byte order kept (`UtilsKt.h`, `de/jung/common/UtilsKt.java:141-151`; `X8/a.java:90-93`) | all |
| `0x0004` (4) | `BootloaderVersionProperty` (`r.c.C2065h`, `:1468`) | M | R | same formatting (`X8/a.java:52-55`) | all |
| `0x0005` (5) | `STM32VersionProperty` (`r.c.x0`, `:2287`) | M | R | same formatting, saved as "STM software version" (`MeshPropertyRepositoryImpl.java:1085`) | RTR (`Z8.b`) |
| `0x0009` (9) | `EnforceOutputProperty` → *LockFunction* (`r.c.C2081w`, `:2217`) | A | RW | `[controlCommand u8][priority u8][time u16 LE seconds][value bytes…]` — encoder `D8/a.java:75-91`, decoder `C8/a.java:29-73` (time absent if <2 bytes remain; `time==0` → "NoLimit"; remaining bytes = value). Enums §2.9 | lamps, sockets, blinds, mini actuators |
| `0x000F` (15) | `AutomaticDaylightSavingTime` (`r.c.C2057d`, `:1207`) | A | RW | `u8` bool (1/0) (`p234v7/C2357f.java:37-51`; read `app/ui/timer/threshold/devices/d.java:243-246`) | lamps, sockets, blinds |
| `0x0012` (18) | `RtrTouchSensitivity` (`r.c.w0`, `:2240`) | A | RW | `u8` bool (`J8/H.java:81-84`; `resolver/C1901b0.java:385-394`, only offered when SW ≥ 1.0.3.3) | RTR |
| `0x0013` (19) | `DimMode` (`r.c.C0389r`, `:1940`) | A | RW (set with access=READ) | `u8` DimModeType (§2.13) (`J8/C0542j.java:66-69`; `z.java:479-500`; `p234v7/C2386u.java:36-41`) | dim lamp |

### 1.3 Generic load parameters (`0x10xx`)

| ID hex (dec) | Name in code | Model | R/W | Value layout | Devices |
|---|---|---|---|---|---|
| `0x1001` (4097) | `GeneralOnDelayProperty` (`r.c.E`, `:370`) | A | RW | `uint32 LE` milliseconds; 0 = off; valid 1..86 400 000, app caps at 14 400 000 (4 h) (`J8/C0545m.java:69-72`, `z.java:519-526`, `p056e8/d.java:11`) | lamps, sockets |
| `0x1002` (4098) | `GeneralOffDelayProperty` (`r.c.D`, `:324`) | A | RW | same (`J8/C0544l.java:69-72`, `z.java:510-517`, `p056e8/c.java:11`) | lamps, sockets |
| `0x1007` (4103) | `TimedOnDurationProperty` (`r.c.F0`, `:439`) | A | RW | `uint32 LE` ms, max 4 h (`A6/b.java:239-244`; `C1901b0.java:718-722`; detectors default 120 s `ConfigureDetector.java:261-267`) | lamps, sockets, detector loads |
| `0x100A` (4106) | `PrewarningProperty` (`r.c.m0`, `:1728`) | A | RW | `u8` bool (`J8/E.java:68-70`, `C1901b0.java:265-270`) | lamps |
| `0x100B` (4107) | `ManualOffEnableProperty` (`r.c.U`, `:924`) | A | RW | `u8` bool (`p096i8/l.java:70-72`, `C1901b0.java:132-138`) | lamps, sockets |
| `0x100C` (4108) | `InvertOutputProperty` (`r.c.K`, `:623`) | A | RW | `u8` bool (`P8/k.java:68-70`, `C1901b0.java:89-95`) | lamps |
| `0x100D` (4109) | `SwitchBlockingTime` (`r.c.A0`, `:209`) | A | RW | `uint16 LE` ms, 100..10 000 (`J8/L.java:67-69`, `C1901b0.java:708-716`, `I8/Q.java:20`) | lamps, sockets |
| `0x100E` (4110) | `DimToWarm` (`r.c.C2077s`, `:1987`) | A | RW | `u8` bool (`J8/C0543k.java:65-67`, `z.java:503-508`) | dim lamp (declared) |
| `0x1014` (4116) | `RtrOperationMode` (`r.c.s0`, `:2010`) | A | RW | `u8` bool — set TRUE when a relay output is bound to a room thermostat as heating actuator, FALSE on removal (`p096i8/r.java:71-73`, `C1901b0.java:335-341`, `interactors/connection/RemoveMultiConnection$work$1.java:221`). (Firmware: named `LBC_PROP_SWITCH_OPERATION_MODE_ID`; a device on FW 2.0.0.4 answers "Lbc Property doesn't exist", `docs/cross-repo-analysis.md` §1.4) | switch lamp, measure lamp, sockets, RTR |

### 1.4 Blind / shutter parameters (`0x11xx`)

| ID hex (dec) | Name in code | Model | R/W | Value layout | Devices |
|---|---|---|---|---|---|
| `0x1101` (4353) | `MoveRevisionTimeProperty` (`r.c.C2055b0`, `:1135`) | A | RW | `uint16 LE` ms ("slat move revision time"), 300..10 000 (`J8/y.java:67-69`, `C1901b0.java:183-192`, `I8/C.java:20`) | blind |
| `0x1102` (4354) | `BlindMoveUpDownTime` (`r.c.C2061f`, `:1303`) — "hang time" | A | RW | `uint16 LE` **seconds**, 5..600 (`J8/C0534b.java:70-72`, `z.java:425-431`, `p056e8/a.java:12`, UI `blinds/setup/setps/a.java:37`). **Same ID is also declared as** `MoveUpDownTimeProperty` (`r.c.C2062f0`, `:1327`, `uint32 LE` ms, `p234v7/C2358f0.java:39-53`, `resolver/Y0.java`); the ID lookup list puts `C2061f` first (`app/ui/project/shareViaFile/f.java:81`), so incoming statuses are always parsed as the 2-byte seconds form. | blind |
| `0x1103` (4355) | `MoveSlatsTimeProperty` (`r.c.C2058d0`, `:1231`) | A | RW | `uint16 LE` ms, 0..10 000 (≥300 in BLINDS mode); defaults BLINDS=2000, SHUTTER=0, AWNING=300 (`J8/A.java:67-69`, `C1901b0.java:202-208`, `p056e8/m.java:14,40-41`, `BlindOperationModeViewModel.java:57-63`) | blind |
| `0x1104` (4356) | `MoveOperationMode` (`r.c.C2053a0`, `:1087`) | A | RW | `u8` BlindMoveOperationMode (§2.7) (`J8/C0535c.java:71-73`, `resolver/C1924j.java`) | blind |
| `0x1105` (4357) | `MoveOnPowerMode` (`r.c.Z`, `:1039`) | A | RW | `u8` MoveOnPowerMode (§2.8) (`J8/x.java:69-71`, `C1901b0.java:157-182`) | blind |
| `0x1106` (4358) | `MoveBlindPositionOnPower` (`r.c.X`, `:993`) | A | RW | `u8` position 0..255 (`Position` type: app maps 100 % → 254 and swaps 0↔255 on read, `p056e8/q.java:13-32,50-52`; `J8/v.java:63-72`; `C1901b0.java:139-147`) | blind |
| `0x1107` (4359) | `MoveSlatPositionOnPower` (`r.c.C2056c0`, `:1183`) | A | RW | `u8` position (`J8/z.java:63-72`, `C1901b0.java:193-201`) | blind |
| `0x1108` (4360) | `BlindsInvertOutputProperty` (`r.c.C2063g`, `:1351`) | A | RW | `u8` bool (`J8/C0536d.java:70-72`, `z.java:418-424`) | blind |
| `0x110A` (4362) | `MoveBlindsVentilationPosition` (`r.c.Y`, `:1016`) | A | RW | `u8` position (`J8/w.java:63-72`, `C1901b0.java:148-156`) | blind |
| `0x110B` (4363) | `MoveSlatsVentilationPosition` (`r.c.C2060e0`, `:1279`) | A | RW | `u8` position (`J8/B.java:63-72`, `C1901b0.java:209-217`) | blind |
| `0x110D` (4365) | `ReferenceRunProperty` (`r.c.o0`, `:1822`) | A | W1/R | set always writes `[0x01]` (start reference run) (`p234v7/C2385t0.java:36-40`); read `u8`, `1` = running (`resolver/A0.java:135-138`) | blind |

### 1.5 Room-thermostat (RTR) parameters (`0x12xx`)

| ID hex (dec) | Name in code | Model | R/W | Value layout | Devices |
|---|---|---|---|---|---|
| `0x1201` (4609) | `ControllerTypeProperty` (`r.c.C2070k`, `:1611`) | A | RW | `u8` RtrControllerType (§2.11) (`J8/C0537e.java:66-68`, `C1901b0.java:265-290`) | RTR |
| `0x1203` (4611) | `TempComfortProperty` (`r.c.C0`, `:301`) | A | RW | `int16 LE` in 0.01 °C (`p234v7/y0.java:54-82`: `putShort(temp/0.01)`; read `X8/a.java:56-66` `BigInteger(reversed)×0.01`) | RTR |
| `0x1204` (4612) | `TempStandbyProperty` ("Eco") (`r.c.E0`, `:393`) | A | RW | same (`X8/a.java:94-104`) | RTR |
| `0x1205` (4613) | `TempFreezeProperty` (`r.c.D0`, `:347`) | A | RW | same (`X8/a.java:67-77`) | RTR |
| `0x1208` (4616) | `HeatingOptionProperty` ("heating optimisation") (`r.c.F`, `:416`) | A | RW | `u8` bool (`J8/C0547o.java:64-66`, `C1901b0.java:82-88`) | RTR |
| `0x120A` (4618) | `RTRValveOutput` (`r.c.n0`, `:1775`) | A | RW | `u8` RtrValveOutput (§2.12) (`J8/S.java:65-67`, `resolver/e2.java` case 0) | RTR |
| `0x120B` (4619) | `RtrHvacMode` (`r.c.q0`, `:1916`) | A | RW | `u8` HvacMode (§2.10) (`p096i8/q.java:85-87`, `C1901b0.java:291-308`; needs SW ≥ 2.2.0.0) | RTR |
| `0x120D` (4621) | `RtrBoostModeProperty` (`r.c.p0`, `:1869`) | A | RW | `u8` bool (`p096i8/c.java:70-72`, `z.java:432-438`) | RTR |
| `0x120F` (4623) | `BtSensProperty` (`r.c.C2067i`, `:1516`) | A | R (TODO) | 1 byte read then `TODO()` (`resolver/C1936n.java`, `threshold/devices/d.java` case 11) | — |
| `0x1218` (4632) | `ExtendedModeProperty` (`r.c.C2082x`, `:2264`) | A | R (TODO) | 1 byte (`resolver/H.java`) | — |
| `0x1212` (4626) | `DropOfTempStateProperty` (`r.c.C2078t`, `:2034`) | A | R (TODO) | 1 byte (`resolver/F.java`) | — |
| `0x121C` (4636) | `KeyLockProperty` (`r.c.M`, `:669`) | A | R (TODO) | 1 byte (`resolver/C1946q0.java`) | — |
| `0x121D` (4637) | `KeyLockLocalProperty` (`r.c.L`, `:646`) | A | R (TODO) | 1 byte (`resolver/C1943p0.java`) | — |
| `0x121F` (4639) | `KeyLockStateProperty` (`r.c.N`, `:692`) | A | R (TODO) | 1 byte (`resolver/C1948r0.java`) | — |
| `0x1221` (4641) | `RtrSensorSelectionProperty` (`r.c.v0`, `:2193`) | A | RW | `u8` RtrSensorSelection (§2.11) (`J8/J.java:64-66`, `C1901b0.java:359-384`) | RTR |
| `0x1224` (4644) | `RtrSensorOffsetProperty` (`r.c.u0`, `:2104`) | A | RW | `int16 LE` in 0.01 K (`J8/G.java:62-67` + `I8/K.java:24-29`; read `C1901b0.java:349-358`) | RTR |
| `0x1246` (4678) | `SchedulerEnabledProperty` (`r.c.y0`, `:2334`) | A | RW | `u8` bool (`J8/I.java:69-71`, `C1901b0.java:664-670`) | RTR |
| `0x1240` (4672) | `RtrMainPageProperty` (`r.c.r0`, `:1963`) | A | RW | `u8` RtrMainPage (§2.11) (`J8/F.java:64-66`, `C1901b0.java:309-334`) | RTR |
| `0x1247` (4679) | `BlAutoOffProperty` (display backlight auto-off) (`r.c.C2059e`, `:1255`) | A | RW | `u8` bool (`J8/C0533a.java:57-59`, `z.java:411-417`) | RTR |
| `0x1249` (4681) | `RtrSchedulerFunctionStatus` (`r.c.t0`, `:2057`) | A | R | `u8` bool, stored into the SchedulerEnabled capability (`resolver/C1952s1.java`, `C1901b0.java:342-348`) | RTR |

Firmware cross-check (gateway middleware `btmesh_property_ids.js:78-106`, §1.10): the ids the app declares with
`TODO()` parsers in the `0x120F–0x121F` block do **not** exist there — that block is commented "Reserved For Future
Use". The firmware's ids for the same names are `0x1222 RTR_BTSENS_ENABLED` (app: `0x120F`),
`0x1225 RTR_DROP_OF_TEMP_STATE` (app: `0x1212`; plus `0x1220 RTR_DROP_OF_TEMP_ENABLE`) and
`0x1245 RTR_EXTENDED_MODE_ENABLE` (app: `0x1218`); the `KeyLock*` names (`0x121C/0x121D/0x121F`) have no firmware
counterpart at all (the device-wide key lock is `0x0001`). `0x120B` is `RTR_HVACMODE_DISPLAY` in the firmware and the
gateway labels the same wire values 1/2/3 as `heat`/`cool`/`frost` (`jung-home-state-mode.js:61-67`) where the app
says Comfort/Eco/Freeze. `0x1246` is presented as auto (1) / manu (0) (`:17-21`).

### 1.6 Button / key parameters (`0x50xx`)

| ID hex (dec) | Name in code | Model | R/W | Value layout | Devices |
|---|---|---|---|---|---|
| `0x5001` (20481) | `ButtonLayoutProperty` (`r.c.C2069j`, `:1564`) | A | RW | `uint16 LE` LayoutMode (§2.3) (`p234v7/C2371m.java:40-55`, `resolver/C1942p.java:105-108`) | control switches (rockers, wall transmitters), mini actuators |
| `0x5002` (20482) | `KeyModeSceneConfigProperty` (`r.c.P`, `:738`) | A | RW | 6 bytes: `[sceneId uint16 LE][transitionTime uint32 LE ms]` (`p254x7/b.java:40-58`, `resolver/C1954t0.java`) | control switches, mini actuators (per button element) |
| `0x5003` (20483) | `KeyModeProperty` (`r.c.O`, `:715`) | A | RW | `u8` KeyMode (§2.4) (`p254x7/a.java:37-51`, `resolver/C1960v0.java`) | same |
| `0x5004` (20484) | `TurnOnThreshold` (`r.c.I0`, `:577`) | A | RW | 8 bytes: `[sensorPropertyId uint16 LE = 0x0081][time uint16 LE seconds][value uint24 LE ×0.1 W, 0xFFFFFF = none][active u8]` (`p234v7/L0.java:34-62`; decoder `p057e9/a.java:27-41`; time = "Duration (sec)" `strings.xml threshold_time_value_title`) | measuring socket |
| `0x5005` (20485) | `TurnOffThreshold` (`r.c.H0`, `:531`) | A | RW | same layout | measuring socket |
| `0x5006` (20486) | `KeySetPropertyMode` (`r.c.Q`, `:761`) | A | RW | 3 bytes: `[targetPropertyId uint16 LE][mode u8: 0=STATELESS, 1=STATEFUL]` (`p234v7/E.java:39-58`, `resolver/C1963w0.java`, `PropertySetMode.java:30-33`) | control switches / mini actuators in "Property" key mode |
| `0x5007` (20487) | `KeySetPropertyValueUpOn` (`r.c.S`, `:807`) | A | RW | raw value bytes of the property selected via 0x5006; app supports EnforceOutput(9) struct, SchedulerEnabled(0x1246) bool, RtrHvacMode(0x120B) u8 (`p085h8/e.java:3-16`, `p278z8/d.java:71-74`, `p234v7/G.java`) | same |
| `0x5008` (20488) | `KeySetPropertyValueDownOff` (`r.c.R`, `:784`) | A | RW | same (`p234v7/F.java`, `resolver/C1966x0.java`) | same |
| `0x5009` (20489) | `InputEdgeDetectionProperty` (`r.c.I`, `:554`) | A | RW | 1 byte bit-field: bits 7:5 = 0, bits 4:3 = falling-edge behaviour, bits 2:1 = rising-edge behaviour, bit 0 = mode (§2.6) (`p234v7/C.java:39-56`, `resolver/C1916g0.java`) | binary-input pucks (`p085h8.i` / `p096i8/h.java`) |
| `0x5010` (20496) | `DailyEnergyChart` (`r.c.AbstractC2080v.a`, `:2135`) | U | R | array of `uint16` **big-endian** samples, value ×0.1, entry *i* = *i* hours ago, `0xFFFF` → invalid/0 (`J7/b.java:106-125,127-133`, builder `p234v7/U.java:29-48`) | measuring socket, measuring lamp, puck energy |
| `0x5011` (20497) | `MonthlyEnergyChart` (`r.c.AbstractC2080v.b`, `:2159`) | U | R | array of `uint24` big-endian samples ×0.1, entry *i* = *i* days ago, `0xFFFFFF` → invalid/0 (`J7/b.java:85-105`) | same |
| `0x5012` (20498) | — not in the app; firmware `LBC_PROP_KEY_EVT_ID` | U (the key's `0527:1015` client **publishes** it as User Property Set Unack `D0 27 05` to the gateway's element group) | — | 2 bytes `[counter u8][event u8]`; event 0/1 pushed down/up, 2/3 held down/up (rocker halves), 4 released, 5 pushed = click, 6 held (single keys); every publication sent twice with the same counter. Captured on air and decoded from the gateway firmware — full table in `docs/poc-gatt-proxy.md` "Button events" | keys / rockers in KeyMode 6 (Gateway) |
| `0x5013` (20499) | — not in the app; firmware `LBC_PROP_KEY_STATUS_ID` | M (read-only for users) | W by the gateway | `u8` = the key's **status LED** (the gateway's `status_led` datapoint). The gateway does not use a Set: it sends a **User Property Status** (opcode `0x11`, `D1 27 05`) to the button element (`docs/cross-repo-analysis.md` §1.2); the gateway also answers Gets for `0x5013` addressed to itself with one zero byte (`btmesh_property_service.js:44,156,168-170`). Not the LED triple's "third slot" (see §1.8) | keys in KeyMode 6 |

### 1.7 Presence / motion detector parameters (`0x60xx`)

| ID hex (dec) | Name in code | Model | R/W | Value layout | Devices |
|---|---|---|---|---|---|
| `0x6001` (24577) | `PresenceTestProperty` ("walking test") (`r.c.l0`, `:1681`) | A | RW | `u8` bool (`J8/D.java:68-70`, `C1901b0.java:258-264`) | detectors |
| `0x6003` (24579) | `PresenceControlProperty` (`r.c.j0`, `:1587`) | A | RW | `u8` bool (`p234v7/C2368k0.java:38-52`, `resolver/A0.java:107-110`) | detectors |
| `0x6004` (24580) | `PresenceBrightnessProperty` ("current brightness") (`r.c.C2066h0`, `:1492`) | **M** | RW | `uint16 LE` (lux) (`p234v7/C2366j0.java:37-50`, `A0.java:103-106`) | detectors |
| `0x6005` (24581) | `PresenceControlPirProperty` (`r.c.C2068i0`, `:1540`) | **M** | RW | `u8` (`p234v7/C2370l0.java:36-49`, `A0.java:111-114`) | detectors |
| `0x6006` (24582) | `DetectorOperationModeProperty` (`r.c.C2073n`, `:1752`) | A | RW | `u8` DetectorOperationMode (§2.14) (`J8/C0539g.java:71-73`, `z.java:446-467`) | detectors |
| `0x6007` (24583) | `PresenceSimulationProperty` (`r.c.k0`, `:1634`) | A | R (TODO) | 1 byte (`resolver/C1917g1.java`, `A0.java` case 15) | — |
| `0x6008` (24584) | `PirSensorA` (`r.c.AbstractC2064g0.a`, `:1385`) | A | RW | `u8` sensitivity 0..255 (UI shows %; `p234v7/C2362h0.java:43-55` writes `percent→0..255`, `io/grpc/u.java:37-45,361-366`; read `A0.java:88-102`) | detectors |
| `0x6009` (24585) | `PirSensorB` (`:1409`) | A | RW | same | detectors |
| `0x600A` (24586) | `PirSensorC` (`:1433`) | A | RW | same | detectors |
| `0x600B` (24587) | `HysteresisOffsetProperty` (`r.c.G`, `:462`) | A | R (TODO) | 1 byte (`resolver/C1907d0.java`) | — |
| `0x600C` (24588) | `MinBrightnessChangeProperty` (`r.c.V`, `:947`) | A | R (TODO) | 1 byte (`resolver/O0.java`) | — |
| `0x600D` (24589) | `MinHysteresisProperty` (`r.c.W`, `:970`) | A | R (TODO) | 1 byte (`resolver/P0.java`) | — |
| `0x600F` (24591) | `SwitchOnBrightnessProperty` ("brightness threshold") (`r.c.B0`, `:255`) | A | RW | `uint16 LE` lux (`J8/C.java:68-70` + `I8/C0510b.java` case 1; `C1901b0.java:244-250`) | detectors |
| `0x6010` (24592) | `FollowupTimeProperty` (`r.c.C2083y`, `:2311`) | A | R (TODO) | 1 byte (`resolver/I.java`) | — |
| `0x6011` (24593) | `AlarmFunctionProperty` (`r.c.C2052a`, `:1063`) | A | R (TODO) | 1 byte (`resolver/C1897a.java`) | — |
| `0x6012` (24594) | `ImpulseModeProperty` (`r.c.H`, `:508`) | A | R (TODO) | 1 byte (`resolver/C1910e0.java`) | — |
| `0x6013` (24595) | `DynamicFollowupTimeProperty` (`r.c.C2079u`, `:2081`) | A | R (TODO) | 1 byte (`resolver/G.java`) | — |
| `0x6014` (24596) | `TurnOffPrewarningProperty` (`r.c.G0`, `:485`) | A | R (TODO) | 1 byte (`resolver/d2.java`, `A0.java` case 26) | — |
| `0x6015` (24597) | `DayModeProperty` ("daytime operation") (`r.c.C2071l`, `:1658`) | A | RW | `u8` bool (`J8/C0538f.java:70-72`, `z.java:439-445`) | detectors |
| `0x6016` (24598) | `DetectorForcedOff` (`r.c.C2072m`, `:1705`) | A | RW | `u8` ForcedOffMode (§2.14) (`p234v7/r.java:36-50`, `threshold/devices/d.java:308+`) | detectors |
| `0x6017` (24599) | `DetectorOperationSiteProperty` (`r.c.C2074o`, `:1799`) | A | RW | `u8` DetectorOperationSite (§2.14) (`J8/C0540h.java:71-73`, `C1901b0.java:218-243`) | detectors |
| `0x6021` (24609) | `DetectorRepetitionTime` (`r.c.C2075p`, `:1846`) | A | RW | `u8` (`p234v7/C2389v0.java:38-52`, `A0.java:139-142`); unit not visible in code (UI hint "update every 5 s") | detectors |

### 1.8 LED parameters (`0xA0xx`, dynamic IDs)

| ID | Name in code | Model | R/W | Value layout | Devices |
|---|---|---|---|---|---|
| `0xA001 + 3·(n−1)` (n = 1..6) | `LedModeOn(id)` (`r.c.T.b`, `p056e8/r.java:870-903`) | A | RW | 4 bytes `[red u8][green u8][blue u8][mode u8]`, mode `5` = LED enabled, `0` = disabled (`C6/a.java:190-209`, `A8/b.java:8-11`; read `C1901b0.java:96-131`: RGB = first 3 bytes, enabled = last byte == 5). Builder `p266y7/b.java:30-48`. | control switches, sockets (`compatibles.h`) |
| `0xA002 + 3·(n−1)` | `LedModeOff(id)` (`r.c.T.a`, `:833-867`) | A | RW | same layout; builder `p266y7/a.java:30-48` | same |

`n` = `LedPosition` (§2.15); validity check `r.c.T.e()` (`r.java:909-921`): id in `[base, base+15]` and `(id−base) % 3 == 0`.
The app never touches `0xA000`, `0xA003`, `0xA006`, …: the gateway firmware shows the triples actually start at
`0xA000` as `LEDn_CH_SELECTION` / `LEDn_ON_MODE` / `LEDn_OFF_MODE` for n = 1..16 (`0xA000–0xA02F`,
`btmesh_property_ids.js:164-211`), so `0xA003` is LED 2's *channel selection*, not a spare slot of LED 1, and the
name space covers 16 LEDs rather than the app's 6.
RGB values are 0..100 "percent" bytes; per-product palettes (Red, Green, White, Blue, Violet, Orange, Yellow, Cyan, NoColor)
are hard-coded, e.g. `Z7/i.java:58` (wall transmitter 1-gang), `Z7/a.java:81` (rocker 1-gang), `Y7/p0.java:24` (socket).

### 1.9 Scheduler / astro / gateway (`0x0007`, `0x0008`, `0xC0xx`)

| ID hex (dec) | Name in code | Model | R/W | Value layout | Devices |
|---|---|---|---|---|---|
| `0x0007` (7) | `AstroSchedulerRegisterProperty` (`r.c.C2054b`, `:1111`) | A | RW | **Get** carries 1 extra byte = scheduler register index (`B7/a.java:38-43`). Value = 5 bytes, LE 40-bit field (encoder `B7/a.java:59-70`, decoder `resolver/C1903c.java`): bits 3:0 `registerIndex`, 7:4 `AstroTimerMode` (§2.16), 15:8 `timeOffset` int8 two's complement (minutes; sign handled `W7/d.java:71-73`), 20:16 earliest hour, 26:21 earliest minute, 31:27 latest hour, 37:32 latest minute, 39:38 = 0 | all (base device) |
| `0x0008` (8) | `AstroSchedulerStatusProperty` (`r.c.C0387c`, `:1159`) | A | RW | `uint32 LE`; low 16 bits = `status[i]` (bit *i* = register *i* active), high 16 bits = `mode[i]` (0 STATIC / 1 ASTRO, `timer/AstroSchedulerStatusCapable$Companion$MODE.java:20`) (`B7/b.java:45-72`, `resolver/C1906d.java`, `g9/b.java:84-95`) | all |
| `0xC000` (49152) | `GatewayAPIStatus` (`r.c.C2084z`, `:2358`) | M | R | the app declares it only (no builder/resolver). 1-byte bit field per the gateway firmware (§1.10): bit 0 = API available, bit 1 = a client name is waiting for approval; served by the gateway's Manufacturer server (`hidden-features.md` §2), `gateway_api_status` in `jhmesh/properties.py` | gateway |
| `0xC001` (49153) | `GatewayAPIToken` (`r.c.A`, `:186`) | M | R | UTF-8 string, NULs stripped (`UtilsKt.c`, `resolver/K.java`, `p245w7/a.java:29`) | gateway |
| `0xC002` (49154) | `GatewayIP` (`r.c.C`, `:278`) | M | R | UTF-8 string (`resolver/M.java`, `p245w7/c.java:28`) | gateway |
| `0xC003` (49155) | `GatewayFingerprint` (`r.c.B`, `:232`) | M | R | UTF-8 string, trimmed (`resolver/L.java`, `p245w7/b.java:29`) | gateway |

Unknown IDs become `UnknownProperty(id)` (`p056e8/r.java:2411-2435`).

### 1.10 Property ids the gateway firmware knows and the app does not

Source: the JUNG HOME Gateway 2.1.3 middleware, `models/btmesh_property_ids.js:17-229` (`LbcPropertyId` and
`GenericPropertyId` enums; names only, no layouts). Only ids absent from §1.1–§1.9, or whose firmware name changes the
reading, are listed; the file's own section headings give the category. Value layouts are unknown unless stated.

| ID hex (dec) | Firmware name | Category (file section) | Notes |
|---|---|---|---|
| `0x0000` (0) | `NOT_SET` | general | placeholder |
| `0x000A`–`0x000D` (10–13) | `APPLICATION_SCHEMA_VERSION`, `STACK_SCHEMA_VERSION`, `MASTER_SCHEMA_VERSION`, `CO_PROCESSOR_SCHEMA_VERSION` | general | version block next to `0x0003`–`0x0005`; `0x0005` is named `APPLICATION_IMAGE_VERSION` (the app calls it "STM32 version") |
| `0x000E` (14) | `SERVER_STATE_PUBLISH_REQUEST` | general | "publish your state now": **verified** — Admin Set `01` makes a dimmer / DALI insert publish its CTL, Lightness, OnOff and Level Status at once; switch inserts and sockets acknowledge and publish nothing; reads back empty |
| `0x0010`, `0x0011` (16, 17) | `LPN_STATE_TIMEOUT`, `BAT_TEST_RAW_DATA` | general | battery devices; note the numeric overlap with SIG property ids 0x0010/0x0011 in §1.1 (different servers) |
| `0x0F00`–`0x0F02` (3840–3842) | `TRANS_SETTINGS`, `CURRENT_RUNTIME_STATS`, `ALL_TIME_RUNTIME_STATS` | general | diagnostics |
| `0x1008`, `0x1009` (4104, 4105) | `HOTEL_DIMM_VALUE`, `BASIC_LIGHT_FUNC_ENABLE` | light | |
| `0x100F`, `0x1010` (4111, 4112) | `TOTAL_DEVICE_OFF_ON_CYCLES`, `DEVICE_POWER_ON_CYCLES` | light | counters |
| `0x1011`–`0x1013` (4113–4115) | `NIGHT_DIMM_VALUE`, `PRESENTATION_MODE_ENABLE`, `PRESENTATION_MODE_TIME` | light | (property ids, not the SIG model ids of the same value) |
| `0x1109`, `0x110C` (4361, 4364) | `BLINDS_STEP_UP_DOWN`, `BLINDS_WIND_ALERT_ENABLE` | blinds/slats | `0x110D` is `REFERENCING_REQUEST` (app: ReferenceRun) |
| `0x1202`, `0x1206`, `0x1207` (4610, 4614, 4615) | `RTR_COOLING_ENABLE`, `RTR_TEMP_COOLING`, `RTR_TEMP_FLOORMAX` | RTR | cooling set-point presumably `int16 LE` ×0.01 °C like `0x1203`–`0x1205` |
| `0x120C`, `0x120E` (4620, 4622) | `RTR_TEMP_TARGET`, `RTR_TEMP_HOLIDAY` | RTR | a direct target-temperature property; the app sets the target through Generic Level instead |
| `0x1220`, `0x1222`, `0x1223`, `0x1225` (4640, 4642, 4643, 4645) | `RTR_DROP_OF_TEMP_ENABLE`, `RTR_BTSENS_ENABLED`, `RTR_SENSOR_TEMP_ACT`, `RTR_DROP_OF_TEMP_STATE` | RTR | see the note under §1.5 — the app's `0x120F`/`0x1212`/`0x1218` ids fall in the firmware's reserved block |
| `0x1241`–`0x1243`, `0x1245` (4673–4675, 4677) | `RTR_FAHRENHEIT_ENABLE`, `RTR_TEMP_ADJUST_MIN`, `RTR_TEMP_ADJUST_MAX`, `RTR_EXTENDED_MODE_ENABLE` | RTR | |
| `0x1300`–`0x1303` (4864–4867) | `SCENE_ESCAPE_ACTION_SET1..4` | scene escape | unknown feature ("scene escape" = leaving a scene?) |
| `0x500A`, `0x500C` (20490, 20492) | `KEY_RTR_TEMP_STEP_SIZE`, `KEY_TOGGLE_ENABLE` | keys ("sensor") | per-key parameters for KeyMode 4 (RTR) and toggle behaviour |
| `0x5012`, `0x5013` (20498, 20499) | `KEY_EVT`, `KEY_STATUS` | keys | in §1.6 |
| `0x5014` (20500) | — not in the app, not in the gateway's property table | metering sockets, **meter element**, Manufacturer + User servers (access 1) | **read**: 7 bytes `[year−1900 u16 LE][month][day][h][m][s]`, constant per device — `7C 00 0A 03 0E 2E 2E` = 2024-10-03 14:46:46 on one socket, 2024-10-12 22:40:15 on the other. A stored moment (commissioning? last energy reset?), codec `Timestamp7` as `meter_timestamp` (`hidden-features.md` §10) |
| `0x6002` (24578) | `PRESENCE_ALARM_ENABLE` | PM detector | |
| `0x600E` (24590) | `MELDER_MIN_SWITCH_OFF_BRIGHTNESS` | PM detector | between the app's `0x600D` and `0x600F` |
| `0x6018`–`0x601F` (24600–24607) | `MELDER_CTLC_ENABLE`, `CTLC_SET_VALUE`, `CTLC_HYS_SET_POINT`, `CTLC_HYS_SW_OFF_VALUE`, `CTLC_MIN_STEP_SIZE`, `CTLC_MIN_STEP_TIME`, `CTLC_MAX_LUX_LEVEL`, `CTLC_REDUC_PPART` | PM detector | constant-light control (daylight-dependent dimming) — not exposed by the app |
| `0x6020` (24608) | `MELDER_NIGHT_LIGHT_LEVEL` | PM detector | |
| `0xA000 + 3·(n−1)`, n = 1..16 | `LEDn_CH_SELECTION` | visual | the first slot of every LED triple (§1.8); the app only knows ON/OFF modes for n ≤ 6 |
| `0xA100` (41216) | `BATTERY_CHANGED` | battery powered device | |
| `0xA200`, `0xA201` (41472, 41473) | `WIND_ALERT_ACTIVE`, `WIND_ALERT_PRIORITY` | wind alert | the app's wind alarm goes through `0x0009` EnforceOutput (§2.9) |
| SIG `0x000D` (13), `0x0072` (114) | `DEVICE_ENERGY_USE_SINCE_TURNON`, `PRECISE_TOTAL_DEVICE_ENERGY_USE` | `GenericPropertyId` | SIG device properties next to `0x006A`/`0x006D` (§1.1); whether the sockets serve them is untested |

Firmware names that read differently from the app's for the **same** id: `0x0002 ACT_FUNC_ID` (InsertId),
`0x0009 ENFORCED_OUTPUT_STATE_ID` (EnforceOutput), `0x000F AUTO_SUMMER_WINTER_TIME_ID`, `0x1014 SWITCH_OPERATION_MODE_ID`
(RtrOperationMode, §1.3), `0x120A RTR_VALUE_OUTPUT_ID` (valve output), `0x120B RTR_HVACMODE_DISPLAY_ID` (§1.5),
`0x6007 MELDER_ANWESENDHEITSSYM_ID` (presence simulation) and — materially — `0x6017 MELDER_BASIS_SENSITIVITY_ID`, which
the app exposes as `DetectorOperationSite` INDOOR=7 / OUTDOOR=0x70 (§2.14), i.e. the two "sites" are two base-sensitivity
presets. The gateway's `0xC000`–`0xC003` are not in this enum; they live in the property service's topic map
(`btmesh_property_service.js:38-45`: `api_status`, `api_token`, `ip_v4`, `fingerprint_sha256`), and `0xC000` is a
1-byte bit field: bit 0 = API available, bit 1 = a client name is waiting for approval (`:161-167`).

---

## 2. Enumerations (wire values)

### 2.1 `ActuatorFunctionId` — "insert" / actuator function (`de/jung/junghome/domain/item/mesh/ActuatorFunctionId.java`)

| Value | Name | Source line | Notes / UI name (`strings.xml insert_*_description`) |
|---|---|---|---|
| 0 | Switch | `:172` | "Switch insert" → creates SwitchLampDevice (or MeasureLampDevice on puck 0x0010) per load location |
| 1 | TwoGangSwitch | `:247` | "2-gang switch" |
| 2 | Dimming | `:47` | "Dimming insert" → DimLampDevice |
| 3 | TwoGangDimming | `:222` | "2-gang touch dimmer insert" |
| 4 | TwDimming | `:197` | tunable-white / "DALI insert" → TunableWhiteLampDevice |
| 5 | Blind | `:22` | "Blinds insert" → BlindDevice |
| 6 | Extension | `:72` | "Satellite insert" — no load device created |
| 7 | NotAvailable | `:122` | |
| 8 | Rtr | `:147` | "Room thermostat insert" |
| 9 | Gateway | `:97` | |
| −1 (0xFFFF) | Unset | `:277` | "does not support insert id's" |
| other | Unknown(raw) | `:266,338` | |

Also carried in the manufacturer-specific advertisement (`LBCAdvertisementData.java:76-121`):
`[0x27 0x05][advType u8][productId u16 LE][actuatorFunctionId u8 (advType 1) / u16 (advType 2,3)][layoutMode u8/u16][mac 6 B reversed (advType 3)]`.

### 2.2 `InsertType` (`InsertType.java:24-35, 47, 70, 109`)
`0` Unknown, `1` NoInsert, `2` GenericInsert, other → NotSupported(raw). `PARAMETER_BIT_SIZE = 16` (`:14`).

### 2.3 `LayoutMode` — button layout (`nodes/models/LayoutMode.java:22-28`), `uint16`

| Value | Name | UI (`strings.xml switch_layout_type_*`) |
|---|---|---|
| 0 | ONE_TOP_ONE_BOTTOM | one key top / one key bottom |
| 1 | ONE_ROCKER | one rocker |
| 2 | TWO_LEFT_TWO_RIGHT | "Button / Button" (two keys left, two right) |
| 3 | ONE_LEFT_TWO_RIGHT | "Rocker / Button" |
| 4 | TWO_LEFT_ONE_RIGHT | "Button / Rocker" |
| 5 | ONE_LEFT_ONE_RIGHT | "Rocker / Rocker" |
| 255 | UNKNOWN | |

### 2.4 `KeyMode` (`p056e8/i.java:14-63`, names from `MiniActuatorConfigurationFragment.java:348-366` + `strings.xml key_mode_*`), `u8`

| Value | Class | Name | Client models the key uses (`p056e8/j.java:16-42`) |
|---|---|---|---|
| 0 | `i.b` | Light | GenericOnOff (0x1000) + GenericLevel (0x1002) |
| 1 | `i.c` | Move (blinds) | GenericLevel |
| 2 | `i.f` | Scene | Scene Server (0x1203) — uses 0x5002 scene config |
| 3 | `i.d` | Property | LBC User Property Server — uses 0x5006/0x5007/0x5008 |
| 4 | `i.e` | RTR (temperature) | GenericLevel |
| 5 | `i.g` | Switch | GenericOnOff |
| 6 | `i.a` | Gateway | LBC User Property Server |
| −1 (0xFF) | `i.h` | Unknown | none |

`GroupConnection.Function` (`GroupConnection.java:77-93`): LIGHT=0, LIGHT_PROPERTY_MODE=1, BLIND=2, BLIND_PROPERTY_MODE=3, SWITCH=4, SWITCH_PROPERTY_MODE=5, LIGHT_AND_SWITCH=6, LIGHT_AND_SWITCH_PROPERTY_MODE=7, RTR_PROPERTY_MODE=8; mapped to KeyMode in `domain/item/a.java:9-26` (0,6→Light; 1,3,5,7,8→Property; 2→Move; 4→Switch).

`KeyModeGroupConfig` (app-side, `KeyModeGroupConfig.java:14-31`): `elementAddress`, `groupAddress`, `publishAddress`, `function`.

### 2.5 `PropertySetMode` (`PropertySetMode.java:30-33`): STATELESS=0, STATEFUL=1 (byte in 0x5006).

### 2.6 `InputEdgeDetection` (`nodes/models/InputEdgeDetection.java:49-56,102-107`) — 1 byte
Encoded with MSB-first `BitWriter` (`p234v7/C.java:46-55`, decoded `resolver/C1916g0.java`):
`bits 7:5 = 0`, `bits 4:3 = fallingEdgeBehaviour`, `bits 2:1 = risingEdgeBehaviour`, `bit 0 = mode`.
Mode: `STATE=0, EDGE=1, UNKNOWN=2`. Behaviour: `NO_REACTION=0, ON=1, OFF=2, TOGGLE=3, UNKNOWN=4`.
Sent as an LBC Admin set of `InputEdgeDetectionProperty` **0x5009** (20489) (`p056e8/r.java:554-575`, `p234v7/C.java:35,51`) — see §1.6.

### 2.7 `BlindMoveOperationMode` (`BlindMoveOperationMode.java:23-26`): BLINDS=0, SHUTTER=1, AWNING=3, UNKNOWN=4.

### 2.8 `MoveOnPowerMode` (`MoveOnPowerMode.java:23-30`): NO_REACTION=0, MOVE_UP=1, MOVE_DOWN=2, STOP=3, MOVE_TO_STORED_POSITION=4, POSITION_FOR_NETWORK_FAILURE=5.

### 2.9 LockFunction (EnforceOutput 0x0009) sub-enums
* `controlCommand` (`C8/c.java:14-43`): `0` = unlock (`LockFunctionViewModelDelegate$unlockDevice$1.java:76`), `1` = wind alarm (`…$startWindAlarm$1.java:72`, sent with priority 255, time 0, value `[0,0]`), `2` = lock-out protection (`…$startLockOutProtection$1.java:76`), `3` = unused, `0xFF` = unknown.
* `priority` (`C8/f.java:16-27`): raw `u8`; `254` and `255` are special singletons (255 used for wind alarm), otherwise `f.b(n)`; default 1.
* `time` `uint16 LE` seconds, `0` = NoLimit (`C8/g.java:13-21`).
* `value` = remaining bytes (target level bytes).

### 2.10 `HvacMode` (`HvacMode.java:24-27`): NONE=0, COMFORT=1, ECO=2, FREEZE=3.

### 2.11 RTR enums
* `RtrControllerType` (`RtrControllerType.java:23-25`): PI_CONTROL=1 ("PWM"), TWO_POINT_CONTROL=2, UNKNOWN=3.
* `RtrMainPage` (`RtrMainPage.java:23-26`): TARGET_TEMPERATURE_PAGE=1, CURRENT_TEMPERATURE_PAGE=2, CLOCK_PAGE=3, UNKNOWN=4.
* `RtrSensorSelection` (`RtrSensorSelection.java:23-26`): INTERNAL_SENSOR=1 ("Room"), EXTERNAL_SENSOR=2 ("Floor"), INTERNAL_AND_EXTERNAL_SENSOR=3, UNKNOWN=4.
* RTR set-points: `RtrMode` sealed class Comfort/Eco/Freeze (`p056e8/v.java`), UI range 5..30 °C (`v.java:116`).

### 2.12 `RtrValveOutput` (`RtrValveOutput.java:23-25`): NORMALLY_OPEN=1, NORMALLY_CLOSED=0, UNKNOWN=2.

### 2.13 `DimModeType` (`nodes/models/parameter/DimModeType.java:23-25`): LEADING_EDGE=1 ("RL"), TRAILING_EDGE=2 ("RC"), UNKNOWN=5.
`OnPowerUpBehaviorState` (SIG Generic OnPowerUp, not a property) (`OnPowerUpBehaviorState.java:23-26`): OFF=0, ON=1, RESTORE=2, UNKNOWN=3.

### 2.14 Detector enums
* `DetectorOperationMode` (`DetectorOperationMode.java:23-25`): GUARD=0, PRESENCE=1, UNKNOWN=2.
* `DetectorOperationSite` (`DetectorOperationSite.java:23-25`): INDOOR=7, OUTDOOR=112 (0x70), UNKNOWN=0.
* `ForcedOffMode` (`ForcedOffMode.java:23-25`): INACTIVE=0, OFF=2, ON=3.

### 2.15 `LedPosition` (`nodes/models/led/LedPosition.java:24-26`): FIRST=1, SECOND=2, UNKNOWN=−1 → property id = base + 3·(pos−1).
LED colour classes (`A8/c.java`): Blue(0,g,b), Cyan(0,g,b), Green(r,g,0), Orange(r,g,0), Red(r,0,0), Violet(r,0,b), White(r,g,b), Yellow(r,g,0), NoColor(0,0,0).

### 2.16 Astro / scheduler
* `AstroTimerMode` (`domain/item/automatic/AstroTimerMode.java:34-40`): STATIC_POINT_OF_TIME=0, SUNRISE=1, SUNSET=2, RESERVED_FOR_FUTURE_USE=3.
* `AstroSchedulerStatusCapable.MODE` (`timer/AstroSchedulerStatusCapable$Companion$MODE.java:20`): STATIC=0, ASTRO=1.
* `DayOfWeek` bit mask (`DayOfWeek.java:24-30`): MON=1, TUE=2, WED=4, THU=8, FRI=16, SAT=32, SUN=64.
* JUNG scheduler vendor model (`0x0527:0x1016`, opcodes 337874/337876) sub-commands (`p056e8/h.java`): Schedule=0, Action=1, EffectiveTime=2, ScheduleList=15; `JHSchedulerType` (`JHSchedulerType.java:14-22`): Available=0, Reserved=1, TimedInactive=2, TimedActive=3, SunriseInactive=4, SunriseActive=5, SunsetInactive=6, SunsetActive=7, Unknown=−1; `JHSchedulerStatus` (`JHSchedulerStatus.java:14-17`): Available=0, CentralScheduleIdMatched=1, Inactive=2, Active=3. (Framing: other agent.)
* `TimeRole` (SIG Time Role, `TimeRole.java:22,47,72,97`): None=0, MeshTimeAuthority=1, MeshTimeRelay=2, MeshTimeClient=3.

### 2.17 Misc
* `MeasureType` (`domain/item/MeasureType.java:26-28`): DAILY=0 → property 0x5010, THIRTY_ONE_DAYS=1 → 0x5011 (`p234v7/U.java:37-46`).
* `ThresholdAction` (`nodes/models/threshold/ThresholdAction.java:26-30`): ON (ordinal 0) → 0x5004, OFF (1) → 0x5005 (`p234v7/L0.java:71,97`).
* `BatteryStatusIndicator` (app-side, `BatteryStatusIndicator.java:24-29`): GOOD=2, LOW=1, CRITICAL=0, UNKNOWN=3.
* `Position` (`p056e8/q.java`): 0..255; app converts percent→raw as `round(pct/100·255)`, 100 % → 254; on read `255→0` and `0→255` are swapped (`:22-31`). Whether the write path swaps too is not settled (`jhmesh.properties.Position` swaps both ways so its own round trip holds; `cross-repo-analysis.md` §8 — needs a device).

---

## 3. How properties combine into the app's "device" objects

### 3.1 Node → elements → capabilities
* Nordic `ProvisionedMeshNode` is mapped to `p065f8.f` (Node.kt) by `E7/f.java` (NodeMapper). Each element becomes
  `MeshElement(name, address, models, location)` with `location = LocationId(element.getLocationDescriptor())`
  (`E7/f.java:361-362`, `p065f8/d.java:57-62`). Node carries `productId` (string like `"0x0003"`), `companyId` (1319)
  and a list of `Capability` objects (`p065f8/f.java`).
* `LocationId` (`p065f8/c.java:12-15`): `FirstSensorElementId = 64 (0x40)`; the list `[64,65,66,67]` (`0x40..0x43`) is the
  range of "sensor/button" element locations. Elements whose location is `>= 64` are the button/sensor elements
  (e.g. the time-role capability is attached only if the lowest location is ≥ 64: `Z7/a.java:712-720`); locations `1`, `2`
  (`0x0001/0x0002`) are the load outputs — every such location that the primary device class does not claim becomes an
  extra load device (§3.2).
* Every `Capability` implementation has an element address `O()` and a `toByteArray()`; a `MessageBuilder`
  (`p234v7/W.java:14-35`: `b()` = Get, `c()` = Set, `d()` = expected status opcode) turns it into a mesh message and a
  `StatusMessageResolver` (`data/mesh/resolver/*`) turns the status back into a capability update.
* Config properties are addressed to the element holding the LBC Admin Property Server (usually the primary element,
  `Y7/AbstractC0914c.java:334-345`); per-button properties (KeyMode, KeyModeSceneConfig, KeySet*, InputEdgeDetection)
  are addressed to the button element; InsertId is requested once per LBC-User-Property-Server element (`Z7/a.java:283-288`).

### 3.2 DeviceMapper — from product ID + insert to device classes (`Y7/C0919h.java:121-216`)
1. The **primary device** is chosen by `(companyId == 1319, productId)` (else-if chain `Y7/C0919h.java:121-160`, helpers
   `io/grpc/u.java:60-70`, `p089i0/c.java:527-531`, `Y7/h0.java`): see §4.
2. The primary device claims a set of element locations `F1()` (e.g. detectors claim only the highest element
   `Y7/AbstractC0915d.java:57-84`; control switches claim the button elements `Y7/AbstractC0914c.java:65-73`).
3. For every **remaining** element location the node-level `ActuatorFunctionId` (InsertId property 0x0002) decides which
   extra load device is created (`C0919h.java:176-212`): Blind → `BlindDevice`; Switch → `SwitchLampDevice`
   (`MeasureLampDevice` if pid 0x0010); TwoGangSwitch → `SwitchLampDevice`; Dimming/TwoGangDimming → `DimLampDevice`;
   TwDimming → `TunableWhiteLampDevice`; Extension/Unset/Unknown → nothing.
   The lamp/blind classes compute their unicast range from the given location set (`Y7/i0.java:33-40`, `p065f8/e.java:18-35`).
4. `DeviceIdentifier` = `(nodeId, actuatorId{functionId, insertType}, locationIds, productId, uuid)` (`DeviceIdentifier.java:29-44`);
   a device with `locationStart == 64` is a "button" device (`:194-199`).

### 3.3 What is read when
* **On app open / refresh** (`domain/interactors/networking/LoadStateForDevices.java:147-285`): only live state —
  Generic OnOff, blind level (Generic Level) and actual temperature (Sensor 0x004F). No property reads.
* **On provisioning** (`ConfigureDevice`, `RequestManufacturerInfos`, `ConfigureControlSwitchLayout`, `ConfigureDetector`,
  `ConfigureSocket` in `domain/interactors/configuration/` and `manufacturer/`): manufacturer infos (SIG 16/17/26,
  `RequestManufacturerInfos.java:80-93`), STM32 version for RTR, button layout (0x5001, `ConfigureDevice.Params.requestButtonLayout`
  `ConfigureDevice.java:33-47`), detector defaults (TimedOnDuration 120 s, `ConfigureDetector.java:261-267`), lock function unlock, etc.
* **Per parameter screen**: the `deviceParameter/providers/*` request the capability's Get when the screen opens and send Set on change
  (`CommunicateWithDevice`, `domain/interactors/manufacturer/CommunicateWithDevice.java`).
* Version gates: RtrHvacMode needs SW ≥ 2.2.0.0, touch sensitivity ≥ 1.0.3.3 (`resolver/C1901b0.java:291-308,385-394`).

### 3.4 Device classes and the properties they carry (from their `implements` lists)
Capability-interface → property mapping is in the file names of `K8/*.java`, `Q8/*.java`, `p108j8/*.java`, `W8/*.java`, `T8/*.java`, `E8/a.java`, `N8/a.java`, `p225u8/*.java`, `H8/*.java`, `Z8/*.java`, `p086h9/*.java` (Kotlin names visible in each file header).

| Device class (file) | Properties / capabilities |
|---|---|
| Base `Device` (`Y7/AbstractC0916e.java:16`) | SIG 16/17 (`H8.a`), SIG 26 (`H8.b`), Astro register/status 0x0007/0x0008 (`p086h9.a/b`), Bootloader 0x0004 (`Z8.a`), SecureElement 0x0003 (`Z8.c`), InsertId 0x0002 (`p108j8.a`), SIG Generic Location, config beacon/netkey/relay/transmit |
| ControlSwitch base (`Y7/AbstractC0914c.java:23`) → rockers `Z7/a.java`, `Z7/g.java`, wall transmitters `Z7/i.java`, `Z7/j.java` | ButtonLayout 0x5001 (`p108j8.d`), KeyMode 0x5003 / SceneConfig 0x5002 / KeySet 0x5006-8 (`compatibles.g`), LED 0xA0xx (`compatibles.h`), DeviceKeyConfiguration 0x0001 (`K8.i`); rockers add SIG Time/TimeRole; wall transmitters add SIG Generic Battery (`p108j8.b`) |
| Detector base (`Y7/AbstractC0915d.java:26`) → `p012a8/a.java` (motion), `p012a8/b.java` (presence) | PIR A/B/C 0x6008-A (`compatibles.k`), PresenceControl 0x6003, PresenceBrightness 0x6004, PresenceControlPir 0x6005, DayMode 0x6015, OperationSite 0x6017, PresenceTest 0x6001, SwitchOnBrightness 0x600F, ForcedOff 0x6016, RepetitionTime 0x6021, OperationMode 0x6006, DeviceKeyConfiguration 0x0001, Time/TimeRole, sensor cadence/descriptor/settings |
| Blind (`Y7/C0892b.java:26`) | BlindMoveUpDownTime 0x1102, MoveRevisionTime 0x1101, MoveSlatsTime 0x1103, MoveOperationMode 0x1104, MoveOnPowerMode 0x1105, Blind/Slat position on power 0x1106/0x1107, Ventilation positions 0x110A/0x110B, BlindsInvertOutput 0x1108, ReferenceRun 0x110D, AutoDST 0x000F, EnforceOutput 0x0009, JH scheduler + scene action setup, Time/TimeRole |
| Lamp base (`Y7/i0.java:20`) → `f0` dim, `q0` switch, `n0` measure, `t0` tunable white | GeneralOn/OffDelay 0x1001/0x1002, TimedOnDuration 0x1007, InvertOutput 0x100C, Prewarning 0x100A, ManualOffEnable 0x100B, SwitchBlockingTime 0x100D, AutoDST 0x000F, EnforceOutput 0x0009, SIG OnPowerUp; `f0` adds DimMode 0x0013; `n0` adds SIG 0x6A energy, sensor 0x81, charts 0x5010/0x5011, RtrOperationMode 0x1014; `q0` adds RtrOperationMode 0x1014; `t0` adds Light CTL |
| Socket base (`Y7/p0.java:24`) → `s0` (switch socket), `r0` (measuring socket) | as lamp base + DeviceKeyConfiguration 0x0001, LED 0xA0xx, RtrOperationMode 0x1014; `r0` adds SIG 0x6A/0x6D, sensor 0x81, charts, thresholds 0x5004/0x5005 |
| RTR (`de/jung/junghome/domain/item/devices/RtrDevice.java`) | Temp set-points 0x1203-0x1205, HeatingOption 0x1208, ValveOutput 0x120A, HvacMode 0x120B, Boost 0x120D, SensorSelection 0x1221, SensorOffset 0x1224, SchedulerEnabled 0x1246, MainPage 0x1240, BlAutoOff 0x1247, ControllerType 0x1201, TouchSensitivity 0x0012, DeviceKeyConfiguration 0x0001, STM32 0x0005, sensor 0x4F, JH scheduler, scenes |
| MiniActuator base (`domain/item/devices/MiniActuatorDevice.java`) → `p023b8/a.java` energy, `p023b8/b.java` hardwired, `p023b8/c.java` wall transmitter | ButtonLayout, KeyMode set, DeviceKeyConfiguration 0x0001, InputEdgeDetection 0x5009; energy adds charts 0x5010/11, SIG 0x6A, sensor 0x81; wall transmitter adds Generic Battery |
| Gateway (`Y7/g0.java`) | Gateway strings 0xC001-0xC003 (`p255x8.a`), Time |

---

## 4. Product IDs (`productId` string from composition / advertisement, company 0x0527)

| pid | Device class (`Y7/C0919h.java`) | Display name (`app/utils/o.java` → `res/values/strings.xml device_name_*`) | Composition notes |
|---|---|---|---|
| `0x0001` | `Z7.a` OneGangRockerDevice (`:135`) | push-button 1-gang | primary element: LBC admin/user/manufacturer property servers, scene, scheduler, time, light lightness; button elements (location ≥ 0x40) with client models; loads per insert (§3.2). Demo composition `Z7/a.java:168-260` |
| `0x0002` | `Z7.g` TwoGangRockerDevice (`:133`) | push-button 2-gang | as above, 2 rockers (`Z7/g.java`) |
| `0x0003` | `Y7.r0` SwitchMeasureSocketDevice (`:10`, also `:121` in the try-branch) | socket | measuring socket: Generic OnOff, Sensor (0x81), SIG admin props 0x6A/0x6D, thresholds |
| `0x000C` | `Y7.s0` SwitchSocketDevice (`:143`) | socket | non-measuring socket |
| `0x0004` | `p023b8.b` MiniActuatorHardwiredDevice (`io/grpc/u.java:65`, `:137`) | switch actuator mini | |
| `0x000D` | `p023b8.b` MiniActuatorHardwiredDevice | blinds actuator mini | |
| `0x0010` | `p023b8.a` MiniActuatorEnergyDevice (`p089i0/c.java:530`, `:141`) | Puck switch actuator | energy-measuring puck; Switch insert loads become MeasureLampDevice (`C0919h.java:182-186`) |
| `0x0011` | `p023b8.b` MiniActuatorHardwiredDevice | Puck switch actuator 2-gang | |
| `0x0012` | `p023b8.b` | Puck dimmer | |
| `0x0013` | `p023b8.b` | Puck blinds actuator | |
| `0x0014` | `p023b8.b` | Puck DALI controller | |
| `0x0015` | `p023b8.b` | Puck binary input (230 V) | |
| `0x0016` | `p023b8.c` MiniActuatorWallTransmitterDevice (`:139`) | Puck binary input (battery) | low-power device, special update handling (`UpdateDeviceViewModel.java:250-260`) |
| `0x0005` | `Z7.i` WallTransmitterOneGangDevice (`:147`) | push-button 1-gang, battery-powered | Generic Battery |
| `0x0006` | `Z7.j` WallTransmitterTwoGangDevice (`:150-154`) | push-button 2-gang, battery-powered | |
| `0x0007` | `p012a8.a` MotionDetectorDevice (`:131`) | motion detector 1,1 m | detector element = highest element; relay loads separate |
| `0x0008` | `p012a8.a` MotionDetectorDevice | motion detector 2,2 m | |
| `0x0009` | `p012a8.b` PresenceDetectorDevice (`:127`) | presence detector | |
| `0x000A` | `RtrDevice` (`:129`) | room thermostat | |
| `0x000B` | `Y7.g0` Gateway (`Y7/h0.java`, `:156`) | gateway | |
| other | `Y7.u0` UnknownDevice (`:160`) | "Unknown" | logged "Unknown product id" |

Advertisement-based names come from the same table in `data/…/DeviceDiscoveringImpl` (`app/ui/project/gateway/detail/z.java:305-395`).

Cross-check against the gateway firmware's `ProductID` enum (`models/btmesh_product_ids.js:6-25`) — same numbering,
and the names settle what the "puck" family is: `0x01/0x02 PushButton1gang/2gang`, `0x03 SocketAct1gangEnergy`,
`0x04 SwitchAct1gang2input`, `0x05/0x06 PushButton1gangBat/2gangBat`, `0x07/0x08 MotionDetector1m/2m`,
`0x09 PresenceDetector`, `0x0A RoomThermostat`, `0x0B Gateway`, `0x0C SocketAct1gang`,
**`0x0D BlindsAct1gang2input`** (the blinds actuator mini — not a second switch-actuator variant),
`0x10 SwitchAct1gang2inputEnergy`, `0x11 SwitchAct2gang2input`, `0x12 DimmerAct1gang2input`,
`0x13 BlindsPP2Act1gang2input`, `0x14 DaliAct1gang2input`, `0x15 MiniSensor2inputMains`, `0x16 MiniSensor2inputBat`.
Every mini actuator / puck has two binary inputs ("2input").

---

## 5. Confidence and open questions

High confidence (both encoder and decoder read, consistent): all `u8`/bool/enum properties, ButtonLayout, KeyMode,
KeyModeSceneConfig, KeySetPropertyMode, thresholds, LED layout, AstroSchedulerRegister/Status, temperatures (×0.01),
sensor offset, delays (ms), SwitchBlockingTime, MoveSlatsTime, MoveRevisionTime, energy charts, LockFunction struct,
version strings, model/opcode mapping, product-ID table.

Open questions / caveats:
1. **DeviceKeyLock 0x0001 bit order**: encoder (`J8/C0541i.java:77-84`) and decoder (`z.java:471-478`) assign the five flags in
   mirrored bit positions (bit0 ↔ bit4). Verify against a real device; one of the two is a bug in the app.
   (Firmware: the gateway maps the raw value `0 → unlocked, 2 → reset-lock, 4 → button-lock, 6 / 12 → full-lock`
   (`jung-home-state-mode.js:52-59`), which matches the *encoder's* bit order — bit 1 = factory reset with time
   limit, bit 2 = local devices lock, bit 3 = key lock — so the decoder is the suspect one. `jhmesh/properties.py`
   `DEVICE_LOCK_FLAGS` follows the encoder; still unverified on a device.)
2. **0x1102 duplicate**: `BlindMoveUpDownTime` (u16 seconds) vs `MoveUpDownTimeProperty` (u32 ms) share the ID; the blind
   device holds both capabilities (`Y7/C0892b.java:1610`, `:2978-2979`). Only the 2-byte-seconds form is ever parsed from
   statuses; whether the 4-byte form is ever written could not be confirmed from the decompiled call sites.
3. ~~**InsertId 0x0002 byte order** relies on the Nordic `BitReader.bitsLeft()` quirk (`BitReader.java:21-23`; `currentBit` is the bit index inside the
   current byte, so `bitsLeft()==32` ⇔ value length == 4 bytes). Layout `[insertType u16][functionId u16]` is inferred from that; the
   iOS dump should confirm which half is which.~~ **Settled** on air: `[actuatorFunctionId u16 LE][insertType u16 LE]`
   (Field-tested corrections below; `InsertIdCodec` in `jhmesh/properties.py`).
4. ~~**SIG 0x001A software revision** is parsed as an ASCII digit string (`"02020002"` → `"2.2.0.2"`); if the device really returns
   binary bytes `02 02 00 02` (as the iOS note suggests) this parser would yield garbage — check what the iOS app does with it.~~
   **Settled** on air: 8 ASCII digits (Field-tested corrections below; `ASCII_VERSION` in `jhmesh/properties.py`).
   The LBC manufacturer versions 0x0003/0x0004/0x0005 are decoded from binary bytes (dotted decimal) — little-endian, also below.
5. Units not visible in code: DetectorRepetitionTime 0x6021 (u8), PresenceControlPir 0x6005 (u8), energy-chart scale (raw/10,
   probably Wh), PresenceBrightness/SwitchOnBrightness (assumed lux from UI wording "Lighting strength"/"Brightness threshold").
6. Properties the app declares but cannot decode (`TODO()`): 0x120F, 0x1218, 0x1212, 0x121C, 0x121D, 0x121F, 0x6007, 0x600B, 0x600C,
   0x600D, 0x6010-0x6014; GatewayAPIStatus 0xC000 has no message at all. Their lengths beyond the first byte are unknown —
   except 0xC000, a 1-byte bit field per the gateway firmware (§1.9, §1.10).
7. ~~The third ID of each LED triple (0xA003, 0xA006, …) is unused by the app; its meaning is unknown.~~ Resolved by the
   gateway firmware: the triples start at `0xA000` (`LEDn_CH_SELECTION`, `ON_MODE`, `OFF_MODE`; §1.8), so `0xA003` is
   LED 2's channel selection. The status LED of a gateway-linked key is a separate property, `0x5013` (§1.6) — the
   earlier guess that it might hide in the LED triple's spare slot was wrong.
8. The `writeProperty` hex helper (`UtilsKt.i`) pads oddly; it is a debug path and should not be used as a reference encoder.

---

## Field-tested corrections (see `docs/poc-gatt-proxy.md`)

Reads performed on the live mesh with `tools/mesh_poc.py prop …` against node `0148` (Push-button 1-gang, FW 2.2.0.2):

- **`0x0002` InsertId** on the wire is `[actuatorFunctionId u16 LE][insertType u16 LE]` (`00 00 02 00` = Switch, GenericInsert,
  matching the iOS cache). The decompiled parser reads the *reversed* byte array MSB-first, which is why §1 above
  describes the order the other way round.
- **Version properties `0x0003` (secure element) and `0x0004` (bootloader)** are little-endian: `0d 02 01 00` → 0.1.2.13,
  `00 00 04 02` → 2.4.0.0 (both equal the images in `assets/updates/*_update.json`). Display by reversing the bytes.
- `0x5003` KeyMode is also served by the **User** property server (`CE 27 05` Get → `D1 27 05` Status, access=3); the gateway polls it that way.
  Asking a *load* element (loc `0001`) returns a status with an empty value.
- `userAccess` in Status replies: 1 for manufacturer/user read-only properties, 3 for KeyMode.
- Socket sensor scaling confirmed: `0x0081` Active Power Loadside uint24 ×0.1 W, `0x005D` voltage uint16 in **1 V** steps
  (not SIG 1/64 V), `0x005C` current uint16 ×0.01 A.
- **`0x5012` (20498) — button event (not in the app).** Sent by button elements in key mode 6 as `LBC User Property Set
  Unack` to the gateway's element group: value `[counter u8][event u8]`; captured from *key* elements: event `0x05`
  click / `0x06` hold start / `0x04` hold end; double press = two clicks in quick succession (same second); each event
  published twice. Rocker halves use events 0–3 instead (gateway firmware; full table in §1.6 and
  `docs/poc-gatt-proxy.md`).
- SIG `0x001A` Device Software Revision confirmed as 8-char ASCII (`"02020002"`), read by the gateway with
  `Generic Manufacturer Property Get` and answered with opcode `0x46`. SIG `0x006D` Total Device Power On Time on
  sockets is read via `Generic Admin Property Get` (`0x4A` status, uint24 hours).
- **Errata (cross-check while writing `docs/gap-analysis/control-and-state.md`):** `SchedulerEnabledProperty` is
  `0x1246` (decimal 4678, `r.java:2340`), not 0x1236; `DropOfTempStateProperty` is `0x1212` (4626, `r.java:2040`), not
  0x121A. Fixed in the tables above.
