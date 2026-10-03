"""The `ProxyClient` end to end against the simulated mesh (`tests/sim`): relays, loss, segmentation with lost
acknowledgements, the proxy filter under loss, an IV Update and a key refresh followed across the proxy, a second
proxy, a node commissioned and audited — every test ending on the harness's invariants (no (SRC, IV, SEQ) twice,
nothing replayed or undecryptable, nothing lost that the loss model did not drop).

Everything runs on the virtual clock: minutes of mesh time take milliseconds.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from jhmesh import audit as A
from jhmesh import client as client_mod
from jhmesh import commission
from jhmesh import config_messages as C
from jhmesh import messages as M
from jhmesh.client import IV_UPDATE_MIN_STATE, AccessMessage, LocalState, ProxyClient
from jhmesh.crypto import NetKeyMaterial
from jhmesh.keyrefresh import PROOF_STATUSES
from jhmesh.onboarding import commission as run_commission
from jhmesh.onboarding import free_unicast_block, node_for
from jhmesh.pdu import FILTER_BLACKLIST, decode_opcode
from jhmesh.vault import RefreshProgress, Vault, VaultNode
from jhmesh.vaultrefresh import carry, target_of, wanted
from tests.sim import (
    CLIENT_ADDRESS,
    FIXTURE_HOP_MATRIX,
    LossModel,
    Mesh,
    Packet,
    Quirks,
    Topology,
    parse_hop_matrix,
    patch_monotonic,
    run,
)
from tests.sim.clock import VirtualClocks, VirtualDeadlock, VirtualTimeout

PROXY_A, PROXY_B = 0x0148, 0x0300
LIGHT_2G = 0x0232  # CTL light (temperature on 0x0233)
DIMMER = 0x0300  # Light Lightness server
FAR = 0x0400  # 2-channel actuator, four hops from PROXY_A
NEW_UUID = "11111111-2222-4333-8444-555555555555"
NEW_DEV_KEY = bytes(range(0xA0, 0xB0))
NEW_NET_KEY = bytes(range(0x30, 0x40))
HOP_MATRIX = Path(__file__).resolve().parents[2] / "docs" / "hop-matrix.md"


@pytest.fixture(autouse=True)
def virtual_monotonic(monkeypatch: pytest.MonkeyPatch) -> VirtualClocks:
    """The client's `time.monotonic()` (reassembly expiry, `last_rx`, message stamps) and its wall clock (the IV
    Update timing) on the virtual clock."""
    return patch_monotonic(monkeypatch, client_mod)


class Session:
    """A mesh, a client attached to one of its proxies, and every message the client handed out."""

    def __init__(self, mesh: Mesh, *, mtu: int = 69) -> None:
        self.mesh = mesh
        self.messages: list[AccessMessage] = []
        self.phases: list[int] = []
        self.client = ProxyClient(
            mesh.client_cdb(),
            LocalState(None, CLIENT_ADDRESS),
            ttl=5,
            on_message=self.messages.append,
            on_key_refresh=lambda phase, _key: self.phases.append(phase),
        )
        mesh.watch(self.client)
        self.mtu = mtu

    async def attach(self, proxy: int = PROXY_A, *, mtu: int | None = None) -> None:
        link = self.mesh.proxy(proxy).connect(mtu or self.mtu)
        link.disconnected_callback = self.client.handle_disconnected
        await self.client.attach(link)

    async def onoff(self, dst: int, on: bool | None = None, **kw: Any) -> bool:
        pdu = M.generic_onoff_get() if on is None else M.generic_onoff_set(on)
        reply = await self.client.request(dst, pdu, M.GEN_ONOFF_STATUS, **kw)
        return bool(reply.params[0])

    def from_(self, src: int, opcode: int) -> list[AccessMessage]:
        return [m for m in self.messages if m.src == src and m.opcode == opcode]


def simulate(
    body: Callable[[Session], Awaitable[None]],
    mesh: Mesh | None = None,
    *,
    attach: bool = True,
    mtu: int = 69,
) -> Session:
    """Run `body` against a fresh session on the virtual clock, then let the air settle and check the invariants."""
    session = Session(mesh or Mesh(), mtu=mtu)

    async def main() -> None:
        if attach:
            await session.attach()
        await body(session)
        await session.mesh.settle()
        await session.client.detach()

    run(main())
    session.mesh.assert_invariants()
    return session


# ============================================================================= the harness itself


def test_the_fixture_topology_needs_relays_and_the_measured_one_parses() -> None:
    topology = Topology.from_hop_matrix(FIXTURE_HOP_MATRIX)
    assert topology.hops(PROXY_A, FAR) == 4
    assert topology.hops(PROXY_A, LIGHT_2G) == 1
    assert topology.neighbours(0x0001) == {0x00DC}
    measured = parse_hop_matrix(HOP_MATRIX.read_text(encoding="utf-8"))
    assert len(measured) == 29
    assert 0x0133 in measured[0x00DC]  # 00DC → 0133 in one hop
    assert all(
        a in measured[b] for a, neighbours in measured.items() for b in neighbours
    )
    topology.cut(PROXY_A, LIGHT_2G)
    assert topology.hops(PROXY_A, LIGHT_2G) == 3
    assert Topology({1: set()}).hops(1, 2) is None


def test_the_virtual_clock_jumps_and_catches_what_never_ends() -> None:
    async def an_hour() -> float:
        loop = asyncio.get_running_loop()
        await asyncio.sleep(3600)
        await asyncio.wait_for(asyncio.sleep(10), 20)
        return loop.time()

    started = time.monotonic()
    assert run(an_hour()) == pytest.approx(3610)
    assert time.monotonic() - started < 1

    async def forever() -> None:
        await asyncio.get_running_loop().create_future()

    with pytest.raises(VirtualDeadlock):
        run(forever())

    async def ticking() -> None:
        for _ in range(10**9):  # a periodic job that never ends
            await asyncio.sleep(60)

    with pytest.raises(VirtualTimeout):
        run(ticking(), limit=3600)


def test_the_invariants_catch_a_reused_sequence_number() -> None:
    """The mutant review-3 Q1 found passing every HA test: a client that reuses a sequence number (nonce reuse)."""
    mesh = Mesh()
    session = Session(mesh)

    async def main() -> None:
        await session.attach()
        assert not await session.onoff(LIGHT_2G)
        session.client.state.seq -= 1  # the next PDU reuses the last number
        with pytest.raises(TimeoutError):
            await session.onoff(LIGHT_2G, timeout=0.5, retries=1)
        await mesh.settle()

    run(main())
    problems = mesh.violations()
    assert any("reused" in p for p in problems)


def test_the_invariants_catch_a_lost_message_and_an_undecryptable_one() -> None:
    mesh = Mesh()
    session = Session(mesh)

    async def main() -> None:
        await session.attach()
        mesh.topology.cut(
            0x0300, FAR
        )  # unreachable, and no drop recorded: a loss the model did not make
        with pytest.raises(TimeoutError):
            await session.onoff(FAR, timeout=0.5, retries=1)
        session.client._kr.current = NEW_NET_KEY  # a stale export's key
        with pytest.raises(TimeoutError):
            await session.onoff(LIGHT_2G, timeout=0.5, retries=1)
        await mesh.settle()

    run(main())
    problems = mesh.violations()
    assert any("lost, yet the loss model dropped nothing" in p for p in problems)
    assert any("could not decrypt a client PDU" in p for p in problems)


def test_the_invariants_catch_a_client_that_drops_what_reached_it() -> None:
    mesh = Mesh()
    session = Session(mesh)

    async def main() -> None:
        await session.attach()
        deliver = session.client._deliver
        calls = [0]

        def every_other(*args: Any, **kwargs: Any) -> None:
            calls[0] += 1
            if calls[0] % 2:
                deliver(*args, **kwargs)

        session.client._deliver = every_other  # type: ignore[method-assign]
        for _ in range(4):
            await session.onoff(LIGHT_2G, timeout=0.5, retries=2)
        await mesh.settle()

    run(main())
    assert any("the client lost 0232→0D00 820400" in p for p in mesh.violations())


def test_copies_the_proxy_forwards_are_handed_out_once() -> None:
    """A proxy that forwards every copy it hears: the client's replay list alone keeps messages single."""
    quirks = Quirks(proxy_forwards_every_copy=True)

    async def body(s: Session) -> None:
        for i in range(4):
            assert await s.onoff(FAR, bool(i % 2)) is bool(i % 2)
        reply = await s.client.request_config(
            FAR,
            C.model_subscription_get(FAR, "1000"),
            C.CONFIG_SIG_MODEL_SUBSCRIPTION_LIST,
        )
        assert reply.src == FAR

    session = simulate(
        body, Mesh(quirks=quirks, loss=LossModel(duplicate=0.5), retransmissions=True)
    )
    assert len(session.messages) == 5

    # the same with a client whose replay list admits everything: messages handed out twice
    mesh = Mesh(quirks=quirks, loss=LossModel(duplicate=0.5), retransmissions=True)
    careless = Session(mesh)
    careless.client._is_replay = lambda *_args: False  # type: ignore[method-assign]

    async def main() -> None:
        await careless.attach()
        await careless.onoff(FAR)
        await mesh.settle()

    run(main())
    assert any("handed out" in p for p in mesh.violations())


