"""jhmesh/vendor_models.py: JH Scheduler and Scene Action Setup, against the bytes read off the nodes."""

from __future__ import annotations

import pytest

from jhmesh import messages as M
from jhmesh import vendor_models as V
from jhmesh.pdu import encode_opcode

h = bytes.fromhex
STATUS = encode_opcode(V.SCENE_ACTION_SETUP_STATUS, V.JUNG_CID)
SCHED = encode_opcode(V.JH_SCHEDULER_STATUS, V.JUNG_CID)


# ----------------------------------------------------------------------------- actions


@pytest.mark.parametrize(
    ("action", "wire", "text"),
    [
        (V.Action(V.ACTION_SWITCH, on=True), "010100000000", "switch on"),
        (V.Action(V.ACTION_SWITCH, on=False), "010000000000", "switch off"),
        (
            V.Action(V.ACTION_LIGHTNESS, lightness=0xFFFF),
            "02ffff000000",
            "lightness 100%",
        ),
        (
            V.Action(V.ACTION_LIGHTNESS_CT, lightness=0xFFFF, temperature_k=2000),
            "03ffffd00700",
            "lightness 100% 2000K",
        ),
        (
            V.Action(V.ACTION_LIGHTNESS_CT, lightness=0, temperature_k=2000),
            "030000d00700",
            "lightness 0% 2000K",
        ),
        (
            V.Action(V.ACTION_BLINDS, blind=0, slat=-32768),
            "040000008000",
            "blinds 50% slats 0%",
        ),
        (
            V.Action(V.ACTION_TEMPERATURE, temperature_c=21.5),
            "056608000000",
            "target 21.5°C",
        ),
        (V.NO_ACTION, "000000000000", "no action"),
    ],
)
def test_action_round_trip(action: V.Action, wire: str, text: str):
    assert action.encode() == h(wire)
    assert V.decode_action(h(wire)) == action
    assert action.describe() == text


def test_action_decode_pads_short_payloads_and_keeps_unknown_codes():
    assert V.decode_action(h("01")) == V.Action(V.ACTION_SWITCH, on=False)
    assert V.decode_action(h("0101")) == V.Action(V.ACTION_SWITCH, on=True)
    assert V.decode_action(h("07aabb")) == V.Action(7)
    assert V.Action(7).describe() == "action 7"
    with pytest.raises(ValueError, match="action code"):
        V.decode_action(b"")


def test_action_encode_validates():
    with pytest.raises(ValueError, match="lightness"):
        V.Action(V.ACTION_LIGHTNESS).encode()
    with pytest.raises(ValueError, match="lightness"):
        V.Action(V.ACTION_LIGHTNESS, lightness=0x10000).encode()
    with pytest.raises(ValueError, match="colour temperature"):
        V.Action(V.ACTION_LIGHTNESS_CT, lightness=1).encode()
    with pytest.raises(ValueError, match="slat"):
        V.Action(V.ACTION_BLINDS, blind=0).encode()
    with pytest.raises(ValueError, match="temperature_c"):
        V.Action(V.ACTION_TEMPERATURE).encode()
    with pytest.raises(ValueError, match="unknown action code"):
        V.Action(9).encode()


# ----------------------------------------------------------------------------- Scene Action Setup


def test_scene_action_reply_to():
    """MSG-06: a Scene Action Setup Status answers a Get only for the scene it names (FF FF: "no such scene")."""
    f = V.scene_action_reply_to(9)
    assert f(bytes.fromhex("09000101000000"))
    assert not f(bytes.fromhex("08000101000000"))
    assert f(bytes.fromhex("ffff"))
    assert not f(bytes.fromhex("0000080000"))  # the list is no answer to a scene's Get
    assert not f(b"\x09")  # too short to name a scene
    assert V.scene_action_reply_to(0)(bytes.fromhex("000008000000"))
    assert not V.scene_action_reply_to(0)(
        bytes.fromhex("ffff")
    )  # nor "no such scene" to the list's


def test_scene_action_get_and_set_builders():
    assert V.scene_action_get() == h("d62705") + h("00000000")
    assert V.scene_action_get(8) == h("d62705") + h("08000000")
    assert V.scene_action_set(8, V.Action(V.ACTION_SWITCH, on=True)) == h("d82705") + h(
        "0800010100000000"
    )
    assert V.scene_action_set(8) == h("d82705") + h("0800")  # remove
    with pytest.raises(ValueError, match="scene"):
        V.scene_action_get(0x10000)
    with pytest.raises(ValueError, match="scene"):
        V.scene_action_set(0)


