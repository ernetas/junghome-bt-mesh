"""jhmesh.properties: every codec round-trips, the catalogue is consistent, and the documented byte vectors decode."""

from __future__ import annotations

import re
from typing import Any

import pytest

from jhmesh import messages as M
from jhmesh import properties as P

h = bytes.fromhex


# ----------------------------------------------------------------------------- scalar codecs


def test_int_codec_round_trips_and_rejects_bad_input():
    assert P.U8.encode(7) == b"\x07"
    assert P.U16.encode(0x1234) == h("3412")
    assert P.U24.decode(h("a00100ff")) == 0x1A0  # trailing bytes are ignored
    assert P.U32.encode(1) == h("01000000")
    assert P.S16.encode(-5) == h("fbff")
    assert P.S16.decode(h("fbff")) == -5
    with pytest.raises(ValueError, match="does not fit"):
        P.U8.encode(256)
    with pytest.raises(ValueError, match="expected an integer"):
        P.U8.encode(True)
    with pytest.raises(ValueError, match="expected an integer"):
        P.U8.encode(1.5)
    with pytest.raises(ValueError, match="need 2"):
        P.U16.decode(b"\x01")


def test_counter_codec_reads_any_length_and_writes_fixed_size():
    c = P.Counter(4)
    assert c.decode(h("102700")) == 10000  # uint24 on air (power-on hours)
    assert c.decode(h("e803000000")) == 1000
    assert c.encode(0) == h("00000000")  # the app's reset
    with pytest.raises(ValueError, match="need 1"):
        c.decode(b"")


def test_bool_codec():
    assert P.BOOL.encode(True) == b"\x01"
    assert P.BOOL.encode(0) == b"\x00"
    assert P.BOOL.decode(b"\x01") is True
    assert P.BOOL.decode(b"\x00\xff") is False
    assert P.BOOL.decode(b"\x02") is True
    with pytest.raises(ValueError, match="need 1"):
        P.BOOL.decode(b"")


def test_enum_codec_names_ints_and_unknowns():
    e = P.Enum(P.KEY_MODE)
    assert e.encode("gateway") == b"\x06"
    assert e.encode(2) == b"\x02"
    assert e.decode(b"\x05") == "switch"
    assert e.decode(b"\x09") == 9  # unmapped values come back as the raw integer
    assert e.options[:3] == ("light", "move", "scene")
    with pytest.raises(ValueError, match="unknown option"):
        e.encode("teleport")
    layout = P.Enum(P.BUTTON_LAYOUT, size=2)
    assert layout.encode("one_left_one_right") == h("0500")
    assert layout.decode(h("ff00")) == "unknown"
    with pytest.raises(ValueError, match="need 2"):
        layout.decode(b"\x05")


def test_scaled_codec_temperatures_and_unknown_marker():
    assert P.TEMP_001C.encode(21.0) == h("3408")  # 0x0834 = 2100 -> 21.00 °C
    assert P.TEMP_001C.decode(h("3408")) == 21.0
    assert P.TEMP_001C.decode(h("6608")) == 21.5
    assert P.TEMP_001C.encode(-1.25) == (-125).to_bytes(2, "little", signed=True)
    assert P.TEMP_001C.decode(h("0080")) is None  # 0x8000 = unknown
    assert P.TEMP_001C.encode(None) == h("0080")
    ambient = P.SIG_PROPERTIES[0x004F].codec
    assert ambient.decode(b"\x2a") == 21.0  # sint8 x 0.5 °C
    assert ambient.decode(b"\x7f") is None
    power = P.Scaled(3, 0.1)
    assert (
        power.decode(h("a00100")) == 41.6
    )  # 41.6 W from the on-air capture, no float noise
    assert power.encode(41.6) == h("a00100")
    with pytest.raises(ValueError, match="a value is required"):
        power.encode(None)
    with pytest.raises(ValueError, match="need 3"):
        power.decode(h("a001"))


def test_percent_codec():
    assert P.PERCENT.encode(100) == b"\xff"
    assert P.PERCENT.encode(0) == b"\x00"
    assert P.PERCENT.encode(50) == b"\x80"
    assert P.PERCENT.decode(b"\x80") == 50
    assert P.PERCENT.decode(b"\xff") == 100
    with pytest.raises(ValueError, match=r"outside 0\.\.100"):
        P.PERCENT.encode(101)
    with pytest.raises(ValueError, match="need 1"):
        P.PERCENT.decode(b"")


def test_position_codec_follows_the_apps_wire_quirks():
    assert P.POSITION.encode(100) == b"\xfe"  # 100 % -> 254 (never 255)
    assert P.POSITION.encode(0) == b"\xff"  # 0 % -> raw 0 -> swapped to 255
    assert P.POSITION.encode(50) == b"\x80"
    assert P.POSITION.decode(b"\xfe") == 100
    assert P.POSITION.decode(b"\xff") == 0
    assert P.POSITION.decode(b"\x00") == 100  # raw 0 is swapped to 255 on read
    assert P.POSITION.decode(b"\x80") == 50
    for pct in range(101):
        assert P.POSITION.decode(P.POSITION.encode(pct)) == pct
    with pytest.raises(ValueError, match="need 1"):
        P.POSITION.decode(b"")


def test_duration_codec_ms_and_s():
    assert P.MS32.encode(1.5) == h("dc050000")
    assert P.MS32.decode(h("dc050000")) == 1.5
    assert P.MS32.decode(h("00000000")) == 0.0  # 0x1001 on node 0148: off
    assert P.MS32.encode(14400) == (14_400_000).to_bytes(4, "little")
    seconds = P.Duration(2, "s")
    assert seconds.encode(120) == h("7800")
    assert seconds.decode(h("7800")) == 120.0
    with pytest.raises(ValueError, match="negative"):
        seconds.encode(-1)
    with pytest.raises(ValueError, match="need 2"):
        seconds.decode(b"\x78")


