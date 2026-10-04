"""The dimming entity actions (`start_dim` / `stop_dim` / `step_dim`) of the light platform's dimmable lights.

They send one command to a dimmer and write nothing; the light entity does the work (`light.JungHomeLight`). The
handlers live here, not in `light.py`, so `services.py` registers them without importing a platform module: a light
is told apart by what it can do (`Dimmable`), not by its class (review-4 A4-11).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from homeassistant.exceptions import ServiceValidationError

from custom_components.junghome_ble.const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import ServiceCall
    from homeassistant.helpers.entity import Entity

ATTR_DIRECTION, ATTR_SPEED, ATTR_STEP = "direction", "speed", "step"
DIM_DIRECTIONS = ("up", "down")


@runtime_checkable
class Dimmable(Protocol):
    """A light the dimming actions can target: `light.JungHomeLight` (*All lights* is none)."""

    @property
    def dimmable(self) -> bool:
        """Whether the light's element hosts the Generic Level server the dimming actions talk to."""

    async def async_start_dim(self, direction: str, speed: int) -> None:
        """Start dimming `up` or `down` at `speed` % of the range per second."""

    async def async_stop_dim(self) -> None:
        """Stop dimming."""

    async def async_step_dim(self, step: int) -> None:
        """Dim by `step` % of the range."""


def _dimmable(entity: Entity) -> Dimmable:
    """Return the light a dimming action targets; *All lights* and a switched light cannot be dimmed this way."""
    if isinstance(entity, Dimmable) and entity.dimmable:
        return entity
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="dim_not_dimmable",
        translation_placeholders={"entity": entity.entity_id},
    )


async def async_start_dim(entity: Entity, call: ServiceCall) -> None:
    """`junghome_ble.start_dim` on one light entity."""
    await _dimmable(entity).async_start_dim(
        call.data[ATTR_DIRECTION], call.data[ATTR_SPEED]
    )


async def async_stop_dim(entity: Entity, call: ServiceCall) -> None:
    """`junghome_ble.stop_dim` on one light entity."""
    await _dimmable(entity).async_stop_dim()


async def async_step_dim(entity: Entity, call: ServiceCall) -> None:
    """`junghome_ble.step_dim` on one light entity."""
    await _dimmable(entity).async_step_dim(call.data[ATTR_STEP])
