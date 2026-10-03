"""GSS "value is not known" / "value is not valid" markers in the sensor and counter codecs (B-2 / P2-15)."""

from __future__ import annotations

import pytest

from jhmesh import properties as P


@pytest.mark.parametrize(
    ("pid", "raw", "value"),
    [
        (0x0081, b"\xff\xff\xff", None),  # Power: 0xFFFFFF not known
        (0x0081, b"\xfe\xff\xff", 1677721.4),  # ... the value below it is a value
        (0x0052, b"\xff\xff\xff", None),
        (0x005C, b"\xff\xff", None),  # Electric Current: 0xFFFF not known
        (0x005C, b"\x19\x02", 5.37),
        (0x0057, b"\xff\xff", None),
        (0x005D, b"\xff\xff", None),  # Voltage: 0xFFFF not known
        (0x005D, b"\xe6\x00", 230.0),
        (0x004F, b"\x7f", None),  # a signed one keeps its own marker ...
        (0x004F, b"\xff", -0.5),  # ... and all-ones is a value (-1 x 0.5 °C)
    ],
)
def test_scaled_sensor_markers(pid: int, raw: bytes, value: float | None) -> None:
    assert P.SIG_PROPERTIES[pid].codec.decode(raw) == value


def test_scaled_unknown_marker_round_trips() -> None:
    """None encodes as the marker only where the catalogue names one; the implicit all-ones is a decode rule."""
    assert P.SIG_PROPERTIES[0x004F].codec.encode(None) == b"\x7f"
    for codec in (P.SIG_PROPERTIES[0x0081].codec, P.Scaled(2, 0.01, signed=True)):
        with pytest.raises(ValueError, match="a value is required"):
            codec.encode(None)
    assert P.Scaled(2, 0.01, signed=True).decode(b"\xff\xff") == -0.01
    assert P.Scaled(2, 0.01, unknown=0x1234).decode(b"\xff\xff") == 655.35


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        (b"\xff\xff\xff", None),  # Energy / Time Hour 24 (uint24): not known
        (b"\xfe\xff\xff", None),  # Energy: not valid
        (b"\xfd\xff\xff", 0xFFFFFD),
        (b"\xff\xff\xff\xff", None),  # Energy32: not known
        (b"\xfe\xff\xff\xff", None),  # Energy32: not valid
        (b"\xfd\xff\xff\xff", 0xFFFFFFFD),
        (b"\x39\x30\x00", 12345),
        (b"\x00\x00\x00\x00", 0),
    ],
)
def test_counter_markers_of_the_received_length(raw: bytes, value: int | None) -> None:
    for pid in (0x006A, 0x006D, 0x000D, 0x0072):
        assert P.SIG_PROPERTIES[pid].codec.decode(raw) == value
    assert P.Counter(4).encode(0) == b"\x00\x00\x00\x00"