# ============================================================================= request / response


def test_requests_through_relays() -> None:
    async def body(s: Session) -> None:
        mesh = s.mesh
        assert s.client.proxy_addr == PROXY_A
        assert await s.onoff(FAR) is False
        # JUNG: a Set that changes the state is answered only by the publication to the element group
        assert await s.onoff(FAR, True) is True
        assert s.from_(FAR, M.GEN_ONOFF_STATUS)[-1].dst == 0xC080
        assert mesh.node(FAR).servers.state[FAR].on  # type: ignore[union-attr]
        # the same transaction again (same TID): nothing changes, so the node answers by unicast
        pdu = M.generic_onoff_set(True)
        await s.client.request(FAR, pdu, M.GEN_ONOFF_STATUS)
        await s.client.request(FAR, pdu, M.GEN_ONOFF_STATUS)
        assert s.from_(FAR, M.GEN_ONOFF_STATUS)[-1].dst == CLIENT_ADDRESS
        # Light Lightness on the dimmer, CTL and CTL Temperature on the 2-gang
        reply = await s.client.request(
            DIMMER, M.light_lightness_set(0x8000), M.LIGHT_LIGHTNESS_STATUS
        )
        assert int.from_bytes(reply.params[:2], "little") == 0x8000
        assert await s.onoff(DIMMER) is True
        reply = await s.client.request(
            LIGHT_2G, M.light_ctl_set(0x4000, 4000), M.LIGHT_CTL_STATUS
        )
        assert reply.params[2:4] == (4000).to_bytes(2, "little")
        reply = await s.client.request(
            LIGHT_2G + 1, M.light_ctl_temperature_set(2700), M.LIGHT_CTL_TEMP_STATUS
        )
        assert int.from_bytes(reply.params[:2], "little") == 2700
        reply = await s.client.request(LIGHT_2G, M.light_ctl_get(), M.LIGHT_CTL_STATUS)
        assert reply.params == (0x4000).to_bytes(2, "little") + (2700).to_bytes(
            2, "little"
        )
        # the LBC vendor property servers: a preloaded property read, one written and read back
        get = M.vendor_property_get("admin", 0x000F)
        status_op = M.VENDOR_PROPERTY_STATUS_OPCODES["admin"]
        reply = await s.client.request(FAR, get, status_op, expect_cid=M.JUNG_CID)
        assert reply.params == b"\x0f\x00\x03\x00"
        set_pdu = M.vendor_property_set("user", 0x1007, b"\x2a\x00", ack=True)
        reply = await s.client.request(
            FAR,
            set_pdu,
            M.VENDOR_PROPERTY_STATUS_OPCODES["user"],
            expect_cid=M.JUNG_CID,
        )
        assert reply.params == b"\x07\x10\x03\x2a\x00"
        with pytest.raises(
            TimeoutError
        ):  # a property the server does not hold goes unanswered
            await s.client.request(
                FAR, M.vendor_property_get("user", 0x7777), M.VENDOR_PROPERTY_STATUS_OPCODES["user"],
                expect_cid=M.JUNG_CID, timeout=0.5, retries=1, quiet=True,
            )  # fmt: skip
        # scenes: store the dimmer's state, change it, recall it
        stored = await s.client.request(
            DIMMER, M.scene_store(7), M.SCENE_REGISTER_STATUS
        )
        assert M.decode_scene_register_status(stored.params).scenes == (7,)
        await s.client.request(
            DIMMER, M.light_lightness_set(0), M.LIGHT_LIGHTNESS_STATUS
        )
        recalled = await s.client.request(DIMMER, M.scene_recall(7), M.SCENE_STATUS)
        assert recalled.params == b"\x00\x07\x00"
        assert await s.onoff(DIMMER) is True
        missing = await s.client.request(DIMMER, M.scene_recall(9), M.SCENE_STATUS)
        assert missing.params[0] == M.SCENE_NOT_FOUND
        deleted = await s.client.request(
            DIMMER, M.scene_delete(7), M.SCENE_REGISTER_STATUS
        )
        assert M.decode_scene_register_status(deleted.params).scenes == ()
        current = await s.client.request(DIMMER, M.scene_get(), M.SCENE_STATUS)
        assert current.params == b"\x00\x00\x00"

    session = simulate(body)
    assert session.mesh.stats()["replays"] == 0


