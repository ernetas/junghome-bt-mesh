"""Access-PDU builders for the messages nodes publish, plus registry lookups."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.junghome_ble.const import DOMAIN
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.pdu import encode_opcode

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

OUR_ADDRESS = 0x0D00
MESH_UUID = "1baf3ade-0000-4000-8000-000000000001"
SEQ_STORE_KEY = f"{DOMAIN}.seq.{MESH_UUID}"  # the mesh's sequence-number store (`coordinator.seq_store`)

# Node identities of the synthetic exports (tests/fixtures/make_fixture.py): EUI-64 UUIDs over MACs from the IANA
# documentation block 00:00:5E:00:53:xx (RFC 7042 §2.1.2), never a real device. Lower-cased, as the registries and
# unique ids carry them; `.upper()` gives the CDB spelling.
PROVISIONER_UUID = (
    "00000001-0000-4000-8000-000000000001"  # the phone, node 0001 (no MAC)
)
# MeshNetwork.json: gateway, "WC mirror" push-button 1-gang, "Boiler" metering socket, "Living room DALI"
# push-button 2-gang, "WC ceiling" push-button 1-gang with a dimmer, "Kitchen ceiling" 2-channel actuator
NODE_GATEWAY = "00005eff-fe00-530d-0000-000000000000"
NODE_LIGHT_SWITCH = "00005eff-fe00-5314-0000-000000000000"
NODE_SOCKET = "00005eff-fe00-5317-0000-000000000000"
NODE_LIGHT_CTL = "00005eff-fe00-5323-0000-000000000000"
NODE_LIGHT_DIMMER = "00005eff-fe00-5330-0000-000000000000"
NODE_ACTUATOR = "00005eff-fe00-5340-0000-000000000000"
NODE_THERMOSTAT = "00005eff-fe00-5350-0000-000000000000"  # MeshNetwork-rtr.json
# MeshNetwork-detectors.json: motion detector, presence detector, wall transmitters 1-gang / 2-gang
NODE_MOTION = "00005eff-fe00-5351-0000-000000000000"
NODE_PRESENCE = "00005eff-fe00-5352-0000-000000000000"
NODE_TRANSMITTER_1G = "00005eff-fe00-5353-0000-000000000000"
NODE_TRANSMITTER_2G = "00005eff-fe00-5354-0000-000000000000"
# Blinds.json: blinds actuator mini, push-button 2-gang with a blinds insert, PP2 puck
NODE_BLIND = "00005eff-fe00-5355-0000-000000000000"
NODE_SHUTTER = "00005eff-fe00-5360-0000-000000000000"
NODE_PUCK = "00005eff-fe00-5370-0000-000000000000"


def mac_of(node_uuid: str) -> str:
    """The MAC a fixture node's EUI-64 UUID encodes (`00005eff-fe00-5314-…` → `00:00:5E:00:53:14`)."""
    raw = node_uuid.replace("-", "").upper()
    return ":".join(raw[i : i + 2] for i in (0, 2, 4, 10, 12, 14))


MAC_LIGHT_SWITCH = mac_of(
    NODE_LIGHT_SWITCH
)  # the fake proxy link's address (`conftest.PROXY_ADDRESS`)
MAC_LIGHT_CTL = mac_of(NODE_LIGHT_CTL)

# elements of the synthetic export
GATEWAY = 0x00DC  # the gateway node
LIGHT_SWITCH = 0x0148  # "WC mirror", switched
LIGHT_CTL = 0x0232  # "Living room DALI", tunable white
LIGHT_CTL_TEMPERATURE = (
    LIGHT_CTL + 1
)  # its temperature element (Light CTL Temperature Server)
LIGHT_DIMMER = 0x0300  # "WC ceiling", dimmer
LIGHT_OUT1, LIGHT_OUT2 = 0x0400, 0x0401  # 2-channel actuator
SOCKET = 0x0172  # "Boiler"
SOCKET_SENSOR = 0x0173
BUTTON_WC = 0x0149  # single key, wired to the gateway (vendor events)
ROCKER_A, ROCKER_B = 0x0234, 0x0235
BUTTON_DIMMER = 0x0301  # wired to its own load 0x0300 (SIG messages to group C070)
GROUP_DIMMER = 0xC070