def test_delay_codec_reads_as_the_app_shows():
    delay = P.PROPERTIES[0x1002].codec
    assert delay is P.PROPERTIES[0x1001].codec is P.DELAY_MS
    assert (
        delay.decode(h("ffffffff")) == 0.0
    )  # the factory 0x1002 on air: -1, off in the app
    assert delay.decode(h("00000000")) == 0.0
    assert delay.decode(h("dc050000")) == 1.5
    assert delay.decode((14_400_000).to_bytes(4, "little")) == 14400.0
    assert (
        delay.decode((14_400_001).to_bytes(4, "little")) == 14400.0
    )  # clamped to the picker's 4 h
    assert delay.decode((86_400_000).to_bytes(4, "little")) == 14400.0
    assert (
        delay.decode((86_400_001).to_bytes(4, "little")) == 0.0
    )  # past the app's range: off
    assert delay.encode(0) == h("00000000")  # writes are MS32's
    assert delay.encode(14400) == P.MS32.encode(14400)
    with pytest.raises(ValueError, match="need 4"):
        delay.decode(h("ffff"))
    assert P.PROPERTIES[0x1007].codec is P.MS32  # the run-on time is not read this way


def test_raw_and_text_codecs():
    assert P.RAW.encode(bytearray(b"\x01\x02")) == b"\x01\x02"
    assert P.RAW.decode(b"\x01") == b"\x01"
    with pytest.raises(ValueError, match="expected bytes"):
        P.RAW.encode(5)
    text = P.Text()
    assert text.decode(b"192.168.1.5\x00\x00") == "192.168.1.5"
    assert text.decode(b" sha256:ab \x00") == "sha256:ab"
    assert text.encode("JUNG") == b"JUNG"
    with pytest.raises(ValueError, match="expected a string"):
        text.encode(b"JUNG")


def test_version_codecs():
    assert P.ASCII_VERSION.decode(b"02020002") == "2.2.0.2"
    assert P.ASCII_VERSION.decode(b"01000303\x00") == "1.0.3.3"
    assert P.ASCII_VERSION.decode(b"0210") == "2.10"
    assert P.ASCII_VERSION.encode("2.2.0.2") == b"02020002"
    with pytest.raises(ValueError, match="not an ASCII version"):
        P.ASCII_VERSION.decode(b"2.2.0.2")
    with pytest.raises(ValueError, match="not an ASCII version"):
        P.ASCII_VERSION.decode(b"020")
    assert P.VERSION_LE.decode(h("0d020100")) == "0.1.2.13"  # secure element, on air
    assert P.VERSION_LE.decode(h("00000402")) == "2.4.0.0"  # bootloader, on air
    assert P.VERSION_LE.encode("2.4.0.0") == h("00000402")
    with pytest.raises(ValueError, match="need 1"):
        P.VERSION_LE.decode(b"")


def test_flags_codec_device_lock_bit_order():
    lock = P.PROPERTIES[0x0001].codec
    assert lock.encode({"local_devices_lock": True}) == h(
        "0400"
    )  # gateway: 4 = button lock
    assert lock.encode({"factory_reset_time_limit": True}) == h(
        "0200"
    )  # gateway: 2 = reset lock
    assert lock.encode({}) == h("0000")
    assert lock.encode(6) == h("0600")
    assert lock.decode(h("0600")) == {
        "local_factory_reset_lock": False,
        "factory_reset_time_limit": True,
        "local_devices_lock": True,
        "key_lock": False,
        "configuration_lock": False,
    }
    assert lock.decode(h("1800"))["key_lock"] is True
    assert lock.decode(h("1800"))["configuration_lock"] is True
    with pytest.raises(ValueError, match="unknown flag"):
        lock.encode({"turbo": True})
    with pytest.raises(ValueError, match="need 2"):
        lock.decode(b"\x04")
    status = P.PROPERTIES[0xC000].codec
    assert status.decode(b"\x03") == {
        "api_available": True,
        "client_waiting_for_approval": True,
    }


# ----------------------------------------------------------------------------- struct codecs


def test_rgb_mode_codec_led_mode_on_vector():
    red_night = P.LedMode(100, 0, 0, night_mode=True)
    assert P.encode(0xA001, red_night) == h("64000005")  # [r][g][b][mode 5 = night]
    assert P.decode(0xA001, h("64000005")) == red_night
    assert P.decode(0xA002, h("00003c00")) == P.LedMode(0, 0, 60)
    assert (
        P.decode(0xA001, h("00000001")).night_mode is False
    )  # only 5 means night mode
    assert red_night.rgb == (100, 0, 0)
    with pytest.raises(ValueError, match="expected LedMode"):
        P.RGB_MODE.encode((100, 0, 0, 5))
    with pytest.raises(ValueError, match=r"0\.\.100"):
        P.RGB_MODE.encode(P.LedMode(255, 0, 0))
    with pytest.raises(ValueError, match="need 4"):
        P.RGB_MODE.decode(h("640000"))


