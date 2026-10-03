"""jhmesh.messages: builders produce the expected wire bytes, describe() decodes every known message."""

from __future__ import annotations

import importlib.util
import math
import secrets
import sys
from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jhmesh import messages as M
from jhmesh import properties
from jhmesh import vendor_models as V

h = bytes.fromhex


# ----------------------------------------------------------------------------- TID handling


def test_next_tid_increments_and_wraps(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(M, "_tid", 0xFE)
    assert M.next_tid() == 0xFF
    assert M.next_tid() == 0x00
    assert M.next_tid() == 0x01


def test_tid_counter_is_seeded_randomly_per_process(monkeypatch: pytest.MonkeyPatch):
    # Two CLI runs within 6 s must not both start at TID 1, or the node drops the second run's Set as a
    # retransmission of the first; a fresh copy of the module with a pinned random source shows the seed is used.
    monkeypatch.setattr(secrets, "randbelow", lambda n: 0x41 if n == 256 else 0)
    spec = importlib.util.spec_from_file_location("jhmesh._tid_probe", M.__file__)
    assert spec is not None
    assert spec.loader is not None
    fresh = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, fresh)
    spec.loader.exec_module(fresh)
    assert fresh.next_tid() == 0x42


def test_builders_use_fresh_tid_when_none_given(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(M, "_tid", 10)
    assert M.generic_onoff_set(True) == h("820201") + bytes([11])
    assert M.light_lightness_set(1) == h("824c0100") + bytes([12])
    assert M.light_ctl_set(1, 2700) == h("825e0100") + (2700).to_bytes(2, "little") + h(
        "00000d"
    )  # (was temperature 2 K, which §6.1.3.1 prohibits)
    assert M.scene_recall(1) == h("824201000e")


# ----------------------------------------------------------------------------- builders


def test_generic_onoff_builders():
    assert M.generic_onoff_get() == h("8201")
    assert M.generic_onoff_set(True, tid=5) == h("82020105")
    assert M.generic_onoff_set(False, tid=5) == h("82020005")
    assert M.generic_onoff_set(True, ack=False, tid=0x22) == h("82030122")
    assert M.generic_onoff_set(True, tid=1, transition=0x42) == h("820201014200")
    assert M.generic_onoff_set(False, ack=False, tid=1, transition=0x42, delay=4) == h(
        "820300014204"
    )


def test_light_lightness_builders():
    assert M.light_lightness_get() == h("824b")
    assert M.light_lightness_set(0x1234, tid=9) == h("824c341209")
    assert M.light_lightness_set(0xFFFF, ack=False, tid=0) == h("824dffff00")


def test_light_ctl_builders():
    assert M.light_ctl_get() == h("825d")
    assert (
        M.light_ctl_set(1000, 2700, tid=3)
        == h("825e")
        + (1000).to_bytes(2, "little")
        + (2700).to_bytes(2, "little")
        + h("0000")
        + b"\x03"
    )
    assert (
        M.light_ctl_set(1, 6500, delta_uv=-1, ack=False, tid=3)
        == h("825f") + h("0100") + (6500).to_bytes(2, "little") + h("ffff") + b"\x03"
    )
    assert M.light_ctl_set(1, 800, tid=3, transition=0x0A, delay=2) == h(
        "825e010020030000030a02"
    )  # (was temperature 2 K, which §6.1.3.1 prohibits)


def test_light_ctl_temperature_builders():
    """§6.3.2.4-6.3.2.6: the temperature and Delta UV without a lightness, then the usual TID and transition tail."""
    assert M.light_ctl_temperature_get() == h("8261")
    assert M.light_ctl_temperature_set(2700, tid=3) == h(
        "8264" + "8c0a" + "0000" + "03"
    )
    assert M.light_ctl_temperature_set(6500, delta_uv=-1, ack=False, tid=4) == h(
        "8265" + "6419" + "ffff" + "04"
    )
    assert M.light_ctl_temperature_set(800, tid=5, transition=0x0A, delay=2) == h(
        "8264" + "2003" + "0000" + "050a02"
    )


@pytest.mark.parametrize(
    ("build", "match"),
    [
        (
            lambda: M.light_ctl_temperature_set(799),
            "colour temperature 799 K is outside 800..20000",
        ),
        (
            lambda: M.light_ctl_temperature_set(2700, delta_uv=-0x8001),
            "delta UV -32769 is not -32768..32767",
        ),
        (
            lambda: M.light_ctl_temperature_set(2700, transition=0x3F),
            "steps 0x3F .unknown. is prohibited in a Set",
        ),
        (
            lambda: M.light_ctl_set(1, 100),
            "colour temperature 100 K is outside 800..20000",
        ),
        (lambda: M.light_ctl_set(1, 20001), "colour temperature 20001 K"),
        (lambda: M.light_ctl_set(0x10000, 2700), "lightness 65536 is not 0..65535"),
        (
            lambda: M.light_ctl_set(1, 2700, delta_uv=0x8000),
            "delta UV 32768 is not -32768..32767",
        ),
        (lambda: M.light_lightness_set(0x10000), "lightness 65536 is not 0..65535"),
        (lambda: M.light_lightness_set(-1), "lightness -1 is not 0..65535"),
        (lambda: M.light_lightness_set(1, tid=300), "tid 300 is not 0..255"),
        (lambda: M.generic_onoff_set(True, tid=-1), "tid -1 is not 0..255"),
        (
            lambda: M.generic_onoff_set(True, transition=0x3F),
            "steps 0x3F .unknown. is prohibited in a Set",
        ),
        (
            lambda: M.generic_onoff_set(True, transition=0xFF),
            "steps 0x3F .unknown. is prohibited in a Set",
        ),
        (
            lambda: M.generic_onoff_set(True, transition=0x100),
            "transition time 256 is not 0..255",
        ),
        (
            lambda: M.generic_onoff_set(True, transition=0x42, delay=300),
            "delay 300 is not 0..255",
        ),
        (lambda: M.scene_recall(0), "scene 0 is not 1..65535"),
        (lambda: M.scene_recall(0x10000), "scene 65536 is not 1..65535"),
        (lambda: M.generic_level_set(32768), "level 32768 is not -32768..32767"),
        (
            lambda: M.generic_delta_set(1 << 31),
            "delta 2147483648 is not -2147483648..2147483647",
        ),
        (lambda: M.generic_move_set(1 << 15), "delta 32768 is not -32768..32767"),
        (lambda: M.generic_move_set(1, transition=0x7F), "prohibited in a Set"),
    ],
)
def test_set_builders_refuse_out_of_range_fields_with_value_error(build, match: str):
    """Out-of-range input raised OverflowError (or a bare "bytes must be in range") from `int.to_bytes`, which the
    services / config entities do not catch and which named no field; prohibited values the spec says a node
    ignores (§3.1.3 transition 0x3F, §5.1.3.1 scene 0, §6.1.3.1 temperature) were sent and timed out."""
    with pytest.raises(ValueError, match=match):
        build()


def test_scene_builders():
    assert M.scene_get() == h("8241")
    assert M.scene_recall(1, tid=7) == h("8242010007")
    assert M.scene_recall(0x0102, ack=False, tid=7) == h("8243020107")


# ----------------------------------------------------------------------------- transitions (review-4 F4-1)


def test_lightness_and_scene_builders_carry_a_transition():
    """Lightness Set and Scene Recall take the optional `[transition][delay]` pair as the other Sets do; without a
    transition their bytes are the ones they always were (the light's Default Transition Time applies)."""
    assert M.light_lightness_set(0x8000, tid=5) == h("824c008005")
    assert M.light_lightness_set(0x8000, tid=5, transition=0x1E) == h("824c0080051e00")
    assert M.light_lightness_set(
        0xFFFF, ack=False, tid=0, transition=0x41, delay=4
    ) == h("824dffff004104")
    assert M.scene_recall(3, tid=7, transition=0x1E) == h("82420300071e00")
    assert M.scene_recall(0x0102, ack=False, tid=7, transition=0x0A, delay=1) == h(
        "82430201070a01"
    )
    assert M.light_ctl_set(0x8000, 3000, tid=2, transition=0x1E) == h(
        "825e" + "0080" + "b80b" + "0000" + "02" + "1e00"
    )
    assert M.generic_onoff_set(True, tid=1, transition=M.encode_transition(3)) == h(
        "820201011e00"
    )
    # what `listen` shows of them (the probe's)
    assert M.describe(M.light_lightness_set(0x8000, tid=5, transition=0x1E)) == (
        "Light Lightness Set 32768 tid=5 transition=30x100ms delay=0ms"
    )
    assert M.describe(M.light_ctl_set(0x8000, 3000, tid=2, transition=0x43)) == (
        "Light CTL Set l=32768 t=3000K tid=2 transition=3x1s delay=0ms"
    )
    assert M.describe(M.scene_recall(3, ack=False, tid=7, transition=0x1E)) == (
        "Scene Recall Unack scene=3 tid=7 transition=30x100ms delay=0ms"
    )
    assert M.describe(M.scene_recall(3, tid=7)) == "Scene Recall scene=3 tid=7"


@pytest.mark.parametrize(
    "build",
    [
        lambda: M.light_lightness_set(1, transition=0x3F),
        lambda: M.light_lightness_set(1, transition=0xBF),
        lambda: M.scene_recall(1, transition=0x7F),
        lambda: M.scene_recall(1, ack=False, transition=0xFF),
    ],
)
def test_the_new_transitions_refuse_63_steps(build):
    """Steps 0x3F (unknown) is a Status's value, prohibited in a Set (§3.1.3), at every resolution."""
    with pytest.raises(ValueError, match="prohibited in a Set"):
        build()


@pytest.mark.parametrize(
    ("seconds", "byte"),
    [
        (0, 0x00),
        (0.04, 0x00),  # under half a step: no transition
        (0.05, 0x01),  # half up
        (0.1, 0x01),
        (1, 0x0A),  # the finest resolution: 10 x 100 ms, not 1 x 1 s
        (3, 0x1E),
        (6.2, 0x3E),
        (6.3, 0x3E),  # 62 x 100 ms is nearer than 6 x 1 s
        (6.8, 0x47),  # 7 x 1 s
        (62, 0x7E),
        (66, 0x7E),  # 62 s and 70 s tie: the finer resolution
        (67, 0x87),  # 7 x 10 s
        (620, 0xBE),
        (621, 0xBE),
        (3600, 0xC6),  # an hour: 6 x 10 min
        (M.TRANSITION_MAX_SECONDS, 0xFE),
    ],
)
def test_encode_transition_picks_the_nearest_finest_step(seconds: float, byte: int):
    assert M.encode_transition(seconds) == byte


@pytest.mark.parametrize(
    "seconds", [-0.1, M.TRANSITION_MAX_SECONDS + 1, math.inf, math.nan]
)
def test_encode_transition_refuses_what_no_byte_carries(seconds: float):
    with pytest.raises(ValueError, match=r"is not 0\.\.37200 s"):
        M.encode_transition(seconds)


def test_decode_transition():
    assert M.decode_transition(0x00) == 0
    assert M.decode_transition(0x1E) == 3.0
    assert M.decode_transition(0x47) == 7.0
    assert M.decode_transition(0x86) == 60.0
    assert M.decode_transition(0xFE) == M.TRANSITION_MAX_SECONDS
    for unknown in (0x3F, 0x7F, 0xBF, 0xFF):
        assert M.decode_transition(unknown) is None


@given(st.integers(0, 0xFF).filter(lambda b: b & 0x3F != M.TRANSITION_UNKNOWN))
def test_transition_byte_round_trip(byte: int):
    """Every time a byte stands for encodes to a byte standing for the same time (the finest of its spellings)."""
    seconds = M.decode_transition(byte)
    assert seconds is not None
    again = M.encode_transition(seconds)
    assert M.decode_transition(again) == seconds
    assert again >> 6 <= byte >> 6  # never a coarser resolution than needed


@given(st.floats(0, M.TRANSITION_MAX_SECONDS, allow_nan=False))
def test_encode_transition_is_within_half_a_step(seconds: float):
    """Never steps 63, and within half a step of the first resolution whose 62 steps reach the time."""
    byte = M.encode_transition(seconds)
    assert byte & 0x3F <= M.TRANSITION_MAX_STEPS
    reach = next(
        step
        for step in M.TRANSITION_STEP_MS
        if seconds * 1000 <= M.TRANSITION_MAX_STEPS * step
    )
    decoded = M.decode_transition(byte)
    assert decoded is not None
    assert abs(decoded - seconds) * 1000 <= reach / 2 + 1e-6


@pytest.mark.parametrize(
    ("opcode", "params", "seconds"),
    [
        (M.GEN_ONOFF_STATUS, h("00" + "01" + "1e"), 3.0),
        (M.GEN_ONOFF_STATUS, h("01"), None),  # at rest
        (M.LIGHT_LIGHTNESS_STATUS, h("0010" + "0080" + "41"), 1.0),
        (M.LIGHT_LIGHTNESS_STATUS, h("0080"), None),
        (M.LIGHT_LIGHTNESS_STATUS, h("0010" + "0080" + "3f"), None),  # unknown
        (M.LIGHT_CTL_STATUS, h("0010b80b" + "0080b80b" + "0a"), 1.0),
        (M.LIGHT_CTL_TEMP_STATUS, h("b80b0000" + "a00f0000" + "86"), 60.0),
        (M.GEN_LEVEL_STATUS, h("0000" + "ff7f" + "02"), 0.2),
        (M.SCENE_STATUS, h("00" + "0000" + "0300" + "14"), 2.0),
        (M.SCENE_STATUS, h("00" + "0300"), None),
        (M.SENSOR_STATUS, h("4200" + "1e"), None),  # not a load's status
    ],
)
def test_remaining_time(opcode: int, params: bytes, seconds: float | None):
    assert M.remaining_time(opcode, params) == seconds


def test_a_set_with_a_transition_is_shown_by_its_target():
    """review-4 D32 with a transition: the Status answering the Set shows the old present state, the requested one
    as its target and the remaining time — the Set took effect; the old state at rest does not show it."""
    dim = M.set_shown_by(M.light_lightness_set(0x8000, transition=0x1E))
    assert dim is not None
    assert dim(h("0010" + "0080" + "1e"))
    assert not dim(h("0010"))
    ctl = M.set_shown_by(M.light_ctl_set(0x8000, 3000, transition=0x1E))
    assert ctl is not None
    assert ctl(h("0010a00f" + "0080b80b" + "1e"))
    assert not ctl(h("0010a00f"))
    warm = M.set_shown_by(M.light_ctl_temperature_set(3000, transition=0x1E))
    assert warm is not None
    assert warm(h("a00f0000" + "b80b0000" + "1e"))
    on = M.set_shown_by(M.generic_onoff_set(False, transition=0x1E))
    assert on is not None
    assert on(h("01" + "00" + "1e"))  # switching off: on until the fade ends


def test_vendor_property_get_builders():
    assert M.vendor_property_get("admin", 0x5003) == h("c22705") + h("0350")
    assert M.vendor_property_get("manufacturer", 0x0002) == h("c82705") + h("0200")
    assert M.vendor_property_get("user", 0x5010) == h("ce2705") + h("1050")
    with pytest.raises(KeyError):
        M.vendor_property_get("owner", 1)


# ----------------------------------------------------------------------------- describe(): vendor


def test_describe_vendor_property_status_with_key_mode():
    """The value is decoded by the codec catalogue (`properties.describe_status`), after the raw hex."""
    pdu = h("c52705") + h("0350") + b"\x01" + b"\x02"
    assert (
        M.describe(pdu)
        == "LBC Admin Property Status prop 0x5003 access=1 value=02 key_mode=scene"
    )
    pdu = h("cb2705") + h("0350") + b"\x00" + b"\x09"
    assert (
        M.describe(pdu)
        == "LBC Manufacturer Property Status prop 0x5003 access=0 value=09 key_mode=9"
    )
    assert (
        M.describe(h("c52705") + h("0350") + b"\x03\x06")
        == "LBC Admin Property Status prop 0x5003 access=3 value=06 key_mode=gateway"
    )
    assert (
        M.describe(M.vendor_property_status("user", 0x5013, b"\x01"))
        == "LBC User Property Status prop 0x5013 access=3 value=01 key_status_led=on"
    )
    # an id outside the catalogue: raw hex only
    assert (
        M.describe(h("c52705") + h("0060") + b"\x03\x01")
        == "LBC Admin Property Status prop 0x6000 access=3 value=01"
    )


def test_describe_never_shows_the_gateway_api_token():
    """Property 0xC001 (`Text(secret=True)`) is the gateway's API token: neither the raw `value=` hex nor the
    decoded field may carry it, whichever opcode carries the property (every LBC / SIG Status and Set shape)."""
    token = b"SECRET-TOKEN-abc123"
    pid = h("01c0")
    vendor_status = h("cb2705") + pid + b"\x01" + token
    assert M.describe(vendor_status) == (
        "LBC Manufacturer Property Status prop 0xC001 access=1 value=<redacted> gateway_api_token=<redacted>"
    )
    assert M.describe(h("c92705") + pid + token) == (
        "LBC Manufacturer Property Set prop 0xC001 value=<redacted> gateway_api_token=<redacted>"
    )
    assert M.describe(h("c32705") + pid + b"\x03" + token) == (
        "LBC Admin Property Set prop 0xC001 access=3 value=<redacted> gateway_api_token=<redacted>"
    )
    assert M.describe(h("46") + pid + b"\x01" + token) == (
        "Generic Manufacturer Property Status prop 0xC001 access=1 value=<redacted>"
    )  # an LBC id on a SIG server: not labelled with the LBC name, still redacted
    assert M.describe(h("4c") + pid + token) == (
        "Generic User Property Set prop 0xC001 value=<redacted>"
    )
    assert (
        M.describe(h("c82705") + pid)
        == "LBC Manufacturer Property Get prop 0xC001 gateway_api_token"
    )
    for pdu in (
        vendor_status,
        h("c92705") + pid + token,
        h("c32705") + pid + b"\x03" + token,
        h("46") + pid + b"\x01" + token,
        h("4c") + pid + token,
        h("cb2705") + pid,  # no body at all
        h("cb2705") + pid + b"\x01",  # empty value
    ):
        text = M.describe(pdu)
        assert token.hex() not in text
        assert token.decode() not in text
    assert M.describe(h("cb2705") + pid + b"\x01") == (
        "LBC Manufacturer Property Status prop 0xC001 access=1 value=<redacted> gateway_api_token=<redacted>"
    )
    # the other gateway credentials are plain text on purpose (the app shows them)
    assert M.describe(h("cb2705") + h("02c0") + b"\x01" + b"192.168.1.5\x00") == (
        "LBC Manufacturer Property Status prop 0xC002 access=1 value=3139322e3136382e312e3500 gateway_ip=192.168.1.5"
    )


def test_describe_vendor_property_set_and_get():
    assert (
        M.describe(h("c32705") + h("0350") + b"\x03\x00")
        == "LBC Admin Property Set prop 0x5003 access=3 value=00 key_mode=light"
    )
    assert (  # a value the codec cannot decode: `name=?hex`
        M.describe(h("cf2705") + h("0450") + b"\x10\x00")
        == "LBC User Property Set prop 0x5004 value=1000 turn_on_threshold=?1000"
    )
    assert (
        M.describe(h("ce2705") + h("0350"))
        == "LBC User Property Get prop 0x5003 key_mode"
    )
    assert M.describe(h("c22705") + h("0060")) == "LBC Admin Property Get prop 0x6000"
    # a status without any body after the property id: no access byte, empty value
    assert (
        M.describe(h("c52705") + h("0350"))
        == "LBC Admin Property Status prop 0x5003 access=? value= key_mode=?"
    )
    # a property PDU that is too short to carry an id is printed raw
    assert M.describe(h("c52705") + b"\x03") == "LBC Admin Property Status 03"


@pytest.mark.parametrize(
    ("event", "name"),
    [(0x05, "'pushed'"), (0x06, "'held'"), (0x04, "'released'"), (0x01, "'pushed_up'")],
)
def test_describe_button_event(event: int, name: str):
    """0x5012 decodes through `properties.KeyEventCodec` (the firmware's labels, docs/cross-repo-analysis.md §1.2)."""
    pdu = h("d02705") + h("1250") + bytes([3, event])
    assert (
        M.describe(pdu)
        == f"LBC User Property Set Unack prop 0x5012 value={bytes([3, event]).hex()} key_event=KeyEvent(counter=3, event={name})"
    )


def test_describe_insert_id():
    pdu = h("d12705") + h("0200") + b"\x00" + h("04000200")
    assert (
        M.describe(pdu)
        == "LBC User Property Status prop 0x0002 access=0 value=04000200 insert_id=InsertId(function='tw_dimming', insert_type='generic_insert')"
    )
    pdu = h("d12705") + h("0200") + b"\x00" + h("0400")
    assert (
        M.describe(pdu)
        == "LBC User Property Status prop 0x0002 access=0 value=0400 insert_id=InsertId(function='tw_dimming', insert_type='unknown')"
    )


def test_describe_other_vendor_opcodes():
    # JH Scheduler / Scene Action Setup go through vendor_models (their own tests); the rest stays hex
    assert (
        M.describe(h("d32705") + h("aabb"))
        == "JH Scheduler Status slot 10 sub 10: nothing"
    )
    assert M.describe(h("d32705")) == "JH Scheduler Status"
    assert M.describe(h("d52705") + h("aabb")) == "vendor op 15 aabb"
    assert M.describe(h("e02705") + h("aabb")) == "vendor op 20 aabb"
    assert M.describe(h("c13412") + h("aa")) == "vendor 1234 op 01 aa"


# ----------------------------------------------------------------------------- describe(): SIG


def test_describe_generic_status_messages():
    assert M.describe(h("820401")) == "Generic OnOff Status present=ON"
    assert (
        M.describe(h("8204000142"))
        == "Generic OnOff Status present=OFF target=ON remaining=2x1s"
    )
    assert M.describe(h("8208ffff")) == "Generic Level Status present=-1"
    assert (
        M.describe(h("82080080ff7fc0"))
        == "Generic Level Status present=-32768 target=32767 remaining=0x10min"
    )
    assert M.describe(h("824effff")) == "Light Lightness Status present=65535"
    assert (
        M.describe(h("824e0100020083"))
        == "Light Lightness Status present=1 target=2 remaining=3x10s"
    )


def test_describe_ctl_status_messages():
    assert M.describe(h("8260e803201c")) == "Light CTL Status lightness=1000 temp=7200K"
    assert (
        M.describe(h("8260e803201c0000b80b05"))
        == "Light CTL Status lightness=1000 temp=7200K target_l=0 target_t=3000 remaining=5x100ms"
    )
    assert (
        M.describe(h("8266b80bffff"))
        == "Light CTL Temperature Status temp=3000K deltaUV=-1"
    )


def test_describe_scene_messages():
    assert M.describe(h("5e000100")) == "Scene Status status=0 current=1"
    assert (
        M.describe(h("5e000100020005"))
        == "Scene Status status=0 current=1 target=2 remaining=5x100ms"
    )
    assert (
        M.describe(h("824500010001000200"))
        == "Scene Register Status status=0 current=1 scenes=[1, 2]"
    )
    assert (
        M.describe(h("8245000000"))
        == "Scene Register Status status=0 current=0 scenes=[]"
    )
    assert M.describe(h("8242010003")) == "Scene Recall scene=1 tid=3"
    assert M.describe(h("8243020107")) == "Scene Recall Unack scene=258 tid=7"
    assert M.describe(h("8241")) == "Scene Get"


def test_decode_scene_status():
    """The on-air form (3 bytes, what the nodes publish after a recall) and the transition form of §5.2.2.6."""
    status = M.decode_scene_status(h("000100"))
    assert status == M.SceneStatus(0, 1)
    assert status.ok
    assert status.target is None
    assert M.decode_scene_status(h("000100020005")) == M.SceneStatus(0, 1, 2, 5)
    assert not M.decode_scene_status(h("020000")).ok  # Scene Not Found
    with pytest.raises(ValueError, match="3 bytes"):
        M.decode_scene_status(h("0001"))
    assert M.describe(h("5e0001")) == "?? 5e0001"  # too short: undecodable


def test_describe_sensor_status_formats():
    fmt_a = h("c209") + h("0201")  # format A: property 0x004E, 2 bytes
    fmt_b = h("05") + h("8100") + h("010203")  # format B: property 0x0081, 3 bytes
    assert M.describe(h("52") + fmt_a) == "Sensor Status prop=0x004E raw=0201 le=258"
    assert (
        M.describe(h("52") + fmt_b) == "Sensor Status prop=0x0081 raw=010203 le=197121"
    )
    assert (
        M.describe(h("52") + fmt_a + fmt_b)
        == "Sensor Status prop=0x004E raw=0201 le=258; prop=0x0081 raw=010203 le=197121"
    )
    assert M.describe(h("52") + h("ff8100")) == "Sensor Status prop=0x0081 raw= le="
    assert M.describe(h("52")) == "Sensor Status "


def test_sensor_values_unmarshalling():
    assert M.sensor_values(b"") == []
    assert M.sensor_values(h("c2090201")) == [(0x004E, h("0201"))]
    assert M.sensor_values(h("058100010203")) == [(0x0081, h("010203"))]
    assert M.sensor_values(h("ff8100")) == [
        (0x0081, b"")
    ]  # format B length 0x7F+1 means zero-length
    assert M.sensor_values(h("c2090201" + "058100010203" + "ff6d00")) == [
        (0x004E, h("0201")),
        (0x0081, h("010203")),
        (0x006D, b""),
    ]
    # format A header: bit0=0, 4-bit length-1, 11-bit property id (max 0x7FF, max length 16)
    hdr = (0x7FF << 5) | (15 << 1)
    assert M.sensor_values(hdr.to_bytes(2, "little") + bytes(range(16))) == [
        (0x7FF, bytes(range(16)))
    ]


def test_describe_sig_property_status_and_get():
    assert (
        M.describe(h("4e1a0001") + b"02020002")
        == "Generic User Property Status prop 0x001A access=1 value=3032303230303032 software_version=2.2.0.2"
    )
    assert (
        M.describe(h("4e1a0001") + h("ff00"))
        == "Generic User Property Status prop 0x001A access=1 value=ff00 software_version=?ff00"
    )
    assert (
        M.describe(h("4a810000") + h("a00100"))
        == "Generic Admin Property Status prop 0x0081 access=0 value=a00100 active_power=41.6W"
    )
    assert (
        M.describe(h("4a6a0003") + h("393000"))
        == "Generic Admin Property Status prop 0x006A access=3 value=393000 total_energy=12345Wh"
    )
    assert (
        M.describe(h("466d00"))
        == "Generic Manufacturer Property Status prop 0x006D access=? value= power_on_time=?"
    )
    assert (
        M.describe(h("822f1a00"))
        == "Generic User Property Get prop 0x001A software_version"
    )
    assert M.describe(h("822d0350")) == "Generic Admin Property Get prop 0x5003"


def test_describe_property_ids_by_the_addressed_server_only():
    # SIG and LBC ids overlap: 0x0010 is the SIG hardware revision and an LBC property of its own; an id one
    # catalogue lacks is not borrowed from the other (a vendor 0x001A is not the SIG software version)
    lbc_0010 = properties.PROPERTIES[0x0010].name
    assert M.describe(h("822b1000")).endswith("prop 0x0010 hardware_revision")
    assert M.describe(h("c82705") + h("1000")).endswith(f"prop 0x0010 {lbc_0010}")
    assert (
        M.describe(h("c82705") + h("1a00"))
        == "LBC Manufacturer Property Get prop 0x001A"
    )
    assert M.describe(h("cb2705") + h("1a00") + b"\x01" + b"02020002") == (
        "LBC Manufacturer Property Status prop 0x001A access=1 value=3032303230303032"
    )
    assert M.describe(h("822f5050")) == "Generic User Property Get prop 0x5050"
    assert (
        M.describe(h("4e0350") + b"\x01\x06")
        == "Generic User Property Status prop 0x5003 access=1 value=06"
    )
    assert M.describe(h("822b0060")) == "Generic Manufacturer Property Get prop 0x6000"


def test_describe_misc_status_and_config():
    assert M.describe(h("82100a")) == "Default Transition Time Status 10x100ms"
    assert M.describe(h("821201")) == "OnPowerUp Status 1"
    assert (
        M.describe(h("80030000000000"))
        == "Config AppKey Status Success: netkey=0 appkey=0"
    )
    assert M.describe(h("02aa")) == "Config Composition Data Status ?? aa"
    assert M.describe(h("8049")) == "Config Node Reset"
    assert (
        M.describe(h("8019") + h("0102")) == "Config Model Publication Status ?? 0102"
    )  # truncated
    assert (
        M.describe(h("801d") + h("0102"))
        == "Config Model Subscription Delete All ?? 0102"  # truncated
    )


def test_describe_set_messages():
    assert M.describe(h("82020105")) == "Generic OnOff Set ON tid=5"
    assert (
        M.describe(h("820300054202"))
        == "Generic OnOff Set Unack OFF tid=5 transition=2x1s delay=10ms"
    )
    assert M.describe(h("8206008007")) == "Generic Level Set level=-32768 tid=7"
    assert M.describe(h("8207ff7f07")) == "Generic Level Set Unack level=32767 tid=7"
    assert M.describe(h("824cffff01")) == "Light Lightness Set 65535 tid=1"
    assert M.describe(h("824d000002")) == "Light Lightness Set Unack 0 tid=2"
    assert (
        M.describe(
            h("825e")
            + (1000).to_bytes(2, "little")
            + (2700).to_bytes(2, "little")
            + h("0000")
            + b"\x09"
        )
        == "Light CTL Set l=1000 t=2700K tid=9"
    )
    assert (
        M.describe(
            h("825f")
            + (1).to_bytes(2, "little")
            + (6500).to_bytes(2, "little")
            + h("ffff")
            + b"\x09"
        )
        == "Light CTL Set Unack l=1 t=6500K tid=9"
    )


def test_describe_get_messages_and_unknown():
    assert M.describe(h("8201")) == "Generic OnOff Get"
    assert M.describe(h("824b")) == "Light Lightness Get"
    assert M.describe(h("825d")) == "Light CTL Get"
    assert M.describe(h("8299aa")) == "SIG op 8299 aa"
    assert M.describe(h("7e")) == "SIG op 007E "
    assert (
        M.describe(h("7f")) == "?? 7f"
    )  # 0x7F is RFU (§3.7.3.1): not an opcode at all
    assert M.describe(h("82")) == "?? 82"  # a 2-byte opcode cut after its first octet
    assert M.describe(b"") == "?? "


def test_describe_of_a_device_key_message_never_dumps_undecoded_bytes():
    """A DevKey-encrypted PDU may be a key refresh carrying the new NetKey / AppKey: with `devkey=True` every
    fallback that would print raw parameters (unknown SIG or vendor opcode, truncated message) prints the byte
    count instead, while everything decoded reads exactly as before."""
    key = bytes(range(0xA0, 0xB0))
    assert M.describe(h("8299") + key, devkey=True) == "SIG op 8299 <16 bytes>"
    assert M.describe(h("7e"), devkey=True) == "SIG op 007E <0 bytes>"
    assert M.describe(h("7f"), devkey=True) == "?? <1 byte>"
    assert M.describe(h("e02705") + key, devkey=True) == "vendor op 20 <16 bytes>"
    assert (
        M.describe(h("c13412") + b"\xaa", devkey=True) == "vendor 1234 op 01 <1 byte>"
    )
    assert (
        M.describe(h("8204"), devkey=True) == "?? <2 bytes>"
    )  # truncated: the whole PDU is undecoded
    assert M.describe(b"", devkey=True) == "?? <0 bytes>"
    assert M.describe(h("8045") + h("0000") + key, devkey=True) == (
        "Config NetKey Update netkey=0 key=<16 bytes>"
    )
    assert M.describe(h("01") + h("000000") + key, devkey=True) == (
        "Config AppKey Update netkey=0 appkey=0 key=<16 bytes>"
    )
    assert M.describe(h("8045") + h("0000") + key[:7], devkey=True) == (
        "Config NetKey Update ?? <9 bytes>"
    )
    assert M.describe(h("800e05"), devkey=True) == "Config Default TTL Status ttl=5"
    assert M.describe(h("820401"), devkey=True) == "Generic OnOff Status present=ON"
    for pdu in (
        h("8299") + key,
        h("8045") + h("0000") + key,
        h("8040") + h("0000") + key[:5],
    ):
        assert key[:4].hex() not in M.describe(pdu, devkey=True)
    assert (
        M.describe(h("8299") + key) == "SIG op 8299 " + key.hex()
    )  # default: hex, as always


def test_describe_round_trips_every_builder():
    assert M.describe(M.generic_onoff_set(True, tid=1)) == "Generic OnOff Set ON tid=1"
    assert (
        M.describe(M.light_lightness_set(500, ack=False, tid=2))
        == "Light Lightness Set Unack 500 tid=2"
    )
    assert (
        M.describe(M.light_ctl_set(500, 4000, tid=3))
        == "Light CTL Set l=500 t=4000K tid=3"
    )
    assert M.describe(M.scene_recall(2, tid=4)) == "Scene Recall scene=2 tid=4"
    assert (
        M.describe(M.vendor_property_get("user", 0x5012))
        == "LBC User Property Get prop 0x5012 key_event"
    )


def test_sensor_get_builder_and_describe():
    assert M.sensor_get() == h("8231")
    assert M.sensor_get(0x0081) == h("82318100")
    assert M.describe(M.sensor_get()) == "Sensor Get"
    assert M.describe(M.sensor_get(0x0081)) == "Sensor Get prop 0x0081"
    assert M.describe(h("823181")) == "Sensor Get"


# ----------------------------------------------------------------------------- Time Set (Mesh Model spec §5.2.1.2)

EEST = timezone(timedelta(hours=3))


def test_time_set_vector():
    """12:00:00.5 EEST = 09:00:00.5 UTC. Days 2000-01-01..2025-01-01 = 25*365 + 7 leap = 9132, + 257 to
    Sep 15 = 9389 days; 9389*86400 + 9*3600 + 37 (TAI-UTC) = 811_242_037 = 0x305A9235."""
    when = datetime(2025, 9, 15, 12, 0, 0, 500_000, tzinfo=EEST)
    pdu = M.time_set(when)
    assert pdu == (
        h("5c")  # Time Set opcode
        + h("35925a3000")  # TAI seconds u40 LE
        + h("80")  # subsecond: 0.5 s * 256 = 128
        + h("00")  # uncertainty
        + h("4802")  # authority=0 | (37 + 255) << 1 = 0x0248, u16 LE
        + h("4c")
    )  # zone offset: +3 h = 12 quarter hours + 64 = 76
    assert len(pdu) == 11
    assert (
        M.describe(pdu)
        == "Time Set 2025-09-15T12:00:00.500000+03:00 tai=811242037 uncertainty=0ms authority=0 tai_utc_delta=37"
    )


def test_time_set_explicit_fields_and_zone():
    when = datetime(2025, 9, 15, 9, 0, tzinfo=UTC)
    pdu = M.time_set(
        when,
        zone_offset=timedelta(hours=-5, minutes=-45),
        authority=True,
        uncertainty=3,
        tai_utc_delta=38,
    )
    assert pdu == h("5c") + h("36925a3000") + h("00") + h("03") + (
        (38 + 255) << 1 | 1
    ).to_bytes(2, "little") + bytes([64 - 23])
    assert (
        M.describe(pdu)
        == "Time Set 2025-09-15T03:15:00-05:45 tai=811242038 uncertainty=30ms authority=1 tai_utc_delta=38"
    )
    assert M.time_set(when, zone_offset=timedelta(0)) == M.time_set(when)
    assert M.time_set(when.astimezone(EEST), zone_offset=timedelta(0))[1:6] == h(
        "35925a3000"
    )  # the instant, not the wall clock
    assert M.time_set(M.TAI_EPOCH)[1:6] == (37).to_bytes(5, "little")
    assert (
        M.time_set(when, zone_offset=timedelta(hours=47, minutes=45))[-1] == 255
    )  # encoding limits (§5.1.1.5)
    assert M.time_set(when, zone_offset=timedelta(hours=-16))[-1] == 0


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"when": datetime(2025, 9, 15, 12)}, "timezone-aware"),  # noqa: DTZ001  # naive on purpose: the rejection under test
        (
            {"when": datetime(2025, 9, 15, 12, tzinfo=timezone(timedelta(minutes=10)))},
            "multiple of 15 minutes",
        ),
        (
            {
                "when": datetime(2025, 9, 15, 12, tzinfo=UTC),
                "zone_offset": timedelta(hours=-16, minutes=-15),
            },
            "multiple of 15 minutes",
        ),
        (
            {
                "when": datetime(2025, 9, 15, 12, tzinfo=UTC),
                "zone_offset": timedelta(hours=48),
            },
            "multiple of 15 minutes",
        ),
        (
            {"when": datetime(2025, 9, 15, 12, tzinfo=UTC), "tai_utc_delta": -256},
            "TAI-UTC delta",
        ),
        (
            {"when": datetime(2025, 9, 15, 12, tzinfo=UTC), "tai_utc_delta": 32513},
            "TAI-UTC delta",
        ),
        (
            {"when": datetime(1999, 12, 31, 23, 59, 22, tzinfo=UTC)},
            "outside the TAI range",
        ),
    ],
)
def test_time_set_rejects_bad_input(kwargs, match):
    with pytest.raises(ValueError, match=match):
        M.time_set(**kwargs)