UID_LIGHT_SWITCH = f"{NODE_LIGHT_SWITCH}-0001"
UID_LIGHT_CTL = f"{NODE_LIGHT_CTL}-0001"
UID_LIGHT_DIMMER = f"{NODE_LIGHT_DIMMER}-0001"
UID_LIGHT_OUT2 = f"{NODE_ACTUATOR}-0002"
UID_SOCKET = f"{NODE_SOCKET}-0001"
UID_BUTTON_WC = f"{NODE_LIGHT_SWITCH}-0040"
UID_ROCKER_A = f"{NODE_LIGHT_CTL}-0040"
UID_ROCKER_B = f"{NODE_LIGHT_CTL}-0041"
UID_BUTTON_DIMMER = f"{NODE_LIGHT_DIMMER}-0040"
UID_PROXY = f"{MESH_UUID}-proxy"

SENSOR_POWER, SENSOR_VOLTAGE, SENSOR_CURRENT = 0x0081, 0x005D, 0x005C
# SIG device property of a metering socket, polled with Generic Admin Property Get; the total-energy counter 0x006A
# next to it in the gap analysis is absent on air (a Status without a value is how the socket says so)
PROPERTY_POWER_ON_TIME = 0x006D
PROPERTY_TOTAL_ENERGY = (
    0x006A  # meter element, Admin server: the app's resettable total
)
PROPERTY_PRECISE_TOTAL_ENERGY = (
    0x0072  # meter element, Manufacturer server: lifetime total
)
PROPERTY_ENERGY_SINCE_TURN_ON = 0x000D  # meter element, Manufacturer server
BUTTON_CLICK, BUTTON_HOLD_START, BUTTON_HOLD_END = 0x05, 0x06, 0x04


def onoff_status(on: bool, target: bool | None = None, remaining: int = 0) -> bytes:
    p = bytes([1 if on else 0])
    if target is not None:
        p += bytes([1 if target else 0, remaining])
    return encode_opcode(M.GEN_ONOFF_STATUS) + p


def lightness_status(
    present: int, target: int | None = None, remaining: int = 0
) -> bytes:
    p = present.to_bytes(2, "little")
    if target is not None:
        p += target.to_bytes(2, "little") + bytes([remaining])
    return encode_opcode(M.LIGHT_LIGHTNESS_STATUS) + p


def ctl_status(
    lightness: int,
    kelvin: int,
    target: tuple[int, int] | None = None,
    remaining: int = 0,
) -> bytes:
    p = lightness.to_bytes(2, "little") + kelvin.to_bytes(2, "little")
    if target is not None:
        p += (
            target[0].to_bytes(2, "little")
            + target[1].to_bytes(2, "little")
            + bytes([remaining])
        )
    return encode_opcode(M.LIGHT_CTL_STATUS) + p


def ctl_temperature_status(
    kelvin: int, target: int | None = None, delta_uv: int = 0, remaining: int = 0
) -> bytes:
    """Light CTL Temperature Status `[temp][delta UV]`, with `[target temp][target delta UV][remaining]` if `target`."""
    p = kelvin.to_bytes(2, "little") + delta_uv.to_bytes(2, "little", signed=True)
    if target is not None:
        p += (
            target.to_bytes(2, "little")
            + delta_uv.to_bytes(2, "little", signed=True)
            + bytes([remaining])
        )
    return encode_opcode(M.LIGHT_CTL_TEMP_STATUS) + p