def test_enforced_output_codec_vectors():
    assert P.encode(0x0009, P.WIND_ALARM) == h("01ff00000000")  # the app's wind alarm
    assert P.encode(0x0009, P.lock_output(60)) == h(
        "02013c00"
    )  # lock current state for 60 s
    assert P.encode(0x0009, P.lock_output(30, lockout=True)) == h("02fe1e00")
    assert P.encode(0x0009, P.UNLOCK) == h("00010000")
    eight = h("02013c00aabbccdd")  # 8-byte form: command, priority, time, 4 value bytes
    decoded = P.decode(0x0009, eight)
    assert decoded == P.EnforcedOutput(2, 1, 60, h("aabbccdd"))
    assert P.encode(0x0009, decoded) == eight
    assert decoded.locked
    assert not decoded.wind_alarm
    assert P.decode(0x0009, h("01ff00000000")).wind_alarm is True
    assert P.decode(0x0009, h("00ff")) == P.EnforcedOutput(
        0, 255
    )  # no time -> no limit
    assert P.decode(0x0009, h("0001")).locked is False
    assert P.decode(0x0009, h("ff01")).locked is False  # unknown command
    assert P.decode(0x0009, h("020105")) == P.EnforcedOutput(
        2, 1, 0, b"\x05"
    )  # 3 bytes: no time, the third byte is the value (the app's parser; was dropped)
    assert P.decode(0x0009, h("02013c00")) == P.EnforcedOutput(2, 1, 60, b"")
    with pytest.raises(ValueError, match="expected EnforcedOutput"):
        P.encode(0x0009, b"\x01")
    with pytest.raises(ValueError, match="need 2"):
        P.decode(0x0009, b"\x01")


def test_threshold_codec():
    on = P.Threshold(41.6, 30, True)
    assert P.encode(0x5004, on) == h("8100") + h("1e00") + h("a00100") + b"\x01"
    assert P.decode(0x5004, h("81001e00a0010001")) == on
    none = P.Threshold(None, 0, False)
    assert P.encode(0x5005, none) == h("81000000ffffff00")
    assert P.decode(0x5005, h("81000000ffffff00")) == none
    with pytest.raises(ValueError, match="expected Threshold"):
        P.encode(0x5004, 41.6)
    with pytest.raises(ValueError, match="need 8"):
        P.decode(0x5004, h("8100"))


def test_scene_config_and_property_mode_codecs():
    assert P.encode(0x5002, P.SceneConfig(5)) == h("050000000000")
    assert P.decode(0x5002, h("0500e8030000")) == P.SceneConfig(5, 1000)
    with pytest.raises(ValueError, match="expected SceneConfig"):
        P.encode(0x5002, 5)
    with pytest.raises(ValueError, match="need 6"):
        P.decode(0x5002, h("0500"))
    assert P.encode(0x5006, P.PropertyMode(0x0009)) == h(
        "090001"
    )  # lock function, stateful
    assert P.decode(0x5006, h("0b1200")) == P.PropertyMode(0x120B, stateful=False)
    with pytest.raises(ValueError, match="expected PropertyMode"):
        P.encode(0x5006, 9)
    with pytest.raises(ValueError, match="need 3"):
        P.decode(0x5006, h("0900"))


def test_edge_detection_codec_bit_layout():
    value = P.EdgeDetection(edge_mode=True, rising="on", falling="off")
    assert P.encode(0x5009, value) == b"\x13"  # falling 2 << 3 | rising 1 << 1 | mode 1
    assert P.decode(0x5009, b"\x13") == value
    assert P.decode(0x5009, b"\x18") == P.EdgeDetection(False, "no_reaction", "toggle")
    for raw in range(32):
        assert P.encode(0x5009, P.decode(0x5009, bytes([raw]))) == bytes([raw])
    with pytest.raises(ValueError, match="expected EdgeDetection"):
        P.encode(0x5009, 1)
    with pytest.raises(ValueError, match="need 1"):
        P.decode(0x5009, b"")


def test_astro_register_codec_40_bit_field():
    reg = P.AstroRegister(3, "sunset", -15, (6, 30), (22, 45))
    assert P.encode(0x0007, reg) == h("23f1c6b32d")
    assert P.decode(0x0007, h("23f1c6b32d")) == reg
    plain = P.AstroRegister(0)
    assert P.decode(0x0007, P.encode(0x0007, plain)) == plain
    assert (
        P.decode(0x0007, h("f000000000")).mode == 15
    )  # unmapped 4-bit mode stays numeric
    with pytest.raises(ValueError, match="expected AstroRegister"):
        P.encode(0x0007, 3)
    with pytest.raises(ValueError, match="out of range"):
        P.encode(0x0007, P.AstroRegister(16))
    with pytest.raises(ValueError, match="bad time"):
        P.encode(0x0007, P.AstroRegister(1, latest=(24, 0)))
    with pytest.raises(ValueError, match="need 5"):
        P.decode(0x0007, h("23f1c6b3"))


def test_astro_status_codec():
    assert P.encode(0x0008, P.AstroStatus(0x0005, 0x0001)) == h("05000100")
    assert P.decode(0x0008, h("05000100")) == P.AstroStatus(5, 1)
    with pytest.raises(ValueError, match="expected AstroStatus"):
        P.encode(0x0008, 5)
    with pytest.raises(ValueError, match="need 4"):
        P.decode(0x0008, h("0500"))


def test_insert_id_codec_field_order_from_the_air():
    assert P.decode(0x0002, h("00000200")) == P.InsertId("switch", "generic_insert")
    assert P.decode(0x0002, h("0400")) == P.InsertId("tw_dimming")
    assert P.decode(0x0002, h("ffff0100")) == P.InsertId("unset", "no_insert")
    assert P.decode(0x0002, h("2a000900")) == P.InsertId(42, 9)
    assert P.encode(0x0002, P.InsertId("blind", "generic_insert")) == h("05000200")
    with pytest.raises(ValueError, match="expected InsertId"):
        P.encode(0x0002, "switch")
    with pytest.raises(ValueError, match="need 2"):
        P.decode(0x0002, b"\x00")


def test_key_event_codec():
    assert P.decode(0x5012, h("0305")) == P.KeyEvent(3, "pushed")
    assert P.decode(0x5012, h("1006")) == P.KeyEvent(16, "held")
    assert P.decode(0x5012, h("0009")) == P.KeyEvent(0, 9)
    assert P.encode(0x5012, P.KeyEvent(3, "released")) == h("0304")
    with pytest.raises(ValueError, match="expected KeyEvent"):
        P.encode(0x5012, 5)
    with pytest.raises(ValueError, match="need 2"):
        P.decode(0x5012, b"\x03")