@pytest.mark.parametrize(
    ("wire", "status", "text"),
    [
        # 0150 (kitchen table light) listing its scenes
        (
            "0000080009000a000b000000",
            V.SceneActionStatus(0, scenes=(8, 9, 10, 11)),
            "scenes=[8, 9, 10, 11]",
        ),
        (
            "00000000",
            V.SceneActionStatus(0, scenes=()),
            "scenes=[]",
        ),  # DALI insert, no scenes
        ("0000", V.SceneActionStatus(0, scenes=()), "scenes=[]"),
        (
            "0800010100000000",
            V.SceneActionStatus(8, V.Action(V.ACTION_SWITCH, on=True)),
            "scene 8: switch on",
        ),
        (
            "080003ffffd00700",  # dimmer 016A in scene 8
            V.SceneActionStatus(
                8, V.Action(V.ACTION_LIGHTNESS_CT, lightness=0xFFFF, temperature_k=2000)
            ),
            "scene 8: lightness 100% 2000K",
        ),
        ("ffff", V.SceneActionStatus(0xFFFF), "scene 65535: no action"),
        ("0100", V.SceneActionStatus(1), "scene 1: no action"),
    ],
)
def test_decode_scene_action_status(wire: str, status: V.SceneActionStatus, text: str):
    decoded = V.decode_scene_action_status(h(wire))
    assert decoded == status
    assert decoded.describe() == text
    assert M.describe(STATUS + h(wire)) == f"Scene Action Setup Status {text}"


def test_scene_action_status_needs_the_scene_number():
    with pytest.raises(ValueError, match="2 bytes"):
        V.decode_scene_action_status(h("01"))
    assert M.describe(STATUS + h("01")) == "Scene Action Setup Status"
    assert M.describe(STATUS) == "Scene Action Setup Status"


def test_describe_scene_action_requests():
    assert M.describe(V.scene_action_get()) == "Scene Action Setup Get list"
    assert M.describe(V.scene_action_get(5)) == "Scene Action Setup Get scene 5"
    assert M.describe(h("d62705") + h("01")) == "Scene Action Setup Get"
    assert (
        M.describe(V.scene_action_set(5, V.Action(V.ACTION_SWITCH, on=False)))
        == "Scene Action Setup Set scene 5: switch off"
    )
    assert M.describe(V.scene_action_set(5)) == "Scene Action Setup Set scene 5: remove"


# ----------------------------------------------------------------------------- JH Scheduler


def test_scheduler_get_builders():
    assert V.scheduler_get(0) == h("d22705") + h("00")
    assert V.scheduler_get(3, V.SUB_ACTION) == h("d22705") + h("13")
    assert V.scheduler_get(15, V.SUB_EFFECTIVE_TIME) == h("d22705") + h("2f")
    assert V.scheduler_list_get() == h("d22705") + h("f000")
    assert V.scheduler_list_get(1) == h("d22705") + h("f001")
    with pytest.raises(ValueError, match="slot"):
        V.scheduler_get(16)
    with pytest.raises(ValueError, match="sub-command"):
        V.scheduler_get(0, 16)
    with pytest.raises(ValueError, match="central schedule id"):
        V.scheduler_list_get(256)


@pytest.mark.parametrize(
    ("wire", "text"),
    [
        # every status an empty slot returns (0148 / 0172)
        (
            "0000000000000000",
            "slot 0 schedule: available no days 00:00-00:00 offset +0min",
        ),
        ("10000000000000", "slot 0 action: no action"),
        (
            "2000000000000000",
            "slot 0 effective time: available no days at 00:00 offset +0min",
        ),
        (
            "0f00000000000000",
            "slot 15 schedule: available no days 00:00-00:00 offset +0min",
        ),
        ("f00000000000", "slots (central id 0): 16 available"),
        ("f00100000000", "slots (central id 1): 16 available"),
        ("30", "slot 0 sub 3: nothing"),  # an undefined sub-command: header echoed
        ("05", "slot 5 schedule: nothing"),  # too short for the sub-command
        ("f000", "slot 0 list: nothing"),
    ],
)
def test_decode_empty_scheduler_statuses(wire: str, text: str):
    assert V.decode_scheduler_status(h(wire)).describe() == text
    assert M.describe(SCHED + h(wire)) == f"JH Scheduler Status {text}"


