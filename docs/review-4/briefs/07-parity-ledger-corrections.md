# 07 — Parity ledger corrections, symbol citations

Phase P1 · Wave 1 · Size M · Closes: D31 (report 6 §2 A–E); records the "not worth doing" decisions of report 6 §1.

Follow the [conventions](README.md#conventions) in full.

## Goal

The parity ledgers say what the code actually does: no row claims *implemented* for absent or CLI-only behaviour,
rows already done are marked so, dup chains point at the right row, declined items carry a decision record, and every
citation names a symbol that `tools/parity.py check` verifies.

## Background

`docs/parity/ledger-*.json` track every message, property, UI element, product and network use case of the JUNG app
and gateway against this integration (`docs/parity/README.md` gives the rules); `tools/parity.py check` and
`tests/test_parity.py` only check that cited paths exist.

Report 6 audited the implemented rows. Spot-checked here: `msg:op:5d` is *implemented* but no Time Status handler
exists outside `jhmesh/messages.py` describe; `msg:proxy:01` is *implemented* but `pdu.proxy_config_add_addresses` has
no caller.

- **A, claimed but absent / CLI-only** → partial, na/declined (with record) or "CLI only": `msg:op:5d`,
  `msg:proxy:01` + `mgmt:builder:addproxyfilteraddressesmessagebuilder`, `prop:0xc001`,
  `prop:meta:jungmeshapi-readproperty` / `-writeproperty`, `prop:model:0x1013`,
  `prod:insert-type:{generic-insert,no-insert,not-supported,unknown}`, `prod:provider:{sensor-values-parameter,
  blind-parameters,shared-device-parameters}`, `prod:pid:0x0015`, `ui:vm:mainviewmodel.observejungmeshnetwork`,
  `ui:vm:controlswitchconfigurationviewmodel.observeitems`, `mgmt:builder:netkeyupdatemessagebuilder`,
  `mgmt:builder:updatekeyrefreshphasemessagebuilder`, `mgmt:cfgop:8017`, `air:access:8038/803a/803b/803c`,
  `air:access:0e-0527:0x1007`, `mgmt:debug:meshmodelviewmodel.*`, `mgmt:str:error_empty_iv_index`,
  `mgmt:pref:show_mesh_network_cell`, `mgmt:str:settings_mesh_network_title`,
  `mgmt:str:settings_show_mesh_network_setting`, `mgmt:debug:entry`, `mgmt:ble:clearcache`,
  `net:enum:communicatewithdevice.communicationtype`, `mgmt:proxyfilter:white_list_filter`,
  `mgmt:const:proxyfiltertype.inclusion_list_filter`, `mgmt:setup:setwhitelistfilter`.
- **B, too pessimistic** → implemented (on-air check as a note, brief 30) or na/declined: `msg:op:8241`,
  `net:uc:{create,delete,toggle}threshold`, `prop:meta:status-parse`, `prop:0xa004`, `prop:0xa005`,
  `ui:vm:roomtemperatureviewmodel.changemode`, `prod:param:shared-device:*` (5) and `prod:param:lamp:prewarning`,
  Sensor Cadence / Settings / Series / Column `msg:op:53`–`5b`, `8232`–`8236`, `air:access:52:0x0052` and
  `air:access:8231:0x0052`, Light LC / HSL opcodes, `msg:op:820d` / `820e`, `mgmt:provauth:01-03`,
  `mgmt:provpubkeyoob:public_key_information_available`, `mgmt:nordic:unprovisionedbeacon.init`,
  `mgmt:api:meshmanagerapi.isivupdatetestmodeactive`.
- **C, dup chains**: `ui:screen:groupiconselectionfragment`, `ui:vm:groupdetailsviewmodel.updategroupicon` →
  `net:enum:groupicon`; `ui:uc:removeinvaliddevices` / `net:uc:removeinvaliddevices` say the target is gap/build;
  `ui:screen:deviceinfofragment` → `mgmt:flow:checkfordeviceupdate`; `prod:class:rtr-device` →
  `prod:param:time-keeper:time-keeper`; `prod:class:device` → `prop:0x0002`.
- **D, text wrong**: `net:enum:timerole`, `net:export:meta.sceneinfo`, `net:uc:updatedevicetypegroup`,
  `air:access:8018`, `msg:op:5e`, `mgmt:appop:823a`, `msg:op:8203` / `824d`,
  `ui:msg:series_timer_scene_not_available_title`, `prod:ui:device_info_scenes_description`,
  `ui:vm:deviceinfoviewmodel.observedevices`, `ui:uc:getscenesfordevice`, `ui:uc:updategroup`.
- **E, citation drift**: most `coordinator.py` citations past about line 1590 point ~16 lines above the function
  (~23 near the end); also `mesh_config.py`, `config_entities.py`, `onboard.py`, `client.py`.

## Read first

`docs/parity/README.md`; `tools/parity.py` (`check`); `tests/test_parity.py`; the ledgers; `docs/hidden-features.md`
§1–§3, §7.3 (evidence for the declines); `docs/roadmap.md` "Candidates from the device side".

## Steps

1. Re-verify each row above against the code (grep the symbol); change status / dup target / text accordingly. For
   every new na/declined row add a one-line decision record (the evidence: "no JUNG product hosts the server",
   "Setup Server holds no cadence", "0x0052 reads 0 on loaded sockets", "explicit transitions supersede DTT", …).
2. Citation format: `path::symbol` (or `path:line::symbol`). Teach `tools/parity.py check` to verify the symbol is
   defined in the file (and within a few lines of a given line); convert existing citations with a script, by symbol
   lookup, never by hand-guessed lines.
3. Update `docs/parity/README.md` for the new citation rule.

## Tests to add

`tests/test_parity.py`: a row citing a missing symbol fails; a drifted line with the right symbol passes or is
reported per the new rule; the ledger as committed passes.

## Acceptance criteria

`tools/parity.py check` and `tests/test_parity.py` green; every row in A–D decided; gates green.

## Verifiable on air here?

Local (documentation). Rows whose only gap is an on-air check go to brief 30.

## Risks / off-by-default / "unverified on air"

Later briefs edit the same ledgers; this lands in wave 1 so they build on the corrected rows.

## Depends on

None.

## Files touched

`docs/parity/ledger-*.json`, `docs/parity/README.md`, `tools/parity.py`, `tests/test_parity.py`, `CHANGELOG.md`
(Internal).