def test_energy_chart_codec():
    assert P.decode(0x5010, h("0064ffff01f4")) == [
        10.0,
        None,
        50.0,
    ]  # big-endian u16 x 0.1
    assert P.decode(0x5011, h("000064ffffff0001f4ab")) == [
        10.0,
        None,
        50.0,
    ]  # u24, partial tail ignored
    assert P.decode(0x5010, b"") == []
    with pytest.raises(ValueError, match="read-only"):
        P.encode(0x5010, [1.0])


def test_energy_charts_at_full_length():
    """What the app asks for: 24 hourly u16 samples (0x5010) and 31 daily u24 ones (0x5011), newest first, kept in
    that order; the largest hourly sample (6553.4 Wh) is above what a 16 A socket passes in an hour."""
    hourly = bytes.fromhex("fffe") + bytes.fromhex("0001") * 22 + bytes.fromhex("ffff")
    assert P.decode(0x5010, hourly) == [6553.4, *[0.1] * 22, None]
    daily = bytes.fromhex("0186a0") + bytes(3 * 30)
    assert P.decode(0x5011, daily) == [10000.0, *[0.0] * 30]
    assert P.PROPERTIES[0x5010].unit == P.PROPERTIES[0x5011].unit == "Wh"


# ----------------------------------------------------------------------------- LED helpers


def test_led_palettes_and_ids():
    assert P.led_palette(0x03)["white"] == (40, 58, 42)
    assert P.led_palette(0x0C) is P.led_palette(0x03)
    assert P.led_palette(0x01)["red"] == (100, 0, 0)
    assert P.led_palette(0x02)["red"] == (75, 0, 0)
    assert P.led_palette(0x05)["blue"] == (0, 14, 100)
    assert P.led_palette(0x06)["red"] == (80, 0, 0)
    for pid, palette in P.LED_PALETTES.items():
        assert tuple(palette) == P.LED_COLOURS, pid
        assert palette["no_color"] == (0, 0, 0)
    assert P.led_colour_name((0, 60, 0), 0x03) == "green"
    assert P.led_colour_name((1, 2, 3), 0x03) is None
    with pytest.raises(KeyError):
        P.led_palette(0x0A)
    assert P.led_property_id(1, "on") == 0xA001
    assert P.led_property_id(1, "off") == 0xA002
    assert P.led_property_id(2, "channel") == 0xA003
    assert P.led_property_id(2, "on") == 0xA004
    assert P.led_property_id(16, "off") == 0xA02F
    with pytest.raises(ValueError, match=r"outside 1\.\.16"):
        P.led_property_id(17, "on")


# ----------------------------------------------------------------------------- the catalogue


VENDOR_ID_RANGES = (
    (0x0000, 0x001F),
    (0x0F00, 0x0F02),
    (0x1000, 0x13FF),
    (0x5000, 0x50FF),
    (0x6000, 0x60FF),
    (0xA000, 0xA2FF),
    (0xC000, 0xC0FF),
)


def test_catalogue_consistency():
    specs = [*P.PROPERTIES.values(), *P.SIG_PROPERTIES.values()]
    names = [s.name for s in specs]
    assert len(set(names)) == len(names), (
        "names must be unique across vendor and SIG tables"
    )
    for spec in specs:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", spec.name), spec.name
        assert isinstance(spec.codec, P.Codec), spec.name
        assert spec.products <= P.ALL_PRODUCTS, spec.name
        assert spec.set_access in (1, 3), spec.name
        if spec.min is not None or spec.max is not None:
            assert spec.min is not None, spec.name
            assert spec.max is not None, spec.name
            assert spec.min <= spec.max, spec.name
            assert spec.step is not None, spec.name
            assert spec.unit is not None, spec.name
        if spec.firmware_min is not None:
            assert len(spec.firmware_min) == 4, spec.name
        if spec.access == "wo":
            assert spec.name == "server_state_publish_request"
        if isinstance(spec.codec, P.Enum):
            for raw, name in spec.codec.values.items():
                assert spec.codec.decode(spec.codec.encode(name)) == name, (
                    spec.name,
                    name,
                )
                assert spec.codec.encode(raw) == spec.codec.encode(name)
    for pid, spec in P.PROPERTIES.items():
        assert pid == spec.id
        assert spec.vendor, spec.name
        assert spec.server in ("admin", "manufacturer", "user"), spec.name
        assert any(lo <= pid <= hi for lo, hi in VENDOR_ID_RANGES), (
            f"0x{pid:04X} outside the documented ranges"
        )
    for pid, spec in P.SIG_PROPERTIES.items():
        assert pid == spec.id
        assert not spec.vendor, spec.name
        assert spec.server in ("sig_admin", "sig_manufacturer", "sensor"), spec.name
        assert pid < 0x0100
    assert len(P.PROPERTIES) > 150
    assert len(P.SIG_PROPERTIES) == 14