def test_without_the_publication_quirk_a_set_is_answered_by_unicast() -> None:
    async def body(s: Session) -> None:
        assert await s.onoff(FAR, True) is True
        assert s.from_(FAR, M.GEN_ONOFF_STATUS)[-1].dst == CLIENT_ADDRESS

    simulate(body, Mesh(quirks=Quirks(set_reply_by_publication=False)))


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_requests_over_a_lossy_air(seed: int) -> None:
    """Loss, duplicates and reordering on every hop: the retries get through, nothing is replayed or reused."""

    async def body(s: Session) -> None:
        for i in range(6):
            assert await s.onoff(FAR, bool(i % 2)) is bool(i % 2)
        reply = await s.client.request_config(
            LIGHT_2G,
            C.model_subscription_get(LIGHT_2G, "1000"),
            C.CONFIG_SIG_MODEL_SUBSCRIPTION_LIST,
        )
        assert set(C.decode_model_subscription_list(reply.opcode, reply.params).addresses) == {
            0xC044, 0xFEF5, 0xC010,
        }  # fmt: skip

    loss = LossModel(loss=0.1, duplicate=0.1, reorder=0.02, gatt_loss=0.02)
    session = simulate(body, Mesh(seed=seed, loss=loss, retransmissions=True))
    assert session.mesh.dropped


