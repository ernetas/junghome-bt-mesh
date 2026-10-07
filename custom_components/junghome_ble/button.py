"""Buttons: device triggers (the blind reference run), Identify / Clear faults per mains node, Reset consumption per meter.

Identify sends a Health Attention Set (a standard Bluetooth Mesh message the JUNG app only uses while provisioning):
the node's LED blinks for IDENTIFY_SECONDS (verified on a push-button, `docs/hidden-features.md` §10). Clear faults
sends a Health Fault Clear and reads the register back, so the node's *Fault* binary sensor (`binary_sensor.py`)
shows what registers anew. Both show under the device people look at for the node — a push-button's keys, a
socket (`entity.node_unit_device_info`); a node with nothing visible (a mini actuator in a junction box, the
gateway) keeps them on the node device. A battery node gets neither: it sleeps and would not answer
(`entity.health_nodes`).

Reset consumption zeroes the socket's resettable energy total (`0x006A`) and power-on hours (`0x006D`), as the app's
"reset consumption" does; the lifetime total behind the *Energy* sensor (`0x0072`) cannot be reset. Any other
metered load (the energy puck's output, `jhmesh.devices.meter_element`) has one too, which zeroes `0x006A` alone: it
keeps no power-on hours (unverified on air). Like the counters it zeroes (`sensor.py`), it is a diagnostic entity,
off by default.

*Fetch export from gateway*, on the gateway node's device of an entry set up from the gateway, asks the gateway for its
export now and takes it over when the app changed something since (`app_follow.AppFollower`): what the
entry otherwise does a few minutes after the phone was heard on the mesh, and every six hours. Unverified on air.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .config_entities import PropertyEntity, config_targets
from .const import DOMAIN
from .entity import (
    JungHomeEntity,
    async_setup_platform,
    health_nodes,
    metered_device_info,
    node_device_info,
    node_unit_device_info,
)
from .errors import mesh_errors
from .gateway_status import gateway_polls
from .hub.energy import CounterNotReset

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import JungHomeConfigEntry
    from .coordinator import JungHomeHub
    from .jhmesh.cdb import Node
    from .jhmesh.devices import MeteredLoad

PARALLEL_UPDATES = (
    0  # push-based; the property reader serialises the mesh exchanges per element
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the entities of `build_entities`, kept with the hub to follow a new export in place (`model_update`)."""
    async_setup_platform(entry.runtime_data, "button", build_entities, add_entities)


def build_entities(hub: JungHomeHub) -> list[ButtonEntity]:
    """Return the trigger-property buttons, Identify / Clear faults per mains node, Reset consumption per metered load."""
    entities: list[ButtonEntity] = [
        JungHomePropertyButton(hub, target) for target in config_targets(hub, "button")
    ]
    for node in health_nodes(hub):
        entities.append(JungHomeIdentifyButton(hub, node))
        entities.append(JungHomeClearFaultsButton(hub, node))
    entities += [
        JungHomeResetConsumptionButton(hub, load) for load in hub.devices.metered
    ]
    if hub.follows_gateway and (polls := gateway_polls(hub.hass, hub)) is not None:
        entities.append(JungHomeFetchExportButton(hub, polls.node))
    return entities


class JungHomeIdentifyButton(JungHomeEntity, ButtonEntity):
    """Make the node blink its LED (Health Attention Set)."""

    _attr_device_class = ButtonDeviceClass.IDENTIFY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "identify"

    def __init__(self, hub: JungHomeHub, node: Node) -> None:
        """Bind to the node's primary element, under the device whose LED it blinks."""
        super().__init__(
            hub,
            node.unicast,
            f"node:{node.uuid.lower()}-identify",
            node_unit_device_info(hub, node),
        )
        self.node = node

    async def async_press(self) -> None:
        """Ask the node for attention; a node that does not answer is reported.

        Its own text: the generic `no_answer` promises a re-check with the next connection, which state-changing
        commands get and a Health Attention Set does not.
        """
        with mesh_errors(timeout_key="identify_no_answer"):
            await self.hub.identify(self.node)


class JungHomeClearFaultsButton(JungHomeEntity, ButtonEntity):
    """Forget the node's registered Health faults (Health Fault Clear), then read the register back."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "clear_faults"

    def __init__(self, hub: JungHomeHub, node: Node) -> None:
        """Bind to the node's primary element, next to its Fault entity."""
        super().__init__(
            hub,
            node.unicast,
            f"node:{node.uuid.lower()}-clear-faults",
            node_unit_device_info(hub, node),
        )
        self.node = node

    async def async_press(self) -> None:
        """Clear the register; a node that does not answer the read-back is reported."""
        with mesh_errors(timeout_key="no_answer"):
            await self.hub.clear_faults(self.node)


class JungHomeResetConsumptionButton(JungHomeEntity, ButtonEntity):
    """Zero a metered load's resettable energy total, and a socket's power-on hours (an Admin Property Set each)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "reset_consumption"

    def __init__(self, hub: JungHomeHub, load: MeteredLoad) -> None:
        """Bind to the load's main element, next to its energy sensors."""
        super().__init__(
            hub,
            load.address,
            f"{load.unique_id}-reset_consumption",
            metered_device_info(hub, load),
        )
        self.load = load

    async def async_press(self) -> None:
        """Reset the counters; one the load keeps counting, or does not answer for, is reported."""
        try:
            with mesh_errors(timeout_key="no_answer"):
                await self.hub.reset_consumption(self.load)
        except CounterNotReset as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="reset_consumption_refused",
                translation_placeholders={"property": f"0x{err.pid:04X}"},
            ) from err


class JungHomeFetchExportButton(ButtonEntity):
    """Fetch the gateway's export now and follow it when it changed (`app_follow.AppFollower.async_fetch`).

    No mesh entity: the gateway is asked over the network, so it is available without a link. Why nothing was
    fetched — the token rejected, a certificate the gateway node did not vouch for, no answer — is the press's error;
    an unchanged export is no error.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "fetch_gateway_export"

    def __init__(self, hub: JungHomeHub, node: Node) -> None:
        """Bind to the gateway node's device."""
        self.hub = hub
        self._attr_unique_id = f"node:{node.uuid.lower()}-fetch_gateway_export"
        self._attr_device_info = node_device_info(hub, node)

    async def async_press(self) -> None:
        """Fetch, adopt and follow; errors say why nothing was taken over."""
        follower = self.hub.app_follow
        assert follower is not None  # set by the setup, before the platforms
        await follower.async_fetch(raise_errors=True)


class JungHomePropertyButton(PropertyEntity, ButtonEntity):
    """A trigger property: pressing writes `True` (the app's `[01]` for the reference run)."""

    async def async_press(self) -> None:
        """Start the action."""
        await self.async_write_value(True)