def test_catalogue_covers_every_id_the_app_reads_or_writes():
    app_ids = {
        0x0001, 0x0002, 0x0003, 0x0004, 0x0005, 0x0007, 0x0008, 0x0009, 0x000F, 0x0012, 0x0013,
        0x1001, 0x1002, 0x1007, 0x100A, 0x100B, 0x100C, 0x100D, 0x100E, 0x1014,
        0x1101, 0x1102, 0x1103, 0x1104, 0x1105, 0x1106, 0x1107, 0x1108, 0x110A, 0x110B, 0x110D,
        0x1201, 0x1203, 0x1204, 0x1205, 0x1208, 0x120A, 0x120B, 0x120D, 0x1221, 0x1224, 0x1240, 0x1246, 0x1247, 0x1249,
        0x5001, 0x5002, 0x5003, 0x5004, 0x5005, 0x5006, 0x5007, 0x5008, 0x5009, 0x5010, 0x5011,
        0x6001, 0x6003, 0x6004, 0x6005, 0x6006, 0x6008, 0x6009, 0x600A, 0x600F, 0x6015, 0x6016, 0x6017, 0x6021,
        0xA001, 0xA002, 0xA004, 0xA005, 0xC001, 0xC002, 0xC003,
    }  # fmt: skip
    for pid in app_ids:
        assert P.PROPERTIES[pid].source == "app", f"0x{pid:04X}"
    for pid in (0x0010, 0x0011, 0x001A, 0x006A, 0x006D, 0x004F, 0x0081):
        assert P.SIG_PROPERTIES[pid].source == "app", f"0x{pid:04X}"
    # the app's TODO() ids in the firmware's reserved RTR block are deliberately absent
    for pid in (0x120F, 0x1212, 0x1218, 0x121C, 0x121D, 0x121F):
        assert pid not in P.PROPERTIES
    # firmware-only ids are tagged so the entity layer can skip them
    for pid in (0x000E, 0x1206, 0x500C, 0x6018, 0xA000, 0xA100, 0xC000):
        assert P.PROPERTIES[pid].source == "firmware", f"0x{pid:04X}"


def test_firmware_ids_waiting_for_the_probe_stay_raw():
    """Review-4 brief 36: no codec before a supervised probe settles an id; the runtime statistics are where the
    devices list them, on the Manufacturer server (`docs/hidden-features.md` §2)."""
    for pid in (0x0F00, 0x0F01, 0x0F02, 0x1008, 0x1009, 0x1011, 0x1012, 0x1013, 0x500C):
        spec = P.PROPERTIES[pid]
        assert spec.source == "firmware", f"0x{pid:04X}"
        assert spec.codec is P.RAW, f"0x{pid:04X}"
        assert P.decode(pid, b"\x01\x00") == b"\x01\x00"
    assert P.PROPERTIES[0x0F00].server == "admin"
    assert P.PROPERTIES[0x0F01].server == "manufacturer"
    assert P.PROPERTIES[0x0F02].server == "manufacturer"


def test_led_specs_cover_16_triples():
    led_specs = [s for s in P.PROPERTIES.values() if s.element == "led"]
    assert len(led_specs) == 48
    assert P.PROPERTIES[0xA000].name == "led1_channel_selection"
    assert P.PROPERTIES[0xA001].name == "led1_mode_on"
    assert P.PROPERTIES[0xA005].name == "led2_mode_off"
    assert P.PROPERTIES[0xA02F].name == "led16_mode_off"
    assert P.PROPERTIES[0xA001].products == P.LED_HOSTS
    assert P.PROPERTIES[0xA004].products == P.TWO_GANG
    assert P.PROPERTIES[0xA007].products == frozenset()
    assert P.PROPERTIES[0xA010].source == "app"  # LED 6 on mode, the app's last
    assert P.PROPERTIES[0xA013].source == "firmware"  # LED 7
    assert P.PROPERTIES[0xA000].source == "firmware"
    assert all(isinstance(s.codec, P.RgbMode) for s in led_specs if "mode" in s.name)


def test_spec_fields_and_lookups():
    key_mode = P.spec_for(0x5003)
    assert (key_mode.name, key_mode.server, key_mode.access, key_mode.element) == (
        "key_mode",
        "admin",
        "rw",
        "key",
    )
    assert key_mode.readable
    assert key_mode.writable
    assert key_mode.vendor
    assert P.by_name("key_mode") is key_mode
    assert P.by_name("total_energy") is P.spec_for(0x006A, sig=True)
    assert P.spec_for(0x0010).name == "lpn_state_timeout"  # vendor id
    assert (
        P.spec_for(0x0010, sig=True).name == "hardware_revision"
    )  # SIG id, different property
    assert P.spec_for(0x0013).set_access == 1  # DimMode is set with access READ
    assert P.spec_for(0x0001).set_access == 1  # on air, see the test below
    assert P.spec_for(0x000F).set_access == 3
    assert P.spec_for(0x120B).firmware_min == (2, 2, 0, 0)
    assert P.spec_for(0x000F).firmware_min == (1, 1, 0, 0)
    assert P.spec_for(0x0012).firmware_min == (1, 0, 3, 3)
    assert P.spec_for(0x1014).firmware_min == (2, 0, 0, 5)
    assert P.spec_for(0x1203).step == 0.5
    assert P.spec_for(0x1203).unit == "°C"
    assert (
        P.spec_for(0x1001).min,
        P.spec_for(0x1001).max,
        P.spec_for(0x1001).unit,
    ) == (0, 14400, "s")
    assert P.spec_for(0x100D).unit == "ms"
    assert P.spec_for(0x5010).readable
    assert not P.spec_for(0x5010).writable
    assert not P.spec_for(0x000E).readable
    ro = P.spec_for(0x6004)
    assert (ro.server, ro.access, ro.element, ro.unit) == (
        "manufacturer",
        "ro",
        "detector",
        "lx",
    )
    with pytest.raises(KeyError):
        P.spec_for(0x7777)
    with pytest.raises(KeyError):
        P.by_name("warp_drive")


def test_supported_tolerates_an_unparseable_version():
    """MOD-01: `AsciiVersion` decodes any even number of digit pairs, `parse_version` reads at most four; a revision
    that cannot be read is "unknown", which counts as supported, instead of raising out of entity setup."""
    spec = P.PROPERTIES[0x120B]
    assert P.supported(spec, P.ASCII_VERSION.decode(b"0202000201")) is True
    assert P.supported(spec, "2.2.0.2.1") is True
    assert P.supported(spec, "x.y") is True
    assert P.supported(spec, "2.1.9.9") is False  # unchanged