def test_segmented_config_with_lost_segment_acks() -> None:
    """The node's acknowledgements of a segmented Publication Set are lost twice: the client retransmits (with new
    sequence numbers), the node re-acknowledges, the Status arrives once."""

    async def body(s: Session) -> None:
        mesh = s.mesh
        rule = mesh.loss.drop(
            lambda p: p.is_segment_ack and p.net is not None and p.net.src == FAR,
            limit=2,
        )
        reply = await s.client.request_config(
            FAR,
            C.model_publication_set(FAR, 0xC0AA, "1000"),
            C.CONFIG_MODEL_PUBLICATION_STATUS,
        )
        assert C.decode_model_publication_status(reply.params).publish_address == 0xC0AA
        assert rule.exhausted
        segments = [n for n in mesh.client_pdus_to(FAR) if n.transport_pdu[0] & 0x80]
        assert len(segments) > 2  # sent again
        assert len({n.seq for n in segments}) == len(segments)
        assert (
            mesh.node(FAR).servers.config.models[(FAR, "1000")].publish_address
            == 0xC0AA
        )  # type: ignore[union-attr]

    simulate(body)


def test_segmented_replies_with_lost_client_acks() -> None:
    """The client's acknowledgements of a segmented Subscription List are lost: the node sends it again with new
    sequence numbers, and the client hands it out once (it recognises the SeqAuth)."""

    async def body(s: Session) -> None:
        mesh = s.mesh
        mesh.loss.drop(lambda p: p.bearer == "gatt-in" and p.is_segment_ack, limit=2)
        reply = await s.client.request_config(
            FAR,
            C.model_subscription_get(FAR, "1000"),
            C.CONFIG_SIG_MODEL_SUBSCRIPTION_LIST,
        )
        assert set(C.decode_model_subscription_list(reply.opcode, reply.params).addresses) == {
            0xC080, 0xFEF5, 0xC011,
        }  # fmt: skip
        await asyncio.sleep(5)
        assert len(s.from_(FAR, C.CONFIG_SIG_MODEL_SUBSCRIPTION_LIST)) == 1
        assert not mesh.node(FAR).sar_failed

    simulate(body)


