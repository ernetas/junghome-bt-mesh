"""Firmware: one read-only `update` entity per node, its software version against the JUNG HOME app's.

The JUNG HOME app bundles one firmware image per device family and offers it when a device runs an older version
(`CheckForDeviceUpdate`); it then streams the image to the device over the Silicon Labs OTA GATT service, not over
the mesh (`docs/android/transport-provisioning.md` §5.2). Home Assistant does not update firmware (a
failed update leaves a device out of the network), but it can say what the app would: each node's *Firmware* entity
compares the software version the node reports (SIG 0x001A, the device page's version, `entity.software_version`)
with `BUNDLED_FIRMWARE`, the versions the app bundles per product id (`docs/android/firmware-products.md`, JUNG HOME
2.2.0). An older one shows as an update available, with the release summary pointing to the app; there is no
install feature, so Home Assistant offers no install button and `update.install` is refused.

Only the application image is compared, by product id: the hardware revisions an image is for, the bootloader and
secure-element sub-images and the room thermostat's STM32 co-processor image are not (the node reports none of
them). A product the table does not list (one newer than the app's table) has no latest version: the entity shows
unknown, and so does a node that has not reported its version yet. The gateway gets none: no image exists for it in
the app, it updates itself. Diagnostic and disabled by default, as is everything that is not everyday.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .entity import (
    JungHomeEntity,
    async_setup_platform,
    node_device_info,
    software_version,
)
from .jhmesh import properties as P

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import JungHomeConfigEntry
    from .coordinator import JungHomeHub
    from .jhmesh.cdb import Node

PARALLEL_UPDATES = 0  # push-based: the version arrives with the node's property reads

# The JUNG HOME app (2.2.0) the table is taken from, named in the release summary.
BUNDLED_BY_APP = "2.2.0"
# product id → the application firmware the app bundles for it (`docs/android/firmware-products.md`; the room
# thermostat's STM32 image 4.4.5.0 is a co-processor's and not compared)
BUNDLED_FIRMWARE: dict[int, str] = {
    1: "2.2.0.2",  # push-button 1-gang (lb-connect-steuertaste)
    2: "2.2.0.2",  # push-button 2-gang
    3: "2.2.0.1",  # socket with metering (lb-connect-steckdose)
    12: "2.2.0.1",  # socket
    4: "2.2.0.1",  # switch actuator mini (lb-connect-miniaktor)
    13: "2.2.0.1",  # blinds actuator mini
    5: "2.2.0.1",  # wall transmitter 1-gang (lb-connect-wandsender)
    6: "2.2.0.1",  # wall transmitter 2-gang
    7: "2.2.0.2",  # detectors (lb-connect-melder)
    8: "2.2.0.2",
    9: "2.2.0.2",
    10: "2.2.0.5",  # room thermostat (lb-connect-rtr)
    16: "2.2.0.1",  # the puck actuators (lb-connect-miniaktor-2k)
    17: "2.2.0.1",
    18: "2.2.0.1",
    19: "2.2.0.1",
    20: "2.2.0.1",
    21: "2.2.0.2",  # binary input 2-fold, 230 V (lb-connect-bin2f-230)
    22: "2.2.0.1",  # binary input 2-fold, battery (lb-connect-bin2f-batt)
}
RELEASE_SUMMARY = (
    "The JUNG HOME app {app} brings firmware {version} for this device. Update it with the JUNG HOME app: "
    "Home Assistant only compares the versions and installs nothing."
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the entities of `build_entities`, kept with the hub to follow a new export in place (`model_update`)."""
    async_setup_platform(entry.runtime_data, "update", build_entities, add_entities)


def build_entities(hub: JungHomeHub) -> list[UpdateEntity]:
    """Return a Firmware entity per provisioned node but the gateway."""
    return [
        JungHomeFirmware(hub, node)
        for node in hub.cdb.nodes
        if node.pid is not None and node.pid not in P.GATEWAY
    ]


class JungHomeFirmware(JungHomeEntity, UpdateEntity):
    """The node's firmware against the one the JUNG HOME app bundles for its product; information only."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_supported_features = UpdateEntityFeature(0)  # no install: the app updates
    _attr_translation_key = "firmware"

    def __init__(self, hub: JungHomeHub, node: Node) -> None:
        """Bind to the node's primary element (where its version arrives), on the node device."""
        super().__init__(
            hub,
            node.unicast,
            f"{node.uuid.lower()}-firmware",
            node_device_info(hub, node),
        )
        self.node = node

    @property
    def installed_version(self) -> str | None:
        """The software version the node reported (`2.2.0.2`), None until it has."""
        return software_version(self.hub, self.node)

    @property
    def latest_version(self) -> str | None:
        """The version the app bundles for the node's product, None for a product it has no image for."""
        return BUNDLED_FIRMWARE.get(self.node.pid or 0)

    @property
    def release_summary(self) -> str | None:
        """Where the update comes from: the app (no release notes exist outside it)."""
        if (latest := self.latest_version) is None:
            return None
        return RELEASE_SUMMARY.format(app=BUNDLED_BY_APP, version=latest)

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Refuse: Home Assistant never transfers or flashes firmware (no install feature is offered either)."""
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="firmware_update_in_app"
        )