def test_version_parsing_and_support():
    assert P.parse_version("2.2.0.2") == (2, 2, 0, 2)
    assert P.parse_version("2.2") == (2, 2, 0, 0)
    with pytest.raises(ValueError, match="not a version"):
        P.parse_version("1.2.3.4.5")
    hvac = P.spec_for(0x120B)
    assert P.supported(hvac, None)
    assert P.supported(hvac, "2.2.0.2")
    assert P.supported(hvac, (2, 2, 0, 0))
    assert not P.supported(hvac, "2.1.9.9")
    assert P.supported(P.spec_for(0x5003), "1.0.0.0")  # no minimum
    assert not P.supported(P.spec_for(0x1014), "2.0.0.4")


def test_for_product_push_button_2_gang_vs_socket():
    two_gang = {s.name for s in P.for_product(0x02)}
    socket = {s.name for s in P.for_product(0x03)}
    # keys and LEDs on the push-button, thresholds / energy on the metering socket
    assert {
        "key_mode",
        "key_scene_config",
        "button_layout",
        "led1_mode_on",
        "led2_mode_on",
        "dim_mode",
        "running_time",
    } <= two_gang
    assert {
        "turn_on_threshold",
        "daily_energy_chart",
        "total_energy",
        "power_on_time",
        "active_power",
        "led1_mode_on",
    } <= socket
    assert "led2_mode_on" not in socket
    assert "key_mode" not in socket
    assert "turn_on_threshold" not in two_gang
    assert "prewarning" in two_gang
    assert "prewarning" not in socket
    assert "on_delay" in socket
    assert "device_lock" in socket
    # shared identification
    assert {"insert_id", "software_version", "manufacturer_name"} <= two_gang & socket
    # nothing firmware-only unless asked for
    assert "key_event" not in two_gang
    assert "key_event" in {
        s.name for s in P.for_product(0x02, include_firmware_only=True)
    }
    # order: vendor ids ascending, then SIG ids
    ids = [(s.vendor, s.id) for s in P.for_product(0x03)]
    assert ids == sorted(ids, key=lambda t: (not t[0], t[1]))
    # products that host nothing of a kind
    assert "key_mode" not in {s.name for s in P.for_product(0x0A)}
    assert {"comfort_temperature", "hvac_mode", "stm32_version"} <= {
        s.name for s in P.for_product(0x0A)
    }
    assert {"gateway_ip", "gateway_api_token"} <= {s.name for s in P.for_product(0x0B)}
    assert {"pir_sensor_a", "current_brightness", "timed_on_duration"} <= {
        s.name for s in P.for_product(0x09)
    }
    assert {"input_edge_detection", "running_time"} <= {
        s.name for s in P.for_product(0x0D)
    }


def test_encode_decode_helpers_and_key_mode_vector():
    assert P.encode(0x5003, "gateway") == b"\x06"  # KeyMode 6 as polled by the gateway
    assert P.decode(0x5003, b"\x06") == "gateway"
    assert P.decode(0x5003, b"\x05") == "switch"  # node 0149 on air
    assert P.encode(0x1203, 21) == h("3408")
    assert P.decode(0x1001, h("00000000")) == 0.0
    assert P.decode(0x0003, h("0d020100")) == "0.1.2.13"
    assert P.decode(0x001A, b"02020002", sig=True) == "2.2.0.2"
    assert P.encode(0x006A, 0, sig=True) == h("00000000")
    with pytest.raises(KeyError):
        P.encode(0x7777, 1)
    with pytest.raises(KeyError):
        P.decode(0x001A, b"02020002")  # SIG id, not a vendor property


def test_duplicate_ids_are_rejected_when_indexing():
    spec = P.spec_for(0x5003)
    with pytest.raises(ValueError, match="duplicate property id 0x5003"):
        P._index([spec, spec])


# ----------------------------------------------------------------------------- describe


@pytest.mark.parametrize(
    ("pid", "data", "sig", "expected"),
    [
        (0x5003, b"\x06", False, "key_mode=gateway"),
        (0x5003, b"\x09", False, "key_mode=9"),
        (0x1203, h("3408"), False, "comfort_temperature=21°C"),
        (0x1203, h("0080"), False, "comfort_temperature=unknown"),
        (0x1001, h("dc050000"), False, "on_delay=1.5s"),
        (0x100D, h("f401"), False, "switch_blocking_time=500ms"),
        (0x6008, b"\x80", False, "pir_sensor_a=50%"),
        (0x000F, b"\x01", False, "automatic_dst=on"),
        (0x120D, b"\x00", False, "boost_mode=off"),
        (
            0x0001,
            h("0600"),
            False,
            "device_lock=factory_reset_time_limit,local_devices_lock",
        ),
        (0x0001, h("0000"), False, "device_lock=none"),
        (0xA001, h("64000005"), False, "led1_mode_on=rgb(100,0,0) night_mode=on"),
        (0xA002, h("00003c00"), False, "led1_mode_off=rgb(0,0,60) night_mode=off"),
        (0x5010, h("0064ffff"), False, "daily_energy_chart=[10,-]"),
        (0x5007, h("0201"), False, "key_property_value_up=0201"),
        (0x5007, b"", False, "key_property_value_up=(empty)"),
        (0x0003, h("0d020100"), False, "secure_element_version=0.1.2.13"),
        (
            0x0002,
            h("00000200"),
            False,
            "insert_id=InsertId(function='switch', insert_type='generic_insert')",
        ),
        (0x5012, h("0305"), False, "key_event=KeyEvent(counter=3, event='pushed')"),
        (0xC001, b"very-secret-token", False, "gateway_api_token=<redacted>"),
        (0xC002, b"192.168.1.5\x00", False, "gateway_ip=192.168.1.5"),
        (0xC000, b"\x01", False, "gateway_api_status=api_available"),
        (0x001A, b"02020002", True, "software_version=2.2.0.2"),
        (0x006D, h("102700"), True, "power_on_time=10000h"),
        (0x0081, h("a00100"), True, "active_power=41.6W"),
        (0x0011, b"JUNG\x00", True, "manufacturer_name=JUNG"),
        # the vectors of the app's settings session (on air): 16 and 36 bytes, NUL-padded
        (0x0010, b"10000000" + bytes(8), True, "hardware_revision=10000000"),
        (
            0x0011,
            b"Albrecht Jung GmbH & Co.KG" + bytes(10),
            True,
            "manufacturer_name=Albrecht Jung GmbH & Co.KG",
        ),
        (
            0x5003,
            b"",
            False,
            "key_mode=?",
        ),  # empty value: a load element answering a key property
        (0x1203, b"\x34", False, "comfort_temperature=?34"),
        (0x7777, h("abcd"), False, "0x7777=abcd"),
        (0x7777, b"", False, "0x7777=(empty)"),
    ],
)
def test_describe_status(pid: int, data: bytes, sig: bool, expected: str):
    assert P.describe_status(pid, data, sig=sig) == expected