def test_the_proxy_filter_is_set_under_loss() -> None:
    """The first Set Filter Type request and then the Filter Status answering the second are lost: the client sends
    the filter again until the proxy confirms it (three tries, `FILTER_SET_TRIES`), and group publications reach it."""
    mesh = Mesh()
    mesh.loss.drop(lambda p: p.bearer == "gatt-in" and p.proxy_opcode == 0x00, limit=1)
    mesh.loss.drop(lambda p: p.bearer == "gatt-out" and p.proxy_opcode == 0x03, limit=1)

    async def body(s: Session) -> None:
        proxy = s.mesh.proxy(PROXY_A)
        assert (
            proxy.filter_type != FILTER_BLACKLIST
        )  # still the white list of a new connection
        await asyncio.sleep(10)
        assert proxy.filter_type == FILTER_BLACKLIST
        assert s.client.proxy_addr == PROXY_A
        assert proxy.filter_statuses == 1  # the third request's
        provisioner = s.mesh.provisioner
        assert provisioner is not None
        await provisioner.send_app(
            DIMMER, M.generic_onoff_set(True)
        )  # someone else switches the dimmer
        await asyncio.sleep(1)
        published = s.from_(DIMMER, M.GEN_ONOFF_STATUS)
        assert published
        assert published[-1].dst == 0xC070

    simulate(body, mesh)


def test_an_iv_update_followed_across_the_proxy(
    virtual_monotonic: VirtualClocks,
) -> None:
    async def body(s: Session) -> None:
        mesh, state = s.mesh, s.client.state
        assert await s.onoff(FAR, True)
        mesh.start_iv_update()
        await asyncio.sleep(1)
        assert (state.iv_index, state.iv_update_active, state.tx_iv_index) == (
            1,
            True,
            0,
        )
        seq_before = state.seq
        assert await s.onoff(FAR) is True  # still transmitting under the old index
        assert state.seq > seq_before
        await asyncio.sleep(600)  # periodic beacons keep saying "in progress"
        mesh.complete_iv_update()  # too early: the spec keeps every node in progress for 96 hours at least
        await asyncio.sleep(1)
        assert (state.iv_index, state.iv_update_active) == (1, True)
        virtual_monotonic.wall_offset += IV_UPDATE_MIN_STATE
        await asyncio.sleep(20)  # the next periodic beacon
        assert (state.iv_index, state.iv_update_active) == (1, False)
        assert await s.onoff(FAR, False) is False
        assert state.seq < 0x10  # restarted with the new transmit index
        far = mesh.node(FAR)
        assert far.rpl[CLIENT_ADDRESS][0] == 1
        assert {n.iv_index for n in mesh.client_tx.values()} == {0, 1}

    session = simulate(body)
    assert session.mesh.proxy(PROXY_A).beacons_sent > 60


