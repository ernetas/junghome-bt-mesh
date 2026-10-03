# Firmware images bundled in the APK → product IDs

Source: `android/jadx-out/resources/assets/updates/*_update.json` (JUNG HOME 2.2.0). Each descriptor lists the
`product_id`s (= mesh Composition Data **PID**, the `pid` in `MeshNetwork.json`) and hardware revisions an image applies to.
The firmware family is "LB Connect" (`lb-connect-*`), which is what the `LBC…` prefix in the app code stands for.

| Firmware family | App version | product_id (PID) | HW rev | Product |
|---|---|---|---|---|
| `lb-connect-steuertaste` | 2.2.0.2 | 1, 2 | 0, 1 | Push-button ("Steuertaste") 1-gang / 2-gang (230 V, with load) |
| `lb-connect-steckdose` | 2.2.0.1 | 3, 12 | 0–3, 4095 | Sockets ("Steckdose"): PID 3 = socket with power metering (`SocketAct1gangEnergy`), PID 12 = socket without metering (`SocketAct1gang`; gateway firmware `btmesh_product_ids.js`, `properties.md` §4, `jhmesh/devices.py` `SOCKET_PIDS`) — one firmware image, two products |
| `lb-connect-miniaktor` | 2.2.0.1 | 4, 13 | 0–2 | Mini actuators: PID 4 = switch actuator 1-gang mini (`SwitchAct1gang2input`), PID 13 = **blinds** actuator mini (`BlindsAct1gang2input`, gateway firmware `btmesh_product_ids.js`; `properties.md` §4) — one firmware image, two products |
| `lb-connect-wandsender` | 2.2.0.1 | 5, 6 | 2, 3 | Battery wall transmitter ("Wandsender") 1-/2-gang |
| `lb-connect-melder` | 2.2.0.2 | 7, 8, 9 | 1, 2 | Detectors ("Melder": motion / presence variants) |
| `lb-connect-rtr` | 2.2.0.5 | 10 | 1 | Room thermostat ("Raumtemperaturregler") |
| `STM32_Image_block_compressed_V4-4-5` | 4.4.5.0 | 10 (`image_type: "stm"`) | 1 | Room thermostat STM32 co-processor image ("BIZ0" container, not GBL; requires application ≥ 1.9.2.1) — **not** the gateway |
| `lb-connect-miniaktor-2k` | 2.2.0.1 | 16–20 | 0 | The "puck" actuators, each with two binary inputs: PID 16 = switch actuator 1-gang with energy metering (`SwitchAct1gang2inputEnergy`), 17 = switch actuator 2-gang (`SwitchAct2gang2input`), 18 = dimmer (`DimmerAct1gang2input`), 19 = blinds (`BlindsPP2Act1gang2input`), 20 = DALI controller (`DaliAct1gang2input`) (gateway firmware `btmesh_product_ids.js`, `properties.md` §4) — only PID 17 is 2-gang |
| `lb-connect-bin2f-230` | 2.2.0.2 | 21 | 0 | Binary input 2-fold, 230 V ("Puck") |
| `lb-connect-bin2f-batt` | 2.2.0.1 | 22 | 0 | Binary input 2-fold, battery |

Cross-check with the iOS dump: push-buttons report SIG property 0x001A (Device Software Revision, Fixed String 8) =
ASCII `"02020002"` → 2.2.0.2 = `steuertaste` image; sockets and mini actuators report `"02020001"` → 2.2.0.1. The app
splits the 8-digit string into `major.minor.patch.build` two digits each (`properties.md`).

The stale metadata entry with `productIdentifier.albrechtJung = 9` (`F082C0FF-FE62-538A`, "Balcony A") was therefore a
detector (`melder`) that has since been removed from the network.

## Image format
- `lb-connect-*.gbl`: Silicon Labs **Gecko Bootloader (GBL)** containers (magic `EB 17 A6 03`), variant
  `application-secure-secure_bootloader-seupgrade-sign-encrypt-lzma` → the devices are Silicon Labs EFR32 SoCs running the
  Silabs Bluetooth Mesh stack; images are signed and encrypted (not decryptable from the APK alone). Each descriptor also
  carries `bootloader` (2.4.0.0) and `secure_element` (0.1.2.13) sub-images with minimum-version `requires` chains.
- STM32 image: `BIZ0` container header (Cortex-M vector table at `0x0800xxxx`); its manifest targets `product_id: 10`
  (RTR) only, so it is the thermostat's STM32 co-processor firmware. It is flashed through the same Silicon Labs OTA
  GATT service as the EFR32 images (the EFR32 forwards it), not via mesh BLOB/Firmware Update models — the app never
  uses those. No update image exists for the gateway (product 11). See `transport-provisioning.md` §5.
- `json_schema_version` 1.3.0.0, `storage_schema_version {application:1, stack:2, master:1}`.
