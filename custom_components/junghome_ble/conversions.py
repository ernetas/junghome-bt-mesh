"""Pure Generic Level conversions of the thermostat set-point and the blind position.

They live here rather than in `climate.py` / `cover.py` so the configurator, the actions and the schedules can use
them without importing a platform; the platforms re-export them under their old names.
"""

from __future__ import annotations

from .const import (
    CLIMATE_MAX_TEMP,
    CLIMATE_MIN_TEMP,
    COVER_LEVEL_CLOSED,
    COVER_LEVEL_OPEN,
)

LEVEL_MIN, LEVEL_MAX = -32768, 32767


def temperature_to_level(temperature: float) -> int:
    """Map a set-point in °C to the Generic Level the app sends: `pct = round((t - 5) / 25 * 100)`, `level = -32768 + pct / 100 * 65535`."""
    span = CLIMATE_MAX_TEMP - CLIMATE_MIN_TEMP
    pct = max(0, min(100, round((temperature - CLIMATE_MIN_TEMP) / span * 100)))
    return max(LEVEL_MIN, min(LEVEL_MAX, round(LEVEL_MIN + pct / 100 * 65535)))


def level_to_temperature(level: int) -> float:
    """Map a Generic Level back to °C: `pct = round((level + 32768) * 100 / 65535)`, `t = 5 + 25 * pct / 100` (0.25 °C steps)."""
    pct = max(0, min(100, round((level - LEVEL_MIN) * 100 / 65535)))
    return CLIMATE_MIN_TEMP + (CLIMATE_MAX_TEMP - CLIMATE_MIN_TEMP) * pct / 100


def level_to_closedness(level: int) -> int:
    """Return the JUNG percent (0 open .. 100 closed) of a Generic Level, the app's rounding (`control-and-state.md` §0)."""
    return max(0, min(100, round((level + 32768) * 100 / 65535)))


def closedness_to_level(pct: int) -> int:
    """Return the Generic Level of a JUNG percent: -32768 for 0 %, 32767 for 100 %."""
    return max(
        COVER_LEVEL_OPEN, min(COVER_LEVEL_CLOSED, round(-32768 + pct / 100 * 65535))
    )