def test_an_iv_update_missed_while_away_is_recovered_through_the_second_proxy() -> None:
    async def body(s: Session) -> None:
        mesh = s.mesh
        assert await s.onoff(LIGHT_2G) is False
        mesh.proxy(PROXY_A).drop_link()
        assert not s.client.connected
        mesh.start_iv_update()
        await asyncio.sleep(100)
        mesh.complete_iv_update()
        await s.attach(
            PROXY_B, mtu=23
        )  # the smallest ATT MTU: every PDU is SAR-framed both ways
        assert s.client.proxy_addr == PROXY_B
        assert s.client.state.tx_iv_index == 1
        assert await s.onoff(LIGHT_2G, True) is True
        assert await s.onoff(PROXY_A) is False
        reply = await s.client.request_config(
            PROXY_A,
            C.model_subscription_get(PROXY_A, "1000"),
            C.CONFIG_SIG_MODEL_SUBSCRIPTION_LIST,
        )
        assert reply.src == PROXY_A

    simulate(body)


def test_a_key_refresh_followed_from_the_provisioners_messages() -> None:
    """The phone refreshes the NetKey; the client learns the new key from the NetKey Update it overhears (sealed with
    the node's device key), follows Phase 2 and 3, and keeps talking to the far end throughout."""

    async def body(s: Session) -> None:
        mesh, client = s.mesh, s.client
        provisioner = mesh.provisioner
        assert provisioner is not None
        nodes = mesh.configured_nodes()
        for node in nodes:
            await provisioner.expect_ok(
                node, C.netkey_update(NEW_NET_KEY), C.CONFIG_NETKEY_STATUS
            )
        new = NetKeyMaterial.derive(NEW_NET_KEY)
        provisioner.key_refresh(1, new)
        assert client.key_refresh_phase == 1
        assert (
            await s.onoff(FAR, True) is True
        )  # both keys accepted, the old one transmitted
        for node in nodes:
            await provisioner.expect_ok(
                node, C.key_refresh_phase_set(2), C.CONFIG_KEY_REFRESH_PHASE_STATUS
            )
        provisioner.key_refresh(2)
        mesh.key_refresh_moved()
        await asyncio.sleep(1)
        assert client.key_refresh_phase == 2
        assert client.nk.key == NEW_NET_KEY
        assert await s.onoff(FAR) is True
        for node in nodes:
            await provisioner.expect_ok(
                node, C.key_refresh_phase_set(3), C.CONFIG_KEY_REFRESH_PHASE_STATUS
            )
        provisioner.key_refresh(3)
        mesh.key_refresh_moved()
        assert client.key_refresh_phase == 0
        assert client.rx_net_keys == (new,)
        done = client.state.key_refresh
        assert done is not None
        # review 4: moved on by the nodes' own Phase Status, not by the requests
        assert (done.key, done.phase, done.proof) == (NEW_NET_KEY, 3, PROOF_STATUSES)
        assert all(n.net_key == new and n.kr_phase == 0 for n in mesh.nodes.values())
        assert await s.onoff(FAR, False) is False  # only the new key works now
        # phase 1 twice: learnt, then proven by the nodes' NetKey Status (review-4 D11)
        assert s.phases == [1, 1, 2, 0]

    simulate(body)


def test_the_whole_key_refresh_while_the_client_keeps_asking() -> None:
    async def body(s: Session) -> None:
        provisioner = s.mesh.provisioner
        assert provisioner is not None
        refresh = asyncio.ensure_future(
            provisioner.refresh_net_key(NEW_NET_KEY, between=2.0)
        )
        answers = 0
        while not refresh.done():
            await s.onoff(LIGHT_2G)
            answers += 1
            await asyncio.sleep(0.5)
        await refresh
        assert answers > 5
        assert s.client.nk.key == NEW_NET_KEY
        assert await s.onoff(FAR) is False

    simulate(body, Mesh(seed=5, loss=LossModel(loss=0.05)))


