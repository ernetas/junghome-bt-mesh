"""The integration's brand images: what Home Assistant (2026.3+) and the HACS `brands` validator read from `brand/`."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

BRAND = Path(__file__).parent.parent / "custom_components" / "junghome_ble" / "brand"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
RGBA = 6  # IHDR colour type: truecolour with alpha (the brands spec asks for a transparent background)


@pytest.mark.parametrize(
    ("name", "size"),
    [
        ("icon.png", 256),
        ("icon@2x.png", 512),
        ("dark_icon.png", 256),
        ("dark_icon@2x.png", 512),
    ],
)
def test_brand_icon_is_a_square_transparent_png_of_the_spec_size(
    name: str, size: int
) -> None:
    head = (BRAND / name).read_bytes()[:26]
    assert head[:8] == PNG_SIGNATURE
    assert head[12:16] == b"IHDR"
    assert struct.unpack(">II", head[16:24]) == (size, size)
    assert head[25] == RGBA


def test_brand_holds_only_names_home_assistant_reads() -> None:
    """A misspelt file would be ignored silently: every file is one of the names the brands proxy serves."""
    known = {
        f"{dark}{kind}{scale}.png"
        for dark in ("", "dark_")
        for kind in ("icon", "logo")
        for scale in ("", "@2x")
    }
    assert {p.name for p in BRAND.iterdir()} <= known
