"""A metering socket's threshold wiring and a node's sensor publication, as Config plans.

`Thresholds` (review-4 brief 55): the loads a socket's thresholds switch subscribe to its meter element's group, as
the app's `CreateThreshold` wires them (the thresholds' values are `thresholds.py`'s), and the *Sensor values for
IoT systems* publications of a node's Sensor Servers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import TYPE_CHECKING

from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh.export import ProjectFile, has_model, hexaddr
from custom_components.junghome_ble.jhmesh.plan import (
    ConfigStep,
    bind_step,
    config_step,
    config_steps,
)

from .executor import Operations
from .plan import applied_text
from .store import _validation
from .wiring import (
    ONOFF_CLIENT,
    ONOFF_SERVER,
    SENSOR_SERVER,
    THRESHOLD_TARGET_MODELS,
    element_groups,
    find_element,
    sensor_elements,
    threshold_client,
    threshold_wiring,
)

if TYPE_CHECKING:
    from custom_components.junghome_ble.jhmesh.cdb import Element

_LOGGER = logging.getLogger(__name__)


class Thresholds(Operations):
    """Thresholds and sensor publication of one hub's sockets and nodes."""

    def _threshold_plan(
        self, pf: ProjectFile, socket_address: int, devices: Iterable[int]
    ) -> tuple[Element, Element, list[Element], int | None]:
        """Return the socket, its OnOff Client, the wanted loads and the client's element group, or refuse.

        The group is None only for an empty list: nothing listens to a group the client does not have.
        """
        socket = find_element(pf, socket_address)
        client = threshold_client(socket.node)
        if client is None:
            raise _validation("threshold_not_supported", name=hexaddr(socket.address))
        wanted = [find_element(pf, address) for address in devices]
        for element in wanted:
            if not has_model(element, ONOFF_SERVER):
                raise _validation("service_not_a_load", name=hexaddr(element.address))
        group = element_groups(pf).get(client.address)
        if group is None and wanted:
            raise _validation(
                "service_no_element_group", address=hexaddr(client.address)
            )
        return socket, client, wanted, group

    async def check_threshold_devices(
        self, socket_address: int, devices: Iterable[int]
    ) -> None:
        """Refuse what `set_threshold_devices` would refuse, writing nothing: the caller checks before the threshold."""
        async with self.store.lock:
            self._threshold_plan(await self.store.load(), socket_address, devices)

    async def set_threshold_devices(
        self,
        socket_address: int,
        devices: Iterable[int],
        *,
        applied: Callable[[int, int], str] = applied_text,
    ) -> bool:
        """Make the socket's thresholds switch exactly `devices` (load elements), the wiring of `CreateThreshold`.

        In the app's order (on air): the socket's OnOff Client subscribes to its element's own group
        and publishes there, then each load subscribes its JUNG User Property Server (`0x0527:1013`, where it has
        one) and its OnOff server to that group; a load no longer wanted leaves it (`threshold_wiring`). Both
        thresholds of the socket share the list: the app wires one client for both. The caller writes the
        threshold first, as the app does; `applied` words a stop, with what the call wrote before (W4-13).
        """
        async with self.store.lock:
            pf = await self.store.load()
            before = pf.snapshot()
            socket, client, wanted, group = self._threshold_plan(
                pf, socket_address, devices
            )
            if group is None:
                return (
                    self.store.adopted
                )  # no group, so no load listens to one: nothing to unwire
            steps: list[ConfigStep] = []
            if wanted:
                bind = bind_step(client, ONOFF_CLIENT)
                if bind is not None:
                    steps.append(bind)
                steps += config_steps(pf, pf.subscribe(client, ONOFF_CLIENT, group))
                if pf.publication(client, ONOFF_CLIENT) != group:
                    steps += config_steps(
                        pf, pf.set_publication(client.node, client, ONOFF_CLIENT, group)
                    )
            for element, model in threshold_wiring(pf.cdb, client, group):
                if element not in wanted:
                    steps += config_steps(pf, pf.unsubscribe(element, model, group))
            for element in wanted:
                for model in THRESHOLD_TARGET_MODELS:
                    if has_model(element, model):
                        steps += config_steps(pf, pf.subscribe(element, model, group))
            if not steps and pf.snapshot() == before:
                return self.store.adopted  # already wired so
            await self.executor.send(
                steps, action="junghome_ble.set_threshold", applied=applied
            )
            await self.store.save(pf)
            _LOGGER.info(
                "Socket %04X's thresholds now switch %s (group %04X), %d Config messages",
                socket.address,
                ", ".join(hexaddr(e.address) for e in wanted) or "nothing",
                group,
                len(steps),
            )
            return True

    async def unwire_threshold(
        self,
        socket_address: int,
        *,
        applied: Callable[[int, int], str] = applied_text,
    ) -> bool:
        """Stop the socket's thresholds switching anything, as the app does when it disables or deletes one.

        On air (the app settings session), once no threshold of the socket is active: every load leaves
        the client's element group (`threshold_wiring`: OnOff server, then `0x0527:1013`), then the client's
        publication is reset — `Publication Set 0x0000` (TTL 0, as the app sends it), then its element group
        again — even when no load was left to unwire. The steps go out in that order
        (`PlanExecutor.send(as_planned=True)`). A publication the client does not have to its group is left alone. The
        app also writes KeyMode 5 to the meter element around it, which that element does not hold
        (`air:access:03-0527:0x5003`): not sent. `applied` words a stop, with what the call wrote before (W4-13).
        """
        async with self.store.lock:
            pf = await self.store.load()
            before = pf.snapshot()
            socket = find_element(pf, socket_address)
            client = threshold_client(socket.node)
            if client is None:
                raise _validation(
                    "threshold_not_supported", name=hexaddr(socket.address)
                )
            group = element_groups(pf).get(client.address)
            if group is None:
                return self.store.adopted  # no group, so no load listens to one
            steps: list[ConfigStep] = []
            for element, model in threshold_wiring(pf.cdb, client, group):
                steps += config_steps(pf, pf.unsubscribe(element, model, group))
            if pf.publication(client, ONOFF_CLIENT) == group:
                [off] = pf.set_publication(client.node, client, ONOFF_CLIENT, None)
                steps.append(
                    replace(
                        config_step(pf, off),
                        pdu=C.model_publication_set(
                            client.address, 0, ONOFF_CLIENT, ttl=0
                        ),
                    )
                )
                steps += config_steps(
                    pf, pf.set_publication(client.node, client, ONOFF_CLIENT, group)
                )
            if not steps:
                return self.store.adopted
            await self.executor.send(
                steps,
                action="junghome_ble.set_threshold / delete_threshold",
                applied=applied,
                as_planned=True,
            )
            if pf.snapshot() == before:
                await (
                    self.store.journal_close()
                )  # nothing to write: the plan is over all the same
                return (
                    self.store.adopted
                )  # the publication reset alone: the file already says so
            await self.store.save(pf)
            _LOGGER.info(
                "Socket %04X's thresholds switch nothing now (group %04X), %d Config messages",
                socket.address,
                group,
                len(steps),
            )
            return True

    async def set_sensor_publication(
        self, node_unicast: int, on: bool, *, live: bool | None = None
    ) -> bool:
        """Publish the node's sensor values or stop: the app's *Sensor values for IoT systems*.

        `ConfigurePublicationForSensorServer`: every Sensor Server of the node publishes to its element's own group,
        where the gateway (and Home Assistant) hear it. Off is a `Publication Set` to `0x0000`. The app's publication parameters (TTL 0xFF, no period: the node
        publishes on change) are inferred, not captured.

        `live` is what the node last answered (the switch's read, None when it has not): a node that differs from
        `on` gets its Publication Sets even where the export already agrees (review-4 W4-6) — the app changed it
        since, or never recorded it, and the switch shows the node's state, so a skip would leave it unchangeable.
        Unverified on air.
        """
        async with self.store.lock:
            pf = await self.store.load()
            before = pf.snapshot()
            node = pf.cdb.node_by_addr(node_unicast)
            if node is None:
                raise _validation(
                    "service_unknown_element", address=hexaddr(node_unicast)
                )
            groups = element_groups(pf)
            steps: list[ConfigStep] = []
            for element in sensor_elements(node):
                group = groups.get(element.address)
                if on and group is None:
                    raise _validation(
                        "service_no_element_group", address=hexaddr(element.address)
                    )
                want = group if on else None
                if pf.publication(element, SENSOR_SERVER) == want and live in (
                    None,
                    on,
                ):
                    continue
                if on and (bind := bind_step(element, SENSOR_SERVER)) is not None:
                    steps.append(bind)
                steps += config_steps(
                    pf, pf.set_publication(node, element, SENSOR_SERVER, want)
                )
            if not steps and pf.snapshot() == before:
                return self.store.adopted  # already so
            await self.executor.send(
                steps, action="the switch Sensor values for IoT systems"
            )
            await self.store.save(pf)
            _LOGGER.info(
                "Node %04X's sensor values %s, %d Config messages",
                node.unicast,
                "published" if on else "no longer published",
                len(steps),
            )
            return True
