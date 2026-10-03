"""jhmesh.advert: the JUNG manufacturer-specific advertisement record and the UUID ↔ MAC relation."""

from __future__ import annotations

import pytest

from jhmesh.advert import (
    JUNG_COMPANY_ID,
    JungAdvertisement,
    mac_from_uuid,
    parse_jung_advertisement,
    parse_manufacturer_data,
)

h = bytes.fromhex


def test_type_3_record_as_seen_on_air():
    # the record layout as captured (the MAC little-endian behind the layout byte); the addresses themselves are
    # documentation-block ones (`00:00:5E:00:53:xx`), not the captured devices'
    # a mini actuator (pid 4, function 0, layout 1) and a 2-gang push-button (pid 2, function 6, layout 2)
    assert parse_jung_advertisement(
        h("2705030400000001000453005e0000")
    ) == JungAdvertisement(3, 4, 0, 1, "00:00:5E:00:53:04")
    assert parse_jung_advertisement(
        h("2705030200060002000253005e0000")
    ) == JungAdvertisement(3, 2, 6, 2, "00:00:5E:00:53:02")
    # the metering socket: layout 0x14
    socket = parse_jung_advertisement(h("2705030300000014001753005e0000"))
    assert socket is not None
    assert (socket.product_id, socket.button_layout, socket.mac) == (
        3,
        0x14,
        "00:00:5E:00:53:17",
    )


def test_type_1_and_2_records():
    assert parse_jung_advertisement(
        h("2705010100 0405".replace(" ", ""))
    ) == JungAdvertisement(1, 1, 4, 5)
    assert parse_jung_advertisement(h("270502020006000500")) == JungAdvertisement(
        2, 2, 6, 5
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        h("2705"),
        h("270503"),
        h("4c000215"),  # another company
        h("27050401000000"),  # unknown type
        h("2705030400000001"),  # type 3 but too short
        h("27050101000405ff"),  # type 1 but too long
    ],
)
def test_other_payloads_are_not_jung_records(payload: bytes):
    assert parse_jung_advertisement(payload) is None


def test_parse_manufacturer_data_map():
    assert JUNG_COMPANY_ID == 1319
    data = {JUNG_COMPANY_ID: h("030400000001000453005e0000"), 0x004C: h("0215")}
    assert parse_manufacturer_data(data) == JungAdvertisement(
        3, 4, 0, 1, "00:00:5E:00:53:04"
    )
    assert parse_manufacturer_data({0x004C: h("0215")}) is None
    assert parse_manufacturer_data({}) is None
    assert (
        parse_manufacturer_data(
            {JUNG_COMPANY_ID: bytearray(h("030400000001000453005e0000"))}
        )
        is not None
    )


def test_mac_from_uuid():
    assert mac_from_uuid("00005EFF-FE00-5314-0000-000000000000") == "00:00:5E:00:53:14"
    assert mac_from_uuid("00005eff-fe00-5314-0000-000000000000") == "00:00:5E:00:53:14"
    assert (
        mac_from_uuid("00000001-0000-4000-8000-000000000001") is None
    )  # a phone's random UUID
    assert mac_from_uuid("00005EFFFE005314") is None
    assert (
        mac_from_uuid("00005EFFFE0053140000000000000000") == "00:00:5E:00:53:14"
    )  # dashes optional
    assert (
        mac_from_uuid("00005EFF-FE00-5314-1234-56789ABCDEF0") == "00:00:5E:00:53:14"
    )  # the second half is not inspected here (the export insists on zeros; matching on air does not)
    assert (
        mac_from_uuid("ZZZZZZFF-FEZZ-ZZZZ-0000-000000000000") is None
    )  # not hex: was "ZZ:ZZ:ZZ:ZZ:ZZ:ZZ", which no Bluetooth address can equal
    assert mac_from_uuid("00005EFF-FE00-5314-0000-00000000000G") is None
    assert mac_from_uuid("") is None