def test_describe_time_status_and_get():
    assert (
        M.describe(h("5d") + h("0000000000")) == "Time Status unknown"
    )  # 5-byte form: TAI seconds 0 = unknown
    assert (
        M.describe(h("5d") + h("35925a3000") + h("00") + h("00") + h("4802") + h("40"))
        == "Time Status 2025-09-15T09:00:00+00:00 tai=811242037 uncertainty=0ms authority=0 tai_utc_delta=37"
    )
    assert M.describe(h("8237")) == "Time Get"
    assert (
        M.describe(h("5c") + h("35925a3000")) == "?? 5c35925a3000"
    )  # truncated Time Set


def test_describe_truncated_status_does_not_raise():
    """A truncated PDU must degrade to the '?? <hex>' form, never raise (send_access() logs via describe())."""
    assert M.describe(h("8204")) == "?? 8204"
    assert M.describe(h("820201")) == "?? 820201"


# ======================================================================

# SIG additions (roadmap step 0.2): Generic Level / Delta / Move, OnPowerUp, Battery, Location, SIG properties,
# Light Lightness Range / Default, Light CTL Temperature Range / Default. Byte vectors per Mesh Model spec 1.0.1.
# =============================================================================


def test_generic_level_builders():
    assert M.generic_level_get() == h("8205")
    assert M.generic_level_set(0x1234, tid=5) == h("8206341205")
    assert M.generic_level_set(-1, tid=5) == h("8206ffff05")
    assert M.generic_level_set(-32768, ack=False, tid=0x22) == h("8207008022")
    assert M.generic_level_set(32767, tid=1, transition=0x42) == h("8206ff7f014200")
    assert M.generic_level_set(0, ack=False, tid=1, transition=0x42, delay=4) == h(
        "82070000014204"
    )
    with pytest.raises(ValueError, match="level 32768"):  # was OverflowError
        M.generic_level_set(32768, tid=1)


