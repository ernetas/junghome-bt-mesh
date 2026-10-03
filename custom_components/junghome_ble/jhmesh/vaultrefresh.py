"""Carrying the nodes only Home Assistant knows through the provisioner's key refresh (review-4 D11).

The app refreshes the NetKey by sending Config NetKey Update, then Key Refresh Phase Set 2 and 3, to every
non-excluded node of *its own* database (`docs/android/transport-provisioning.md` §4.2). A node Home Assistant
provisioned (`onboarding`, kept in the `vault`) is not in it — the app never downloads the project — so it would
keep the old key alone: at Phase 3 the rest of the mesh and Home Assistant drop that key, and the node is cut off
until it is factory-reset and provisioned again.

Home Assistant follows the refresh (`keyrefresh`) and, once it is *proven*, takes each vault node along with the
same messages, sealed with the node's device key, which only Home Assistant holds:

- proven at Phase 1 (`KeyRefreshFollower.distribution`): Config NetKey Update with the new key, expecting a NetKey
  Status of success. It is sealed under the old NetKey (`ProxyClient.request_config(old_net_key=True)`), the only
  one a node without the new key accepts, so a node that missed Phase 1 still gets it during Phase 2;
- proven at Phase 2: Key Refresh Phase Set 2, expecting a Phase Status reporting phase 2;
- proven complete: Key Refresh Phase Set 3, expecting a Phase Status reporting phase 0. Never earlier: a node told
  to drop the old key before the mesh did would be cut off from it.

A step goes out only once the one before it was confirmed, and the node's `RefreshProgress` records what it
confirmed, so the next link takes it up where this one stopped. Nothing is sent before the refresh is proven: a
key one node made up never reaches another. Only nodes of the vault are written, and one whose addresses another
node of the file holds by now is left alone.

A node that never got the new key before Home Assistant reached Phase 3 cannot be reached any more: the mesh no
longer relays the old key, and Home Assistant no longer accepts it. The caller reports it (Home Assistant: a
repair issue). None of this has run on air yet (no key refresh was run on an installation with a node Home
Assistant added).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import config_messages as C
from .crypto import NetKeyMaterial
from .vault import RefreshProgress

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .cdb import Node
    from .client import AccessMessage, ProxyClient
    from .vault import VaultNode

__all__ = [
    "NET_KEY_INDEX",
    "Target",
    "carry",
    "target_of",
    "wanted",
]

log = logging.getLogger(__name__)

NET_KEY_INDEX = 0  # the only subnet a JUNG network has


@dataclass(frozen=True)
class Target:
    """Where a vault node is to be taken: a proven phase (1, 2 or 3 = complete) of the refresh to `key`."""

    phase: int
    key: bytes = field(repr=False)  # key material: never in logs or error text
    network_id: bytes  # the new key's, which names the refresh in `RefreshProgress`

    @classmethod
    def of(cls, phase: int, key: bytes) -> Target:
        """Return the target of `phase` of the refresh to `key`."""
        return cls(phase, key, NetKeyMaterial.derive(key).network_id)


def target_of(client: ProxyClient) -> Target | None:
    """Return what the followed refresh proved so far (`ProxyClient.key_refresh_target`); None: nothing to hand out."""
    proven = client.key_refresh_target
    return None if proven is None else Target.of(*proven)


def wanted(target: Target | None, current: bytes, node: VaultNode) -> Target | None:
    """Return where `node` is to be taken now; None when it is there already, or there is nowhere to take it.

    `target`: the proven state of the refresh (`target_of`). Without one — no refresh, or the export holds the new
    key already (`current`, the key we transmit with) — a node whose progress names the refresh to `current` and
    that has not confirmed its end still needs Phase Set 3: Home Assistant transmits with the new key, which a node
    in Phase 1 or 2 accepts.
    """
    progress = node.key_refresh
    if target is not None:
        if (
            progress is not None
            and progress.network_id == target.network_id
            and progress.phase >= target.phase
        ):
            return None
        return target
    if progress is None or progress.phase >= 3:
        return None
    done = Target.of(3, current)
    return done if progress.network_id == done.network_id else None


def _addressable(client: ProxyClient, node: VaultNode) -> tuple[Node, bool] | None:
    """Return the CDB node the client addresses `node` as and whether it is a temporary one; None when it cannot.

    The client's own when it knows the node (the export records it, or `add_device` made it known); a temporary
    one (`VaultNode.as_node`) when no node of the file holds its addresses; None when another node does by now.
    """
    owners = [
        o for a in node.addresses() if (o := client.cdb.node_by_addr(a)) is not None
    ]
    if not owners:
        return node.as_node(), True
    same = [
        o
        for o in owners
        if o.uuid == node.uuid
        and o.unicast == node.unicast
        and o.dev_key == node.dev_key
    ]
    return (owners[0], False) if len(same) == len(owners) else None


async def carry(
    client: ProxyClient,
    node: VaultNode,
    target: Target,
    *,
    timeout: float = 3.0,
    on_progress: Callable[[], Awaitable[None]] | None = None,
) -> bool | None:
    """Take `node` to `target.phase` of its refresh; True once it confirmed it, False when it did not.

    False: a step went unanswered (the node is off, out of range, or — at Phase 3 — never got the new key) or was
    refused; the next call takes it up again. None: another node of the file holds its addresses now, nothing was
    sent. `node.key_refresh` records every step the node confirmed, `on_progress` is awaited after each (the caller
    persists the vault). `ConnectionError` when the link goes away meanwhile.
    """
    if node.key_refresh is None or node.key_refresh.network_id != target.network_id:
        node.key_refresh = RefreshProgress(target.network_id)
    addressed = _addressable(client, node)
    if addressed is None:
        log.warning(
            "The node Home Assistant added at %04X is not taken through the key refresh: another node of the "
            "export holds its addresses now",
            node.unicast,
        )
        return None
    as_node, temporary = addressed
    if temporary:
        client.add_node(as_node)
    try:
        return await _steps(client, node, target, timeout, on_progress)
    finally:
        if temporary:
            client.remove_node(as_node)


async def _steps(
    client: ProxyClient,
    node: VaultNode,
    target: Target,
    timeout: float,
    on_progress: Callable[[], Awaitable[None]] | None,
) -> bool:
    async def confirmed(phase: int) -> None:
        node.key_refresh = RefreshProgress(target.network_id, phase)
        log.info(
            "The node Home Assistant added at %04X confirmed key refresh phase %d",
            node.unicast,
            phase,
        )
        if on_progress is not None:
            await on_progress()

    assert node.key_refresh is not None  # `carry` set it
    if target.phase in (1, 2) and node.key_refresh.phase < 1:
        reply = await _ask(
            client,
            node,
            C.netkey_update(target.key),
            C.CONFIG_NETKEY_STATUS,
            timeout,
            old_net_key=True,
        )
        if reply is None:
            return False
        status = C.decode_netkey_status(reply.params)
        if status.ok:
            await confirmed(1)
        else:
            log.warning(
                "The node Home Assistant added at %04X refused the new network key (%s)",
                node.unicast,
                status.status_name,
            )
            if target.phase == 1:
                return False
            # in Phase 2 it may hold the key already (it refuses an Update then): its Phase Status says
    if target.phase == 1 or node.key_refresh.phase >= target.phase:
        return True
    # Phase Set 2 answered by phase 2; Phase Set 3 by phase 0, normal operation with the new key alone
    reported = 2 if target.phase == 2 else 0
    reply = await _ask(
        client,
        node,
        C.key_refresh_phase_set(target.phase),
        C.CONFIG_KEY_REFRESH_PHASE_STATUS,
        timeout,
    )
    if reply is None:
        return False
    phase_status = C.decode_key_refresh_phase_status(reply.params)
    if not phase_status.ok or phase_status.phase != reported:
        log.warning(
            "The node Home Assistant added at %04X did not take key refresh phase %d (%s, phase %d)",
            node.unicast,
            target.phase,
            phase_status.status_name,
            phase_status.phase,
        )
        return False
    await confirmed(target.phase)
    return True


async def _ask(
    client: ProxyClient,
    node: VaultNode,
    pdu: bytes,
    expect: int,
    timeout: float,
    *,
    old_net_key: bool = False,
) -> AccessMessage | None:
    """Send `pdu` to the node's Configuration Server; its status for NetKey index 0, or None when it stayed silent."""
    length = 4 if expect == C.CONFIG_KEY_REFRESH_PHASE_STATUS else 3

    def for_our_subnet(m: AccessMessage) -> bool:
        return (
            len(m.params) >= length
            and int.from_bytes(m.params[1:3], "little") & 0xFFF == NET_KEY_INDEX
        )

    try:
        return await client.request_config(
            node.unicast,
            pdu,
            expect,
            timeout=timeout,
            match=for_our_subnet,
            old_net_key=old_net_key,
        )
    except TimeoutError:
        log.warning(
            "The node Home Assistant added at %04X did not answer its key refresh step",
            node.unicast,
        )
        return None