async def _a_node_only_the_client_knows(s: Session) -> tuple[int, VaultNode]:
    """Provision and commission a node as `add_device` does; the vault keeps it. The app's database lacks it."""
    mesh, client = s.mesh, s.client
    template = client.cdb.node_by_addr(LIGHT_2G)
    sim_template = mesh.cdb.node_by_addr(LIGHT_2G)
    assert template is not None
    assert sim_template is not None
    count = len(template.elements)
    unicast = free_unicast_block(client.cdb, count)
    assert unicast is not None
    plan = commission.plan(client.cdb, unicast, count, template)
    mesh.add_node(
        node_for(
            sim_template,
            uuid=NEW_UUID,
            unicast=unicast,
            dev_key=NEW_DEV_KEY,
            name="New",
        ),
        [0x0172, DIMMER],
    )
    client.add_node(
        node_for(
            template, uuid=NEW_UUID, unicast=unicast, dev_key=NEW_DEV_KEY, name="New"
        )
    )
    await run_commission(client, plan)
    vault = Vault.create()
    return unicast, vault.remember_provisioned(NEW_UUID, unicast, count, NEW_DEV_KEY)


async def _carry_along(
    client: ProxyClient, node: VaultNode, done: asyncio.Event, *, from_phase: int
) -> list[int]:
    """What Home Assistant does (`vault_refresh.py`): take the node as far as the refresh is proven, again and again.

    Returns the phases it was taken to, in order. `from_phase`: only once the refresh is proven that far (a node
    that missed the earlier phases).
    """
    taken: list[int] = []
    while True:
        target = target_of(client)
        want = wanted(target, client.nk.key, node)
        if want is not None and want.phase >= from_phase:
            if await carry(client, node, want, timeout=1.0):
                taken.append(want.phase)
        elif done.is_set():
            return taken
        await asyncio.sleep(0.25)


@pytest.mark.parametrize("from_phase", [1, 2])
def test_a_node_the_app_does_not_know_keeps_working_after_its_key_refresh(
    from_phase: int,
) -> None:
    """Review-4 D11: the app refreshes the NetKey of the nodes in its database only. A node only the client knows
    (the vault's) is taken along once each phase is proven: NetKey Update, Phase Set 2, Phase Set 3 — or, when it
    missed Phase 1, its NetKey Update in Phase 2 sealed under the old key it still holds. It answers under the new
    key alone afterwards, and was never told to drop the old one before the mesh proved it had."""

    async def body(s: Session) -> None:
        mesh, client = s.mesh, s.client
        unicast, vault_node = await _a_node_only_the_client_knows(s)
        provisioner = mesh.provisioner
        assert provisioner is not None
        targets = [a for a in mesh.configured_nodes() if a != unicast]
        done = asyncio.Event()
        driver = asyncio.ensure_future(
            _carry_along(client, vault_node, done, from_phase=from_phase)
        )
        await provisioner.refresh_net_key(NEW_NET_KEY, nodes=targets, between=5.0)
        await asyncio.sleep(2)
        done.set()
        taken = await driver
        assert taken == ([1, 2, 3] if from_phase == 1 else [2, 3])
        new = NetKeyMaterial.derive(NEW_NET_KEY)
        sim = mesh.node(unicast)
        assert (sim.net_key, sim.new_net_key, sim.kr_phase) == (new, None, 0)
        assert vault_node.key_refresh == RefreshProgress(new.network_id, 3)
        assert client.rx_net_keys == (new,)
        assert await s.onoff(unicast, True) is True  # under the new key alone

    simulate(body)


def test_a_node_restarting_continues_in_the_next_sequence_block() -> None:
    async def body(s: Session) -> None:
        far = s.mesh.node(FAR)
        assert await s.onoff(FAR) is False
        far.restart()
        assert far.seq == 0x10000
        assert (
            await s.onoff(FAR) is False
        )  # a jump forward: the client's replay list accepts it
        assert s.from_(FAR, M.GEN_ONOFF_STATUS)[-1].seq >= 0x10000

    simulate(body)