def test_generic_delta_and_move_builders():
    assert M.generic_delta_set(1, tid=9) == h("82090100000009")
    assert M.generic_delta_set(-2, ack=False, tid=9) == h("820afeffffff09")
    assert M.generic_delta_set(0x7FFFFFFF, tid=9, transition=0x0A, delay=2) == h(
        "8209ffffff7f090a02"
    )
    assert M.generic_move_set(5, tid=1) == h("820b050001")
    assert M.generic_move_set(-5, ack=False, tid=1) == h("820cfbff01")
    assert M.generic_move_set(1, tid=1, transition=0xC0) == h("820b010001c000")
    with pytest.raises(ValueError, match="delta 2147483648"):  # was OverflowError
        M.generic_delta_set(1 << 31, tid=1)
    with pytest.raises(ValueError, match="delta 32768"):
        M.generic_move_set(1 << 15, tid=1)


def test_generic_level_builders_use_fresh_tid(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(M, "_tid", 0x10)
    assert M.generic_level_set(1) == h("8206010011")
    assert M.generic_delta_set(1) == h("82090100000012")
    assert M.generic_move_set(1) == h("820b010013")


def test_describe_generic_level_delta_move_sets():
    assert M.describe(h("8206008007")) == "Generic Level Set level=-32768 tid=7"
    assert (
        M.describe(h("8207ff7f074202"))
        == "Generic Level Set Unack level=32767 tid=7 transition=2x1s delay=10ms"
    )
    assert M.describe(h("82090100000009")) == "Generic Delta Set delta=1 tid=9"
    assert (
        M.describe(h("820afeffffff090a02"))
        == "Generic Delta Set Unack delta=-2 tid=9 transition=10x100ms delay=10ms"
    )
    assert M.describe(h("820b050001")) == "Generic Move Set delta=5 tid=1"
    assert (
        M.describe(h("820cfbff01c000"))
        == "Generic Move Set Unack delta=-5 tid=1 transition=0x10min delay=0ms"
    )
    assert M.describe(h("8205")) == "Generic Level Get"
    assert M.describe(h("8209010000")) == "?? 8209010000"  # truncated: no TID


def test_generic_onpowerup_builders_and_describe():
    assert M.generic_onpowerup_get() == h("8211")
    assert M.generic_onpowerup_set(0) == h("821300")
    assert M.generic_onpowerup_set(1, ack=False) == h("821401")
    assert M.generic_onpowerup_set(2) == h("821302")
    with pytest.raises(ValueError, match=r"not 0\.\.2"):
        M.generic_onpowerup_set(3)
    assert M.describe(h("8211")) == "OnPowerUp Get"
    assert M.describe(h("821302")) == "OnPowerUp Set 2 (restore)"
    assert M.describe(h("821400")) == "OnPowerUp Set Unack 0 (off)"
    assert M.describe(h("821405")) == "OnPowerUp Set Unack 5 (?)"
    assert M.describe(h("821201")) == "OnPowerUp Status 1"


def test_generic_battery_get_and_status():
    assert M.generic_battery_get() == h("8223")
    assert M.describe(h("8223")) == "Generic Battery Get"
    # level 64 %, 120 min to discharge, charge time unknown, flags 0b01_10_10_01:
    # presence 1 removable, indicator 2 good, charging 2 charging, serviceability 1 no service required
    pdu = h("8224") + bytes([64]) + h("780000") + h("ffffff") + bytes([0b01101001])
    assert M.battery_status(pdu[2:]) == {
        "level": 64,
        "discharge_minutes": 120,
        "charge_minutes": None,
        "presence": "removable",
        "indicator": "good",
        "charging": "charging",
        "serviceability": "no-service-required",
    }
    assert (
        M.describe(pdu)
        == "Generic Battery Status level=64% discharge=120min charge=?min presence=removable indicator=good charging=charging serviceability=no-service-required"
    )
    unknown = h("8224") + h("ff") + h("ffffff") + h("010000") + h("ff")
    assert M.battery_status(unknown[2:]) == {
        "level": None,
        "discharge_minutes": None,
        "charge_minutes": 1,
        "presence": "unknown",
        "indicator": "unknown",
        "charging": "unknown",
        "serviceability": "unknown",
    }
    assert M.describe(unknown).startswith(
        "Generic Battery Status level=?% discharge=?min charge=1min"
    )
    assert M.describe(h("822440")) == "?? 822440"  # truncated


def test_generic_location_global_set_vector():
    """Berlin fallback of the app (52.516811 / 13.408333): floor(lat/90*(2^31-1)), floor(lon/180*(2^31-1))."""
    lat = math.floor(52.516811 / 90 * 0x7FFFFFFF)
    lon = math.floor(13.408333 / 180 * 0x7FFFFFFF)
    assert (lat, lon) == (0x4AB0C990, 0x0988E99B)
    pdu = M.generic_location_global_set(52.516811, 13.408333, 40)
    assert pdu == h("42") + h("90c9b04a") + h("9be98809") + h("2800")
    assert M.generic_location_global_set(52.516811, 13.408333, 40, ack=True)[0] == 0x41
    assert M.generic_location_global_get() == h("8225")
    assert M.location_global_fields(None, None, None) == (
        -0x80000000,
        -0x80000000,
        0x7FFF,
    )
    assert M.location_global_fields(-90, -180, -32768) == (
        -0x7FFFFFFF,
        -0x7FFFFFFF,
        -32768,
    )
    assert M.location_global_fields(90, 180, 32766) == (0x7FFFFFFF, 0x7FFFFFFF, 0x7FFE)
    assert M.location_global_fields(0, 0, 40000)[2] == 0x7FFE  # "higher than max"
    assert M.generic_location_global_set(None, None) == h("42") + h("00000080") + h(
        "00000080"
    ) + h("ff7f")


@pytest.mark.parametrize(
    ("args", "match"),
    [
        ((90.1, 0, None), "latitude"),
        ((-91, 0, None), "latitude"),
        ((0, 180.5, None), "longitude"),
        ((0, 0, -40000), "altitude"),
    ],
)
def test_generic_location_global_set_rejects_out_of_range(args, match):
    with pytest.raises(ValueError, match=match):
        M.generic_location_global_set(*args)


def test_describe_location_messages():
    pdu = h("40") + h("90c9b04a") + h("9be98809") + h("2800")
    assert (
        M.describe(pdu)
        == "Generic Location Global Status lat=52.516811 lon=13.408333 alt=40m"
    )
    lat, lon, alt = M.location_global(pdu[1:])
    assert lat is not None
    assert lon is not None
    assert (round(lat, 6), round(lon, 6), alt) == (52.516811, 13.408333, 40)
    assert M.location_global(h("00000080") + h("00000080") + h("ff7f")) == (
        None,
        None,
        None,
    )
    assert (
        M.describe(h("41") + h("00000080") + h("00000080") + h("ff7f"))
        == "Generic Location Global Set lat=? lon=? alt=?"
    )
    assert M.describe(h("8225")) == "Generic Location Global Get"
    assert M.describe(h("42") + h("90c9b04a")) == "?? 4290c9b04a"


def test_generic_property_get_builders():
    assert M.generic_property_get("manufacturer", 0x0002) == h("822b0200")
    assert M.generic_property_get("admin", 0x5003) == h("822d0350")
    assert M.generic_property_get("user", 0x001A) == h("822f1a00")
    assert (
        M.describe(M.generic_property_get("admin", 0x006A))
        == "Generic Admin Property Get prop 0x006A total_energy"
    )
    assert (  # an LBC id on a SIG server is not named after the LBC catalogue
        M.describe(M.generic_property_get("admin", 0x5003))
        == "Generic Admin Property Get prop 0x5003"
    )
    with pytest.raises(KeyError):
        M.generic_property_get("owner", 1)


def test_generic_property_set_builders_and_describe():
    # Admin: [pid][user access][value]; Manufacturer: [pid][user access]; User: [pid][value]
    assert M.generic_property_set("admin", 0x5003, b"\x02") == h("4803500302")
    assert M.generic_property_set(
        "admin", 0x5003, b"\x02", user_access=1, ack=False
    ) == h("4903500102")
    assert M.generic_property_set("manufacturer", 0x0002, user_access=1) == h(
        "44020001"
    )
    assert M.generic_property_set("manufacturer", 0x0002, b"ignored", ack=False) == h(
        "45020003"
    )
    assert M.generic_property_set("user", 0x5004, h("1000")) == h("4c04501000")
    assert M.generic_property_set("user", 0x5004, h("1000"), ack=False) == h(
        "4d04501000"
    )
    with pytest.raises(ValueError, match="user access"):
        M.generic_property_set("admin", 1, b"", user_access=4)
    assert (
        M.describe(h("4803500302"))
        == "Generic Admin Property Set prop 0x5003 access=3 value=02"
    )
    assert (
        M.describe(h("486a000339300000"))
        == "Generic Admin Property Set prop 0x006A access=3 value=39300000 total_energy=12345Wh"
    )
    assert (
        M.describe(h("4903500102"))
        == "Generic Admin Property Set Unack prop 0x5003 access=1 value=02"
    )
    assert (
        M.describe(h("44020001"))
        == "Generic Manufacturer Property Set prop 0x0002 access=1"
    )
    assert (
        M.describe(h("451a0003"))
        == "Generic Manufacturer Property Set Unack prop 0x001A software_version access=3"
    )
    assert (
        M.describe(h("4c04501000"))
        == "Generic User Property Set prop 0x5004 value=1000"
    )
    assert (
        M.describe(h("4d0060")) == "Generic User Property Set Unack prop 0x6000 value="
    )
    assert M.describe(h("480350")) == "?? 480350"  # admin set without the access byte


def test_sig_property_status_decoders_carry_the_access_byte():
    """All three SIG property statuses are `[pid][user access][value]` (Mesh Model §3.2.x): 0x4E/0x4A/0x46."""
    for op, kind in ((0x4E, "User"), (0x4A, "Admin"), (0x46, "Manufacturer")):
        assert (
            M.describe(bytes([op]) + h("6a00") + b"\x03" + h("39300000"))
            == f"Generic {kind} Property Status prop 0x006A access=3 value=39300000 total_energy=12345Wh"
        )
        assert (
            M.describe(bytes([op]) + h("6a00"))
            == f"Generic {kind} Property Status prop 0x006A access=? value= total_energy=?"
        )


def test_sig_property_status_is_named_after_its_model():
    """0x46 answers the Generic Manufacturer Property Get 0x822B (Mesh Model §3.2.8.8), not an anonymous
    "SIG Property Status (46)": the app's settings page reads hardware revision / manufacturer name this way
    (on air, 0001 -> 0174: `822B 1000` answered by `46 1000 01 3130…`)."""
    get = h("822b1000")
    status = h("46100001") + b"10000000" + bytes(8)
    assert (
        M.describe(get)
        == "Generic Manufacturer Property Get prop 0x0010 hardware_revision"
    )
    assert M.describe(status).startswith(
        "Generic Manufacturer Property Status prop 0x0010 access=1 value=3130303030303030"
    )
    assert M.describe(h("4a6d0001260d00")) == (
        "Generic Admin Property Status prop 0x006D access=1 value=260d00 power_on_time=3366h"
    )
    assert M.describe(h("4e1a0001") + b"02020001").startswith(
        "Generic User Property Status prop 0x001A"
    )


def test_light_lightness_range_and_default():
    assert M.light_lightness_range_get() == h("8257")
    assert M.light_lightness_range_set(1, 0xFFFF) == h("825b0100ffff")
    assert M.light_lightness_range_set(0x0100, 0x0200, ack=False) == h("825c00010002")
    with pytest.raises(ValueError, match="1 <= min <= max"):
        M.light_lightness_range_set(0, 10)
    with pytest.raises(ValueError, match="1 <= min <= max"):
        M.light_lightness_range_set(11, 10)
    assert M.light_lightness_default_get() == h("8255")
    assert M.light_lightness_default_set(0) == h("82590000")
    assert M.light_lightness_default_set(0x1234, ack=False) == h("825a3412")
    assert M.describe(h("8257")) == "Light Lightness Range Get"
    assert M.describe(h("825b0100ffff")) == "Light Lightness Range Set min=1 max=65535"
    assert (
        M.describe(h("825c00010002"))
        == "Light Lightness Range Set Unack min=256 max=512"
    )
    assert (
        M.describe(h("8258") + h("00") + h("0100ffff"))
        == "Light Lightness Range Status status=0 (success) min=1 max=65535"
    )
    assert (
        M.describe(h("8258") + h("02") + h("0a006400"))
        == "Light Lightness Range Status status=2 (cannot-set-range-max) min=10 max=100"
    )
    assert (
        M.describe(h("8258") + h("07") + h("0a006400"))
        == "Light Lightness Range Status status=7 (?) min=10 max=100"
    )
    assert M.describe(h("8255")) == "Light Lightness Default Get"
    assert M.describe(h("82590000")) == "Light Lightness Default Set 0"
    assert M.describe(h("825a3412")) == "Light Lightness Default Set Unack 4660"
    assert M.describe(h("82563412")) == "Light Lightness Default Status 4660"
    assert M.describe(h("825801")) == "?? 825801"


def test_light_ctl_temperature_range_and_default():
    assert M.light_ctl_temperature_range_get() == h("8262")
    assert M.light_ctl_temperature_range_set(2000, 6000) == h("826bd0077017")
    assert M.light_ctl_temperature_range_set(800, 20000, ack=False) == h("826c2003204e")
    with pytest.raises(ValueError, match=r"outside 800\.\.20000"):
        M.light_ctl_temperature_range_set(799, 6000)
    with pytest.raises(ValueError, match=r"outside 800\.\.20000"):
        M.light_ctl_temperature_range_set(2000, 20001)
    with pytest.raises(ValueError, match="min > max"):
        M.light_ctl_temperature_range_set(6000, 2000)
    assert M.light_ctl_default_get() == h("8267")
    assert M.light_ctl_default_set(100, 2700) == h("8269") + h("6400") + h("8c0a") + h(
        "0000"
    )
    assert M.light_ctl_default_set(0, 6500, delta_uv=-1, ack=False) == h("826a") + h(
        "0000"
    ) + h("6419") + h("ffff")
    with pytest.raises(ValueError, match=r"outside 800\.\.20000"):
        M.light_ctl_default_set(1, 100)
    assert M.describe(h("8262")) == "Light CTL Temperature Range Get"
    assert (
        M.describe(h("826bd0077017"))
        == "Light CTL Temperature Range Set min=2000K max=6000K"
    )
    assert (
        M.describe(h("826c2003204e"))
        == "Light CTL Temperature Range Set Unack min=800K max=20000K"
    )
    assert (
        M.describe(h("8263") + h("00") + h("d0077017"))
        == "Light CTL Temperature Range Status status=0 (success) min=2000K max=6000K"
    )
    assert (
        M.describe(h("8263") + h("01") + h("d0077017"))
        == "Light CTL Temperature Range Status status=1 (cannot-set-range-min) min=2000K max=6000K"
    )
    assert M.describe(h("8267")) == "Light CTL Default Get"
    assert (
        M.describe(h("8269") + h("6400") + h("8c0a") + h("0000"))
        == "Light CTL Default Set l=100 t=2700K deltaUV=0"
    )
    assert (
        M.describe(h("826a") + h("0000") + h("6419") + h("ffff"))
        == "Light CTL Default Set Unack l=0 t=6500K deltaUV=-1"
    )
    assert (
        M.describe(h("8268") + h("6400") + h("8c0a") + h("0100"))
        == "Light CTL Default Status l=100 t=2700K deltaUV=1"
    )
    assert M.describe(h("826364")) == "?? 826364"


def test_describe_round_trips_every_sig_addition():
    assert (
        M.describe(M.generic_level_set(-100, tid=1, transition=0x05))
        == "Generic Level Set level=-100 tid=1 transition=5x100ms delay=0ms"
    )
    assert M.describe(M.generic_delta_set(7, ack=False, tid=2)) == (
        "Generic Delta Set Unack delta=7 tid=2"
    )
    assert (
        M.describe(M.generic_move_set(-7, tid=3)) == "Generic Move Set delta=-7 tid=3"
    )
    assert M.describe(M.generic_onpowerup_set(1)) == "OnPowerUp Set 1 (default)"
    assert M.describe(M.generic_location_global_set(0, 0, 0)) == (
        "Generic Location Global Set Unack lat=0.000000 lon=0.000000 alt=0m"
    )
    assert M.describe(M.generic_property_set("admin", 0x5001, b"\x05")) == (
        "Generic Admin Property Set prop 0x5001 access=3 value=05"
    )
    assert M.describe(M.light_lightness_range_set(1, 2)) == (
        "Light Lightness Range Set min=1 max=2"
    )
    assert (
        M.describe(M.light_lightness_default_set(3)) == "Light Lightness Default Set 3"
    )
    assert M.describe(M.light_ctl_temperature_range_set(2000, 6000)) == (
        "Light CTL Temperature Range Set min=2000K max=6000K"
    )
    assert M.describe(M.light_ctl_default_set(1, 4000)) == (
        "Light CTL Default Set l=1 t=4000K deltaUV=0"
    )
    for get, text in (
        (M.generic_level_get, "Generic Level Get"),
        (M.generic_onpowerup_get, "OnPowerUp Get"),
        (M.generic_battery_get, "Generic Battery Get"),
        (M.generic_location_global_get, "Generic Location Global Get"),
        (M.light_lightness_default_get, "Light Lightness Default Get"),
        (M.light_lightness_range_get, "Light Lightness Range Get"),
        (M.light_ctl_default_get, "Light CTL Default Get"),
        (M.light_ctl_temperature_range_get, "Light CTL Temperature Range Get"),
    ):
        assert M.describe(get()) == text


# ----------------------------------------------------------------------------- vendor property Set (C3/C9/CF 27 05)


def test_vendor_property_set_admin_carries_the_access_byte():
    # KeyMode 6 (Gateway) on a key element — the value the gateway reads back as `D1 27 05 03 50 03 06` on air
    assert (
        M.vendor_property_set("admin", 0x5003, b"\x06")
        == h("c32705") + h("0350") + b"\x03" + b"\x06"
    )
    # the app sets DimMode with userAccess = READ (1)
    assert (
        M.vendor_property_set("admin", 0x0013, b"\x02", user_access=1)
        == h("c32705") + h("1300") + b"\x01\x02"
    )
    # unacknowledged variant: opcode 0x04
    assert M.vendor_property_set("admin", 0x1001, h("dc050000"), ack=False) == h(
        "c42705"
    ) + h("0110") + b"\x03" + h("dc050000")
    # the wind alarm as the app sends it: `C3 27 05 [09 00][03][01 FF 00 00 00 00]`
    assert M.vendor_property_set("admin", 0x0009, h("01ff00000000")) == h(
        "c32705090003"
    ) + h("01ff00000000")


def test_vendor_property_set_manufacturer_and_user_have_no_access_byte():
    assert M.vendor_property_set("manufacturer", 0x6004, h("2c01")) == h("c92705") + h(
        "0460"
    ) + h("2c01")
    assert (
        M.vendor_property_set("manufacturer", 0x6005, b"\x01", ack=False)
        == h("ca2705") + h("0560") + b"\x01"
    )
    assert (
        M.vendor_property_set("user", 0x5013, b"\x01")
        == h("cf2705") + h("1350") + b"\x01"
    )
    # User Set Unack is what keys publish for 0x5012: `D0 27 05 12 50 [counter][event]`
    assert M.vendor_property_set("user", 0x5012, bytes([3, 5]), ack=False) == h(
        "d02705"
    ) + h("1250") + bytes([3, 5])
    # an empty value is legal (write-only triggers)
    assert M.vendor_property_set("user", 0x000E, b"") == h("cf2705") + h("0e00")
    with pytest.raises(KeyError):
        M.vendor_property_set("owner", 1, b"")  # type: ignore[arg-type]


def test_describe_vendor_property_set_variants():
    assert (
        M.describe(M.vendor_property_set("admin", 0x5003, b"\x06"))
        == "LBC Admin Property Set prop 0x5003 access=3 value=06 key_mode=gateway"
    )
    assert (
        M.describe(
            M.vendor_property_set("admin", 0x5003, b"\x00", ack=False, user_access=1)
        )
        == "LBC Admin Property Set Unack prop 0x5003 access=1 value=00 key_mode=light"
    )
    assert (
        M.describe(M.vendor_property_set("manufacturer", 0x6004, h("2c01")))
        == "LBC Manufacturer Property Set prop 0x6004 value=2c01 current_brightness=300lx"
    )
    assert (
        M.describe(M.vendor_property_set("manufacturer", 0x6005, b"\x01", ack=False))
        == "LBC Manufacturer Property Set Unack prop 0x6005 value=01 presence_control_pir=1"
    )
    assert (
        M.describe(M.vendor_property_set("user", 0x5004, h("1000")))
        == "LBC User Property Set prop 0x5004 value=1000 turn_on_threshold=?1000"
    )
    assert (
        M.describe(M.vendor_property_set("user", 0x5012, bytes([3, 5]), ack=False))
        == "LBC User Property Set Unack prop 0x5012 value=0305 key_event=KeyEvent(counter=3, event='pushed')"
    )
    for kind in M.VENDOR_PROPERTY_SET_OPCODES:
        for ack in (True, False):
            assert M.describe(
                M.vendor_property_set(kind, 0x5003, b"\x05", ack=ack)
            ).startswith("LBC ")


# ----------------------------------------------------------------------------- property lists, descriptors, health, time model (probes)


def test_property_list_builders_and_describe():
    assert M.vendor_properties_get("admin") == h("c02705")
    assert M.vendor_properties_get("manufacturer") == h("c62705")
    assert M.vendor_properties_get("user") == h("cc2705")
    assert M.generic_properties_get("admin") == h("822c")
    assert M.generic_properties_get("manufacturer") == h("822a")
    assert M.generic_properties_get("user") == h("822e")
    assert M.describe(M.vendor_properties_get("user")) == "LBC User Properties Get"
    assert (
        M.describe(M.generic_properties_get("admin")) == "Generic Admin Properties Get"
    )
    # status lists: ids named from the matching catalogue, unknown ids bare, odd trailing byte ignored
    assert M.property_ids(h("0100015014")) == [0x0001, 0x5001]
    assert M.describe(h("c12705") + h("01000150ff1f")) == (
        "LBC Admin Properties Status [0001=device_lock, 5001=button_layout, 1FFF]"
    )
    assert M.describe(h("cd2705")) == "LBC User Properties Status []"
    assert (
        M.describe(h("47") + h("6a00"))
        == "Generic Admin Properties Status [006A=total_energy]"
    )
    assert M.describe(h("43") + h("72000d00")) == (
        "Generic Manufacturer Properties Status [0072=precise_total_energy, 000D=energy_since_turn_on]"
    )
    assert M.describe(h("4b")) == "Generic User Properties Status []"


def test_sensor_descriptor_builders_and_describe():
    assert M.sensor_descriptor_get() == h("8230")
    assert M.sensor_descriptor_get(0x0081) == h("82308100")
    assert M.describe(M.sensor_descriptor_get()) == "Sensor Descriptor Get"
    status = h("51") + h("52000000000300505c00ff0f01020040")
    descriptors = M.sensor_descriptors(status[1:])
    assert descriptors[0] == M.SensorDescriptor(0x0052, 0, 0, 3, 0, 0x50)
    assert descriptors[1] == M.SensorDescriptor(0x005C, 0xFFF, 0x010, 2, 0, 0x40)
    assert M.SensorDescriptor.seconds(0) is None
    assert M.SensorDescriptor.seconds(0x50) == pytest.approx(4.59, abs=0.01)
    assert M.SensorDescriptor.seconds(0x40) == 1.0
    assert M.describe(status) == (
        "Sensor Descriptor Status 0x0052 tol=+0/-0 func=3 interval=4.59s; 0x005C tol=+4095/-16 func=2 interval=1s"
    )
    # a 2-byte status = "no such sensor"
    assert M.describe(h("518100")) == "Sensor Descriptor Status 8100"
    assert M.sensor_descriptors(h("8100")) == []


def test_describe_health_and_time_model_statuses():
    assert M.describe(h("05") + h("0027058180")) == (
        "Health Fault Status test=0 company=0527 faults=[0x81 (vendor), 0x80 (vendor)]"
    )
    assert (
        M.describe(h("05") + h("002705"))
        == "Health Fault Status test=0 company=0527 faults=[none]"
    )
    assert M.describe(h("05") + h("00")) == "Health Fault Status 00"
    assert M.describe(h("04") + h("01270501")) == (
        "Health Current Status test=1 company=0527 faults=[0x01 battery low warning]"
    )
    assert M.describe(h("04") + h("012705")) == (
        "Health Current Status test=1 company=0527 faults=[none]"
    )
    assert M.health_fault_name(0x40) == "0x40 reserved"
    assert M.decode_health_fault_status(h("0027058180")) == M.HealthFaults(
        0, 0x0527, (0x81, 0x80)
    )
    with pytest.raises(ValueError, match="needs 3"):
        M.decode_health_fault_status(h("0027"))
    assert M.describe(M.health_fault_get()) == "Health Fault Get company=0527"
    assert M.describe(M.health_fault_get(0x02FF)) == "Health Fault Get company=02FF"
    assert M.describe(M.health_fault_clear()) == "Health Fault Clear company=0527"
    assert (
        M.describe(M.health_fault_clear(ack=False))
        == "Health Fault Clear Unack company=0527"
    )
    # Mesh Profile §4.3.4.1 (and ESP-IDF / Zephyr): 0x802F is the acknowledged Clear, 0x8030 the unacknowledged one
    assert M.health_fault_clear() == h("802F") + h("2705")
    assert M.health_fault_clear(ack=False) == h("8030") + h("2705")
    assert M.describe(M.health_fault_test(3)) == "Health Fault Test test=3 company=0527"
    assert M.health_fault_test(3, ack=False) == h("8033") + h("03") + h("2705")
    assert (
        M.describe(M.health_fault_test(0, ack=False))
        == "Health Fault Test Unack test=0 company=0527"
    )
    with pytest.raises(ValueError, match="test id"):
        M.health_fault_test(256)
    assert M.describe(h("8004")) == "Health Attention Get"
    assert M.describe(h("8034")) == "Health Period Get"
    assert M.describe(h("820d")) == "Default Transition Time Get"
    assert M.describe(h("823d") + h("4c400000000000")) == (
        "Time Zone Status current=+180min new=+0min change_tai=0"
    )
    assert M.describe(h("823d") + h("4c")) == "Time Zone Status 4c"
    assert M.describe(h("8240") + h("2401ff000000000000")) == (
        "TAI-UTC Delta Status current=37s new=0s change_tai=0"
    )
    assert M.describe(h("8240") + h("2401")) == "TAI-UTC Delta Status 2401"
    assert M.describe(h("823a03")) == "Time Role Status client"
    assert M.describe(h("823902")) == "Time Role Set relay"
    assert M.describe(h("823a09")) == "Time Role Status 9"
    assert M.describe(h("823a")) == "Time Role Status"
    for get, text in (
        (h("8238"), "Time Role Get"),
        (h("823b"), "Time Zone Get"),
        (h("823e"), "TAI-UTC Delta Get"),
    ):
        assert M.describe(get) == text


def test_time_role_get_and_status():
    """The app's read of a node's time role, as on air (the settings session, 0001 -> 0293 / 0174 / 01A4):
    `8238` answered by `823A 03`, "client"."""
    assert M.time_role_get() == h("8238")
    assert M.describe(M.time_role_get()) == "Time Role Get"
    status = h("823a03")
    op, _cid, params = M.decode_opcode(status)
    assert op == M.TIME_ROLE_STATUS
    assert M.decode_time_role_status(params) == "client"
    assert [M.decode_time_role_status(bytes([r])) for r in range(4)] == [
        "none",
        "authority",
        "relay",
        "client",
    ]
    for bad in (b"", h("04"), h("ff")):
        with pytest.raises(ValueError, match="not a Time Role Status"):
            M.decode_time_role_status(bad)


def test_time_get_and_time_zone_get():
    """Both Gets carry no parameters (Mesh Model §5.2.1.1, §5.2.1.5)."""
    assert M.time_get() == h("8237")
    assert M.describe(M.time_get()) == "Time Get"
    assert M.time_zone_get() == h("823b")
    assert M.describe(M.time_zone_get()) == "Time Zone Get"


def test_decode_time_status():
    """The Time Set layout (§5.2.1.3): the vector of `test_time_set_vector`, answered back as a Status."""
    status = M.decode_time_status(
        h("35925a3000") + h("80") + h("05") + h("4802") + h("4c")
    )
    assert status == M.TimeStatus(
        811_242_037,
        subsecond=128,
        uncertainty=5,
        authority=False,
        tai_utc_delta=37,
        zone_offset=180,
    )
    # 811 242 037 TAI seconds less the 37 of TAI-UTC, and half a second
    assert status.utc == M.TAI_EPOCH + timedelta(seconds=811_242_000.5)
    # the authority bit, a zone west of UTC
    other = M.decode_time_status(
        h("35925a3000") + h("00") + h("00") + h("4902") + h("29")
    )
    assert (other.authority, other.tai_utc_delta, other.zone_offset) == (True, 37, -345)
    # TAI seconds 0: no time; the 5-byte form, or the fields sent all the same
    assert M.decode_time_status(h("0000000000")) == M.TimeStatus(0)
    assert M.decode_time_status(h("0000000000") + bytes(5)).utc is None
    # a u40 past what a datetime holds
    assert (
        M.decode_time_status(h("ffffffffff") + h("0000") + h("4802") + h("40")).utc
        is None
    )
    for bad in (b"", h("35925a30")):
        with pytest.raises(ValueError, match="not a Time Status"):
            M.decode_time_status(bad)
    with pytest.raises(ValueError, match="truncated Time Status"):
        M.decode_time_status(h("35925a3000") + h("8000"))


def test_time_status_reads_back_what_time_set_sends():
    when = (M.TAI_EPOCH + timedelta(seconds=811_242_000.25)).astimezone(EEST)
    status = M.decode_time_status(M.time_set(when)[1:])
    assert status.utc == when
    assert status.zone_offset == 180


def test_decode_time_zone_status():
    """`[current][new]` in quarter hours + 64, then the TAI second of the change (§5.2.1.7)."""
    assert M.decode_time_zone_status(h("4c50") + h("0102030405")) == M.TimeZoneStatus(
        180, 240, 0x0504030201
    )
    assert M.decode_time_zone_status(h("4040") + bytes(5)) == M.TimeZoneStatus(0, 0, 0)
    for bad in (b"", h("4c50010203")):
        with pytest.raises(ValueError, match="not a Time Zone Status"):
            M.decode_time_zone_status(bad)


def test_health_attention_builders_and_describe():
    assert M.health_attention_set(10) == h("80050a")
    assert M.health_attention_set(0, ack=False) == h("800600")
    assert M.describe(M.health_attention_set(10)) == "Health Attention Set 10s"
    assert (
        M.describe(M.health_attention_set(5, ack=False))
        == "Health Attention Set Unack 5s"
    )
    assert M.describe(h("800707")) == "Health Attention Status 7s"
    assert M.describe(h("8007")) == "Health Attention Status"
    with pytest.raises(ValueError, match="attention"):
        M.health_attention_set(256)


def test_scene_store_delete_and_register():
    assert M.scene_store(5) == h("8246") + h("0500")
    assert M.scene_store(5, ack=False) == h("8247") + h("0500")
    assert M.scene_delete(5) == h("829e") + h("0500")
    assert M.scene_delete(5, ack=False) == h("829f") + h("0500")
    assert M.scene_register_get() == h("8244")
    for bad in (0, 0x10000):
        with pytest.raises(ValueError, match="scene"):
            M.scene_store(bad)
    assert M.describe(M.scene_store(5)) == "Scene Store scene=5"
    assert M.describe(M.scene_delete(5, ack=False)) == "Scene Delete Unack scene=5"
    assert M.describe(M.scene_register_get()) == "Scene Register Get"
    register = M.decode_scene_register_status(h("00 0100 0100 0500"))  # 0148 on air
    assert register == M.SceneRegister(0, 1, (1, 5))
    assert register.ok
    assert not M.decode_scene_register_status(h("01 0000")).ok  # Scene Register Full
    assert M.decode_scene_register_status(h("00 0000")).scenes == ()
    with pytest.raises(ValueError, match="needs 3"):
        M.decode_scene_register_status(h("0001"))


def test_describe_shows_an_unknown_remaining_time():
    """Steps 0x3F in a Status means "still moving, no estimate" (§3.1.3), not 63 steps."""
    assert (
        M.describe(h("82040100ff"))
        == "Generic OnOff Status present=ON target=OFF remaining=unknown"
    )
    assert M.describe(h("82103f")) == "Default Transition Time Status unknown"
    assert M.describe(h("82107f")) == "Default Transition Time Status unknown"
    assert M.describe(h("82103e")) == "Default Transition Time Status 62x100ms"
    assert M.describe(h("824eff00ffff3f")) == (
        "Light Lightness Status present=255 target=65535 remaining=unknown"
    )


def test_describe_ctl_temperature_get_and_sets():
    assert M.describe(M.light_ctl_temperature_get()) == "Light CTL Temperature Get"
    assert (
        M.describe(M.light_ctl_temperature_set(2700, tid=3))
        == "Light CTL Temperature Set t=2700K deltaUV=0 tid=3"
    )
    assert (
        M.describe(
            M.light_ctl_temperature_set(
                6500, delta_uv=-1, ack=False, tid=4, transition=0x42, delay=2
            )
        )
        == "Light CTL Temperature Set Unack t=6500K deltaUV=-1 tid=4 transition=2x1s delay=10ms"
    )
    assert M.describe(h("8264" + "8c0a" + "00")) == "?? 82648c0a00"  # cut in Delta UV


def test_describe_ctl_temperature_status_with_targets():
    """§6.3.1.14: the 9-byte form carries the target temperature, target Delta UV and remaining time."""
    assert M.describe(h("8266" + "8c0a" + "0000")) == (
        "Light CTL Temperature Status temp=2700K deltaUV=0"
    )
    assert M.describe(h("8266" + "8c0a" + "0000" + "6419" + "ffff" + "42")) == (
        "Light CTL Temperature Status temp=2700K deltaUV=0 target_t=6500 target_uv=-1 remaining=2x1s"
    )


@pytest.mark.parametrize(
    "data",
    [
        h("22"),  # Format A header cut after its first byte (was [(1, b"")])
        h(
            "0581"
        ),  # Format B header cut after the first property-id byte (was [(0x81, b"")])
        h("2000"),  # Format A: 1 raw byte announced, none present
        h("2200aa"),  # Format A: 2 raw bytes announced, one present
        h("0581000102"),  # Format B: 3 raw bytes announced, two present
        h("2000aa" + "20"),  # a second entry cut short
    ],
)
def test_sensor_values_rejects_a_truncated_entry(data: bytes):
    """A marshalled entry ending inside its header or raw value returned `(property, b"")`: the coordinator then
    published 0.0 W for a status that carried no value. §4.2.14: the Length field shall match the raw value."""
    with pytest.raises(ValueError, match="truncated sensor data at offset"):
        M.sensor_values(data)
    assert M.describe(h("52") + data).startswith("?? ")


def test_sensor_values_accepts_every_well_formed_length():
    assert M.sensor_values(h("2000aa")) == [(1, b"\xaa")]
    assert M.sensor_values(h("2200aabb")) == [(1, b"\xaa\xbb")]
    assert M.sensor_values(h("058100" + "010203")) == [(0x81, b"\x01\x02\x03")]
    assert M.sensor_values(h("ff8100")) == [
        (0x81, b"")
    ]  # Format B length 0x7F+1: zero bytes
    assert M.sensor_values(b"") == []


def test_describe_never_raises_for_an_out_of_range_date_utc():
    """Generic Manufacturer Property Status for Date UTC 0x000C with all ones: `date + timedelta` raises
    OverflowError past the year 9999, which escaped `describe()` ("never raises") into the RX debug log and the
    CLI's `prop get`. Now `?` like any other undecodable value."""
    head = "Generic Manufacturer Property Status prop 0x000C access=1 value="
    assert (
        M.describe(h("460c0001ffffff")) == head + "ffffff date_of_manufacture=?ffffff"
    )
    assert (
        M.describe(h("460c0001394b00"))
        == head + "394b00 date_of_manufacture=2022-09-22"
    )
    assert (
        M.describe(h("460c0001000000")) == head + "000000 date_of_manufacture=unknown"
    )


def test_describe_time_status_far_future_does_not_raise():
    """MSG-02: TAI Seconds is a u40 (about 34 800 years), past what a datetime holds: an authenticated Time Status
    with a garbage clock is described, not raised out of `describe` (it used to kill the sniffer)."""
    pdu = (
        bytes([M.TIME_STATUS])
        + b"\xff" * 5
        + b"\x00\x00"
        + ((37 + 255) << 1).to_bytes(2, "little")
        + b"\x40"
    )
    text = M.describe(pdu)
    assert text.startswith("Time Status")
    assert "out of range" in text


@pytest.mark.parametrize(
    "build",
    [
        lambda: M.light_lightness_default_set(0x10000),
        lambda: M.light_ctl_default_set(1, 3000, 0x8000),
        lambda: M.vendor_property_set("admin", 0x10000, b""),
        lambda: M.vendor_property_set("admin", 1, b"", user_access=4),
        lambda: M.generic_property_get("user", -1),
        lambda: M.generic_property_set("user", 0x10000),
        lambda: M.vendor_property_get("admin", 0x10000),
        lambda: M.vendor_property_status("user", 0x10000, b""),
        lambda: M.vendor_property_status("user", 1, b"", user_access=4),
        lambda: M.sensor_get(0x10000),
        lambda: M.sensor_descriptor_get(0x10000),
        lambda: M.health_fault_get(0x10000),
        lambda: M.health_fault_clear(0x10000),
        lambda: M.health_fault_test(0, 0x10000),
    ],
)
def test_builders_reject_out_of_range_with_value_error(build):
    """MSG-03: an out-of-range argument is a ValueError naming the field, like every other builder, not a bare
    OverflowError; `vendor_property_set` checks its user access like the Status builder does."""
    with pytest.raises(
        ValueError, match=r"property id|company id|lightness|delta UV|user access"
    ):
        build()


@pytest.mark.parametrize(
    ("raw", "level"), [(0, 0), (100, 100), (0x65, None), (0xFE, None), (0xFF, None)]
)
def test_battery_level_above_100_is_unknown(raw: int, level: int | None):
    # 0x00-0x64 is a percentage, 0xFF unknown and 0x65-0xFE prohibited (Mesh Model §3.1.5): never "254 %"
    status = M.battery_status(bytes([raw]) + h("ffffff") + h("ffffff") + h("ff"))
    assert status["level"] == level


def test_battery_status_short_payload_is_value_error():
    """MSG-04: a truncated Battery Status raises the ValueError every other public decoder raises."""
    for p in (b"", bytes(7)):
        with pytest.raises(ValueError, match="8 bytes"):
            M.battery_status(p)


@pytest.mark.parametrize("wire", ["824e05", "8260050001", "8208ff", "824501", "5e00"])
def test_describe_truncated_legacy_status_falls_back_to_hex(wire: str):
    """MSG-05: a truncated status is shown as hex, not as a plausible reading the node never sent."""
    assert M.describe(bytes.fromhex(wire)) == f"?? {wire}"


def test_lbc_opcode_tables_are_consistent():
    """MSG-08: the LBC opcode tables are derived from one table; their public values stay the same."""
    assert M.VENDOR_PROPERTY_SET_OPCODES == {
        "admin": (3, 4),
        "manufacturer": (9, 10),
        "user": (15, 16),
    }
    assert M.VENDOR_PROPERTY_LIST_OPCODES == {
        "admin": (0, 1),
        "manufacturer": (6, 7),
        "user": (0x0C, 0x0D),
    }
    assert M.VENDOR_PROPERTY_STATUS_OPCODES == {
        "admin": 5,
        "manufacturer": 0x0B,
        "user": 0x11,
    }
    assert {3, 4, 5, 0x0B, 0x11} == M._VENDOR_PROP_WITH_ACCESS
    assert {0x02, 0x08, 0x0E} == M._VENDOR_PROP_GET
    assert {0x09, 0x0A, 0x0F, 0x10} == M._VENDOR_PROP_VALUE
    assert M.SIG_PROP_GET == {
        0x822B: "Generic Manufacturer Property Get",
        0x822D: "Generic Admin Property Get",
        0x822F: "Generic User Property Get",
    }
    assert M.JUNG_CID is V.JUNG_CID


def test_set_shown_by_reads_the_state_a_load_set_asks_for():
    """review-4 D32: a Status shows an acknowledged load Set's state in its present or (with a remaining time) its
    target field, within the load's own step; at rest with another state, or too short to tell, it does not."""
    on = M.set_shown_by(M.generic_onoff_set(True, transition=0))
    assert on is not None
    assert on(h("01"))
    assert on(h("00" + "01" + "0a"))  # switching on: the target
    assert not on(h("00"))
    assert not on(h("0000"))  # a target without its remaining time is no target
    assert not on(b"")
    ctl = M.set_shown_by(M.light_ctl_set(0x8000, 3000))
    assert ctl is not None
    assert ctl(h("0080" + "1c0c"))  # 3100 K: within the step
    assert not ctl(h("0080" + "200d"))  # 3360 K
    assert not ctl(h("ff7f"))  # short of its temperature
    dim = M.set_shown_by(M.light_lightness_set(0x8000))
    assert dim is not None
    assert dim(h("707d"))  # one step under it
    assert not dim(h("6f7d"))


@pytest.mark.parametrize(
    "access",
    [
        M.generic_onoff_set(True, ack=False),  # nothing answers it
        M.generic_onoff_get(),
        M.scene_recall(1),
        M.generic_delta_set(1),  # no absolute state to compare
        h("8202"),  # an OnOff Set without its state
        b"",
        h("82"),  # a truncated opcode
    ],
    ids=["unack", "get", "scene", "delta", "short", "empty", "truncated"],
)
def test_set_shown_by_has_nothing_to_compare_for_other_messages(access: bytes):
    assert M.set_shown_by(access) is None