def test_scheduler_schedule_round_trip_and_decode():
    schedule = V.Schedule(
        3,
        7,  # sunset_active
        frozenset({"mon", "wed", "fri"}),
        (6, 30),
        (22, 15),
        -15,
    )
    wire = schedule.encode()
    assert wire[:3] == h("d42705")
    status = V.decode_scheduler_status(wire[3:])
    assert status.index == 3
    assert status.sub == V.SUB_SCHEDULE
    assert status.schedule == schedule
    assert status.schedule is not None
    assert status.schedule.type_name == "sunset_active"
    assert (
        status.describe()
        == "slot 3 schedule: sunset_active mon,wed,fri 06:30-22:15 offset -15min"
    )
    assert M.describe(wire) == (
        "JH Scheduler Set slot 3 schedule: sunset_active mon,wed,fri 06:30-22:15 offset -15min"
    )
    # the documented bit positions (vendor-models.md §4.1.1): type in the low nibble of byte 1, days in byte 2
    params = wire[3:]
    assert params[0] == 0x03
    assert params[1] & 0x0F == 7
    assert params[2] == 0b0010101
    assert params[6] == (-15) & 0xFF
    positive = V.Schedule(0, 3, frozenset({"sun"}), (7, 0), (7, 0), 120)
    assert V.decode_scheduler_status(positive.encode()[3:]).schedule == positive
    assert V.days_mask({"sun"}) == 0x40
    with pytest.raises(ValueError, match="unknown day"):
        V.days_mask({"fun"})
    with pytest.raises(ValueError, match="offset"):
        V.Schedule(0, 3, frozenset(), (0, 0), (0, 0), 200).encode()
    with pytest.raises(ValueError, match="unknown schedule type"):
        V.Schedule(0, 9, frozenset(), (0, 0), (0, 0), 0).encode()
    for bad in ((32, 0), (25, 0), (24, 0), (0, 61), (0, 60), (-1, 0), (0, -1)):
        with pytest.raises(ValueError, match="is not a time of day"):
            V.Schedule(
                0, 3, frozenset(), bad, (0, 0), 0
            ).encode()  # 25:61 fits the 5-/6-bit fields
        with pytest.raises(ValueError, match=r"not-after .* is not a time of day"):
            V.Schedule(0, 3, frozenset(), (0, 0), bad, 0).encode()
    assert V.Schedule(0, 3, frozenset(), (23, 59), (0, 0), 0).encode()
    # an open astro bound is hour 31; it survives the round trip
    open_window = V.Schedule(1, 7, frozenset({"mon"}), V.UNSET_TIME, V.UNSET_TIME, -15)
    assert V.decode_scheduler_status(open_window.encode()[3:]).schedule == open_window
    assert V.Schedule(0, 8, frozenset(), (0, 0), (0, 0), 0).type_name == "type 8"


def test_scheduler_action_and_effective_time():
    wire = V.scheduler_action_set(2, V.Action(V.ACTION_LIGHTNESS, lightness=0x8000))
    assert wire == h("d42705") + h("12") + h("020080000000")
    assert M.describe(wire) == "JH Scheduler Set slot 2 action: lightness 50%"
    status = V.decode_scheduler_status(wire[3:])
    assert status.action == V.Action(V.ACTION_LIGHTNESS, lightness=0x8000)
    # an effective time as the node would compute it: sunrise_active, tue+thu, 05:42, +10 min
    params = h("22") + bytes([5]) + bytes([0b0001010])
    params += (42 | (5 << 6)).to_bytes(2, "little") + h("00") + h("0a") + h("00")
    status = V.decode_scheduler_status(params)
    assert status.effective == V.EffectiveTime(
        2, 5, frozenset({"tue", "thu"}), (5, 42), 10
    )
    assert (
        status.describe()
        == "slot 2 effective time: sunrise_active tue,thu at 05:42 offset +10min"
    )
    assert V.scheduler_type_set(4, 0) == h("d42705") + h("0400")
    assert (
        M.describe(V.scheduler_type_set(4, 0))
        == "JH Scheduler Set slot 4 schedule type available"
    )
    with pytest.raises(ValueError, match="unknown schedule type"):
        V.scheduler_type_set(4, 9)
    with pytest.raises(ValueError, match="header byte"):
        V.decode_scheduler_status(b"")


def test_scheduler_list_decodes_two_bit_slots():
    # slot 0 active (3), slot 1 inactive (2), slot 4 matched (1), the rest available
    params = h("f007") + bytes([0b00001011, 0b00000001, 0, 0])
    status = V.decode_scheduler_status(params)
    assert status.slots == (3, 2, 0, 0, 1, *([0] * 11))
    assert status.central_schedule_id == 7
    assert (
        status.describe()
        == "slots (central id 7): 1 active, 1 inactive, 13 available, 1 central_schedule_id_matched"
    )


def test_describe_scheduler_requests():
    assert M.describe(V.scheduler_get(3)) == "JH Scheduler Get slot 3 schedule"
    assert (
        M.describe(V.scheduler_get(3, V.SUB_EFFECTIVE_TIME))
        == "JH Scheduler Get slot 3 effective time"
    )
    assert (
        M.describe(V.scheduler_list_get(2)) == "JH Scheduler Get slots (central id 2)"
    )
    assert M.describe(h("d22705") + h("f0")) == "JH Scheduler Get slots"
    assert M.describe(h("d22705") + h("37")) == "JH Scheduler Get slot 7 sub 3"
    assert M.describe(h("d22705")) == "JH Scheduler Get"
    assert M.describe(h("d42705")) == "JH Scheduler Set"
    assert V.describe_vendor_model(0x00, b"") is None


def test_bit_writer_refuses_a_value_wider_than_its_field():
    """`Schedule.encode` validates every field before packing, so the packer's own guard is exercised here."""
    with pytest.raises(ValueError, match="days 128 does not fit 7 bits"):
        V._Writer().add(128, 7, "days")
    with pytest.raises(ValueError, match="pad -1 does not fit 1 bits"):
        V._Writer().add(-1, 1, "pad")
    assert V._Writer().add(0x5, 4, "a").add(0x3, 2, "b").value == 0x35