def ctl_range_status(kelvin_min: int, kelvin_max: int, status: int = 0) -> bytes:
    """Light CTL Temperature Range Status `[status][min K][max K]`."""
    return (
        encode_opcode(M.LIGHT_CTL_TEMP_RANGE_STATUS)
        + bytes([status])
        + kelvin_min.to_bytes(2, "little")
        + kelvin_max.to_bytes(2, "little")
    )


def admin_property_status(
    pid: int, value: bytes, access: int = 3, opcode: int = M.GEN_ADMIN_PROP_STATUS
) -> bytes:
    """SIG Generic (Admin) Property Status `[pid u16][user access][value]`, as a socket answers a property Get."""
    return encode_opcode(opcode) + pid.to_bytes(2, "little") + bytes([access]) + value


def property_absent_status(pid: int, opcode: int = M.GEN_ADMIN_PROP_STATUS) -> bytes:
    """SIG Generic Property Status carrying only the property id: the socket's answer for a property it does not have."""
    return encode_opcode(opcode) + pid.to_bytes(2, "little")


def sensor_replies(
    power: bytes = b"\x00\x00",
    voltage: bytes = b"\x00\x00",
    current: bytes = b"\x00\x00",
) -> dict[bytes, bytes]:
    """What a socket's meter answers to the refresh's three qualified Sensor Gets (`Sensor Get pid` → Sensor Status)."""
    return {
        M.sensor_get(SENSOR_POWER): sensor_status((SENSOR_POWER, power)),
        M.sensor_get(SENSOR_VOLTAGE): sensor_status((SENSOR_VOLTAGE, voltage)),
        M.sensor_get(SENSOR_CURRENT): sensor_status((SENSOR_CURRENT, current)),
    }


def sensor_status(*values: tuple[int, bytes]) -> bytes:
    """Sensor Status with Format B marshalled (property, raw) pairs."""
    out = encode_opcode(M.SENSOR_STATUS)
    for prop, raw in values:
        out += bytes([((len(raw) - 1) << 1) | 1]) + prop.to_bytes(2, "little") + raw
    return out


def vendor_button_event(counter: int, code: int) -> bytes:
    """JUNG 'LBC User Property Set Unack' carrying property 0x5012 (ButtonEvent) as the gateway sees it."""
    return encode_opcode(0x10, M.JUNG_CID) + bytes([0x12, 0x50, counter, code])


def find_issue(hass: HomeAssistant, key: str) -> ir.IssueEntry | None:
    """Return the integration's repair issue raised under `key` (`coordinator.issue_id` suffixes the entry id), if any."""
    found = [
        issue
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN and issue_id.startswith(f"{key}_")
    ]
    assert len(found) <= 1, [issue.issue_id for issue in found]
    return found[0] if found else None


def entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    eid = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert eid is not None, f"no {domain} entity with unique id {unique_id}"
    return eid


def rtr_links_export(directory: Path) -> Path:
    """`MeshNetwork-rtr.json` with the app's RTR -> actuator links (`SetMultiConnection`), written to `directory`.

    Output 1 of the 2-channel actuator (0x0400) and the socket (0x0172) subscribe their OnOff servers to the
    thermostat's element group 0xC090, which its OnOff client publishes to.
    """
    raw = json.loads(
        (Path(__file__).parent / "fixtures" / "MeshNetwork-rtr.json").read_text()
    )
    for node in raw["meshNetwork"]["nodes"]:
        if node["unicastAddress"] not in ("0400", "0172"):
            continue
        for model in node["elements"][0]["models"]:
            if model["modelId"] == "1000":
                model["subscribe"].append("C090")
    path = directory / "MeshNetwork-rtr-links.json"
    path.write_text(json.dumps(raw))
    return path


def device_name_of(hass: HomeAssistant, eid: str) -> str | None:
    """The name of the device the entity `eid` sits under."""
    entry = er.async_get(hass).async_get(eid)
    assert entry is not None
    assert entry.device_id is not None
    device = dr.async_get(hass).async_get(entry.device_id)
    return device.name if device else None
