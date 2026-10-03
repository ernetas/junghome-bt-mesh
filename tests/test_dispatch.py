"""`dispatch.chain_status_handler`: consumers chained onto one opcode run in registration order."""

from __future__ import annotations

from unittest.mock import patch

from custom_components.junghome_ble import (
    binary_sensor,
    climate,
    conversions,
    cover,
    dispatch,
)
from custom_components.junghome_ble.coordinator import STATUS_HANDLERS
from custom_components.junghome_ble.jhmesh import messages as M


def test_handlers_chained_twice_onto_one_opcode_run_earliest_first() -> None:
    """The way the detectors and the thermostat both chain onto Sensor Status behind the hub's own handler."""
    calls: list[str] = []
    with patch.dict(STATUS_HANDLERS):
        STATUS_HANDLERS[None, 0x7FFF] = lambda hub, m, p: calls.append("hub")
        first = dispatch.chain_status_handler(0x7FFF)(
            lambda hub, m, p: calls.append("first")
        )
        dispatch.chain_status_handler(0x7FFF)(lambda hub, m, p: calls.append("second"))
        # the decorator hands back the handler itself; the table holds the chain
        assert STATUS_HANDLERS[None, 0x7FFF] is not first
        STATUS_HANDLERS[None, 0x7FFF](None, None, b"")  # type: ignore[arg-type]
    assert calls == ["hub", "first", "second"]
    assert (None, 0x7FFF) not in STATUS_HANDLERS


def test_the_platforms_keep_the_old_names() -> None:
    """`binary_sensor.chain_status_handler` and the climate / cover conversions are the moved ones."""
    assert binary_sensor.chain_status_handler is dispatch.chain_status_handler
    assert binary_sensor.STATUS_HANDLERS is STATUS_HANDLERS
    assert not hasattr(climate, "_after")
    assert climate.temperature_to_level is conversions.temperature_to_level
    assert climate.level_to_temperature is conversions.level_to_temperature
    assert cover.closedness_to_level is conversions.closedness_to_level
    assert cover.level_to_closedness is conversions.level_to_closedness
    assert (None, M.SENSOR_STATUS) in STATUS_HANDLERS
