"""What a JUNG node tells the world in its Bluetooth advertisements, without any mesh key.

Every provisioned JUNG node advertises its Mesh Proxy service (0x1828 service data = the Network ID) **from its
public MAC address**, and next to it — every 1.2 s, as `ADV_NONCONN_IND` — a manufacturer-specific structure of
Albrecht JUNG (company id 0x0527), the `LBCAdvertisementData` record the app documents for unprovisioned devices
(`docs/android/transport-provisioning.md` §2.3): product id, actuator function id, button layout and, in its
type-3 form, the MAC again. Seen on air with the nRF sniffer (`docs/hidden-features.md` §8).

So a scanner learns, key-free, which physical JUNG device is behind a Bluetooth address and what it is — the mesh
export knows the same MAC as the node UUID (EUI-64: `30FB10FF-FE12-3456-…` is `30:FB:10:12:34:56`), which is how
`JungHomeHub` names its proxy before the Filter Status arrives and notices nodes the export does not know.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "ADVERT_TYPE_LENGTHS",
    "JUNG_COMPANY_ID",
    "JungAdvertisement",
    "mac_from_uuid",
    "parse_jung_advertisement",
    "parse_manufacturer_data",
]

JUNG_COMPANY_ID = 0x0527  # Albrecht JUNG GmbH & Co. KG (Bluetooth SIG), 1319 decimal — Home Assistant's manufacturer_id
ADVERT_TYPE_LENGTHS = {
    1: 7,
    2: 9,
    3: 15,
}  # `jungAdvType` → total payload length (company id included)


@dataclass(frozen=True)
class JungAdvertisement:
    """One decoded `LBCAdvertisementData` record.

    `mac` is only present in the type-3 record (`AA:BB:CC:DD:EE:FF`, the way the app prints it and the export's
    node UUIDs encode it); the other two carry the product, function and layout alone.
    """

    advert_type: int
    product_id: int
    actuator_function_id: int
    button_layout: int
    mac: str | None = None


def parse_jung_advertisement(payload: bytes) -> JungAdvertisement | None:
    """Decode the manufacturer-specific AD value (`payload` *includes* the leading company id); None when it is not one.

    Layout (little-endian): `[cid u16][type u8][productId u16]` then type 1: `[afid u8][layout u8]`, type 2:
    `[afid u16][layout u16]`, type 3: `[afid u16][layout u16][MAC 6 B, reversed]`. Anything else is not a JUNG record.
    """
    if len(payload) < 5 or int.from_bytes(payload[:2], "little") != JUNG_COMPANY_ID:
        return None
    advert_type = payload[2]
    if ADVERT_TYPE_LENGTHS.get(advert_type) != len(payload):
        return None
    product_id = int.from_bytes(payload[3:5], "little")
    if advert_type == 1:
        return JungAdvertisement(1, product_id, payload[5], payload[6])
    afid = int.from_bytes(payload[5:7], "little")
    layout = int.from_bytes(payload[7:9], "little")
    if advert_type == 2:
        return JungAdvertisement(2, product_id, afid, layout)
    mac = ":".join(f"{b:02X}" for b in payload[9:15][::-1])
    return JungAdvertisement(3, product_id, afid, layout, mac)


def parse_manufacturer_data(
    manufacturer_data: dict[int, bytes],
) -> JungAdvertisement | None:
    """Decode a JUNG record from a Bluetooth manufacturer-data map (company id → value, as bleak / HA hand it over)."""
    value = manufacturer_data.get(JUNG_COMPANY_ID)
    if value is None:
        return None
    return parse_jung_advertisement(
        JUNG_COMPANY_ID.to_bytes(2, "little") + bytes(value)
    )


_EUI64_HEAD = re.compile(r"^([0-9A-F]{6})FFFE([0-9A-F]{6})[0-9A-F]{16}$")


def mac_from_uuid(uuid: str) -> str | None:
    """JUNG node UUIDs are the MAC in EUI-64 form: `30FB10FF-FE12-3456-…` → `30:FB:10:12:34:56`; None for another shape.

    The contract for *matching on air*: 32 hex digits (dashes ignored, any case) with `FFFE` in the middle of
    the first half; the second half is not inspected (JUNG writes zeros there, the export decides what it
    accepts). A non-hex "MAC" (`ZZ:ZZ:…`) can never match a Bluetooth address, so it is None as well.
    `export.mac_from_uuid` answers the same question for what is *written* into the app's file ('' instead of
    None, and the trailing zero half is required).
    """
    m = _EUI64_HEAD.match(uuid.replace("-", "").upper())
    if m is None:
        return None
    raw = m.group(1) + m.group(2)
    return ":".join(raw[i : i + 2] for i in range(0, 12, 2))