def test_describe_status_never_leaks_the_gateway_token():
    token = b"very-secret-token"
    assert token.decode() not in P.describe_status(0xC001, token)
    assert token.hex() not in P.describe_status(0xC001, token)
    assert P.describe_status(0xC001, token) == "gateway_api_token=<redacted>"
    assert P.describe_status(0xC001, b"") == "gateway_api_token=<redacted>"


def test_secret_specs_are_redacted_even_when_their_codec_cannot_decode(
    monkeypatch: pytest.MonkeyPatch,
):
    """The `name=?<hex>` fallback for an undecodable value must not apply to a secret: the hex *is* the token."""
    spec = P.spec_for(0xC001)
    assert spec.secret
    assert not P.spec_for(0xC002).secret  # the gateway IP is a plain Text
    assert not P.spec_for(0x5003).secret

    def refuse(data: bytes) -> str:
        raise ValueError("malformed")

    monkeypatch.setattr(type(spec.codec), "decode", staticmethod(refuse))
    token = b"very-secret-token"
    assert P.describe_status(0xC001, token) == "gateway_api_token=<redacted>"
    assert P.describe_status(0xC002, b"10.0.0.1") == "gateway_ip=?31302e302e302e31"
    assert P.format_value(spec, "anything") == P.REDACTED == "<redacted>"
    assert [s.name for s in P.PROPERTIES.values() if s.secret] == ["gateway_api_token"]
    assert not any(s.secret for s in P.SIG_PROPERTIES.values())


def test_format_value_for_every_kind():
    kinds: dict[str, Any] = {}
    for spec in [*P.PROPERTIES.values(), *P.SIG_PROPERTIES.values()]:
        kinds.setdefault(spec.codec.kind, spec)
    assert set(kinds) == {
        "int",
        "bool",
        "enum",
        "float",
        "percent",
        "duration",
        "raw",
        "string",
        "version",
        "flags",
        "rgb_mode",
        "struct",
        "list",
        "date",
        "datetime",
    }
    assert P.format_value(P.spec_for(0x0009), P.WIND_ALARM).startswith(
        "EnforcedOutput("
    )
    assert P.format_value(P.spec_for(0x6021), 5) == "5"  # int without a unit


# ----------------------------------------------------------------------------- SIG identity / energy block (read off the devices)


def test_date_utc_codec():
    codec = P.SIG_PROPERTIES[0x000C].codec
    assert isinstance(codec, P.DateUTC)
    assert (
        codec.decode(bytes.fromhex("394b00")) == "2022-09-22"
    )  # a metering socket's date of manufacture on air
    assert codec.encode("2022-09-22") == bytes.fromhex("394b00")
    assert codec.decode(bytes(3)) is None
    assert codec.encode(None) == bytes(3)
    with pytest.raises(ValueError, match="need 3"):
        codec.decode(b"\x01")
    with pytest.raises(ValueError, match="ISO date"):
        codec.encode(20220922)
    with pytest.raises(ValueError, match="outside"):
        codec.encode("1969-12-31")
    assert (
        P.describe_status(0x000C, bytes.fromhex("394b00"), sig=True)
        == "date_of_manufacture=2022-09-22"
    )
    assert (
        P.describe_status(0x000C, bytes(3), sig=True) == "date_of_manufacture=unknown"
    )
    # past the year 9999 `date + timedelta` raises OverflowError, which nothing downstream catches: ValueError
    with pytest.raises(
        ValueError, match=r"not a date: ffffff \(16777215 days since 1970\)"
    ):
        codec.decode(bytes.fromhex("ffffff"))
    with pytest.raises(ValueError, match="not a date"):
        codec.decode(
            (2_932_897).to_bytes(3, "little")
        )  # the first day count past 9999-12-31
    assert codec.decode((2_932_896).to_bytes(3, "little")) == "9999-12-31"
    assert (
        P.describe_status(0x000C, bytes.fromhex("ffffff"), sig=True)
        == "date_of_manufacture=?ffffff"
    )


def test_timestamp7_codec_reads_the_meter_timestamps_seen_on_air():
    spec = P.spec_for(0x5014)
    assert spec.name == "meter_timestamp"
    assert spec.element == "meter"
    assert spec.source == "firmware"
    codec = spec.codec
    assert isinstance(codec, P.Timestamp7)
    assert (
        codec.decode(h("7c000a030e2e2e")) == "2024-10-03T14:46:46"
    )  # socket 0172's meter
    assert (
        codec.decode(h("7c000a0c16280f")) == "2024-10-12T22:40:15"
    )  # socket 0174's meter
    assert codec.encode("2024-10-03T14:46:46") == h("7c000a030e2e2e")
    assert (
        P.describe_status(0x5014, h("7c000a030e2e2e"))
        == "meter_timestamp=2024-10-03T14:46:46"
    )
    assert P.format_value(spec, None) == "unknown"
    with pytest.raises(ValueError, match="need 7"):
        codec.decode(h("7c000a"))
    with pytest.raises(ValueError, match="not a timestamp"):
        codec.decode(h("7c000d030e2e2e"))  # month 13
    with pytest.raises(ValueError, match="ISO timestamp"):
        codec.encode(20241003)
    with pytest.raises(ValueError, match="outside"):
        codec.encode("1899-12-31T00:00:00")
    # the publish-request trigger keeps its write-only access and now carries a byte
    trigger = P.spec_for(0x000E)
    assert trigger.access == "wo"
    assert trigger.codec.encode(1) == b"\x01"