def test_a_fresh_node_commissioned_and_audited() -> None:
    """`onboarding.commission` runs the plan against a node that has nothing but its keys; the audit reads back what
    the node then holds, and the node answers under the AppKey it was given."""

    async def body(s: Session) -> None:
        mesh, client = s.mesh, s.client
        template = client.cdb.node_by_addr(LIGHT_2G)
        assert template is not None
        count = len(template.elements)
        unicast = free_unicast_block(client.cdb, count)
        assert unicast is not None
        plan = commission.plan(client.cdb, unicast, count, template)
        node = node_for(
            template, uuid=NEW_UUID, unicast=unicast, dev_key=NEW_DEV_KEY, name="New"
        )
        sim_template = mesh.cdb.node_by_addr(LIGHT_2G)
        assert sim_template is not None
        fresh = mesh.add_node(
            node_for(
                sim_template,
                uuid=NEW_UUID,
                unicast=unicast,
                dev_key=NEW_DEV_KEY,
                name="New",
            ),
            [0x0172, DIMMER],
        )
        client.add_node(node)
        with pytest.raises(TimeoutError):  # no AppKey yet: an OnOff Get goes unanswered
            await s.onoff(unicast, timeout=0.5, retries=1)
        await run_commission(client, plan)
        audit = await A.audit_node(A.client_exchange(client), node)
        assert audit.answered
        servers = fresh.servers
        assert servers is not None
        for row in audit.models:
            cfg = servers.config.models[(row.element, row.model)]
            assert row.node_app_keys == tuple(cfg.bind)
            assert row.node_subscribe == tuple(sorted(cfg.subscriptions))
            assert row.node_publish == cfg.publish_address
        onoff = next(
            r for r in audit.models if (r.element, r.model) == (unicast, "1000")
        )
        assert onoff.node_app_keys == (0,)
        assert 0xFEF5 in onoff.node_subscribe  # type: ignore[operator]
        assert onoff.node_publish == plan.groups[0].address
        assert (
            await s.onoff(unicast, True) is True
        )  # answered by the publication to its new element group
        assert s.from_(unicast, M.GEN_ONOFF_STATUS)[-1].dst == plan.groups[0].address
        told = [decode_opcode(step.pdu)[0] for step in plan.steps]
        assert told.count(C.CONFIG_MODEL_APP_BIND) == sum(
            len(c.bind) for c in servers.config.models.values()
        )
        reset = await client.request_config(
            unicast, C.node_reset(), C.CONFIG_NODE_RESET_STATUS
        )
        assert reset.src == unicast
        await asyncio.sleep(1)
        assert not fresh.provisioned
        with pytest.raises(TimeoutError):
            await s.onoff(unicast, timeout=0.5, retries=1)

    simulate(body)


def test_an_existing_node_audits_clean_through_four_relays() -> None:
    async def body(s: Session) -> None:
        node = s.client.cdb.node_by_addr(FAR)
        assert node is not None
        audit = await A.audit_node(A.client_exchange(s.client), node)
        assert audit.answered
        assert audit.findings == []
        assert len(audit.models) > 20

    simulate(body)


def test_nothing_attached_nothing_forwarded() -> None:
    """A node answering while the client is away is not a loss: the proxy had no link to forward it on."""

    async def body(s: Session) -> None:
        mesh = s.mesh
        get = asyncio.ensure_future(s.onoff(FAR, timeout=0.5, retries=1))
        await asyncio.sleep(0.02)  # the Get is on its way
        mesh.proxy(PROXY_A).drop_link()
        with pytest.raises(ConnectionError):
            await get
        await asyncio.sleep(1)
        assert any("no-link" in why for why in mesh.dropped.values())
        await s.attach(PROXY_A)
        assert await s.onoff(FAR) is False

    simulate(body)


def test_a_packet_view_for_drop_rules() -> None:
    packet = Packet("adv", "beacon", None)
    assert not packet.is_segment_ack
    assert not packet.is_segment
    assert packet.proxy_opcode is None
    loss = LossModel()
    rule = loss.drop(lambda p: p.kind == "beacon", limit=1)
    assert loss.ruled_out(packet) == "rule"
    assert loss.ruled_out(packet) is None
    assert rule.exhausted
