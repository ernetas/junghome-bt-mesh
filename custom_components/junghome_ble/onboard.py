"""Adding a JUNG device from Home Assistant (review-3 N3): provision, commission, read back, record — experimental.

The `add_device` action (administrators only, and only with the entry's *Allow Home Assistant to add devices*
option on) takes an unprovisioned JUNG device advertising the Mesh Provisioning Service (0x1827) nearby and does
what the app does when a device is added:

1. picks a *template*: a node of the export with the product id the device advertises (`JungAdvertisement`) —
   its composition, settings and wiring are what the new node gets, as the app decides them by product. The export
   is the one as it is now (`MeshConfigurator.async_current_export`: the gateway's, adopted first, when the app
   changed the network since the last reload), and so is every address picked next;
2. places it above every provisioner's address range (`onboarding.free_unicast_block`), where no app allocates,
   its element groups at the top of the app's group range, where the app allocates last (review-4 W4-2) — with
   the *provisioner identity* option on, inside Home Assistant's own ranges instead (review-3 N1) — and clear of
   every node the vault holds: one provisioned earlier but never recorded is in no export, yet still sends from
   its addresses and holds its element groups (review-4 D2);
3. provisions it over PB-GATT (`provisioning.provision`, No-OOB, the export's NetKey and the IV state a beacon
   confirmed on the current link), refusing a device whose element count differs from the template's before it
   learns an address; its device key and planned element groups go into the vault (`identity.py`) the moment
   provisioning completes, before anything else can fail, and the replay list forgets the new addresses (they are
   Home Assistant's to give, so whatever it remembers for them is a reset node's);
4. sends the app's post-provisioning Config sequence through the proxy link (`commission.plan`,
   `onboarding.commission`), then reads the node's configuration back (`jhmesh.audit`);
5. records the node in the export — entry, element groups, app device rows — through the configurator, which
   hands the export to the gateway as after any change, and reloads the entry.

The name is checked as the app checks one (blank, a lone `%`, longer than the rename sheet takes) before
anything goes on air, and numbered like the app numbers a name another device has.

A node provisioned but not recorded (its commissioning or its recording failed) stays *pending* in the vault: its
addresses and groups stay reserved and the `pending_device` repair issue names it, until `reset_pending_device`
sends it a Config Node Reset with the vault's key (or, with `force`, forgets it unanswered — a device that was
factory-reset by hand keeps its addresses reserved until then).

Nothing of this has run against a real device yet (unverified on air): try it on a spare device first.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, ISSUE_PENDING_DEVICE, NODE_BOOT_DELAY
from .coordinator import issue_id
from .jhmesh import commission
from .jhmesh import config_messages as C
from .jhmesh.advert import parse_manufacturer_data
from .jhmesh.cdb import Element, Node, canonical_uuid
from .jhmesh.export import (
    RENAME_MAX_LENGTH,
    AllocationCrowded,
    InvalidName,
    check_name,
    suffixed_name,
)
from .jhmesh.onboarding import (
    CommissioningError,
    free_unicast_block,
    node_entry,
    node_for,
)
from .jhmesh.onboarding import commission as run_commission
from .jhmesh.provisioning import (
    MESH_PROVISIONING_SERVICE,
    Capabilities,
    ProvisioningData,
    ProvisioningError,
    parse_provisioning_service_data,
    provision,
)

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import JungHomeHub
    from .identity import VaultKeeper
    from .jhmesh.advert import JungAdvertisement
    from .jhmesh.cdb import CDB
    from .jhmesh.export import ProjectFile
    from .jhmesh.vault import Vault, VaultNode
    from .mesh_config import MeshConfigurator

_LOGGER = logging.getLogger(__name__)

# seconds to wait for a pending node's Node Reset Status, per attempt (three attempts), as `remove_device` does
NODE_RESET_TIMEOUT = 3.0


def _validation(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


def _failure(key: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


def unprovisioned_devices(hass: HomeAssistant) -> list[dict[str, Any]]:
    """List the JUNG devices nearby that advertise the Mesh Provisioning Service: address, UUID, product."""
    out = []
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        data = info.service_data.get(MESH_PROVISIONING_SERVICE)
        parsed = parse_provisioning_service_data(bytes(data)) if data else None
        advert = parse_manufacturer_data(info.manufacturer_data)
        if parsed is None or advert is None:
            continue
        out.append(
            {
                "address": info.address,
                "uuid": parsed[0],
                "product_id": advert.product_id,
                "rssi": info.rssi,
            }
        )
    return sorted(out, key=lambda d: -(d["rssi"] or -127))


def _new_device(
    hass: HomeAssistant, address: str
) -> tuple[BluetoothServiceInfoBleak, str, JungAdvertisement]:
    """Return the unprovisioned JUNG device advertising at `address`: its advert, Device UUID and JUNG record."""
    info = next(
        (
            i
            for i in bluetooth.async_discovered_service_info(hass, connectable=True)
            if i.address.upper() == address.upper()
            and MESH_PROVISIONING_SERVICE in i.service_data
        ),
        None,
    )
    if info is None:
        raise _validation("add_device_not_found", address=address)
    parsed = parse_provisioning_service_data(
        bytes(info.service_data[MESH_PROVISIONING_SERVICE])
    )
    advert = parse_manufacturer_data(info.manufacturer_data)
    if parsed is None or advert is None:
        raise _validation("add_device_not_jung", address=address)
    return info, parsed[0], advert


def _checked_name(name: str) -> str:
    """Return `name` when the app's name check takes it (`check_name`, with the rename sheet's limit); else refuse.

    Before anything goes on air: a name refused after provisioning would leave a provisioned node unrecorded.
    """
    try:
        return check_name(name, RENAME_MAX_LENGTH)
    except InvalidName as err:
        if err.reason == "blank":
            raise _validation("service_name_blank") from err
        if err.reason == "too_long":
            raise _validation(
                "add_device_name_too_long",
                name=name,
                max_length=str(RENAME_MAX_LENGTH),
            ) from err
        raise _validation("service_name_not_allowed", name=name) from err


def _unique_name(pf: ProjectFile, name: str) -> str:
    """Return `name`, numbered as the app numbers a name another device of `pf` has (ignoring case)."""
    names = pf.device_names()
    if any(n.lower() == name.lower() for n in names):
        return suffixed_name(name, names)
    return name


def _template(cdb: CDB, product_id: int) -> Node:
    """Return the node of the export the new one is shaped after: the same product, the first one in the export."""
    template = next((n for n in cdb.nodes if n.pid == product_id), None)
    if template is None:
        raise _validation("add_device_no_template", product=f"0x{product_id:04X}")
    return template


async def _place(
    hub: JungHomeHub, configurator: MeshConfigurator, cdb: CDB, count: int
) -> tuple[int, tuple[int, int] | None]:
    """Return where a node of `count` elements goes in `cdb` and the group range for its element groups (None: the app's).

    Above every provisioner's range, or with the provisioner identity option inside Home Assistant's own ranges;
    never on Home Assistant's own address nor on any address of a node the vault holds, pending or recorded.
    """
    within: tuple[int, int] | None = None
    group_range: tuple[int, int] | None = None
    if configurator.identity_enabled:
        ranges = await configurator.async_identity_ranges()
        within, group_range = ranges.unicast, ranges.group
    vault = hub.vault.vault
    reserved = vault.reserved_unicasts() if vault is not None else set()
    unicast = free_unicast_block(
        cdb,
        count,
        avoid=[hub.proxy.state.src, *reserved],
        within=within,
        own=hub.vault.own_uuid,
    )
    if unicast is None:
        raise _failure("add_device_no_address")
    return unicast, group_range


def _reserved_groups(hub: JungHomeHub) -> set[int]:
    """Return the element groups of every node the vault holds; warn about pending ones it kept none for."""
    vault = hub.vault.vault
    if vault is None:
        return set()
    for node in vault.groups_unknown:
        _LOGGER.warning(
            "The device Home Assistant provisioned at %04X was never recorded, and the vault (kept by an earlier "
            "version) does not say which element groups it holds: they may be handed out again. Reset it with "
            "the action reset_pending_device",
            node.unicast,
        )
    return vault.reserved_groups()


def _require_iv_state(hub: JungHomeHub) -> None:
    """Refuse to provision before a beacon on the current link confirmed the IV index (review-4 P4-6).

    The device takes the IV index from the Provisioning Data: a stale one (a stored state from before an IV
    Update) would leave it unable to talk to the network.
    """
    if not hub.proxy.state.iv_known or not hub.proxy.beacon_seen:
        raise _validation("add_device_no_beacon")


async def _keep_key(
    hub: JungHomeHub,
    uuid: str,
    unicast: int,
    count: int,
    dev_key: bytes,
    groups: list[tuple[int, str]],
) -> None:
    """Keep a just-provisioned node's device key and planned element groups in the vault until it is recorded.

    The key: the only copy until the node is recorded. A vault that cannot be written does not stop the device
    from being added: the key is still in memory, and the export gets it once the node is recorded. Logged (never
    the key) and carried on.
    """
    hub.vault.identity().remember_provisioned(uuid, unicast, count, dev_key, groups)
    try:
        await hub.vault.async_save()
    except Exception as err:
        _LOGGER.warning(
            "The device key of the node at %04X could not be kept in the vault (%s); carrying on — the export "
            "records it once the node is configured",
            unicast,
            type(err).__name__,
        )


async def async_add_device(
    hass: HomeAssistant,
    hub: JungHomeHub,
    configurator: MeshConfigurator,
    address: str,
    name: str,
) -> dict[str, Any]:
    """Add the unprovisioned JUNG device at Bluetooth `address` to the mesh as `name`; return where it went."""
    name = _checked_name(name)
    info, uuid, advert = _new_device(hass, address)
    # the export as it is now — the gateway's, adopted, when the app added a device or a room since the last
    # reload: the addresses go out to the device long before `record_node` reads the export again
    pf = await configurator.async_current_export()
    cdb = pf.cdb
    name = _unique_name(pf, name)
    template = _template(cdb, advert.product_id)
    count = len(template.elements)
    unicast, group_range = await _place(hub, configurator, cdb, count)
    # planned before the node is known: its addresses must still be free in the export. Its element groups from the
    # top of the app's range (review-4 W4-2): the app does not know them until it imports a file, and would give
    # its next room the lowest free group — in Home Assistant's own range, nobody else allocates
    try:
        plan = commission.plan(
            cdb,
            unicast,
            count,
            template,
            group_range=group_range,
            reserved_groups=_reserved_groups(hub),
            policy="top" if group_range is None else "app",
        )
    except AllocationCrowded as err:
        raise _failure("add_device_groups_crowded", free=str(err.below)) from err
    _require_iv_state(hub)
    state = hub.proxy.state
    data = ProvisioningData(
        net_key=hub.proxy.nk.key,
        unicast=unicast,
        iv_index=state.iv_index,
        iv_update=state.iv_update_active,
        key_refresh=hub.proxy.key_refresh_phase == 2,
    )

    def check(capabilities: Capabilities) -> None:
        if capabilities.elements != count:
            raise ProvisioningError(
                f"the device has {capabilities.elements} element(s), the template {count}"
            )

    device = (
        bluetooth.async_ble_device_from_address(hass, info.address, connectable=True)
        or info.device
    )
    _LOGGER.warning(
        "Provisioning the JUNG device %s as %s at %04X (experimental)",
        info.address,
        name,
        unicast,
    )
    try:
        client = await establish_connection(
            BleakClientWithServiceCache, device, f"JUNG {name}", max_attempts=2
        )
    except Exception as err:
        raise _failure("add_device_connect_failed", error=str(err)) from err
    try:
        result = await provision(client, data, check=check)
    except ProvisioningError as err:
        raise _failure("add_device_provisioning_failed", error=str(err)) from err
    finally:
        try:
            await client.disconnect()
        except (
            Exception
        ):  # the node restarts after Complete: its link may be gone already
            _LOGGER.debug("disconnect after provisioning failed", exc_info=True)
    await _keep_key(
        hub,
        uuid,
        unicast,
        count,
        result.device_key,
        [(g.address, g.name) for g in plan.groups],
    )
    # Home Assistant just gave these addresses out: what the replay list holds for them is a reset node's, and
    # would drop the new node's replies (it starts at SEQ 0) until it passed those numbers
    hub.proxy.forget_sources(range(unicast, unicast + count))
    node = node_for(
        template, uuid=uuid, unicast=unicast, dev_key=result.device_key, name=name
    )
    hub.proxy.add_node(node)
    await asyncio.sleep(NODE_BOOT_DELAY)  # the node restarts as a mesh node
    try:
        await hub.async_wait_connected(NODE_BOOT_DELAY)
        await run_commission(hub.proxy, plan)
        audit = (await hub.async_audit([node]))[0]
    except (CommissioningError, ConnectionError) as err:
        raise _failure(
            "add_device_commissioning_failed",
            unicast=f"{unicast:04X}",
            error=str(err),
        ) from err
    try:
        await configurator.record_node(
            template,
            lambda raw: node_entry(
                raw, uuid=uuid, unicast=unicast, dev_key=result.device_key, name=name
            ),
            audit,
            plan,
            name,
        )
    except HomeAssistantError as err:
        raise _failure(
            "add_device_record_failed", unicast=f"{unicast:04X}", error=str(err)
        ) from err
    return {
        "unicast": f"{unicast:04X}",
        "uuid": uuid,
        "name": name,
        "elements": count,
        "template": f"{template.unicast:04X}",
    }


# ------------------------------------------------------------------ pending nodes


def _pending(vault: Vault | None, uuid: str | None, unicast: int | None) -> VaultNode:
    """Return the pending vault node the call names: by UUID, its primary address matching `unicast` when given."""
    pending = vault.pending if vault is not None else []
    if uuid is not None:
        wanted = canonical_uuid(uuid)
        node = next((n for n in pending if n.uuid == wanted), None)
    else:
        node = next((n for n in pending if n.unicast == unicast), None)
    if node is None or (unicast is not None and node.unicast != unicast):
        shown = uuid if uuid is not None else f"{unicast:04X}"
        raise _validation("reset_pending_device_unknown", device=shown)
    return node


def _reachable(pending: VaultNode) -> Node:
    """Return a node the proxy client can address a pending vault node as (its device key, its elements)."""
    node = Node(pending.uuid, "pending", pending.unicast, pending.dev_key, None)
    node.elements = [Element(a, 0, [], node) for a in pending.addresses()]
    return node


async def async_reset_pending_device(
    hub: JungHomeHub, *, uuid: str | None, unicast: int | None, force: bool
) -> dict[str, Any]:
    """Send a pending node a Config Node Reset with the vault's key, then forget it (review-4 D2; unverified on air).

    The node is named by its UUID (the vault's key) or its primary address, and both must agree when both are
    given: a reset meant for a stale entry must not reach another device. Forgotten once it confirmed, or with
    `force` unanswered (a device already factory-reset, or gone). A node of the export at one of its addresses is
    another device now: no reset is sent, and only `force` forgets the vault's entry. Answers whether the node
    confirmed.
    """
    keeper = hub.vault
    pending = _pending(keeper.vault, uuid, unicast)
    address = f"{pending.unicast:04X}"
    proxy = hub.proxy
    owners = [
        o for a in pending.addresses() if (o := proxy.cdb.node_by_addr(a)) is not None
    ]
    # the node itself, made known to the link by the `add_device` call that provisioned it
    same = [
        o
        for o in owners
        if o.uuid == pending.uuid
        and o.unicast == pending.unicast
        and o.dev_key == pending.dev_key
    ]
    confirmed = False
    if len(same) != len(owners):
        if not force:
            raise _validation("reset_pending_device_address_in_use", unicast=address)
        _LOGGER.warning(
            "Another node of the network is at %s now: no reset sent, the pending device's entry is forgotten",
            address,
        )
    else:
        temporary = None if same else _reachable(pending)
        if temporary is not None:
            proxy.add_node(temporary)
        try:
            await proxy.request_config(
                pending.unicast,
                C.node_reset(),
                C.CONFIG_NODE_RESET_STATUS,
                timeout=NODE_RESET_TIMEOUT,
            )
            confirmed = True
        except TimeoutError as err:
            if not force:
                raise _failure(
                    "reset_pending_device_no_answer", unicast=address
                ) from err
            _LOGGER.warning(
                "The pending device at %s did not confirm its reset; forgetting it all the same",
                address,
            )
        except (ConnectionError, OSError) as err:
            raise _failure(
                "reset_pending_device_send_failed", unicast=address, error=str(err)
            ) from err
        finally:
            if temporary is not None:
                proxy.remove_node(temporary)
        for node in same:
            proxy.remove_node(node)
    keeper.identity().forget(pending.uuid)
    await keeper.async_save()
    return {"uuid": pending.uuid, "unicast": address, "confirmed": confirmed}


@callback
def async_update_pending_issue(
    hass: HomeAssistant, entry: ConfigEntry, keeper: VaultKeeper
) -> None:
    """Raise the `pending_device` repair issue while the vault holds a node never recorded; clear it otherwise.

    The issue names the addresses only (the vault's keys never leave it).
    """
    issue = issue_id(entry, ISSUE_PENDING_DEVICE)
    vault = keeper.vault
    pending = sorted(n.unicast for n in vault.pending) if vault is not None else []
    if not pending:
        ir.async_delete_issue(hass, DOMAIN, issue)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_PENDING_DEVICE,
        translation_placeholders={
            "title": entry.title,
            "addresses": ", ".join(f"{a:04X}" for a in pending),
        },
    )