def test_sig_energy_properties_live_on_the_meter_element():
    for pid in (0x006A, 0x0072, 0x000D):
        assert P.SIG_PROPERTIES[pid].element == "meter"
    assert (
        P.SIG_PROPERTIES[0x006A].server == "sig_admin"
    )  # resettable counter (the app's "reset consumption")
    assert (
        P.SIG_PROPERTIES[0x0072].server == "sig_manufacturer"
    )  # lifetime counter, read-only
    assert P.SIG_PROPERTIES[0x006D].element == "node"
    assert (
        P.describe_status(0x0052, bytes.fromhex("250000"), sig=True)
        == "present_input_power=3.7W"
    )
    assert (
        P.describe_status(0x0057, bytes.fromhex("0500"), sig=True)
        == "present_input_current=0.05A"
    )


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: P.AstroRegisterCodec().encode(P.AstroRegister(0, 16)),
            id="astro-mode",
        ),
        pytest.param(
            lambda: P.EdgeDetectionCodec().encode(P.EdgeDetection(True, 4)),
            id="edge-rising",
        ),
        pytest.param(
            lambda: P.EdgeDetectionCodec().encode(P.EdgeDetection(True, 0, 255)),
            id="edge-falling",
        ),
        pytest.param(lambda: P.ASCII_VERSION.encode("2.100.0.1"), id="version-part"),
    ],
)
def test_struct_encoders_reject_out_of_range_fields(build):
    """MOD-07: a value too wide for its bit field is refused, not shifted into the neighbouring field; a version
    part over 99 would not decode again."""
    with pytest.raises(ValueError, match=r"mode|behaviour|version"):
        build()


def test_scaled_decode_keeps_the_precision_of_a_small_scale():
    """MOD-07: `str(0.00001)` is "1e-05": the rounding precision comes from the scale's exponent instead."""
    assert P.Scaled(2, 0.00001).decode(b"\x05\x00") == 0.00005
    assert P.Scaled(2, 0.01).decode(b"\x05\x00") == 0.05
    assert P.Scaled(2, 0.5).decode(b"\x05\x00") == 2.5
    assert P.Scaled(2, 1.0).decode(b"\x05\x00") == 5
    assert P.Scaled(2, 10).decode(b"\x05\x00") == 50


def test_every_spec_is_hashable():
    """MOD-08: a frozen value object must hash; an Enum codec's table no longer takes part in the hash."""
    for spec in [*P.PROPERTIES.values(), *P.SIG_PROPERTIES.values()]:
        hash(spec)
    assert len({*P.PROPERTIES.values()}) == len(P.PROPERTIES)


# Every kind of LBC Admin Property Set the JUNG HOME app sent in the settings session (nRF capture,
# decoded with the installation's keys): (property, the value as this catalogue takes it, the parameters on air
# after the opcode `C3 27 05`). The third byte is the userAccess the node then keeps and reports in its Status.
APP_ADMIN_SETS: list[tuple[int, Any, str]] = [
    (0x0001, {"local_devices_lock": True}, "0100 01 0400"),  # Lock operation
    (0x0001, {}, "0100 01 0000"),
    (0x0001, {"factory_reset_time_limit": True}, "0100 01 0200"),  # Lock factory reset
    (0x1001, 60, "0110 01 60ea0000"),  # switch-on delay 1 min
    (0x1002, 60, "0210 01 60ea0000"),  # switch-off delay 1 min
    (0x1007, 60, "0710 01 60ea0000"),  # run-on time 1 min
    (0x1007, 0, "0710 01 00000000"),
    (0x000F, False, "0f00 03 00"),  # automatic DST
    (0x100B, True, "0b10 03 01"),  # manual switching off during the run-on time
    (0x100D, 1001, "0d10 03 e903"),  # minimum switching repetition time
    (0xA001, P.LedMode(0, 60, 0), "01a0 03 003c0000"),  # socket LED on: green
    (0xA002, P.LedMode(100, 0, 0, night_mode=True), "02a0 03 64000005"),
    (0xA004, P.LedMode(80, 66, 32), "04a0 03 50422000"),  # 2-gang right LED on: white
    (0x5003, 5, "0350 03 05"),
    (0x5004, P.Threshold(20.0, 5, True), "0450 03 81000500c8000001"),
    (0x5004, P.Threshold(None, 0, False), "0450 03 81000000ffffff00"),
]


@pytest.mark.parametrize(("pid", "value", "on_air"), APP_ADMIN_SETS)
def test_admin_set_matches_the_app_on_air(pid: int, value: Any, on_air: str) -> None:
    """The Set this catalogue builds is the app's byte for byte, the userAccess byte included.

    The app sets DeviceLock and the three delays with userAccess 1 (READ), the rest with 3. The node keeps the byte:
    a DeviceLock an earlier writer had set with 3 reported access=3 until the app's Set, access=1 after it. A Set
    with 3 would leave these writable through the User Property server where the app leaves them read-only.
    """
    spec = P.PROPERTIES[pid]
    pdu = M.vendor_property_set(
        "admin", pid, spec.codec.encode(value), user_access=spec.set_access
    )
    assert pdu == bytes.fromhex("c32705 " + on_air)
