"""jhmesh.client: LocalState (sequence / IV index bookkeeping) and ProxyClient driven through a fake GATT link."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import itertools
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jhmesh import client as client_mod
from jhmesh import config_messages as C
from jhmesh import messages as M
from jhmesh import state as state_mod
from jhmesh.cdb import CDB, Element, Node
from jhmesh.client import (
    IV_RECOVERY_MIN_INTERVAL,
    IV_UPDATE_MIN_STATE,
    SEQ_TX_LIMIT,
    AccessMessage,
    Heartbeat,
    LocalState,
    ProxyClient,
    SequenceExhausted,
    SequenceStalled,
    _AckState,
    _TxKey,
    classify_proxy_advert,
)
from jhmesh.crypto import AppKeyMaterial, NetKeyMaterial
from jhmesh.keyrefresh import (
    PROOF_BEACON,
    PROOF_PROXY,
    PROOF_STATUSES,
    KeyRefreshRecord,
)
from jhmesh.pdu import (
    FILTER_BLACKLIST,
    NONCE_APP,
    NONCE_DEVICE,
    PROXY_BEACON,
    PROXY_CONFIG,
    PROXY_NETWORK_PDU,
    PROXY_PROVISIONING,
    NetworkPDU,
    encode_opcode,
    lower_segments_access,
    lower_unsegmented_access,
    network_encrypt,
    parse_lower,
    segment_ack,
    upper_decrypt,
    upper_encrypt_app,
    upper_encrypt_dev,
)

from .conftest import (
    ELEMENT_GROUP_148,
    GATEWAY,
    GROUP_LIVING,
    GROUP_WC,
    LIGHT_2G,
    OUR_SRC,
    PHONE,
    PROXY_NODE,
    SOCKET,
    FakeBleak,
    FastAsyncio,
    Recorder,
    refreshing_cdb,
)

h = bytes.fromhex
DIMMER = 0x0300
ONOFF_STATUS_ON = h("820401")
ONOFF_STATUS_OFF = h("820400")
LEVEL_STATUS = h("82080000")


async def settle(turns: int = 3) -> None:
    """Let tasks spawned by the client (segment acks) run."""
    for _ in range(turns):
        await asyncio.sleep(0)


# ============================================================================= LocalState


def test_local_state_fresh_without_path():
    s = LocalState(None, OUR_SRC)
    assert (s.src, s.seq, s.iv_index, s.iv_update_active, s.tx_iv_index) == (
        OUR_SRC,
        0,
        0,
        False,
        0,
    )
    assert s.to_dict() == {
        "src": "0D00",
        "seq": 0,
        "iv_index": 0,
        "iv_update_active": False,
    }
    assert s.load() is None
    s.persist()  # no path: nothing to write, no error


def test_local_state_creates_the_file(tmp_path: Path):
    p = tmp_path / "state.json"
    s = LocalState(p, OUR_SRC)
    assert (
        json.loads(p.read_text())
        == s.to_stored()
        == {
            "src": "0D00",
            "seq": 0,
            "iv_index": 0,
            "iv_update_active": False,
            "iv_known": False,
            "rpl": {},
            "seq_peak": 0,
            "seq_peak_from": 0,
        }
    )
    assert s.to_dict() == {
        "src": "0D00",
        "seq": 0,
        "iv_index": 0,
        "iv_update_active": False,
    }  # what diagnostics show: identity and counter, no replay list
    assert sorted(f.name for f in tmp_path.iterdir()) == [
        "state.bak",
        "state.json",
        "state.lock",
    ]  # the backup copy and the lock file appear beside it; no `.tmp` is left behind


def test_local_state_load_applies_restart_margin(tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text(
        json.dumps({"src": "0D01", "seq": 100, "iv_index": 5, "iv_update_active": True})
    )
    s = LocalState(p, OUR_SRC)
    assert (s.src, s.seq, s.iv_index, s.iv_update_active, s.tx_iv_index) == (
        0x0D01,
        612,
        5,
        True,
        4,
    )
    assert (
        json.loads(p.read_text())["seq"] == 612
    )  # the margin is persisted immediately
    s.close()  # releases the file lock: the next instance on the same file may start
    s = LocalState(p, OUR_SRC, restart_margin=0)
    assert s.seq == 612
    s.close()
    p.write_text(
        json.dumps({"src": "0D01", "seq": 0xFFFFFF})
    )  # older file without IV fields, seq at the end of the 24-bit space
    s = LocalState(p, OUR_SRC, restart_margin=1)
    assert (s.seq, s.iv_index, s.iv_update_active) == (0xFFFFFF, 0, False)
    with pytest.raises(SequenceExhausted):  # the margin never wraps it back to 0
        s.next_seq()


def test_local_state_configured_src_wins(tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text(
        json.dumps(
            {"src": "0D01", "seq": 100, "iv_index": 5, "iv_update_active": False}
        )
    )
    s = LocalState(p, OUR_SRC, configured_src_wins=True)
    assert (s.src, s.seq, s.iv_index) == (
        OUR_SRC,
        0,
        5,
    )  # new address → fresh sequence space, IV state kept
    assert json.loads(p.read_text())["src"] == "0D00"
    s.close()
    p.write_text(json.dumps({"src": "0D00", "seq": 100}))
    s = LocalState(p, OUR_SRC, configured_src_wins=True)
    assert (s.src, s.seq) == (OUR_SRC, 612)  # same address → sequence carried over
    s.close()
    p.write_text(json.dumps({"src": "0D01", "seq": 100}))
    s = LocalState(p, OUR_SRC)  # default: the stored address wins
    assert (s.src, s.seq) == (0x0D01, 612)


def test_next_seq_and_reserve_seq_never_wrap(tmp_path: Path):
    """The 24-bit sequence stops at SEQ_TX_LIMIT: a silent wrap to 0 would reuse every SeqAuth of the IV index and
    every node's replay list would drop us for good (Mesh Profile §3.8.8); only an IV Update restarts it."""
    p = tmp_path / "state.json"
    s = LocalState(p, OUR_SRC)
    assert (s.next_seq(), s.next_seq(), s.seq) == (0, 1, 2)
    assert (s.reserve_seq(3), s.seq) == (2, 5)
    assert json.loads(p.read_text())["seq"] == 5
    s.seq = SEQ_TX_LIMIT - 1
    assert (s.reserve_seq(2), s.seq) == (SEQ_TX_LIMIT - 1, SEQ_TX_LIMIT + 1)
    with pytest.raises(SequenceExhausted, match="FFFF01 is at the end"):
        s.next_seq()
    assert s.seq == SEQ_TX_LIMIT + 1  # nothing consumed
    assert json.loads(p.read_text())["seq"] == SEQ_TX_LIMIT + 1
    s.seq = SEQ_TX_LIMIT
    assert s.next_seq() == SEQ_TX_LIMIT  # the limit itself is the last usable number
    s.seq = SEQ_TX_LIMIT - 2
    with pytest.raises(SequenceExhausted):
        s.reserve_seq(4)  # would end at SEQ_TX_LIMIT + 1
    assert s.reserve_seq(3) == SEQ_TX_LIMIT - 2
    assert isinstance(SequenceExhausted("x"), ConnectionError)
    assert s.apply_beacon(1, False) is True  # an IV Update restarts the sequence space
    assert (s.seq, s.next_seq()) == (0, 0)


def test_apply_beacon_ignores_lower_or_far_ahead_indexes():
    s = LocalState(None, OUR_SRC)
    s.iv_index, s.seq = 10, 7
    s.iv_known = True  # the 42-ahead recovery bound only applies once an index was actually learnt (CLI-14)
    assert s.apply_beacon(9, False) is False
    assert s.apply_beacon(53, False) is False  # more than 42 ahead
    assert s.apply_beacon(10, False) is False  # nothing changed
    assert (s.iv_index, s.iv_update_active, s.seq) == (10, False, 7)


def test_fresh_state_adopts_a_far_ahead_first_beacon():
    """CLI-14: a state with no stored record starts at iv_index 0 because the export carries none, but the §3.10.6
    recovery bound (42 ahead) is for a node that was already in the network and missed updates — a client that has
    never learnt an index needs to adopt whatever the network is actually at, or it could never join at all."""
    s = LocalState(None, OUR_SRC)
    assert s.iv_known is False
    assert s.apply_beacon(100, False) is True
    assert (s.iv_index, s.tx_iv_index, s.seq) == (100, 100, 0)
    assert s.iv_known is True
    # the recovery bound applies again now that an index has been learnt
    assert s.apply_beacon(200, False) is False
    assert s.iv_index == 100


def test_a_restored_record_without_iv_known_keeps_the_recovery_bound(tmp_path: Path):
    """Backward compatible: a state file from before `iv_known` existed with a non-zero index has learnt one,
    so it must not reopen the unbounded first-beacon window on every restart."""
    p = tmp_path / "state.json"
    p.write_text(
        json.dumps({"src": "0D00", "seq": 5, "iv_index": 3, "iv_update_active": False})
    )
    s = LocalState(p, OUR_SRC)
    assert s.iv_known is True
    assert s.apply_beacon(100, False) is False  # more than 42 ahead: still refused
    assert s.iv_index == 3


def test_a_restored_record_without_iv_known_at_index_zero_adopts_the_first_beacon(
    tmp_path: Path,
):
    """A state file from before `iv_known` existed at index 0 may never have heard a beacon: `__init__` persists
    a fresh state right away, before any beacon. Keeping the 42-ahead bound for it would lock a client that was
    set up but never reached the network out of one whose index is already past 42 — CLI-14's own bug, kept alive
    for every state file written before the fix."""
    p = tmp_path / "state.json"
    p.write_text(
        json.dumps({"src": "0D00", "seq": 5, "iv_index": 0, "iv_update_active": False})
    )
    s = LocalState(p, OUR_SRC)
    assert s.iv_known is False
    assert s.apply_beacon(100, False) is True
    assert (s.iv_index, s.iv_known) == (100, True)


def test_apply_beacon_iv_update_procedure(tmp_path: Path):
    p = tmp_path / "state.json"
    s = LocalState(p, OUR_SRC)
    s.seq = 1234
    s.apply_beacon(0, False, now=0)  # the network's index learnt
    # 1. "IV Update in Progress" with the new index: keep transmitting with the old index and sequence
    assert s.apply_beacon(1, True, now=10) is True
    assert (s.iv_index, s.iv_update_active, s.tx_iv_index, s.seq) == (1, True, 0, 1234)
    assert json.loads(p.read_text()) == {
        "src": "0D00",
        "seq": 1234,
        "iv_index": 1,
        "iv_update_active": True,
        "iv_known": True,
        "rpl": {},
        "seq_peak": 0,
        "seq_peak_from": 0,
        "iv_changed_at": 10,
        # who started it, and when: a beacon (`start_iv_update` is the other way)
        "iv_update_origin": "beacon",
        "iv_update_confirmed": True,
        "iv_update_started_at": 10,
    }
    assert s.apply_beacon(1, True, now=20) is False  # repeated beacon: no change
    s.seq = 1300
    # 2. normal operation resumes, 96 h later at the earliest: the transmit index moves on and sequence numbers
    # restart
    assert s.apply_beacon(1, False, now=10 + IV_UPDATE_MIN_STATE) is True
    assert (s.iv_index, s.iv_update_active, s.tx_iv_index, s.seq) == (1, False, 1, 0)
    assert json.loads(p.read_text()) == {
        "src": "0D00",
        "seq": 0,
        "iv_index": 1,
        "iv_update_active": False,
        "iv_known": True,
        "rpl": {},
        "seq_peak": 1300,
        "seq_peak_from": 0,
        "iv_changed_at": 10 + IV_UPDATE_MIN_STATE,
        "iv_update_origin": "beacon",  # the last update's, kept
        "iv_update_confirmed": True,
        "iv_update_started_at": 10,
    }
    s.seq = 77
    # 3. a lagging node still beaconing "update in progress" for index 1 must not drag us back to index 0
    assert s.apply_beacon(1, True, now=20 + 2 * IV_UPDATE_MIN_STATE) is False
    assert (s.iv_index, s.iv_update_active, s.tx_iv_index, s.seq) == (1, False, 1, 77)
    assert json.loads(p.read_text())["seq"] == 0  # nothing was persisted either
    assert s.apply_beacon(1, False) is False


def test_apply_beacon_stale_update_flag_on_current_index_is_ignored():
    """Mesh Profile §3.10.5: Normal Operation → In Progress is only valid for index + 1. Accepting it for the current
    index would move the transmit index *down* and restart the sequence → SeqAuth reuse → every node drops us."""
    s = LocalState(None, OUR_SRC)
    s.iv_index, s.seq = 3, 50
    assert s.apply_beacon(3, True) is False
    assert (s.iv_index, s.iv_update_active, s.tx_iv_index, s.seq) == (3, False, 3, 50)
    assert s.apply_beacon(4, True) is True  # the genuine next update is still followed
    assert (s.iv_index, s.iv_update_active, s.tx_iv_index, s.seq) == (4, True, 3, 50)


def test_apply_beacon_recovery_without_update_flag_resets_seq():
    s = LocalState(None, OUR_SRC)
    s.seq = 99
    assert s.apply_beacon(42, False) is True  # boundary: exactly 42 ahead is accepted
    assert (s.iv_index, s.iv_update_active, s.tx_iv_index, s.seq) == (42, False, 42, 0)
    s.seq = 5
    assert (
        s.apply_beacon(43, False) is True
    )  # we missed the "in progress" phase entirely (§3.10.6 recovery)
    assert (s.iv_index, s.iv_update_active, s.tx_iv_index, s.seq) == (43, False, 43, 0)


def test_apply_beacon_recovery_into_an_update_in_progress():
    s = LocalState(None, OUR_SRC)
    s.iv_index, s.seq = 3, 50
    assert (
        s.apply_beacon(5, True) is True
    )  # two ahead and in progress: transmit with 4, fresh sequence
    assert (s.iv_index, s.iv_update_active, s.tx_iv_index, s.seq) == (5, True, 4, 0)
    s.seq = 9
    assert s.apply_beacon(6, False) is True  # ... and straight to normal operation on 6
    assert (s.iv_index, s.iv_update_active, s.tx_iv_index, s.seq) == (6, False, 6, 0)


def test_apply_beacon_index_jump_while_update_in_progress():
    s = LocalState(None, OUR_SRC)
    s.iv_index, s.iv_update_active, s.seq = 3, True, 50  # transmitting with index 2
    assert (
        s.apply_beacon(4, True) is True
    )  # transmit index becomes 3 → sequence restarts
    assert (s.iv_index, s.iv_update_active, s.tx_iv_index, s.seq) == (4, True, 3, 0)


def test_apply_beacon_never_lowers_the_transmit_index():
    """Property check over every reachable transition: the transmit index is monotonic and the sequence only restarts
    when it grows."""
    for start_active in (False, True):
        for delta in range(44):
            for flag in (False, True):
                s = LocalState(None, OUR_SRC)
                s.iv_index, s.iv_update_active, s.seq = 10, start_active, 123
                old_tx = s.tx_iv_index
                changed = s.apply_beacon(10 + delta, flag)
                assert s.tx_iv_index >= old_tx, (start_active, delta, flag)
                assert (s.seq == 0) == (s.tx_iv_index > old_tx), (
                    start_active,
                    delta,
                    flag,
                )
                assert changed == (
                    (s.iv_index, s.iv_update_active) != (10, start_active)
                ), (start_active, delta, flag)


# ============================================================================= ProxyClient: link management


async def test_attach_sets_filter_and_learns_proxy_address(
    proxy: ProxyClient, link: FakeBleak, fast: FastAsyncio
):
    assert proxy.nk is proxy.cdb.net_keys[0]
    assert proxy.ak is proxy.cdb.app_keys[0]
    assert (
        proxy._dev_key_of_element[PROXY_NODE]
        == proxy._dev_key_of_element[PROXY_NODE + 1]
        == bytes(15) + b"\x48"
    )
    await proxy.attach(link)
    assert proxy.client is link
    assert proxy.mtu == 69
    assert proxy.connected
    assert proxy.connected_at is not None
    assert link.notify_cb is not None
    assert len(link.config_pdus) == 1
    n = link.config_pdus[0]
    assert (n.ctl, n.ttl, n.seq, n.src, n.dst, n.transport_pdu) == (
        True,
        0,
        0,
        OUR_SRC,
        0x0000,
        b"\x00\x01",
    )
    assert proxy.state.seq == 1
    assert fast.sleeps == [0.3]
    assert proxy.proxy_addr == PROXY_NODE


async def test_attach_without_filter_and_mtu_fallback(proxy: ProxyClient, cdb: CDB):
    link = FakeBleak(cdb, mtu_size=None)
    await proxy.attach(link, filter_blacklist=False)
    assert proxy.mtu == 23
    assert link.config_pdus == []
    assert proxy.proxy_addr is None
    assert proxy.state.seq == 0


def test_connected_property(proxy: ProxyClient, link: FakeBleak):
    assert proxy.connected is False
    proxy.client = link
    assert proxy.connected is True
    link.is_connected = False
    assert proxy.connected is False
    proxy.client = object()  # no is_connected attribute → assumed connected
    assert proxy.connected is True


def test_classify_service_data(proxy: ProxyClient, cdb: CDB):
    nk = cdb.net_keys[0]
    rnd = bytes(range(8))
    assert proxy.classify_service_data(b"") is None
    assert proxy.classify_service_data(b"\x00" + nk.network_id) == ("network-id", None)
    assert proxy.classify_service_data(b"\x00" + nk.network_id + b"\xff") == (
        "network-id",
        None,
    )
    assert proxy.classify_service_data(b"\x00" + bytes(8)) is None
    assert proxy.classify_service_data(
        b"\x01" + nk.node_identity_hash(rnd, LIGHT_2G) + rnd
    ) == ("node-identity", LIGHT_2G)
    assert proxy.classify_service_data(
        b"\x01" + nk.node_identity_hash(rnd, PHONE) + rnd
    ) == ("node-identity", PHONE)
    assert (
        proxy.classify_service_data(b"\x01" + nk.node_identity_hash(rnd, 0x0999) + rnd)
        is None
    )  # not one of our nodes
    assert (
        proxy.classify_service_data(
            b"\x01" + nk.node_identity_hash(rnd, LIGHT_2G) + rnd[:7]
        )
        is None
    )  # truncated
    other = NetKeyMaterial.derive(bytes(16))
    assert (
        proxy.classify_service_data(
            b"\x01" + other.node_identity_hash(rnd, LIGHT_2G) + rnd
        )
        is None
    )
    assert proxy.classify_service_data(b"\x02" + bytes(16)) is None
    assert proxy.classify_service_data(b"\x04" + bytes(16)) is None  # an unknown type


def test_classify_private_identities(proxy: ProxyClient, cdb: CDB):
    """Review-4 P I-4: a proxy with Proxy Privacy on advertises Private Network / Node Identity (Mesh Protocol 1.1)."""
    nk = cdb.net_keys[0]
    other = NetKeyMaterial.derive(bytes(16))
    rnd = bytes(range(8, 16))
    net = nk.private_network_identity(rnd)
    assert proxy.classify_service_data(b"\x02" + net + rnd) == (
        "private-network-id",
        None,
    )
    assert proxy.classify_service_data(b"\x02" + net + rnd[:7]) is None  # truncated
    assert (
        proxy.classify_service_data(b"\x02" + net + bytes(8)) is None
    )  # another Random: another hash
    assert (
        proxy.classify_service_data(b"\x02" + other.private_network_identity(rnd) + rnd)
        is None
    )  # another network
    assert proxy.classify_service_data(
        b"\x03" + nk.private_node_identity(rnd, LIGHT_2G) + rnd
    ) == ("private-node-identity", LIGHT_2G)
    assert (
        proxy.classify_service_data(
            b"\x03" + nk.private_node_identity(rnd, 0x0999) + rnd
        )
        is None
    )  # not one of our nodes
    assert (
        proxy.classify_service_data(
            b"\x03" + other.private_node_identity(rnd, LIGHT_2G) + rnd
        )
        is None
    )
    # neither hash passes for the other kind, nor for the public Node Identity
    assert (
        proxy.classify_service_data(
            b"\x01" + nk.private_node_identity(rnd, LIGHT_2G) + rnd
        )
        is None
    )
    assert (
        proxy.classify_service_data(
            b"\x03" + nk.node_identity_hash(rnd, LIGHT_2G) + rnd
        )
        is None
    )


def test_classify_proxy_advert_without_a_proxy_object(cdb: CDB):
    """The classifier the setup check and the diagnostics share: any keys, any node addresses, any service data."""
    nk, rnd = cdb.net_keys[0], bytes(range(8))
    assert classify_proxy_advert(b"", (nk,), [LIGHT_2G]) is None
    assert classify_proxy_advert(b"\x00" + nk.network_id, (), [LIGHT_2G]) is None
    sd = b"\x03" + nk.private_node_identity(rnd, LIGHT_2G) + rnd
    assert classify_proxy_advert(sd, (nk,), []) is None  # no node of the export
    assert classify_proxy_advert(sd, (nk,), iter([PHONE, LIGHT_2G])) == (
        "private-node-identity",
        LIGHT_2G,
    )


def test_classify_keeps_its_verdicts(
    proxy: ProxyClient, cdb: CDB, monkeypatch: pytest.MonkeyPatch
):
    """Review-4 R4-8: a verdict is kept by the bytes, least recently seen dropped first — another network's Node
    Identity (no node matches: one AES per node) is worked out once, not per advert."""
    hashed: list[int] = []
    real = NetKeyMaterial.node_identity_hash

    def counting(self: NetKeyMaterial, rnd: bytes, address: int) -> bytes:
        hashed.append(address)
        return real(self, rnd, address)

    monkeypatch.setattr(NetKeyMaterial, "node_identity_hash", counting)
    monkeypatch.setattr(client_mod, "CLASSIFY_CACHE_SIZE", 3)
    stranger = bytearray(b"\x01" + bytes(16))  # as bleak hands it over: mutable
    assert proxy.classify_service_data(stranger) is None
    assert len(hashed) == len(cdb.nodes)
    assert proxy.classify_service_data(stranger) is None
    assert len(hashed) == len(cdb.nodes)  # kept
    others = [b"\x00" + bytes([n]) * 8 for n in range(1, 4)]
    proxy.classify_service_data(others[0])
    proxy.classify_service_data(others[1])
    assert (
        proxy.classify_service_data(stranger) is None
    )  # seen again: the most recent now
    proxy.classify_service_data(others[2])  # one too many: the least recent goes
    assert proxy.classify_service_data(stranger) is None
    assert len(hashed) == len(cdb.nodes)
    for other in others:
        proxy.classify_service_data(other)
    assert (
        proxy.classify_service_data(stranger) is None
    )  # dropped meanwhile: worked out again
    assert len(hashed) == 2 * len(cdb.nodes)


def test_lookups_and_verdicts_follow_add_node_and_remove_node(
    proxy: ProxyClient, cdb: CDB
):
    """Review-4 R4-9: a node made known or forgotten re-indexes the addresses and drops the advert verdicts (its
    Node Identity was nobody's before, and is nobody's after)."""
    unicast = 0x0700
    rnd = bytes(range(8))
    identity = b"\x01" + cdb.net_keys[0].node_identity_hash(rnd, unicast) + rnd
    assert cdb.element(unicast) is None
    assert proxy.classify_service_data(identity) is None
    node = Node("00000000-0000-4000-8000-0000000000aa", "new", unicast, bytes(16), 1)
    node.elements = [Element(unicast + i, 1 + i, [], node) for i in range(2)]
    proxy.add_node(node)
    assert cdb.index_is_current()
    assert cdb.element(unicast + 1) is node.elements[1]
    assert cdb.node_by_addr(unicast) is node
    assert proxy.classify_service_data(identity) == ("node-identity", unicast)
    proxy.remove_node(node)
    assert cdb.index_is_current()
    assert cdb.node_by_addr(unicast) is None
    assert proxy.classify_service_data(identity) is None


async def test_detach_disconnects_once(attached: ProxyClient, link: FakeBleak):
    await attached.detach()
    assert link.disconnect_calls == 1
    assert attached.client is None
    assert attached.connected_at is None
    assert not attached.connected
    await attached.detach()  # nothing attached any more
    assert link.disconnect_calls == 1


async def test_detach_variants(proxy: ProxyClient, link: FakeBleak, cdb: CDB):
    await proxy.attach(link)
    await proxy.detach(disconnect=False)
    assert link.disconnect_calls == 0
    assert proxy.client is None
    link2 = FakeBleak(cdb)
    link2.disconnect_error = RuntimeError("boom")
    await proxy.attach(link2)
    await proxy.detach()  # transport errors on disconnect are swallowed
    assert link2.disconnect_calls == 1
    assert proxy.client is None


async def test_handle_disconnected_fails_pending_requests(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
):
    task = asyncio.create_task(
        attached.request(
            PROXY_NODE, M.generic_onoff_get(), M.GEN_ONOFF_STATUS, timeout=5.0
        )
    )
    await settle()
    assert len(attached._waiters) == 1
    assert len(link.sent_access()) == 1
    attached._ack_waiters[(PROXY_NODE, 1)] = _AckState()
    attached.handle_disconnected()
    with pytest.raises(ConnectionError, match="proxy disconnected"):
        await task
    assert attached.client is None
    assert attached.connected_at is None
    assert not attached.connected
    assert attached._waiters == []
    assert attached._ack_waiters == {}
    assert recorder.disconnects == 1
    attached.handle_disconnected()  # idempotent
    assert recorder.disconnects == 2
    with pytest.raises(ConnectionError, match="not connected"):
        await attached.send_access(PROXY_NODE, M.generic_onoff_get())


async def test_client_without_callbacks(
    cdb: CDB, state: LocalState, link: FakeBleak, fast: FastAsyncio
):
    proxy = ProxyClient(cdb, state)
    await proxy.attach(link)
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)
    link.send_beacon(iv_index=1, iv_update=True)
    assert state.iv_index == 1
    proxy.handle_disconnected()
    assert proxy.client is None


@pytest.mark.parametrize("failure", ["notify", "write"])
async def test_attach_failure_releases_the_connection(
    proxy: ProxyClient, link: FakeBleak, recorder: Recorder, failure: str
):
    """A client whose attach failed must not stay referenced as *the* link (the next attach would silently overwrite
    it and its GATT connection — one of the few slots of an ESPHome proxy — would leak)."""
    if failure == "notify":
        link.notify_error = OSError("subscribe failed")
    else:
        link.write_error = OSError("write failed")
    with pytest.raises(
        (OSError, ConnectionError),
        match="subscribe failed" if failure == "notify" else "write failed",
    ):
        await proxy.attach(link)
    assert proxy.client is None
    assert proxy.connected_at is None
    assert not proxy.connected
    assert proxy._filter_type is None
    assert link.disconnect_calls == 1
    assert not link.is_connected
    assert recorder.disconnects == 0  # attach raised; that *is* the notification
    assert proxy.state.seq == (0 if failure == "notify" else 1)


async def test_attach_failure_cleanup_survives_a_failing_disconnect(
    proxy: ProxyClient, link: FakeBleak
):
    link.notify_error = OSError("subscribe failed")
    link.disconnect_error = RuntimeError("already gone")
    with pytest.raises(OSError, match="subscribe failed"):
        await proxy.attach(link)
    assert proxy.client is None
    assert link.disconnect_calls == 1


async def test_attach_cleanup_when_the_link_dropped_meanwhile(
    proxy: ProxyClient, link: FakeBleak
):
    """handle_disconnected() fired while attach was still running: nothing left to release, the error still surfaces."""

    async def drop_then_fail(char: str, cb: Any) -> None:
        proxy.handle_disconnected(link)
        raise OSError("gone")

    link.start_notify = drop_then_fail
    with pytest.raises(OSError, match="gone"):
        await proxy.attach(link)
    assert proxy.client is None
    assert link.disconnect_calls == 0


async def test_handle_disconnected_ignores_clients_that_are_not_ours(
    attached: ProxyClient, link: FakeBleak, cdb: CDB, recorder: Recorder
):
    stale = FakeBleak(cdb, address="11:22:33:44:55:66")
    attached.handle_disconnected(
        stale
    )  # a late callback from an earlier, failed client
    assert attached.client is link
    assert attached.connected
    assert recorder.disconnects == 0
    attached.handle_disconnected(link)  # the current client: the real thing
    assert attached.client is None
    assert recorder.disconnects == 1
    attached.handle_disconnected(
        link
    )  # ... and once we have no client, a late callback is ignored too
    assert recorder.disconnects == 1
    attached.handle_disconnected()  # transports that do not say which client: always honoured
    assert recorder.disconnects == 2


async def test_attach_waits_for_the_proxy_beacon_before_the_first_filter(
    proxy: ProxyClient, link: FakeBleak, recorder: Recorder, state: LocalState
):
    """The IV index moved while we were away: with `beacon_wait` the filter is encrypted with the index from the
    proxy's beacon (sent right after the subscription) instead of the stale stored one, so it is not dropped."""
    link.iv_index, link.beacon_on_subscribe = 7, True
    await proxy.attach(link, beacon_wait=1.0)
    assert (state.iv_index, state.tx_iv_index) == (7, 7)
    assert len(recorder.beacons) == 1
    assert len(link.config_pdus) == 1
    assert (
        link.config_pdus[0].seq == 0
    )  # exactly one filter request, fresh sequence space
    assert proxy.proxy_addr == PROXY_NODE
    assert proxy._filter_type == FILTER_BLACKLIST
    await settle()
    assert (
        len(link.config_pdus) == 1
    )  # the beacon arrived before the filter: nothing to re-send


async def test_attach_beacon_wait_times_out_without_a_beacon(
    proxy: ProxyClient,
    link: FakeBleak,
    fast: FastAsyncio,
    caplog: pytest.LogCaptureFixture,
):
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        await proxy.attach(link, beacon_wait=0.01)
    assert "no beacon within 0.0s" in caplog.text
    assert len(link.config_pdus) == 1
    assert proxy.proxy_addr == PROXY_NODE
    assert fast.sleeps == [0.3]


async def test_attach_without_beacon_wait_sends_the_filter_at_once_and_again_after_the_beacon(
    proxy: ProxyClient, link: FakeBleak, state: LocalState
):
    link.iv_index, link.beacon_on_subscribe, link.strict_decrypt = 7, True, False
    await proxy.attach(link)
    assert len(link.outgoing) == 1
    assert state.iv_index == 7  # the stale-IV filter went out before the beacon
    assert link.config_pdus == []
    assert len(link.undecryptable) == 1  # ... and the proxy could not decrypt it
    assert proxy.proxy_addr is None
    await settle()
    assert len(link.config_pdus) == 1
    assert link.config_pdus[0].seq == 0  # the re-send under IV 7 is what gets through
    assert proxy.proxy_addr == PROXY_NODE
    assert len(link.undecryptable) == 1


# ============================================================================= ProxyClient: sending


async def test_send_access_unsegmented(attached: ProxyClient, link: FakeBleak):
    seq = await attached.send_access(PROXY_NODE, M.generic_onoff_set(True, tid=1))
    assert seq == 1  # seq 0 went into the Set Filter Type request
    assert link.sent_access() == [
        (OUR_SRC, PROXY_NODE, 5, 1, M.generic_onoff_set(True, tid=1))
    ]
    assert link.net_pdus[-1].ivi == 0
    seq = await attached.send_access(GROUP_WC, M.generic_onoff_get(), ttl=2)
    assert seq == 2
    assert link.sent_access()[-1] == (OUR_SRC, GROUP_WC, 2, 2, M.generic_onoff_get())
    assert attached.state.seq == 3
    await attached.send_access(
        PROXY_NODE, bytes(11)
    )  # 11 bytes is the largest unsegmented access PDU
    assert len(link.net_pdus) == 3
    assert not link.net_pdus[-1].transport_pdu[0] & 0x80
    assert attached._ack_waiters == {}


async def test_send_access_small_mtu_uses_proxy_sar(proxy: ProxyClient, cdb: CDB):
    link = FakeBleak(cdb, mtu_size=23)
    await proxy.attach(link)
    assert proxy.proxy_addr == PROXY_NODE
    assert len(link.writes) == 1  # the 19-byte config PDU still fits one notification
    await proxy.send_access(PROXY_NODE, M.light_ctl_set(1, 2700, tid=1))
    frames = link.writes[1:]
    assert [f[0] for f in frames] == [
        0x40,
        0xC0,
    ]  # first / last SAR fragments of a network PDU
    assert all(len(f) <= 20 for f in frames)
    assert link.sent_access() == [
        (OUR_SRC, PROXY_NODE, 5, 1, M.light_ctl_set(1, 2700, tid=1))
    ]
    # ...and the same for what the proxy sends us
    link.send_access(PROXY_NODE, OUR_SRC, bytes(11))
    assert proxy.state.rpl[PROXY_NODE][1] == link.seq


async def test_send_access_segmented_unicast_acknowledged(
    attached: ProxyClient, link: FakeBleak, fast: FastAsyncio
):
    link.auto_ack()
    access = h("8245") + bytes(range(18))  # 20 bytes → 24-byte upper PDU → 2 segments
    seq0 = await attached.send_access(PROXY_NODE, access)
    assert seq0 == 1
    assert [(n.seq, n.dst, n.ttl) for n in link.net_pdus] == [
        (1, PROXY_NODE, 5),
        (2, PROXY_NODE, 5),
    ]
    assert link.sent_access() == [(OUR_SRC, PROXY_NODE, 5, 1, access)]
    assert attached.state.seq == 3
    assert attached._ack_waiters == {}
    # attach only: every segment acknowledged at once needs no grace for late acks (review-3 T4), nor a round
    assert fast.sleeps == [0.3]


async def test_send_access_segmented_partial_ack_retransmits_missing_segment(
    attached: ProxyClient, link: FakeBleak
):
    link.auto_ack(drop_once={1})
    access = h("8245") + bytes(range(18))
    seq0 = await attached.send_access(PROXY_NODE, access)
    segs = [n for n in link.net_pdus if not n.ctl]
    assert [n.seq for n in segs] == [
        1,
        2,
        3,
    ]  # seg 0, seg 1 (lost), seg 1 again with a fresh seq
    hdrs = [int.from_bytes(n.transport_pdu[1:4], "big") for n in segs]
    assert [(x >> 5) & 0x1F for x in hdrs] == [0, 1, 1]
    assert {(x >> 10) & 0x1FFF for x in hdrs} == {seq0 & 0x1FFF}
    assert link.sent_access() == [(OUR_SRC, PROXY_NODE, 5, seq0, access)]
    assert attached.state.seq == 4
    assert attached._ack_waiters == {}


async def test_send_access_segmented_times_out_without_acks(
    attached: ProxyClient, link: FakeBleak, fast: FastAsyncio
):
    with pytest.raises(
        TimeoutError, match="segmented message to 0148 not acknowledged"
    ):
        await attached.send_access(PROXY_NODE, bytes(20))
    segs = [n for n in link.net_pdus if not n.ctl]
    assert len(segs) == 2 * client_mod.SEGMENT_RETRIES
    assert [n.seq for n in segs] == list(
        range(1, 9)
    )  # every retransmission uses a fresh sequence number
    assert attached._ack_waiters == {}
    assert 0.25 not in fast.sleeps
    # the lock is released again
    await attached.send_access(PROXY_NODE, M.generic_onoff_get())


async def test_send_access_segmented_group_sends_twice_without_acks(
    attached: ProxyClient, link: FakeBleak, fast: FastAsyncio
):
    seq0 = await attached.send_access(
        GROUP_WC, bytes(30)
    )  # 34-byte upper PDU → 3 segments
    assert seq0 == 1
    segs = [n for n in link.net_pdus if not n.ctl]
    assert [n.seq for n in segs] == [1, 2, 3, 4, 5, 6]
    assert [m[1:] for m in link.sent_access()] == [(GROUP_WC, 5, 1, bytes(30))] * 2
    assert fast.sleeps == [0.3, 0.3]
    assert attached._ack_waiters == {}


# ============================================================================= ProxyClient: request / collect


async def test_request_matches_reply_by_source_regardless_of_destination(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
):
    def respond(n):
        if n.dst == PROXY_NODE and not n.ctl:
            # JUNG firmware publishes the status to the element's group instead of replying to us
            asyncio.get_running_loop().call_soon(
                link.send_access, PROXY_NODE, ELEMENT_GROUP_148, ONOFF_STATUS_ON
            )

    link.responders.append(respond)
    msg = await attached.request(
        PROXY_NODE, M.generic_onoff_get(), M.GEN_ONOFF_STATUS, timeout=1.0
    )
    assert isinstance(msg, AccessMessage)
    assert (msg.src, msg.dst, msg.opcode, msg.company_id, msg.params, msg.key) == (
        PROXY_NODE,
        ELEMENT_GROUP_148,
        M.GEN_ONOFF_STATUS,
        None,
        b"\x01",
        "app0",
    )
    assert (
        str(msg)
        == f"0148→C061 ttl=3 seq={msg.seq:06X} [app0] Generic OnOff Status present=ON"
    )
    assert recorder.messages == [msg]
    assert attached._waiters == []


async def test_request_ignores_other_sources_and_opcodes(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
):
    def respond(n):
        loop = asyncio.get_running_loop()
        loop.call_soon(
            link.send_access, LIGHT_2G, OUR_SRC, ONOFF_STATUS_OFF
        )  # right opcode, wrong element
        loop.call_soon(
            link.send_access, PROXY_NODE, OUR_SRC, LEVEL_STATUS
        )  # right element, wrong opcode
        loop.call_soon(link.send_access, PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)

    link.responders.append(respond)
    msg = await attached.request(
        PROXY_NODE, M.generic_onoff_get(), M.GEN_ONOFF_STATUS, timeout=1.0
    )
    assert (msg.src, msg.params) == (PROXY_NODE, b"\x01")
    assert [(m.src, m.opcode) for m in recorder.messages] == [
        (LIGHT_2G, M.GEN_ONOFF_STATUS),
        (PROXY_NODE, M.GEN_LEVEL_STATUS),
        (PROXY_NODE, M.GEN_ONOFF_STATUS),
    ]


async def test_request_retries_then_times_out(attached: ProxyClient, link: FakeBleak):
    with pytest.raises(TimeoutError, match="no response from 0148"):
        await attached.request(
            PROXY_NODE,
            M.generic_onoff_get(),
            M.GEN_ONOFF_STATUS,
            timeout=0.01,
            retries=2,
        )
    assert [a[4] for a in link.sent_access()] == [M.generic_onoff_get()] * 2
    assert attached._waiters == []


async def test_request_to_group_accepts_any_source(
    attached: ProxyClient, link: FakeBleak
):
    link.responders.append(
        lambda n: asyncio.get_running_loop().call_soon(
            link.send_access, DIMMER, GROUP_WC, ONOFF_STATUS_OFF
        )
    )
    msg = await attached.request(
        GROUP_WC, M.generic_onoff_get(), M.GEN_ONOFF_STATUS, timeout=1.0
    )
    assert (msg.src, msg.dst) == (DIMMER, GROUP_WC)


async def test_request_vendor_reply_must_match_company_id(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
):
    status = (
        h("c52705") + h("0350") + b"\x01\x02"
    )  # LBC Admin Property Status, KeyMode = Scene

    def respond(n):
        loop = asyncio.get_running_loop()
        loop.call_soon(
            link.send_access, PROXY_NODE, OUR_SRC, b"\x05\x00"
        )  # SIG opcode 0x05: same number, no company id
        loop.call_soon(link.send_access, PROXY_NODE, OUR_SRC, status)

    link.responders.append(respond)
    msg = await attached.request(
        PROXY_NODE,
        M.vendor_property_get("admin", 0x5003),
        0x05,
        expect_cid=M.JUNG_CID,
        timeout=1.0,
    )
    assert (msg.opcode, msg.company_id, msg.params) == (
        0x05,
        M.JUNG_CID,
        h("0350") + b"\x01\x02",
    )
    assert str(msg).endswith(
        "LBC Admin Property Status prop 0x5003 access=1 value=02 key_mode=scene"
    )
    assert len(recorder.messages) == 2


async def test_collect_gathers_one_status_per_element(
    attached: ProxyClient, link: FakeBleak, fast: FastAsyncio
):
    def respond(n):
        if n.dst != GROUP_WC:
            return
        loop = asyncio.get_running_loop()
        loop.call_soon(link.send_access, PROXY_NODE, GROUP_WC, ONOFF_STATUS_ON)
        loop.call_soon(link.send_access, DIMMER, GROUP_WC, ONOFF_STATUS_OFF)
        loop.call_soon(
            link.send_access, PROXY_NODE, GROUP_WC, ONOFF_STATUS_ON
        )  # duplicate from the same element
        loop.call_soon(
            link.send_access, LIGHT_2G, OUR_SRC, LEVEL_STATUS
        )  # different opcode

    link.responders.append(respond)
    got = await attached.collect(
        GROUP_WC, M.generic_onoff_get(), M.GEN_ONOFF_STATUS, window=0.02
    )
    assert [(m.src, m.params) for m in got] == [
        (PROXY_NODE, b"\x01"),
        (DIMMER, b"\x00"),
    ]
    assert attached._waiters == []
    assert link.sent_access()[-1][1:] == (GROUP_WC, 5, 1, M.generic_onoff_get())


async def test_a_status_published_while_the_request_queues_is_not_its_answer(
    attached: ProxyClient, link: FakeBleak, fast: FastAsyncio
):
    """The reply waiter is registered once the send lock is taken: a status the node published while the
    request was still queued behind another send (a segmented Config message, say) is not taken as its answer
    — nor collected by a `collect` queued the same way."""

    def respond(n):
        if not n.ctl:
            asyncio.get_running_loop().call_soon(
                link.send_access, PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON
            )

    link.responders.append(respond)
    async with attached._send_lock:  # another send in flight
        request = asyncio.ensure_future(
            attached.request(
                PROXY_NODE, M.generic_onoff_get(), M.GEN_ONOFF_STATUS, timeout=1.0
            )
        )
        gathered = asyncio.ensure_future(
            attached.collect(
                GROUP_WC, M.generic_onoff_get(), M.GEN_ONOFF_STATUS, window=0.02
            )
        )
        await settle()
        assert attached._waiters == []
        link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_OFF)  # stale: before the Get
        await settle()
        assert not request.done()
    msg = await request
    assert msg.params == b"\x01"  # the answer to the Get, not the stale OFF
    assert [m.params for m in await gathered] == [b"\x01"]
    assert attached._waiters == []


# ============================================================================= ProxyClient: receiving


async def test_receive_unsegmented_app_message(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
):
    seq = link.send_access(PROXY_NODE, GROUP_WC, h("820401000a"), ttl=4)
    assert len(recorder.messages) == 1
    m = recorder.messages[0]
    assert (
        m.src,
        m.dst,
        m.ttl,
        m.seq,
        m.opcode,
        m.company_id,
        m.params,
        m.access_pdu,
        m.key,
    ) == (
        PROXY_NODE,
        GROUP_WC,
        4,
        seq,
        M.GEN_ONOFF_STATUS,
        None,
        h("01000a"),
        h("820401000a"),
        "app0",
    )
    assert m.received <= time.monotonic()
    assert (
        str(m)
        == f"0148→C00F ttl=4 seq={seq:06X} [app0] Generic OnOff Status present=ON target=OFF remaining=10x100ms"
    )
    assert attached.state.rpl[PROXY_NODE] == (0, seq)


async def test_receive_devkey_messages(
    attached: ProxyClient, link: FakeBleak, cdb: CDB, recorder: Recorder
):
    node = cdb.node_by_addr(PROXY_NODE)
    assert node is not None
    link.send_devkey(PROXY_NODE, OUR_SRC, h("80030000000000"), node.dev_key)
    m = recorder.messages[-1]
    assert (m.key, m.opcode, m.params) == ("dev:0148", 0x8003, bytes(5))
    assert str(m).endswith("[dev:0148] Config AppKey Status Success: netkey=0 appkey=0")
    # a relayed config message from the phone to a node decrypts with the *destination's* device key
    target = cdb.node_by_addr(LIGHT_2G)
    assert target is not None
    assert target.dev_key != cdb.nodes[0].dev_key
    link.send_devkey(PHONE, LIGHT_2G, h("800800"), target.dev_key)
    m = recorder.messages[-1]
    assert (m.key, m.src, m.dst, m.opcode) == ("dev:0232", PHONE, LIGHT_2G, 0x8008)
    # device-key traffic between unknown elements, or with a key we do not have, is dropped
    link.send_devkey(0x0999, 0x0998, h("800800"), bytes(16))
    link.send_devkey(PROXY_NODE, OUR_SRC, h("800800"), bytes(range(16)))
    assert len(recorder.messages) == 2


async def test_receive_segmented_message_is_reassembled_and_acked(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder, fast: FastAsyncio
):
    """Mesh Protocol 1.1 §3.5.3.4: the SAR Acknowledgment timer acknowledges what arrived so far; the last segment
    acknowledges all at once; a segment of the completed message is acknowledged again, at most every 150 ms."""
    access = h("8245000100") + b"".join(
        i.to_bytes(2, "little") for i in range(1, 9)
    )  # 21 bytes
    seq0, pdus = link.access_pdus(PROXY_NODE, OUR_SRC, access)
    assert len(pdus) == 3
    fast.sleeps.clear()
    for p in pdus[:-1]:
        link.deliver(PROXY_NETWORK_PDU, p)
    await settle()
    assert recorder.messages == []
    # the timer, started again by the second segment, fired: min(SegN + 0.5, 2.5) * 60 ms
    assert fast.sleeps == [pytest.approx(0.15)]
    assert link.sent_acks() == [(OUR_SRC, PROXY_NODE, seq0 & 0x1FFF, 0b011)]
    link.deliver(PROXY_NETWORK_PDU, pdus[-1])
    assert len(recorder.messages) == 1
    m = recorder.messages[0]
    assert (m.src, m.dst, m.access_pdu, m.key, m.opcode) == (
        PROXY_NODE,
        OUR_SRC,
        access,
        "app0",
        M.SCENE_REGISTER_STATUS,
    )
    assert m.seq == seq0 + 2  # network seq of the segment that completed the message
    assert (
        attached.state.rpl[PROXY_NODE]
        == (
            0,
            seq0 + 2,
        )
    )  # the replay list holds the last sequence number accepted, not the SeqAuth (§3.8.8)
    await settle()
    assert link.sent_acks()[1:] == [(OUR_SRC, PROXY_NODE, seq0 & 0x1FFF, 0b111)]
    ack = [n for n in link.net_pdus if n.ctl][-1]
    assert ack.ttl == 5
    assert ack.seq == 2
    # a repeated segment right after completion is not acknowledged again within 150 ms (§3.5.3.4) ...
    link.deliver(PROXY_NETWORK_PDU, pdus[0])
    await settle()
    assert len(link.sent_acks()) == 2
    # ... later it is, as complete, and the message is not delivered twice
    attached._segments[(PROXY_NODE, seq0 & 0x1FFF)]["acked_at"] -= 0.15
    link.deliver(PROXY_NETWORK_PDU, pdus[0])
    await settle()
    assert len(recorder.messages) == 1
    assert link.sent_acks()[1:] == [(OUR_SRC, PROXY_NODE, seq0 & 0x1FFF, 0b111)] * 2


async def test_receive_segmented_partial_ack_when_last_segment_arrives_first(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
):
    seq0, pdus = link.access_pdus(PROXY_NODE, OUR_SRC, bytes(20))
    assert len(pdus) == 2
    link.deliver(PROXY_NETWORK_PDU, pdus[1])
    await settle()
    assert link.sent_acks() == [(OUR_SRC, PROXY_NODE, seq0 & 0x1FFF, 0b10)]
    assert recorder.messages == []
    link.deliver(PROXY_NETWORK_PDU, pdus[0])
    await settle()
    assert link.sent_acks()[-1] == (OUR_SRC, PROXY_NODE, seq0 & 0x1FFF, 0b11)
    assert len(recorder.messages) == 1
    assert recorder.messages[0].access_pdu == bytes(20)
    assert (
        recorder.messages[0].seq == seq0
    )  # the segment that completed it was segment 0


async def test_receive_segmented_group_message_is_not_acked(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
):
    _seq0, pdus = link.access_pdus(LIGHT_2G, GROUP_WC, bytes(20))
    link.deliver(
        PROXY_NETWORK_PDU, pdus[1]
    )  # last segment first: no partial ack for group traffic
    link.deliver(PROXY_NETWORK_PDU, pdus[0])
    await settle()
    assert link.sent_acks() == []
    assert [(m.src, m.dst) for m in recorder.messages] == [(LIGHT_2G, GROUP_WC)]
    link.deliver(
        PROXY_NETWORK_PDU, pdus[0]
    )  # repeat after completion: no re-ack either
    await settle()
    assert link.sent_acks() == []
    assert len(recorder.messages) == 1


async def test_receive_segmented_with_64bit_transmic(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
):
    seq0, pdus = link.access_pdus(
        PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON, szmic=1
    )  # 3 + 8 bytes: a single segment
    assert len(pdus) == 1
    link.deliver(PROXY_NETWORK_PDU, pdus[0])
    await settle()
    assert recorder.messages[0].access_pdu == ONOFF_STATUS_ON
    assert link.sent_acks() == [(OUR_SRC, PROXY_NODE, seq0 & 0x1FFF, 0b1)]


async def test_receive_segmented_devkey_message(
    attached: ProxyClient, link: FakeBleak, cdb: CDB, recorder: Recorder
):
    node = cdb.node_by_addr(PROXY_NODE)
    assert node is not None
    composition = b"\x02" + bytes(range(30))
    seq0 = link.send_devkey(PROXY_NODE, OUR_SRC, composition, node.dev_key)
    await settle()
    m = recorder.messages[0]
    assert (m.key, m.opcode, m.access_pdu) == ("dev:0148", 0x02, composition)
    assert str(
        m
    ).endswith(
        "Config Composition Data Status ?? <30 bytes>"
    )  # not a real page 0: under a device key, undecoded bytes are never shown (they could be a key)
    assert link.sent_acks() == [(OUR_SRC, PROXY_NODE, seq0 & 0x1FFF, 0b111)]


async def test_stale_segment_state_is_discarded(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
):
    seq_a, pdus_a = link.access_pdus(PROXY_NODE, OUR_SRC, bytes(20))
    link.deliver(PROXY_NETWORK_PDU, pdus_a[0])
    key_a = (PROXY_NODE, seq_a & 0x1FFF)
    assert key_a in attached._segments
    attached._segments[key_a]["t"] -= 11  # pretend the first half arrived > 10 s ago
    seq_b, pdus_b = link.access_pdus(LIGHT_2G, OUR_SRC, bytes(20))
    link.deliver(PROXY_NETWORK_PDU, pdus_b[0])
    assert key_a not in attached._segments
    assert (LIGHT_2G, seq_b & 0x1FFF) in attached._segments
    link.deliver(
        PROXY_NETWORK_PDU, pdus_a[1]
    )  # the late second half starts over → partial ack only
    await settle()
    assert recorder.messages == []
    # the discarded reassembly's acknowledgment timer went with it (§3.5.3.4): no 0b01 for the first half
    assert [a for a in link.sent_acks() if a[1] == PROXY_NODE] == [
        (OUR_SRC, PROXY_NODE, seq_a & 0x1FFF, 0b10)
    ]


async def test_replay_protection(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
):
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON, seq=100)
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON, seq=100)  # exact replay
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_OFF, seq=99)  # older
    assert len(recorder.messages) == 1
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_OFF, seq=101)
    assert len(recorder.messages) == 2
    link.send_access(
        LIGHT_2G, OUR_SRC, ONOFF_STATUS_OFF, seq=5
    )  # sources are tracked independently
    assert len(recorder.messages) == 3
    assert attached.state.rpl == {PROXY_NODE: (0, 101), LIGHT_2G: (0, 5)}


async def test_undecryptable_messages_are_ignored(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
):
    foreign = NetKeyMaterial.derive(bytes(16))
    link.deliver(
        PROXY_NETWORK_PDU,
        network_encrypt(
            foreign, 0, False, 3, 1, PROXY_NODE, OUR_SRC, h("660102030405")
        ),
    )  # other network
    upper = upper_encrypt_app(link.ak, 0, 2, PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)
    link.deliver(
        PROXY_NETWORK_PDU,
        network_encrypt(
            link.nk,
            0,
            False,
            3,
            2,
            PROXY_NODE,
            OUR_SRC,
            lower_unsegmented_access(link.ak.aid ^ 1, upper),
        ),
    )  # unknown AID
    tampered = upper[:-1] + bytes([upper[-1] ^ 1])
    link.deliver(
        PROXY_NETWORK_PDU,
        network_encrypt(
            link.nk,
            0,
            False,
            3,
            2,
            PROXY_NODE,
            OUR_SRC,
            lower_unsegmented_access(link.ak.aid, tampered),
        ),
    )  # bad TransMIC
    link.send_access(
        PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON, iv_index=2
    )  # wrong IV index → NetMIC fails
    link.send_access(
        OUR_SRC, GROUP_WC, ONOFF_STATUS_ON, seq=0
    )  # our own message (the filter request's number) echoed back
    assert recorder.messages == []
    assert attached.state.rpl == {}


async def test_on_message_exception_is_logged(
    attached: ProxyClient, link: FakeBleak, caplog: pytest.LogCaptureFixture
):
    def boom(m: AccessMessage) -> None:
        raise RuntimeError("handler broke")

    attached.on_message = boom
    with caplog.at_level(logging.ERROR, logger="jhmesh"):
        link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)
    assert "on_message handler failed" in caplog.text
    assert (
        attached.state.rpl[PROXY_NODE][1] == link.seq
    )  # the message still counted as received


# ============================================================================= ProxyClient: beacons


async def test_beacon_iv_update_is_followed(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder, state: LocalState
):
    state.seq = 50
    attached.state.rpl[PROXY_NODE] = (0, 10)
    link.send_beacon(iv_index=1, iv_update=True)
    assert (state.iv_index, state.iv_update_active, state.tx_iv_index, state.seq) == (
        1,
        True,
        0,
        50,
    )
    assert attached.state.rpl == {
        PROXY_NODE: (0, 10)
    }  # IV 0 is still receivable: the entry stays
    assert len(recorder.beacons) == 1
    b = recorder.beacons[0]
    assert (b.iv_index, b.iv_update, b.key_refresh, b.authenticated, b.network_id) == (
        1,
        True,
        False,
        True,
        link.nk.network_id,
    )
    # traffic under the new index (IVI=1) *and* the old one is accepted while we keep transmitting under index 0
    seq_2g = link.send_access(LIGHT_2G, OUR_SRC, ONOFF_STATUS_ON, iv_index=0)
    assert recorder.messages[-1].src == LIGHT_2G
    assert attached.state.rpl[LIGHT_2G] == (0, seq_2g)
    link.iv_index = 1
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)
    assert recorder.messages[-1].src == PROXY_NODE
    assert attached.state.rpl[PROXY_NODE][0] == 1
    await attached.send_access(PROXY_NODE, M.generic_onoff_get())
    assert (link.net_pdus[-1].ivi, link.net_pdus[-1].seq) == (0, 50)
    # the update completes: transmit index moves to 1 and the sequence restarts
    link.send_beacon(iv_index=1, iv_update=False)
    assert (state.iv_index, state.iv_update_active, state.tx_iv_index, state.seq) == (
        1,
        False,
        1,
        0,
    )
    await attached.send_access(PROXY_NODE, M.generic_onoff_get())
    assert (link.net_pdus[-1].ivi, link.net_pdus[-1].seq) == (1, 0)
    assert len(link.sent_access()) == 2
    # a lagging node's beacon still flagging the update for index 1: ignored — sequence, replay list and filter untouched
    # (lets the filter re-send go out: the second IV change replaced the first one's re-send before it ran —
    # one filter conversation per link, and only the latest index gets through anyway)
    await settle()
    filters_sent, seq_before = len(link.config_pdus), state.seq
    assert filters_sent == 2
    assert seq_before == 2
    attached.state.rpl[PROXY_NODE] = (1, 10)
    link.send_beacon(iv_index=1, iv_update=True)
    await settle()
    assert (state.iv_index, state.iv_update_active, state.tx_iv_index, state.seq) == (
        1,
        False,
        1,
        seq_before,
    )
    assert attached.state.rpl == {
        PROXY_NODE: (1, 10),
        LIGHT_2G: (0, seq_2g),
    }  # nothing purged: IV 0 stays receivable next to IV 1
    assert len(link.config_pdus) == filters_sent
    assert len(recorder.beacons) == 3


async def test_beacon_iv_change_resends_the_proxy_filter(
    attached: ProxyClient,
    link: FakeBleak,
    fast: FastAsyncio,
    caplog: pytest.LogCaptureFixture,
):
    """The filter sent at attach was encrypted with the stored IV index; once the beacon proves the network moved on
    the proxy cannot have accepted it, so it is sent again under the new index (§4 rob, tracker)."""
    assert [n.seq for n in link.config_pdus] == [0]
    link.iv_index = 3
    with caplog.at_level(logging.INFO, logger="jhmesh"):
        link.send_beacon(iv_index=3, iv_update=False)
        assert len(attached._tasks) == 1
        await settle()  # the re-send task logs while the level is still raised
    assert [n.seq for n in link.config_pdus] == [
        0,
        0,
    ]  # decrypted by the fake under IV 3, fresh sequence space
    assert link.config_pdus[1].transport_pdu == b"\x00\x01"
    assert attached.state.seq == 1
    assert fast.sleeps == [0.3, 0.3]
    assert attached._tasks == set()
    assert "re-sending the proxy filter" in caplog.text
    # an "in progress" beacon for the next index changes the state too → another re-send (harmless, keeps it simple)
    link.send_beacon(iv_index=4, iv_update=True)
    await settle()
    assert len(link.config_pdus) == 3
    assert (
        link.config_pdus[2].ivi == 1
    )  # still transmitting under index 3 (odd IVI bit)


async def test_beacon_iv_change_without_a_filter_does_not_send_one(
    proxy: ProxyClient, link: FakeBleak
):
    await proxy.attach(link, filter_blacklist=False)
    link.iv_index = 1
    link.send_beacon(iv_index=1)
    await settle()
    assert proxy._tasks == set()
    assert link.config_pdus == []
    assert proxy.state.iv_index == 1


async def test_filter_resend_failure_is_logged_not_raised(
    attached: ProxyClient, link: FakeBleak, caplog: pytest.LogCaptureFixture
):
    link.iv_index = 1
    link.write_error = OSError("adapter gone")
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        link.send_beacon(iv_index=1)
        await settle()
    assert (
        "proxy filter re-send failed: proxy write failed: adapter gone" in caplog.text
    )
    assert attached._tasks == set()
    assert attached.connected


async def test_filter_resend_retries_a_sequence_number_refusal(
    cdb: CDB, fast: FastAsyncio, caplog: pytest.LogCaptureFixture
):
    """HAC-05: a `LocalState` subclass (`HAState`) can refuse `reserve_seq` with `SequenceStalled` until its
    store is durably written — exactly the moment an IV-change filter re-send needs a fresh sequence number —
    so the re-send must retry that refusal instead of giving up like a lost link."""

    class FlakyState(LocalState):
        armed = False
        refused = 0

        def reserve_seq(self, count: int) -> int:
            if self.armed and self.refused == 0:
                self.refused += 1
                raise SequenceStalled("store not written yet")
            return super().reserve_seq(count)

    state = FlakyState(None, OUR_SRC)
    link = FakeBleak(cdb)
    proxy = ProxyClient(cdb, state)
    await proxy.attach(link)  # its own filter must not be affected: not armed yet
    state.armed = True
    link.iv_index = 1
    with caplog.at_level(logging.INFO, logger="jhmesh"):
        link.send_beacon(iv_index=1)
        await settle(30)
    assert state.refused == 1
    assert client_mod.FILTER_RESEND_WAIT in fast.sleeps
    assert link.config_pdus[-1].transport_pdu == b"\x00\x01"
    assert "re-sending the proxy filter" in caplog.text


async def test_filter_resend_gives_up_after_its_retries_are_exhausted(
    cdb: CDB, fast: FastAsyncio, caplog: pytest.LogCaptureFixture
):
    class AlwaysStalledState(LocalState):
        armed = False

        def reserve_seq(self, count: int) -> int:
            if self.armed:
                raise SequenceStalled("store not written yet")
            return super().reserve_seq(count)

    state = AlwaysStalledState(None, OUR_SRC)
    link = FakeBleak(cdb)
    proxy = ProxyClient(cdb, state)
    await proxy.attach(link)
    assert len(link.config_pdus) == 1  # attach's own filter
    state.armed = True
    link.iv_index = 1
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        link.send_beacon(iv_index=1)
        await settle(30)
    assert fast.sleeps.count(client_mod.FILTER_RESEND_WAIT) == (
        client_mod.FILTER_RESEND_TRIES - 1
    )
    assert "proxy filter re-send failed: store not written yet" in caplog.text
    assert len(link.config_pdus) == 1  # no re-send ever got through
    assert proxy.connected


async def test_beacon_unauthenticated_or_unchanged_is_not_applied(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder, state: LocalState
):
    attached.state.rpl[PROXY_NODE] = (0, 10)
    link.send_beacon(iv_index=5, valid=False)
    assert state.iv_index == 0
    assert attached.state.rpl == {PROXY_NODE: (0, 10)}
    assert (
        recorder.beacons[-1].authenticated is False
    )  # still reported to the application
    link.send_beacon(iv_index=0)
    assert state.iv_index == 0
    assert len(recorder.beacons) == 2
    assert attached.state.rpl == {PROXY_NODE: (0, 10)}
    link.deliver(
        PROXY_BEACON, b"\x00" + bytes(22)
    )  # unprovisioned device beacon: not a secure network beacon
    link.deliver(PROXY_BEACON, b"")
    assert len(recorder.beacons) == 2


async def test_beacon_key_refresh_is_reported(
    attached: ProxyClient,
    link: FakeBleak,
    recorder: Recorder,
    caplog: pytest.LogCaptureFixture,
):
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        link.send_beacon(iv_index=0, key_refresh=True)
        assert (
            "key refresh" not in caplog.text
        )  # authenticated: our keys are the new ones already
        link.send_beacon(iv_index=0, key_refresh=True, valid=False)
    assert (
        "key refresh in progress (beacon not authenticated by our key): the exported keys may be being replaced"
        in caplog.text
    )
    assert recorder.beacons[-1].key_refresh is True


async def test_private_beacon_moves_the_iv_state_like_a_secure_one(
    attached: ProxyClient,
    link: FakeBleak,
    recorder: Recorder,
    state: LocalState,
    caplog: pytest.LogCaptureFixture,
):
    """Review-4 P I-4: a proxy with Mesh Protocol 1.1 privacy on sends Mesh Private beacons, not Secure Network ones."""
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        link.send_private_beacon(iv_index=1, iv_update=True)
    assert (state.iv_index, state.iv_update_active, state.tx_iv_index) == (1, True, 0)
    b = recorder.beacons[-1]
    assert (b.private, b.authenticated, b.iv_update, b.network_id) == (
        True,
        True,
        True,
        link.nk.network_id,
    )
    assert "private beacon: iv_index=1 iv_update=True" in caplog.text
    link.send_private_beacon(iv_index=1)  # the update completes
    assert (state.iv_index, state.iv_update_active, state.tx_iv_index) == (1, False, 1)
    assert len(recorder.beacons) == 2
    # another network's (or one changed on the way, or cut short): nothing in it is readable, nothing is reported
    ours = link.private_beacon_payload(iv_index=2)
    link.nk = NetKeyMaterial.derive(bytes(16))
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        link.send_private_beacon(iv_index=5, iv_update=True)
        link.deliver(PROXY_BEACON, ours[:5] + bytes([ours[5] ^ 1]) + ours[6:])
        link.deliver(PROXY_BEACON, ours[:26])
    assert len(recorder.beacons) == 2
    assert state.iv_index == 1
    assert caplog.text.count("Mesh Private beacon that no key of ours opens") == 3


# ============================================================================= ProxyClient: control / proxy PDUs


async def test_control_messages_are_logged_only(
    attached: ProxyClient,
    link: FakeBleak,
    recorder: Recorder,
    caplog: pytest.LogCaptureFixture,
):
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        link.send_ctl(PROXY_NODE, OUR_SRC, h("0a000501"))  # heartbeat
        link.send_ctl(PROXY_NODE, OUR_SRC, h("0501"))  # some other control opcode
        link.send_ack(PROXY_NODE, OUR_SRC, 7, 1)  # segment ack nobody waits for
        link.send_ctl(PROXY_NODE, OUR_SRC, h("0000"))  # truncated ack
        link.send_ctl(PROXY_NODE, OUR_SRC, h("80000000"))  # segmented control PDU
    assert "Heartbeat" in caplog.text
    assert "control op 05" in caplog.text
    assert "Segment Ack seq_zero=7" in caplog.text
    assert recorder.messages == []
    assert attached._segments == {}


async def test_segment_ack_only_counts_when_addressed_to_us_from_the_right_node(
    attached: ProxyClient, link: FakeBleak
):
    w = _AckState()
    attached._ack_waiters[(PROXY_NODE, 7)] = w
    link.send_ack(PROXY_NODE, GROUP_WC, 7, 0b1)
    link.send_ack(LIGHT_2G, OUR_SRC, 7, 0b1)
    link.send_ack(PROXY_NODE, OUR_SRC, 8, 0b1)
    assert w.block == 0
    assert not w.event.is_set()
    link.send_ack(PROXY_NODE, OUR_SRC, 7, 0b1)
    link.send_ack(PROXY_NODE, OUR_SRC, 7, 0b10)
    assert w.block == 0b11
    assert w.event.is_set()


async def test_proxy_config_and_unknown_message_types(
    attached: ProxyClient, link: FakeBleak, caplog: pytest.LogCaptureFixture
):
    attached.proxy_addr = None
    with caplog.at_level(logging.INFO, logger="jhmesh"):
        link.deliver(
            PROXY_CONFIG,
            network_encrypt(
                link.nk,
                0,
                True,
                0,
                link.next_seq(),
                LIGHT_2G,
                0,
                h("02c00f"),
                proxy=True,
            ),
        )  # not a Filter Status
        assert attached.proxy_addr is None
        link.deliver(
            PROXY_CONFIG,
            network_encrypt(
                link.nk, 0, True, 0, link.next_seq(), LIGHT_2G, 0, h("03000000")
            ),
        )  # wrong (network) nonce
        assert attached.proxy_addr is None
        link.deliver(PROXY_PROVISIONING, h("0001"))
        link.deliver(
            PROXY_CONFIG,
            network_encrypt(
                link.nk,
                0,
                True,
                0,
                link.next_seq(),
                LIGHT_2G,
                0,
                h("03000002"),
                proxy=True,
            ),
        )
    assert attached.proxy_addr == LIGHT_2G
    assert "proxy config pdu" in caplog.text
    assert "proxy msg type 3: 0001" in caplog.text
    assert "type=whitelist list_size=2" in caplog.text


async def test_malformed_pdu_is_logged_not_raised(
    attached: ProxyClient,
    link: FakeBleak,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    """A PDU that authenticates with the NetKey but is not a lower transport PDU (`parse_lower` raises
    ValueError) is dropped with one DEBUG line: no ERROR, no traceback — any NetKey holder can send one
    per notification. (Rewritten: the old test pinned the traceback as correct.)"""

    def broken(_pdu: bytes, _ctl: bool) -> tuple[Any, ...]:
        raise ValueError("not a transport PDU")

    monkeypatch.setattr(client_mod, "parse_lower", broken)
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        link.send_ctl(PROXY_NODE, OUR_SRC, b"\x00" * 7)
    assert "malformed lower transport PDU dropped: not a transport PDU" in caplog.text
    assert "error handling proxy PDU" not in caplog.text
    assert "Traceback" not in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    monkeypatch.undo()
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)  # the client keeps working
    assert attached.state.rpl[PROXY_NODE][1] == link.seq
    # the same for the empty control PDU the old test sent (whatever layer rejects it): quiet, and no list entry
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        link.send_ctl(PROXY_NODE, OUR_SRC, b"")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert attached.state.rpl[PROXY_NODE][1] == link.seq - 1


async def test_send_ack_edge_cases(attached: ProxyClient, link: FakeBleak):
    await attached._send_ack(GROUP_WC, 1, 1)  # group traffic is never acknowledged
    assert link.net_pdus[1:] == []
    link.write_error = ConnectionError("gone")
    await attached._send_ack(PROXY_NODE, 1, 1)  # link error is swallowed
    link.write_error = None
    attached.client = None
    await attached._send_ack(PROXY_NODE, 1, 1)  # not connected: silently dropped
    assert link.sent_acks() == []


async def test_a_send_without_a_link_takes_no_sequence_number(
    proxy: ProxyClient, link: FakeBleak
):
    """Review-4 R4-4: every send path used to reserve (and persist) its number before `_write` found no client —
    ten sends with no link used ten numbers. Each now refuses first, under the send lock, and takes none."""
    seq = proxy.state.seq
    for _ in range(10):
        with pytest.raises(ConnectionError, match="not connected to a proxy"):
            await proxy.send_access(PROXY_NODE, M.generic_onoff_get())
    with pytest.raises(ConnectionError, match="not connected to a proxy"):
        await proxy.send_access(DIMMER, bytes(20))  # segmented
    with pytest.raises(ConnectionError, match="not connected to a proxy"):
        await proxy.set_filter(FILTER_BLACKLIST)
    await proxy._send_ack(PROXY_NODE, 1, 1)  # swallowed, like any failed ack
    assert proxy.state.seq == seq
    assert not proxy._send_lock.locked()
    assert proxy.filter_writes == 0

    # the filter requests a link actually got are counted, and start from none on the next link
    await proxy.attach(link)
    assert proxy.filter_writes == 1
    await proxy.set_filter(FILTER_BLACKLIST)
    assert proxy.filter_writes == 2
    await proxy.detach()
    assert proxy.filter_writes == 0


async def test_write_errors_surface_as_connection_error(
    attached: ProxyClient, link: FakeBleak
):
    link.write_error = OSError("adapter gone")
    with pytest.raises(
        ConnectionError, match="proxy write failed: adapter gone"
    ) as info:
        await attached.send_access(PROXY_NODE, M.generic_onoff_get())
    assert isinstance(info.value.__cause__, OSError)
    link.write_error = ConnectionError("already a connection error")
    with pytest.raises(ConnectionError, match="already a connection error") as info:
        await attached.send_access(PROXY_NODE, M.generic_onoff_get())
    assert info.value.__cause__ is None
    link.write_error = None
    await attached.send_access(
        PROXY_NODE, M.generic_onoff_get()
    )  # the send lock was released
    assert len(link.sent_access()) == 1


async def test_empty_notification_is_ignored(attached: ProxyClient, link: FakeBleak):
    assert link.notify_cb is not None
    link.notify_cb(None, bytearray())


# ============================================================================= ProxyClient: background tasks / MTU


async def test_segment_acks_are_tracked_and_cancelled_on_link_loss(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
):
    _seq0, pdus = link.access_pdus(PROXY_NODE, OUR_SRC, bytes(20))
    for p in pdus:
        link.deliver(PROXY_NETWORK_PDU, p)
    assert len(recorder.messages) == 1
    assert len(attached._tasks) == 1  # the ack is queued, not yet sent
    attached.handle_disconnected()
    assert attached._tasks == set()
    await settle()
    assert link.sent_acks() == []  # cancelled before it could write
    assert attached._tasks == set()


async def test_detach_cancels_background_tasks(attached: ProxyClient, link: FakeBleak):
    _seq0, pdus = link.access_pdus(PROXY_NODE, OUR_SRC, bytes(20))
    link.deliver(PROXY_NETWORK_PDU, pdus[1])  # last segment first → partial ack task
    task = next(iter(attached._tasks))
    await attached.detach()
    await settle()
    assert task.cancelled()
    assert attached._tasks == set()
    assert link.sent_acks() == []


async def test_background_task_exceptions_are_logged(
    attached: ProxyClient, caplog: pytest.LogCaptureFixture
):
    async def boom() -> None:
        raise RuntimeError("ack code broke")

    async def fine() -> None:
        pass

    with caplog.at_level(logging.ERROR, logger="jhmesh"):
        attached._spawn(boom())
        attached._spawn(fine())
        assert len(attached._tasks) == 2
        await settle()
    assert attached._tasks == set()
    assert "background task failed" in caplog.text
    assert "ack code broke" in caplog.text


async def test_mtu_is_read_from_the_client_on_every_write(proxy: ProxyClient, cdb: CDB):
    """BlueZ reports mtu_size 23 until the exchange completes shortly after connecting; later frames must use the
    negotiated value instead of the one seen at attach()."""
    link = FakeBleak(cdb, mtu_size=23)
    await proxy.attach(link)
    assert proxy.mtu == 23
    await proxy.send_access(PROXY_NODE, M.light_ctl_set(1, 2700, tid=1))
    assert [f[0] >> 6 for f in link.writes[1:]] == [1, 3]  # 2 SAR fragments at MTU 23
    link.mtu_size = 247
    assert proxy.mtu == 247
    await proxy.send_access(PROXY_NODE, M.light_ctl_set(1, 2700, tid=2))
    assert [f[0] >> 6 for f in link.writes[3:]] == [0]  # one complete frame now
    assert [m[4] for m in link.sent_access()] == [
        M.light_ctl_set(1, 2700, tid=1),
        M.light_ctl_set(1, 2700, tid=2),
    ]
    link.mtu_size = None
    assert proxy.mtu == 23  # backends without the attribute
    await proxy.detach()
    assert proxy.mtu == 23


async def test_frames_of_one_pdu_are_not_interleaved_with_another(
    proxy: ProxyClient, cdb: CDB
):
    """Two concurrent writers (a Set and a filter re-send) at MTU 23: every proxy PDU's SAR fragments stay together."""
    link = FakeBleak(cdb, mtu_size=23)
    await proxy.attach(link)
    link.writes.clear()
    await asyncio.gather(
        proxy.send_access(PROXY_NODE, M.light_ctl_set(1, 2700, tid=1)),
        proxy.set_filter(FILTER_BLACKLIST),
        proxy.send_access(LIGHT_2G, M.light_ctl_set(3, 4000, tid=2)),
    )
    kinds = [(f[0] >> 6, f[0] & 0x3F) for f in link.writes]
    assert kinds == [
        (1, PROXY_NETWORK_PDU),
        (3, PROXY_NETWORK_PDU),
        (0, PROXY_CONFIG),
        (1, PROXY_NETWORK_PDU),
        (3, PROXY_NETWORK_PDU),
    ]
    assert len(link.sent_access()) == 2
    assert len(link.config_pdus) == 2


# ============================================================================= ProxyClient: DevKey (Config Server) transport

PUB_SET = C.model_publication_set(
    0x0149, ELEMENT_GROUP_148, "1001"
)  # 12 bytes → always segmented
PUB_STATUS = (
    C.encode_opcode(C.CONFIG_MODEL_PUBLICATION_STATUS) + b"\x00" + PUB_SET[1:]
)  # 14 bytes → segmented
TTL_STATUS_5 = h("800e05")


def test_lower_transport_akf_flag():
    """AKF=0 clears bit 6 of the first octet (AID 0) in both the unsegmented and the segmented header."""
    upper = b"\x01\x02" + bytes(4)  # an access octet or two and a 32-bit TransMIC
    assert lower_unsegmented_access(0, upper, akf=False) == b"\x00" + upper
    assert parse_lower(b"\x00" + upper, ctl=False) == ("unseg", False, 0, upper)
    segs = lower_segments_access(0, 0x3129AB, bytes(16), akf=False)
    assert [s[0] for s in segs] == [0x80, 0x80]
    assert (
        lower_segments_access(0x26, 0x3129AB, bytes(16))[0][0] == 0xE6
    )  # default: AKF=1
    info = parse_lower(segs[1], ctl=False)[1]
    assert (info.akf, info.aid, info.seq_zero, info.seg_o, info.seg_n) == (
        False,
        0,
        0x09AB,
        1,
        1,
    )


@pytest.mark.parametrize("szmic", [0, 1])
def test_upper_transport_dev_round_trip(szmic: int):
    """upper_encrypt_dev uses the device nonce: only a device-nonce decrypt with the same key gets it back."""
    dev_key = bytes(range(16))
    upper = upper_encrypt_dev(dev_key, 5, 0x000123, OUR_SRC, PROXY_NODE, PUB_SET, szmic)
    assert len(upper) == len(PUB_SET) + (8 if szmic else 4)
    assert (
        upper_decrypt(
            dev_key, NONCE_DEVICE, 5, 0x000123, OUR_SRC, PROXY_NODE, upper, szmic
        )
        == PUB_SET
    )
    assert (
        upper_decrypt(
            dev_key, NONCE_APP, 5, 0x000123, OUR_SRC, PROXY_NODE, upper, szmic
        )
        is None
    )
    assert (
        upper_decrypt(
            bytes(16), NONCE_DEVICE, 5, 0x000123, OUR_SRC, PROXY_NODE, upper, szmic
        )
        is None
    )


async def test_send_config_unsegmented_uses_the_device_key(
    attached: ProxyClient, link: FakeBleak
):
    seq = await attached.send_config(PROXY_NODE, C.composition_data_get())
    assert (
        seq == 1
    )  # seq 0 went into the Set Filter Type request, exactly like send_access
    assert link.sent_config() == [(OUR_SRC, PROXY_NODE, 5, 1, C.composition_data_get())]
    assert link.sent_access() == []  # nothing was AppKey-encrypted
    n = link.net_pdus[-1]
    assert n.transport_pdu[0] == 0x00  # SEG=0, AKF=0, AID=0
    assert (n.ivi, n.ttl, n.dst) == (0, 5, PROXY_NODE)
    # the wrong nonce type, the AppKey, or another node's device key do not open it
    dev = link.dev_key(PROXY_NODE)
    upper = n.transport_pdu[1:]
    assert upper_decrypt(dev, NONCE_APP, 0, 1, OUR_SRC, PROXY_NODE, upper, 0) is None
    assert (
        upper_decrypt(link.ak.key, NONCE_APP, 0, 1, OUR_SRC, PROXY_NODE, upper, 0)
        is None
    )
    assert (
        upper_decrypt(link.ak.key, NONCE_DEVICE, 0, 1, OUR_SRC, PROXY_NODE, upper, 0)
        is None
    )
    assert (
        upper_decrypt(
            link.dev_key(LIGHT_2G), NONCE_DEVICE, 0, 1, OUR_SRC, PROXY_NODE, upper, 0
        )
        is None
    )
    seq = await attached.send_config(LIGHT_2G, C.gatt_proxy_set(True), ttl=2)
    assert seq == 2
    assert link.sent_config()[-1] == (OUR_SRC, LIGHT_2G, 2, 2, C.gatt_proxy_set(True))
    assert attached.state.seq == 3
    assert attached._ack_waiters == {}


async def test_send_config_segmented_publication_set_is_acked(
    attached: ProxyClient, link: FakeBleak, fast: FastAsyncio
):
    link.auto_ack()
    seq0 = await attached.send_config(PROXY_NODE, PUB_SET)
    assert seq0 == 1
    segs = [n for n in link.net_pdus if not n.ctl]
    assert [(n.seq, n.dst, n.ttl, n.transport_pdu[0]) for n in segs] == [
        (1, PROXY_NODE, 5, 0x80),  # SEG=1, AKF=0, AID=0
        (2, PROXY_NODE, 5, 0x80),
    ]
    hdrs = [int.from_bytes(n.transport_pdu[1:4], "big") for n in segs]
    assert [(x >> 23) & 1 for x in hdrs] == [
        0,
        0,
    ]  # SZMIC 0: 4-byte TransMIC, as send_access
    assert [(x >> 5) & 0x1F for x in hdrs] == [0, 1]
    assert {(x >> 10) & 0x1FFF for x in hdrs} == {seq0 & 0x1FFF}
    assert link.sent_config() == [(OUR_SRC, PROXY_NODE, 5, 1, PUB_SET)]
    assert attached.state.seq == 3
    assert attached._ack_waiters == {}
    assert fast.sleeps == [
        0.3
    ]  # attach only: fully acknowledged at once, no grace, no retransmission


async def test_send_config_segmented_retransmits_and_times_out_like_send_access(
    attached: ProxyClient, link: FakeBleak, fast: FastAsyncio
):
    link.auto_ack(drop_once={1})
    seq0 = await attached.send_config(PROXY_NODE, PUB_SET)
    segs = [n for n in link.net_pdus if not n.ctl]
    assert [n.seq for n in segs] == [
        1,
        2,
        3,
    ]  # seg 1 lost once, re-sent with a fresh seq
    assert link.sent_config() == [(OUR_SRC, PROXY_NODE, 5, seq0, PUB_SET)]
    link.responders.clear()
    with pytest.raises(
        TimeoutError, match="segmented message to 0232 not acknowledged"
    ):
        await attached.send_config(LIGHT_2G, PUB_SET)
    assert attached._ack_waiters == {}
    await attached.send_config(
        LIGHT_2G, C.beacon_get()
    )  # the send lock is released again


async def test_send_config_rejects_unknown_or_secondary_unicasts(
    attached: ProxyClient, link: FakeBleak
):
    with pytest.raises(ValueError, match="0999 is not an element of any known node"):
        await attached.send_config(0x0999, C.composition_data_get())
    with pytest.raises(ValueError, match="0149 is a secondary element of node 0148"):
        await attached.send_config(0x0149, C.composition_data_get())
    with pytest.raises(ValueError, match="C00F is not an element"):
        await attached.request_config(GROUP_WC, C.composition_data_get(), 0x02)
    assert link.net_pdus == []  # nothing left the client
    assert attached.state.seq == 1  # no sequence number was consumed


async def test_send_config_matches_the_spec_sample_message_6(fast: FastAsyncio):
    """Mesh Profile 1.0.1 §8.3.6, Message #6: Config AppKey Add (NetKeyIndex 0x456, AppKeyIndex 0x123) from
    0x0003 to node 0x1201 under its device key, IV index 0x12345678, SEQ 0x3129AB, TTL 4 — the device-nonce
    upper transport PDU, both segments and both Network PDUs come out byte-exact."""
    node = Node(
        "sample", "sample node", 0x1201, h("9d6dd0e96eb25dc19a40ed9914f8f03f"), None
    )
    node.elements.append(Element(0x1201, 0, ["0000"], node))
    cdb = CDB(
        "sample",
        {0: NetKeyMaterial.derive(h("7dd7364cd842ad18c17c2b820c84c3d6"))},
        {0: AppKeyMaterial.derive(h("63964771734fbd76e3b40519d1d94a48"))},
        [node],
        {},
        {},
    )
    state = LocalState(None, 0x0003)
    state.iv_index, state.seq = 0x12345678, 0x3129AB
    proxy = ProxyClient(cdb, state, ttl=4)
    link = FakeBleak(cdb, proxy_node=0x1201)
    link.iv_index = 0x12345678
    await proxy.attach(link, filter_blacklist=False)
    link.auto_ack()
    access = C.appkey_add(
        h("63964771734fbd76e3b40519d1d94a48"), app_key_index=0x123, net_key_index=0x456
    )
    assert access == h("0056341263964771734fbd76e3b40519d1d94a48")
    assert await proxy.send_config(0x1201, access) == 0x3129AB
    assert [n.transport_pdu for n in link.net_pdus if not n.ctl] == [
        h("8026ac01ee9dddfd2169326d23f3afdf"),
        h("8026ac21cfdc18c52fdef772e0e17308"),
    ]
    assert [p for t, p in link.outgoing if t == PROXY_NETWORK_PDU] == [
        h("68cab5c5348a230afba8c63d4e686364979deaf4fd40961145939cda0e"),
        h("681615b5dd4a846cae0c032bf0746f44f1b8cc8ce5edc57e55beed49c0"),
    ]
    assert link.sent_config() == [(0x0003, 0x1201, 4, 0x3129AB, access)]
    assert state.seq == 0x3129AD


async def test_request_config_matches_a_devkey_status_from_the_node(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
):
    seen: list[tuple[int, bytes]] = []

    def config_server(node: int, access: bytes) -> bytes | None:
        seen.append((node, access))
        return TTL_STATUS_5 if access == C.default_ttl_set(5) else None

    link.auto_config(config_server)
    msg = await attached.request_config(
        PROXY_NODE, C.default_ttl_set(5), C.CONFIG_DEFAULT_TTL_STATUS, timeout=1.0
    )
    assert isinstance(msg, AccessMessage)
    assert (msg.src, msg.dst, msg.opcode, msg.company_id, msg.params, msg.key) == (
        PROXY_NODE,
        OUR_SRC,
        C.CONFIG_DEFAULT_TTL_STATUS,
        None,
        b"\x05",
        "dev:0148",
    )
    assert C.decode_config(msg.opcode, msg.params) == C.DefaultTtlStatus(5)
    assert str(msg).endswith("[dev:0148] Config Default TTL Status ttl=5")
    assert seen == [
        (PROXY_NODE, C.default_ttl_set(5))
    ]  # the fake decrypted it with 0148's key
    assert recorder.messages == [msg]
    assert attached._waiters == []


async def test_request_config_ignores_replies_with_the_wrong_key_source_or_opcode(
    attached: ProxyClient,
    link: FakeBleak,
    recorder: Recorder,
    caplog: pytest.LogCaptureFixture,
):
    proxy_on = h("801401")

    def respond(n):
        if n.ctl or n.dst != PROXY_NODE:
            return
        loop = asyncio.get_running_loop()
        # right node and opcode, but encrypted with a key we do not know → undecryptable, dropped
        loop.call_soon(
            link.send_devkey, PROXY_NODE, OUR_SRC, proxy_on, bytes(range(16))
        )
        # the right status from another node
        loop.call_soon(
            link.send_devkey, LIGHT_2G, OUR_SRC, proxy_on, link.dev_key(LIGHT_2G)
        )
        # the right node, wrong opcode
        loop.call_soon(
            link.send_devkey,
            PROXY_NODE,
            OUR_SRC,
            TTL_STATUS_5,
            link.dev_key(PROXY_NODE),
        )
        # the right node and opcode but AppKey-encrypted: not a Config Server reply
        loop.call_soon(link.send_access, PROXY_NODE, OUR_SRC, proxy_on)

    link.responders.append(respond)
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        with pytest.raises(TimeoutError, match="no response from 0148"):
            await attached.request_config(
                PROXY_NODE,
                C.gatt_proxy_set(True),
                C.CONFIG_GATT_PROXY_STATUS,
                timeout=0.01,
                retries=2,
            )
    assert "undecryptable upper transport (akf=False aid=0)" in caplog.text
    assert [(m.src, m.opcode, m.key) for m in recorder.messages] == [
        (LIGHT_2G, C.CONFIG_GATT_PROXY_STATUS, "dev:0232"),
        (PROXY_NODE, C.CONFIG_DEFAULT_TTL_STATUS, "dev:0148"),
        (PROXY_NODE, C.CONFIG_GATT_PROXY_STATUS, "app0"),
    ] * 2
    assert [m[1:] for m in link.sent_config()] == [
        (PROXY_NODE, 5, 1, C.gatt_proxy_set(True)),
        (PROXY_NODE, 5, 2, C.gatt_proxy_set(True)),
    ]
    assert attached._waiters == []


async def test_request_config_segmented_round_trip(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
):
    """Publication Set (2 segments out) answered with a Publication Status (2 segments back): both directions
    are device-key encrypted, segment-acknowledged and matched."""
    link.auto_ack()
    link.auto_config(
        lambda node, access: (
            PUB_STATUS if access[0] == C.CONFIG_MODEL_PUBLICATION_SET else None
        )
    )
    msg = await attached.request_config(
        PROXY_NODE, PUB_SET, C.CONFIG_MODEL_PUBLICATION_STATUS, timeout=1.0
    )
    assert (msg.src, msg.key, msg.access_pdu) == (PROXY_NODE, "dev:0148", PUB_STATUS)
    status = C.decode_model_publication_status(msg.params)
    assert (
        status.ok,
        status.element,
        status.publish_address,
        status.model,
        status.ttl,
    ) == (
        True,
        0x0149,
        ELEMENT_GROUP_148,
        0x1001,
        0xFF,
    )
    assert link.sent_config() == [(OUR_SRC, PROXY_NODE, 5, 1, PUB_SET)]
    await settle()
    acks = link.sent_acks()  # our ack for the node's two-segment status
    assert len(acks) == 1
    assert acks[0][:2] == (OUR_SRC, PROXY_NODE)
    assert acks[0][3] == 0b11
    assert recorder.messages == [msg]
    assert attached._ack_waiters == {}
    assert attached._waiters == []


async def test_request_config_retries_then_times_out(
    attached: ProxyClient, link: FakeBleak, caplog: pytest.LogCaptureFixture
):
    """Each unanswered attempt is a DEBUG line, never a WARNING (review-4 H4-7): the caller reports the TimeoutError."""
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        with pytest.raises(TimeoutError, match="no response from 0148"):
            await attached.request_config(
                PROXY_NODE,
                C.node_reset(),
                C.CONFIG_NODE_RESET_STATUS,
                timeout=0.01,
                retries=3,
            )
    assert [m[4] for m in link.sent_config()] == [C.node_reset()] * 3
    attempts = [r for r in caplog.records if "no response from 0148" in r.getMessage()]
    assert [r.getMessage() for r in attempts] == [
        f"no response from 0148 (attempt {n}/3)" for n in (1, 2, 3)
    ]
    assert {r.levelno for r in attempts} == {logging.DEBUG}
    assert attached._waiters == []


async def test_config_and_access_messages_share_the_sequence_space(
    attached: ProxyClient, link: FakeBleak
):
    link.auto_ack()
    assert await attached.send_access(PROXY_NODE, M.generic_onoff_get()) == 1
    assert await attached.send_config(PROXY_NODE, C.default_ttl_set(5)) == 2
    assert await attached.send_config(PROXY_NODE, PUB_SET) == 3  # two segments: 3, 4
    assert await attached.send_access(GROUP_WC, M.generic_onoff_get()) == 5
    assert await attached.send_config(LIGHT_2G, C.relay_get()) == 6
    assert attached.state.seq == 7
    assert [n.seq for n in link.net_pdus if not n.ctl] == [1, 2, 3, 4, 5, 6]
    assert [(m[1], m[3], m[4]) for m in link.sent_access()] == [
        (PROXY_NODE, 1, M.generic_onoff_get()),
        (GROUP_WC, 5, M.generic_onoff_get()),
    ]
    assert [(m[1], m[3], m[4]) for m in link.sent_config()] == [
        (PROXY_NODE, 2, C.default_ttl_set(5)),
        (PROXY_NODE, 3, PUB_SET),
        (LIGHT_2G, 6, C.relay_get()),
    ]


async def test_config_traffic_follows_the_transmit_iv_index(
    attached: ProxyClient, link: FakeBleak
):
    """During IV Update in Progress the device nonce, like the network nonce, keeps the old index."""
    link.send_beacon(iv_index=1, iv_update=True)
    assert (attached.state.iv_index, attached.state.tx_iv_index) == (1, 0)
    await attached.send_config(PROXY_NODE, C.beacon_get())
    n = link.net_pdus[-1]
    assert n.ivi == 0
    assert link.sent_config()[-1] == (OUR_SRC, PROXY_NODE, 5, n.seq, C.beacon_get())
    link.iv_index = 1
    link.send_beacon(iv_index=1, iv_update=False)
    assert attached.state.tx_iv_index == 1
    await attached.send_config(PROXY_NODE, C.beacon_get())
    assert link.net_pdus[-1].ivi == 1
    assert link.sent_config()[-1][4] == C.beacon_get()


# ============================================================================= ProxyClient: stale-export signals


async def test_undecryptable_pdus_are_counted_and_reported(
    proxy: ProxyClient, link: FakeBleak, recorder: Recorder, cdb: CDB
):
    """Every PDU our keys cannot open (network or upper transport) bumps `rx_undecryptable` and calls `on_undecryptable`."""
    calls: list[int] = []
    proxy.on_undecryptable = lambda: calls.append(proxy.rx_undecryptable)
    await proxy.attach(link)
    assert proxy.rx_undecryptable == 0
    foreign = NetKeyMaterial.derive(bytes(range(16)))
    link.deliver(
        PROXY_NETWORK_PDU,
        network_encrypt(
            foreign, 0, False, 3, 1, PROXY_NODE, OUR_SRC, h("660102030405")
        ),
    )  # another NetKey: NID / NetMIC mismatch
    assert (proxy.rx_undecryptable, calls) == (1, [1])
    upper = upper_encrypt_app(link.ak, 0, 2, PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)
    link.deliver(
        PROXY_NETWORK_PDU,
        network_encrypt(
            link.nk,
            0,
            False,
            3,
            2,
            PROXY_NODE,
            OUR_SRC,
            lower_unsegmented_access(link.ak.aid, upper[:-1] + bytes([upper[-1] ^ 1])),
        ),
    )  # our NetKey, another AppKey (bad TransMIC)
    assert (proxy.rx_undecryptable, calls) == (2, [1, 2])
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)  # decodable: not counted
    assert (proxy.rx_undecryptable, len(recorder.messages)) == (2, 1)
    link.send_beacon(
        iv_index=0, valid=False
    )  # beacons go through on_beacon, not this counter
    assert proxy.rx_undecryptable == 2
    assert recorder.beacons[-1].authenticated is False

    # a new link starts from zero
    await proxy.detach()
    await proxy.attach(FakeBleak(cdb))
    assert proxy.rx_undecryptable == 0


async def test_undecryptable_pdus_without_a_callback(
    attached: ProxyClient, link: FakeBleak
):
    foreign = NetKeyMaterial.derive(bytes(range(16)))
    link.deliver(
        PROXY_NETWORK_PDU,
        network_encrypt(
            foreign, 0, False, 3, 1, PROXY_NODE, OUR_SRC, h("660102030405")
        ),
    )
    assert attached.rx_undecryptable == 1


async def test_filter_status_reports_the_proxy_node_and_a_new_link_forgets_it(
    proxy: ProxyClient, link: FakeBleak, cdb: CDB
):
    """`proxy_addr` is only ever the current link's node: learnt from its Filter Status, dropped with the link."""
    seen: list[int] = []
    proxy.on_filter_status = seen.append
    await proxy.attach(link)
    assert seen == [PROXY_NODE]
    assert proxy.proxy_addr == PROXY_NODE

    proxy.handle_disconnected(link)
    assert proxy.proxy_addr is None

    second = FakeBleak(cdb, address="11:22:33:44:55:66", proxy_node=LIGHT_2G)
    second.answer_filter = False
    await proxy.attach(second)
    assert (
        proxy.proxy_addr is None
    )  # no Filter Status yet: the old node must not linger
    assert seen == [PROXY_NODE]
    second.deliver(PROXY_CONFIG, second.filter_status_pdu())
    assert proxy.proxy_addr == LIGHT_2G
    assert seen == [PROXY_NODE, LIGHT_2G]

    await proxy.detach()
    assert proxy.proxy_addr is None


# ============================================================================= review regressions (mesh / security probes)


class YieldingBleak(FakeBleak):
    """``write_gatt_char`` yields to the loop before and after the write, like every real transport (the D-Bus or
    ESPHome round trip): notifications the proxy has queued — its connect-time beacon, a segment ack — are
    delivered *while* the write is in flight, before it returns to the client."""

    async def write_gatt_char(
        self, char: str, data: bytes, response: bool | None = None
    ) -> None:
        await asyncio.sleep(0)
        await super().write_gatt_char(char, data, response)
        await asyncio.sleep(0)


def raw_segment(link: FakeBleak, seq_zero: int, seg_o: int, seg_n: int) -> bytes:
    """One AppKey-0 segment with a hand-made header (SegO / SegN need not be consistent)."""
    hdr = ((seq_zero & 0x1FFF) << 10) | (seg_o << 5) | seg_n
    return bytes([0x80 | 0x40 | link.ak.aid]) + hdr.to_bytes(3, "big") + bytes(12)


async def test_segment_ack_arriving_before_the_last_write_returns_is_not_lost(
    proxy: ProxyClient, cdb: CDB, fast: FastAsyncio
):
    """probe_seg_ack_early: the node acks each segment on the loop turn its write completes in, i.e. before
    ``write_gatt_char`` returns. Clearing the ack event only *after* the writes discarded that ack; the wait timed
    out, the whole message went out again with fresh SEQs (the node applying it once more each time) and after
    SEGMENT_RETRIES the send raised TimeoutError although every copy had been acknowledged."""
    link = YieldingBleak(cdb)
    link.auto_ack()
    await proxy.attach(link)
    seq0 = await proxy.send_access(PROXY_NODE, bytes(20))
    segs = [n for n in link.net_pdus if not n.ctl]
    assert [n.seq for n in segs] == [seq0, seq0 + 1]  # one round, no retransmission
    assert link.sent_access() == [(OUR_SRC, PROXY_NODE, 5, seq0, bytes(20))]
    assert proxy._ack_waiters == {}
    assert fast.sleeps == [
        0.3
    ]  # attach only: fully acknowledged, no grace, no timeout round


async def test_request_config_with_early_acks_is_answered_on_the_first_attempt(
    proxy: ProxyClient, cdb: CDB, fast: FastAsyncio, recorder: Recorder
):
    """The same race through `request_config` (what every service call does): the Publication Set must be
    applied once and answered once, not retried three times and reported as `service_no_reply`."""
    link = YieldingBleak(cdb)
    link.auto_ack()
    link.auto_config(
        lambda node, access: (
            PUB_STATUS if access[0] == C.CONFIG_MODEL_PUBLICATION_SET else None
        )
    )
    await proxy.attach(link)
    msg = await proxy.request_config(
        PROXY_NODE, PUB_SET, C.CONFIG_MODEL_PUBLICATION_STATUS, timeout=1.0
    )
    assert msg.access_pdu == PUB_STATUS
    assert link.sent_config() == [(OUR_SRC, PROXY_NODE, 5, 1, PUB_SET)]  # applied once
    assert [(m.src, m.opcode) for m in recorder.messages] == [
        (PROXY_NODE, C.CONFIG_MODEL_PUBLICATION_STATUS)
    ]


async def test_lost_segment_ack_is_recovered_by_the_first_retransmitted_segment(
    proxy: ProxyClient, cdb: CDB, fast: FastAsyncio
):
    """probe_seg_ack_retransmit: the ack of the complete first transmission is lost on air. A spec-compliant
    receiver (§3.5.3.3 — `_on_segment` does the same) answers the first *retransmitted* segment with the full
    ack at once, while the second one is still being written; that ack must end the round instead of being
    discarded by a late `event.clear()` (four rounds later: TimeoutError for a message the node had all along)."""
    link = YieldingBleak(cdb)
    await proxy.attach(link)
    received: dict[tuple[int, int], set[int]] = {}
    lost: list[int] = []

    def node(n: Any) -> None:
        if n.ctl or not n.transport_pdu[0] & 0x80 or n.dst >= 0x8000:
            return
        hdr = int.from_bytes(n.transport_pdu[1:4], "big")
        seq_zero, seg_o, seg_n = (hdr >> 10) & 0x1FFF, (hdr >> 5) & 0x1F, hdr & 0x1F
        got = received.setdefault((n.src, seq_zero), set())
        was_complete = len(got) == seg_n + 1
        got.add(seg_o)
        if len(got) == seg_n + 1 and not was_complete and not lost:
            lost.append(seq_zero)  # the ack of the first transmission never reaches us
            return
        if (
            seg_o == seg_n or was_complete
        ):  # last segment, or a repeat of a finished message
            block = sum(1 << i for i in got)
            asyncio.get_running_loop().call_soon(
                link.send_ack, n.dst, n.src, seq_zero, block
            )

    link.responders.append(node)
    seq0 = await proxy.send_access(PROXY_NODE, bytes(20))
    segs = [n for n in link.net_pdus if not n.ctl]
    assert [n.seq for n in segs] == [
        seq0,
        seq0 + 1,
        seq0 + 2,
        seq0 + 3,
    ]  # exactly one retransmission round
    assert lost == [seq0 & 0x1FFF]
    assert (
        link.sent_access() == [(OUR_SRC, PROXY_NODE, 5, seq0, bytes(20))] * 2
    )  # the same SeqAuth twice: one message, retransmitted once
    assert proxy._ack_waiters == {}
    assert (
        0.25 not in fast.sleeps
    )  # the retransmission was acknowledged in full: no grace


async def test_request_returns_the_reply_when_the_send_failed_after_the_node_answered(
    attached: ProxyClient, link: FakeBleak, fast: FastAsyncio
):
    """A segmented request whose acks are all lost still raises from `_send` — but if the node's reply is already
    in, that reply is the result (the node applied the message; retrying would apply it again)."""
    replies = 0

    def node(n: Any) -> None:
        nonlocal replies
        if n.ctl or n.transport_pdu[0] & 0x40 or replies:
            return
        hdr = int.from_bytes(n.transport_pdu[1:4], "big")
        if (hdr >> 5) & 0x1F == hdr & 0x1F:  # the last segment: answer, never ack
            replies += 1
            asyncio.get_running_loop().call_soon(
                link.send_devkey,
                PROXY_NODE,
                OUR_SRC,
                PUB_STATUS,
                link.dev_key(PROXY_NODE),
            )

    link.responders.append(node)
    msg = await attached.request_config(
        PROXY_NODE, PUB_SET, C.CONFIG_MODEL_PUBLICATION_STATUS, timeout=1.0, retries=3
    )
    assert msg.access_pdu == PUB_STATUS
    sent = link.sent_config()
    assert (
        len(sent) == client_mod.SEGMENT_RETRIES
    )  # every unacknowledged round of the *same* message...
    assert {m[3] for m in sent} == {1}  # ... and no second attempt with a new SeqAuth
    assert attached._waiters == []
    assert attached._ack_waiters == {}


async def test_beacon_during_the_first_filter_write_still_gets_the_filter_resent(
    proxy: ProxyClient, cdb: CDB, state: LocalState
):
    """probe_filter_beacon_race: the proxy beacons right after the subscription, so its beacon lands while our
    first Set Filter Type write (encrypted with the stored IV index) is in flight. The beacon handler saw no
    filter to re-send yet, the stale request was silently dropped by the proxy and the link stayed on the
    default whitelist (unicast-only reception) for good."""
    link = YieldingBleak(cdb)
    link.iv_index, link.beacon_on_subscribe, link.strict_decrypt = 2, True, False
    await proxy.attach(link)
    assert (state.iv_index, state.tx_iv_index) == (2, 2)
    assert [t for t, _ in link.outgoing] == [PROXY_CONFIG, PROXY_CONFIG]
    assert len(link.undecryptable) == 1  # the one written under the stored index 0
    assert [n.seq for n in link.config_pdus] == [
        0
    ]  # the re-send, under IV 2, fresh sequence space
    assert link.config_pdus[0].transport_pdu == b"\x00\x01"
    assert proxy.proxy_addr == PROXY_NODE  # ... which the proxy answered
    assert proxy._filter_type == FILTER_BLACKLIST
    await settle()
    assert len(link.config_pdus) == 1  # and nothing spawned a third


async def test_filter_resent_when_the_iv_moves_during_the_write_is_logged(
    proxy: ProxyClient, cdb: CDB, caplog: pytest.LogCaptureFixture
):
    link = YieldingBleak(cdb)
    link.iv_index, link.beacon_on_subscribe, link.strict_decrypt = 1, True, False
    with caplog.at_level(logging.INFO, logger="jhmesh"):
        await proxy.attach(link)
    assert (
        "IV index changed during the proxy filter write, sending it again"
        in caplog.text
    )
    assert proxy.proxy_addr == PROXY_NODE


async def test_replay_list_survives_an_iv_update(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder, state: LocalState
):
    """probe_rpl_iv: Mesh Profile §3.8.8 — the replay list must outlive the IV Update procedure. Clearing it on
    the 'IV 1 in progress' beacon (when every node still transmits under IV 0 for hours) let a PDU captured
    before the beacon be accepted a second time."""
    button = PROXY_NODE + 1
    on = M.generic_onoff_set(True, tid=7, transition=0)
    seq0, pdus = link.access_pdus(button, GROUP_LIVING, on)
    link.deliver(PROXY_NETWORK_PDU, pdus[0])
    link.deliver(PROXY_NETWORK_PDU, pdus[0])  # same-IV replay
    assert len(recorder.messages) == 1
    link.send_beacon(
        iv_index=1, iv_update=True
    )  # Normal → In Progress: nodes keep sending with IV 0
    assert (state.iv_index, state.tx_iv_index) == (1, 0)
    link.deliver(
        PROXY_NETWORK_PDU, pdus[0]
    )  # the captured PDU, replayed after the beacon
    assert len(recorder.messages) == 1
    assert attached.state.rpl[button] == (0, seq0)
    link.iv_index = 1
    _, new = link.access_pdus(
        button, GROUP_LIVING, M.generic_onoff_set(False, tid=8, transition=0)
    )
    link.deliver(PROXY_NETWORK_PDU, new[0])  # the node moved to IV 1
    assert len(recorder.messages) == 2
    link.deliver(
        PROXY_NETWORK_PDU, pdus[0]
    )  # an old-IV PDU after a new-IV one: rejected as before
    assert len(recorder.messages) == 2
    link.send_beacon(
        iv_index=1, iv_update=False
    )  # In Progress → Normal: IV 0 is *still* receivable
    link.deliver(PROXY_NETWORK_PDU, pdus[0])
    assert len(recorder.messages) == 2
    assert attached.state.rpl[button][0] == 1


async def test_replay_list_purges_only_indexes_that_can_no_longer_be_received(
    attached: ProxyClient, link: FakeBleak, state: LocalState
):
    """Entries of IV indexes below current - 1 are dead weight (no PDU can decrypt under them any more)."""
    attached.state.rpl = {0x0100: (0, 5), 0x0200: (1, 7), 0x0300: (2, 1)}
    link.iv_index = 3
    link.send_beacon(iv_index=3)  # recovery jump to 3: only IV 2 and 3 stay receivable
    assert state.iv_index == 3
    assert attached.state.rpl == {0x0300: (2, 1)}
    attached.state.rpl[0x0400] = (3, 9)
    link.send_beacon(
        iv_index=4, iv_update=True
    )  # 4 in progress: 3 and 4 receivable → (2, 1) goes
    assert attached.state.rpl == {0x0400: (3, 9)}
    link.send_beacon(
        iv_index=4, iv_update=False
    )  # completes: still 3 and 4 → nothing changes
    assert attached.state.rpl == {0x0400: (3, 9)}
    link.send_beacon(iv_index=0)  # a stale beacon is not applied and purges nothing
    assert attached.state.rpl == {0x0400: (3, 9)}


async def test_segmented_send_at_the_sequence_boundary_refuses_instead_of_wrapping(
    attached: ProxyClient, link: FakeBleak, state: LocalState
):
    """probe_seq_boundary: `seq0 + i` past 0xFFFFFF raised OverflowError from `to_bytes` after `reserve_seq` had
    already wrapped the state to 0. Now no sequence number above SEQ_TX_LIMIT is ever used: the send raises
    `SequenceExhausted` (a ConnectionError) before anything goes out, and the state is left untouched."""
    state.seq = SEQ_TX_LIMIT - 1
    with pytest.raises(SequenceExhausted, match="waiting for an IV Update"):
        await attached.send_access(
            GROUP_WC, bytes(30)
        )  # 3 segments: the last would be SEQ_TX_LIMIT + 1
    assert link.net_pdus == []
    assert state.seq == SEQ_TX_LIMIT - 1
    assert attached._ack_waiters == {}
    link.auto_ack()
    assert (
        await attached.send_access(PROXY_NODE, bytes(20)) == SEQ_TX_LIMIT - 1
    )  # 2 segments end at the limit
    assert [n.seq for n in link.net_pdus if not n.ctl] == [
        SEQ_TX_LIMIT - 1,
        SEQ_TX_LIMIT,
    ]
    assert state.seq == SEQ_TX_LIMIT + 1
    with pytest.raises(SequenceExhausted):
        await attached.send_access(PROXY_NODE, M.generic_onoff_get())
    with pytest.raises(SequenceExhausted):
        await attached.set_filter(FILTER_BLACKLIST)
    with pytest.raises(
        ConnectionError
    ):  # the typed error is a ConnectionError for every caller
        await attached.send_config(PROXY_NODE, C.beacon_get())
    assert len(link.net_pdus) == 2
    # the update the network eventually completes restarts the sequence space; the send lock was released
    link.iv_index = 1
    link.send_beacon(iv_index=1)
    assert state.seq == 0
    assert await attached.send_access(PROXY_NODE, M.generic_onoff_get()) == 0


async def test_sequence_exhaustion_during_a_retransmission_round(
    attached: ProxyClient, link: FakeBleak, state: LocalState
):
    """Fresh sequence numbers for retransmissions run out too: the error surfaces and releases the ack waiter."""
    state.seq = SEQ_TX_LIMIT - 1
    with pytest.raises(SequenceExhausted):
        await attached.send_access(
            PROXY_NODE, bytes(20)
        )  # first round fits exactly, the second cannot start
    assert [n.seq for n in link.net_pdus if not n.ctl] == [
        SEQ_TX_LIMIT - 1,
        SEQ_TX_LIMIT,
    ]
    assert attached._ack_waiters == {}
    assert state.seq == SEQ_TX_LIMIT + 1


async def test_exhausted_sequence_stays_attached_and_recovers_with_the_beacon(
    proxy: ProxyClient, link: FakeBleak, state: LocalState, fast: FastAsyncio
):
    """CLI-10: an exhausted counter used to make `attach()` detach before the proxy's connect-time beacon — the
    one thing that could move the transmit IV index and reset SEQ — was ever delivered, so every reconnect
    failed the same way forever. Staying attached, receive-only, lets that beacon actually arrive and recover."""
    state.seq = SEQ_TX_LIMIT + 1
    link.iv_index = 1  # the network has moved on
    link.beacon_on_subscribe = True
    await proxy.attach(link)  # must not raise
    await settle()
    assert proxy.connected
    assert state.tx_iv_index == 1
    assert state.seq >= 1  # the filter re-send took seq 0 under IV 1
    assert link.config_pdus[-1].seq == 0


async def test_segment_acks_we_owe_are_dropped_quietly_when_the_sequence_is_exhausted(
    attached: ProxyClient,
    link: FakeBleak,
    state: LocalState,
    recorder: Recorder,
    caplog: pytest.LogCaptureFixture,
):
    state.seq = SEQ_TX_LIMIT + 1
    _seq0, pdus = link.access_pdus(PROXY_NODE, OUR_SRC, bytes(20))
    with caplog.at_level(logging.ERROR, logger="jhmesh"):
        for p in pdus:
            link.deliver(PROXY_NETWORK_PDU, p)
        await settle()
    assert len(recorder.messages) == 1  # receiving is unaffected
    assert link.sent_acks() == []
    assert "background task failed" not in caplog.text
    assert attached._tasks == set()


async def test_detach_fails_pending_requests_like_a_lost_link(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
):
    """`detach()` used to leave `_waiters` / `_ack_waiters` hanging until their own timeouts (`handle_disconnected`
    failed them at once); both paths share the release now."""
    req = asyncio.create_task(
        attached.request(
            PROXY_NODE, M.generic_onoff_get(), M.GEN_ONOFF_STATUS, timeout=5.0
        )
    )
    await settle()
    seg = asyncio.create_task(
        attached.send_access(LIGHT_2G, bytes(20))
    )  # waits for acks
    await settle()
    assert len(attached._waiters) == 1
    assert len(attached._ack_waiters) == 1
    answered: asyncio.Future[AccessMessage] = asyncio.get_running_loop().create_future()
    answered.set_result(recorder.messages[0] if recorder.messages else None)  # type: ignore[arg-type]
    attached._waiters.append(
        (lambda m: False, answered, None)
    )  # a waiter already resolved: left alone
    await attached.detach()
    with pytest.raises(ConnectionError, match="proxy detached"):
        await req
    assert answered.exception() is None
    with pytest.raises(
        ConnectionError, match="proxy link lost during the segmented message"
    ):
        # woken at once; it sees the link is gone before another round (CLI-06)
        await seg
    assert attached._waiters == []
    assert attached._ack_waiters == {}
    assert link.disconnect_calls == 1
    assert recorder.disconnects == 0  # a detach is ours, not a lost link


async def test_collect_fails_on_link_loss_and_retrieves_its_future(
    attached: ProxyClient,
    link: FakeBleak,
    recorder: Recorder,
    caplog: pytest.LogCaptureFixture,
):
    """The future every waiter carries is what a link loss fails; `collect()` never awaited its own, so a
    disconnect during the window logged 'Future exception was never retrieved' at garbage collection and the
    caller got a partial list as if the window had simply elapsed."""
    task = asyncio.create_task(
        attached.collect(
            GROUP_WC, M.generic_onoff_get(), M.GEN_ONOFF_STATUS, window=5.0
        )
    )
    await settle()
    link.send_access(PROXY_NODE, GROUP_WC, ONOFF_STATUS_ON)
    assert len(attached._waiters) == 1
    attached.handle_disconnected()
    with caplog.at_level(logging.ERROR):
        with pytest.raises(ConnectionError, match="proxy disconnected"):
            await task
        del task
        gc.collect()
        await settle()
    assert "never retrieved" not in caplog.text
    assert attached._waiters == []
    assert recorder.disconnects == 1


@pytest.mark.parametrize("call", ["request", "collect"])
async def test_link_loss_during_a_segmented_request_leaves_no_unretrieved_future(
    attached: ProxyClient,
    link: FakeBleak,
    caplog: pytest.LogCaptureFixture,
    call: str,
):
    """CLI-05: a link lost while the send itself is still going (a segmented message waiting for its ack) fails
    the waiter's future and the send; the send's own error propagates, and the future's must count as read, or
    asyncio logs "Future exception was never retrieved" at ERROR when it is collected."""
    if call == "request":
        coro = attached.request(PROXY_NODE, bytes(20), 0x8204, timeout=1.0)
    else:
        coro = attached.collect(PROXY_NODE, bytes(20), 0x8204)
    task = asyncio.create_task(coro)
    await settle()
    link.is_connected = False
    attached.handle_disconnected()
    with caplog.at_level(logging.ERROR):
        with pytest.raises(ConnectionError):
            await task
        del task
        gc.collect()
        await asyncio.sleep(0)
        gc.collect()
    assert "never retrieved" not in caplog.text
    assert attached._waiters == []


async def test_collect_window_elapsing_is_the_normal_end(
    attached: ProxyClient, link: FakeBleak
):
    link.responders.append(
        lambda n: asyncio.get_running_loop().call_soon(
            link.send_access, DIMMER, GROUP_WC, ONOFF_STATUS_OFF
        )
    )
    got = await attached.collect(
        GROUP_WC, M.generic_onoff_get(), M.GEN_ONOFF_STATUS, window=0.02
    )
    assert [m.src for m in got] == [DIMMER]
    assert attached._waiters == []
    assert attached.connected


async def test_send_reads_the_transmit_iv_index_inside_the_send_lock(
    attached: ProxyClient, link: FakeBleak, state: LocalState, fast: FastAsyncio
):
    """A send queued behind the lock captured the IV index *before* waiting; when an IV Update completed in the
    meantime (transmit index + 1, sequence restarted at 0) it went out under the old index with a reused
    sequence number — a replay for every node."""
    link.send_beacon(
        iv_index=1, iv_update=True
    )  # In Progress: we still transmit under IV 0
    await attached._send_lock.acquire()  # another send holds the lock
    second = asyncio.create_task(attached.send_access(LIGHT_2G, M.generic_onoff_get()))
    await settle()
    link.iv_index = 1
    link.send_beacon(
        iv_index=1, iv_update=False
    )  # the update completes while `second` waits
    assert (state.tx_iv_index, state.seq) == (1, 0)
    attached._send_lock.release()
    await second
    n = link.net_pdus[-1]
    assert n.ivi == 1  # encrypted under the index in force when it was actually sent
    assert link.sent_access()[-1][1:] == (LIGHT_2G, 5, n.seq, M.generic_onoff_get())


async def test_a_segmented_send_waiting_for_its_ack_does_not_hold_up_other_sends(
    attached: ProxyClient, link: FakeBleak, fast: FastAsyncio
):
    """Review-3 T4: the send lock was held across the whole segmented exchange — seconds per round, three rounds,
    to a node that is not there — and every light command queued behind it. Only a round's reservation and
    writes hold it now; a second segmented message to the same node still waits for the first."""
    first = asyncio.create_task(
        attached.send_access(PROXY_NODE, bytes(20))
    )  # never acked
    await settle()
    assert not attached._send_lock.locked()  # waiting for the ack, not holding the lock
    await attached.send_access(LIGHT_2G, M.generic_onoff_get())
    assert link.sent_access()[-1][1] == LIGHT_2G  # went out meanwhile
    second = asyncio.create_task(attached.send_access(PROXY_NODE, bytes(21)))
    await settle()
    assert attached._sar_lock(PROXY_NODE).locked()
    with pytest.raises(TimeoutError):
        await first
    with pytest.raises(TimeoutError):
        await second
    seqs = [n.seq for n in link.net_pdus]
    assert seqs == sorted(
        seqs
    )  # reserved and written in one step: the air sees them in order


def _record_air_order(link: FakeBleak) -> list[int]:
    """Make every GATT write yield to the loop (a real one does) and return the sequence numbers in air order."""
    air: list[int] = []
    write = link.write_gatt_char

    async def yielding_write(
        char: str, data: bytes, response: bool | None = None
    ) -> None:
        await asyncio.sleep(0)
        before = len(link.outgoing)
        await write(char, data, response)
        if len(link.outgoing) > before:
            config = link.outgoing[-1][0] == PROXY_CONFIG
            air.append((link.config_pdus if config else link.net_pdus)[-1].seq)

    link.write_gatt_char = yielding_write  # type: ignore[method-assign]
    return air


async def test_a_segment_ack_does_not_reach_the_air_inside_a_segmented_round(
    attached: ProxyClient, link: FakeBleak, fast: FastAsyncio
):
    """`_send_ack` reserved its number outside the send lock: a segmented message from a node completing while
    our round was being written put its ack between our segments, with a number above the ones they still carried
    — the nodes' replay protection then dropped the rest of the round."""
    air = _record_air_order(link)
    answered = False

    def node_sends_segmented(n: NetworkPDU) -> None:
        nonlocal answered
        # our first segment is on air
        if not answered and not n.ctl and n.dst == GROUP_WC:
            answered = True
            link.send_access(DIMMER, OUR_SRC, bytes(20))  # acknowledged by the client

    link.responders.append(node_sends_segmented)
    await attached.send_access(GROUP_WC, bytes(30))  # 3 segments, sent twice
    await settle()
    assert len(link.sent_acks()) == 1
    assert len(air) == 7
    assert air == sorted(air)  # the air sees them in the order they were reserved


async def test_a_proxy_filter_request_does_not_reach_the_air_inside_a_segmented_round(
    attached: ProxyClient, link: FakeBleak, fast: FastAsyncio
):
    """The same for `set_filter` (a background re-send from `_resend_filter`, say): reserved outside the send
    lock, the request went out between a round's segments with a higher number."""
    air = _record_air_order(link)
    filters: list[asyncio.Task[None]] = []
    sent_before = len(link.config_pdus)

    def resend_filter(n: NetworkPDU) -> None:
        # our first segment is on air
        if not filters and not n.ctl and n.dst == GROUP_WC:
            filters.append(asyncio.create_task(attached.set_filter(FILTER_BLACKLIST)))

    link.responders.append(resend_filter)
    await attached.send_access(GROUP_WC, bytes(30))  # 3 segments, sent twice
    await filters[0]
    assert len(link.config_pdus) == sent_before + 1
    assert len(air) == 7
    assert air == sorted(air)


async def test_a_failing_first_round_releases_the_send_lock(
    attached: ProxyClient, link: FakeBleak, fast: FastAsyncio
):
    link.write_error = OSError("adapter gone")
    with pytest.raises(ConnectionError):
        await attached.send_access(PROXY_NODE, bytes(20))
    assert not attached._send_lock.locked()

    def refuse(count: int) -> int:
        raise SequenceStalled("store not written yet")

    link.write_error = None
    attached.state.reserve_seq = refuse  # type: ignore[method-assign]
    with pytest.raises(SequenceStalled):
        await attached.send_access(PROXY_NODE, bytes(20))
    assert not attached._send_lock.locked()


async def test_segmented_send_starts_over_when_the_iv_index_moves_between_rounds(
    attached: ProxyClient,
    link: FakeBleak,
    state: LocalState,
    caplog: pytest.LogCaptureFixture,
):
    """Retransmitting the segments of an upper transport PDU encrypted under the old index and SeqAuth is useless
    once the transmit index moved (nobody can verify them): the message is re-encrypted under the new index with
    a fresh SeqAuth and sent again from the start."""
    link.send_beacon(iv_index=1, iv_update=True)
    state.seq = 100
    link.auto_ack(drop_once={0, 1})  # the first round gets no acks at all

    def complete_the_update(n: Any) -> None:
        if (
            not n.ctl and n.seq == 101
        ):  # the second segment of the first round is on its way
            link.iv_index = 1
            asyncio.get_running_loop().call_soon(
                lambda: link.send_beacon(iv_index=1, iv_update=False)
            )

    link.responders.append(complete_the_update)
    with caplog.at_level(logging.INFO, logger="jhmesh"):
        seq0 = await attached.send_access(PROXY_NODE, bytes(20))
    assert (
        seq0 == 1
    )  # the SeqAuth of the message that got through: fresh space under IV 1 (0 went to the filter re-send)
    segs = [n for n in link.net_pdus if not n.ctl]
    assert [(n.ivi, n.seq) for n in segs] == [(0, 100), (0, 101), (1, 1), (1, 2)]
    assert [m[1:] for m in link.sent_access()] == [
        (PROXY_NODE, 5, 100, bytes(20)),
        (PROXY_NODE, 5, 1, bytes(20)),
    ]
    assert (
        [n.seq for n in link.config_pdus]
        == [
            0,
            102,
            0,
        ]
    )  # attach; the re-send after the "in progress" beacon; the re-send under the new index
    assert "starting it over" in caplog.text
    assert attached._ack_waiters == {}
    assert state.seq == 3


async def test_retransmission_round_never_reuses_a_nonce_when_the_iv_update_completes_mid_round(
    proxy: ProxyClient, cdb: CDB, state: LocalState
) -> None:
    """CLI-01: every retransmitted segment of a round must take its sequence number from the IV index that was
    checked at the top of that round, with no `await` in between — otherwise a beacon processed mid-round can
    bump the transmit IV index and reset SEQ to 0, so retransmissions reuse `(SRC, SEQ, IV)` nonces already used
    earlier in the same round."""
    link = YieldingBleak(cdb)
    await proxy.attach(link)
    for _ in range(5):
        await proxy.send_access(LIGHT_2G, M.generic_onoff_get())  # seqs 1..5 under IV 0
    link.send_beacon(iv_index=1, iv_update=True)
    await settle()
    state.seq = 100
    link.auto_ack(drop_once={0, 1, 2})

    fired = False

    def complete_the_update(n: Any) -> None:
        nonlocal fired
        if not fired and not n.ctl and n.seq == 103:
            fired = True
            link.iv_index = 1
            asyncio.get_running_loop().call_soon(
                lambda: link.send_beacon(iv_index=1, iv_update=False)
            )

    link.responders.insert(0, complete_the_update)
    with contextlib.suppress(TimeoutError):
        await proxy.send_access(PROXY_NODE, bytes(30))
    keys = [(n.ivi, n.seq, n.ctl, n.ttl) for n in link.net_pdus]
    assert len(keys) == len(set(keys))
    assert [k for k in keys[5:] if k[0] == 0 and k[1] < 100] == []


@pytest.mark.parametrize(
    "bumps", [client_mod.SEGMENT_RESTARTS, client_mod.SEGMENT_RESTARTS + 1]
)
async def test_segmented_send_starts_over_a_bounded_number_of_times(
    attached: ProxyClient,
    link: FakeBleak,
    state: LocalState,
    bumps: int,
    monkeypatch: pytest.MonkeyPatch,
):
    """Starting over is bounded (SEGMENT_RESTARTS): a beacon stream that moves the index on every round ends in
    the usual TimeoutError instead of an endless loop eating sequence numbers — while the last permitted try
    still completes normally when the index finally holds still."""
    # every beacon an IV Index Recovery, each one the spec's 192 h after the one before
    monkeypatch.setattr(
        state_mod,
        "_wall_now",
        itertools.count(0, IV_RECOVERY_MIN_INTERVAL).__next__,
    )
    rounds = 0
    received: dict[tuple[int, int], set[int]] = {}

    def node(n: Any) -> None:
        nonlocal rounds
        if n.ctl:
            return
        hdr = int.from_bytes(n.transport_pdu[1:4], "big")
        seq_zero, seg_o, seg_n = (hdr >> 10) & 0x1FFF, (hdr >> 5) & 0x1F, hdr & 0x1F
        if rounds < bumps:
            if seg_o == seg_n:  # the last segment of a round: move the index, never ack
                rounds += 1
                link.iv_index += 1
                asyncio.get_running_loop().call_soon(
                    lambda: link.send_beacon(iv_index=link.iv_index, iv_update=False)
                )
            return
        got = received.setdefault((n.src, seq_zero), set())
        got.add(seg_o)
        asyncio.get_running_loop().call_soon(
            link.send_ack, n.dst, n.src, seq_zero, sum(1 << i for i in got)
        )

    link.responders.append(node)
    if bumps > client_mod.SEGMENT_RESTARTS:
        with pytest.raises(TimeoutError, match="IV index kept changing"):
            await attached.send_access(PROXY_NODE, bytes(20))
    else:
        seq0 = await attached.send_access(PROXY_NODE, bytes(20))
        assert (
            seq0 == 1
        )  # fresh space under the final index (0 went to that beacon's filter re-send)
        last = [n for n in link.net_pdus if not n.ctl][-1]
        assert (last.ivi, last.seq) == (bumps & 1, seq0 + 1)
    assert rounds == bumps
    assert state.iv_index == bumps
    assert attached._ack_waiters == {}
    link.responders.clear()
    link.auto_ack()
    await attached.send_access(
        PROXY_NODE, bytes(20)
    )  # the send lock is free, the new index in use
    assert [n.ivi for n in link.net_pdus if not n.ctl][-2:] == [bumps & 1] * 2


async def test_segment_with_sego_beyond_segn_is_dropped_quietly(
    attached: ProxyClient, link: FakeBleak, caplog: pytest.LogCaptureFixture
):
    """test_probe_segments: a segment header with SegO > SegN (from anyone holding the NetKey) made the join
    raise KeyError — a traceback per segment via 'error handling proxy PDU' and a poisoned segments table."""
    seq = link.next_seq()
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        for _ in range(3):
            link.deliver(
                PROXY_NETWORK_PDU,
                network_encrypt(
                    link.nk,
                    0,
                    False,
                    3,
                    seq,
                    PROXY_NODE,
                    OUR_SRC,
                    raw_segment(link, seq, 5, 0),
                ),
            )
    assert "error handling proxy PDU" not in caplog.text
    assert "KeyError" not in caplog.text
    assert caplog.text.count("segment 5 of 1: SegO beyond SegN, dropped") == 3
    assert attached._segments == {}
    await settle()
    assert link.sent_acks() == []
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)  # the client keeps working
    assert attached.state.rpl[PROXY_NODE][1] == link.seq


async def test_segment_with_a_different_segn_than_announced_is_dropped(
    attached: ProxyClient, link: FakeBleak, caplog: pytest.LogCaptureFixture
):
    """Segments 2 and 3 of 4, then 'segment 0 of 3' under the same SeqZero: three parts for a three-segment
    message, index 1 missing — the join raised KeyError. The stored SegN of the message being assembled wins."""
    seq = link.next_seq()
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        for seg_o, seg_n in ((2, 3), (3, 3), (0, 2)):
            link.deliver(
                PROXY_NETWORK_PDU,
                network_encrypt(
                    link.nk,
                    0,
                    False,
                    3,
                    seq,
                    PROXY_NODE,
                    OUR_SRC,
                    raw_segment(link, seq, seg_o, seg_n),
                ),
            )
    assert "error handling proxy PDU" not in caplog.text
    assert "segment 0 of 3 contradicts the 4 segments announced, dropped" in caplog.text
    st = attached._segments[(PROXY_NODE, seq & 0x1FFF)]
    assert (sorted(st["parts"]), st["n"], st["done"]) == ([2, 3], 3, False)
    await settle()
    assert link.sent_acks() == [
        (OUR_SRC, PROXY_NODE, seq & 0x1FFF, 0b1100)
    ]  # the partial ack of segment 3


async def test_rx_garbage_counts_oversize_proxy_pdus_per_link(
    attached: ProxyClient, link: FakeBleak, cdb: CDB, recorder: Recorder
):
    """A proxy streaming continuation frames forever (it only needs the public Network ID to be connected to)
    is dropped at the SAR layer and counted, like `rx_undecryptable`, for the application to act on."""
    assert attached.rx_garbage == 0
    assert link.notify_cb is not None
    link.notify_cb(None, bytearray(b"\x40" + bytes(100)))
    link.notify_cb(None, bytearray(b"\x80" + bytes(100)))  # 200 > PROXY_PDU_MAX
    assert attached.rx_garbage == 1
    link.notify_cb(None, bytearray(b"\x00" + bytes(200)))
    assert attached.rx_garbage == 2
    assert attached.rx_undecryptable == 0  # a different signal
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)  # the link still works
    assert len(recorder.messages) == 1
    await attached.detach()
    await attached.attach(FakeBleak(cdb))
    assert attached.rx_garbage == 0  # per link


def test_reprs_never_carry_key_material(proxy: ProxyClient, cdb: CDB, link: FakeBleak):
    """`repr()` of the client and of its transmit keys may end up in a traceback or a diagnostics dump."""
    nk = cdb.net_keys[0]
    secrets = [
        nk.key,
        nk.enc_key,
        nk.priv_key,
        nk.beacon_key,
        cdb.app_keys[0].key,
        *(n.dev_key for n in cdb.nodes),
    ]
    assert all(len(s) == 16 for s in secrets)
    proxy.client = link
    proxy.proxy_addr = PROXY_NODE
    texts = [
        repr(proxy),
        str(proxy),
        repr(proxy._app_key),
        repr(proxy._dev_key(PROXY_NODE)),
        repr(_TxKey(bytes(range(16)), NONCE_APP, True, 1, "app0")),
    ]
    for text in texts:
        for secret in secrets:
            assert secret.hex() not in text.lower()
            assert repr(secret)[2:-1] not in text
        assert "dev_key_of_element" not in text
    nid, aid = nk.nid, cdb.app_keys[0].aid
    assert repr(proxy) == (
        f"ProxyClient(src=0D00, proxy=0148, connected=True, nid={nid}, aid={aid})"
    )
    assert repr(proxy._app_key) == f"_TxKey(nonce=1, akf=True, aid={aid}, label='app0')"
    assert repr(proxy._dev_key(PROXY_NODE)) == (
        "_TxKey(nonce=2, akf=False, aid=0, label='dev:0148')"
    )
    assert proxy._dev_key(PROXY_NODE).devkey
    assert not proxy._app_key.devkey
    proxy.client, proxy.proxy_addr = None, None
    assert repr(proxy) == (
        f"ProxyClient(src=0D00, proxy=None, connected=False, nid={nid}, aid={aid})"
    )


async def test_gateway_api_token_never_reaches_the_log(
    attached: ProxyClient,
    link: FakeBleak,
    recorder: Recorder,
    caplog: pytest.LogCaptureFixture,
):
    """test_probe_token: the phone reads the gateway's API token (Manufacturer property 0xC001) while we listen.
    The per-message RX line printed the raw `value=<hex>` before `describe_status` said `<redacted>` — at INFO."""
    token = b"SECRET-TOKEN-abc123"
    status = M.vendor_property_status("manufacturer", 0xC001, token, user_access=1)
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        link.send_access(GATEWAY, PHONE, status)  # segmented, relayed past us
        await settle()
    m = recorder.messages[-1]
    assert m.params.endswith(token)  # the application still gets the value
    assert token.hex() not in caplog.text
    assert token.decode() not in caplog.text
    assert str(m).endswith(
        "LBC Manufacturer Property Status prop 0xC001 access=1 value=<redacted> gateway_api_token=<redacted>"
    )
    rx = [r for r in caplog.records if r.getMessage().startswith("RX ")]
    assert rx
    assert all(
        r.levelno == logging.DEBUG for r in rx
    )  # per-message traffic is DEBUG, not INFO


async def test_key_refresh_messages_never_reach_the_log(
    attached: ProxyClient,
    link: FakeBleak,
    recorder: Recorder,
    caplog: pytest.LogCaptureFixture,
):
    """test_probe_keyrefresh: during a key refresh the phone sends Config NetKey Update / AppKey Update — the
    *new* keys — to every node under its device key, which the export gives us. They were hex-dumped as
    `SIG op 8045 …` / `SIG op 0001 …`."""
    new_netkey, new_appkey = bytes(range(0xA0, 0xB0)), bytes(range(0xB0, 0xC0))
    dev = link.dev_key(PROXY_NODE)
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        link.send_devkey(PHONE, PROXY_NODE, C.netkey_update(new_netkey), dev)
        link.send_devkey(PHONE, PROXY_NODE, C.appkey_update(new_appkey), dev)
        link.send_devkey(PHONE, PROXY_NODE, C.key_refresh_phase_set(2), dev)
        link.send_devkey(
            PHONE, PROXY_NODE, h("8299") + new_netkey, dev
        )  # an opcode we do not know at all
        link.send_devkey(
            PHONE, PROXY_NODE, C.netkey_add(new_netkey)[:9], dev
        )  # truncated in transit
        await settle()
    assert new_netkey.hex() not in caplog.text
    assert new_appkey.hex() not in caplog.text
    assert new_netkey[:4].hex() not in caplog.text
    assert [str(m).split("] ", 1)[1] for m in recorder.messages] == [
        "Config NetKey Update netkey=0 key=<16 bytes>",
        "Config AppKey Update netkey=0 appkey=0 key=<16 bytes>",
        "Config Key Refresh Phase Set netkey=0 transition=2",
        "SIG op 8299 <16 bytes>",
        "Config NetKey Add ?? <7 bytes>",
    ]
    assert all(m.key == "dev:0148" for m in recorder.messages)
    # the same PDUs under the AppKey are application traffic and keep the hex fallback
    link.send_access(PROXY_NODE, OUR_SRC, h("8299") + new_netkey)
    assert str(recorder.messages[-1]).endswith("SIG op 8299 " + new_netkey.hex())


async def test_tx_log_line_of_a_config_message_hides_undecoded_bytes(
    attached: ProxyClient, link: FakeBleak, caplog: pytest.LogCaptureFixture
):
    new_netkey = bytes(range(0xA0, 0xB0))
    link.auto_ack()
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        await attached.send_config(PROXY_NODE, h("8299") + new_netkey)
        await attached.send_config(PROXY_NODE, C.netkey_update(new_netkey))
    assert new_netkey.hex() not in caplog.text
    assert (
        "TX 0D00→0148 seq=000001 [dev:0148] segmented x2 SIG op 8299 <16 bytes>"
        in caplog.text
    )
    assert (
        "[dev:0148] segmented x2 Config NetKey Update netkey=0 key=<16 bytes>"
        in caplog.text
    )


async def test_replayed_control_messages_are_ignored(
    attached: ProxyClient, link: FakeBleak
):
    """CLI-04: the replay list covers control PDUs too (§3.8.8): a recorded Heartbeat replayed later must not keep
    a dead node "alive", nor a recorded Segment Ack acknowledge a message the node never got."""
    beats: list[Heartbeat] = []
    attached.on_heartbeat = beats.append
    seq = link.next_seq()
    beat = network_encrypt(link.nk, 0, True, 3, seq, PROXY_NODE, OUR_SRC, h("0a050003"))
    for _ in range(3):
        link.deliver(PROXY_NETWORK_PDU, beat)
    assert len(beats) == 1
    assert attached.state.rpl[PROXY_NODE] == (0, seq)

    st = _AckState()
    attached._ack_waiters[(PROXY_NODE, 5)] = st
    ack = network_encrypt(
        link.nk, 0, True, 3, link.next_seq(), PROXY_NODE, OUR_SRC, segment_ack(5, 1)
    )
    link.deliver(PROXY_NETWORK_PDU, ack)
    assert st.block == 1
    st.block = 0
    link.deliver(PROXY_NETWORK_PDU, ack)
    assert st.block == 0
    # an unrecognised control opcode is not acted on, so it does not advance the list
    before = attached.state.rpl[PROXY_NODE]
    link.send_ctl(PROXY_NODE, OUR_SRC, h("7f00"))
    assert attached.state.rpl[PROXY_NODE] == before


async def test_heartbeats_reach_the_callback(
    attached: ProxyClient,
    link: FakeBleak,
    recorder: Recorder,
    caplog: pytest.LogCaptureFixture,
):
    """A Heartbeat control message is parsed (InitTTL, features, hops) and handed to `on_heartbeat`."""
    beats: list[Heartbeat] = []
    attached.on_heartbeat = beats.append
    link.send_ctl(PROXY_NODE, OUR_SRC, h("0a05") + h("0003"), ttl=3)
    assert len(beats) == 1
    beat = beats[0]
    assert (beat.src, beat.dst, beat.init_ttl, beat.ttl, beat.features) == (
        PROXY_NODE,
        OUR_SRC,
        5,
        3,
        3,
    )
    assert beat.hops == 2
    assert Heartbeat(1, 2, 3, 5, 0).hops == 0  # never negative
    assert recorder.messages == []  # not an access message
    # a truncated heartbeat is just logged; a failing handler does not break the link
    link.send_ctl(PROXY_NODE, OUR_SRC, h("0a05"))
    assert len(beats) == 1

    def boom(_beat: Heartbeat) -> None:
        raise RuntimeError("handler")

    attached.on_heartbeat = boom
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        link.send_ctl(PROXY_NODE, OUR_SRC, h("0a050003"))
    assert "on_heartbeat handler failed" in caplog.text
    assert "Heartbeat init_ttl=5 hops=2" in caplog.text


# ============================================================================= replay list vs. segmented messages (P2-1)


def _retransmitted_segment(
    link: FakeBleak, src: int, dst: int, access: bytes, seq0: int, index: int, seq: int
) -> bytes:
    """Segment `index` of the message first sent as `seq0`, re-sent under network sequence number `seq`."""
    upper = upper_encrypt_app(link.ak, link.iv_index, seq0, src, dst, access)
    seg = lower_segments_access(link.ak.aid, seq0, upper)[index]
    return network_encrypt(link.nk, link.iv_index, False, 3, seq, src, dst, seg)


async def test_segmented_message_completing_after_a_later_unsegmented_publish_is_delivered(
    attached: ProxyClient,
    link: FakeBleak,
    recorder: Recorder,
    caplog: pytest.LogCaptureFixture,
):
    """P2-1: the replay list is keyed on the last accepted sequence number and a segmented message is checked on its
    SeqAuth when its first segment is admitted — so a message whose lost segment is retransmitted after the node
    published something else meanwhile is delivered, not acknowledged and then dropped as a "replay"."""
    access = h("8245000100") + b"".join(
        i.to_bytes(2, "little") for i in range(1, 9)
    )  # 3 segments
    seq0, pdus = link.access_pdus(PROXY_NODE, OUR_SRC, access)
    link.deliver(PROXY_NETWORK_PDU, pdus[0])
    link.deliver(PROXY_NETWORK_PDU, pdus[2])  # segment 1 is lost
    await settle()
    assert link.sent_acks()[-1] == (OUR_SRC, PROXY_NODE, seq0 & 0x1FFF, 0b101)
    seq_pub = link.send_access(PROXY_NODE, GROUP_WC, ONOFF_STATUS_ON)  # meanwhile
    assert seq_pub > seq0
    assert attached.state.rpl[PROXY_NODE] == (0, seq_pub)
    assert [m.access_pdu for m in recorder.messages] == [ONOFF_STATUS_ON]
    seq_retx = link.next_seq()
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        link.deliver(
            PROXY_NETWORK_PDU,
            _retransmitted_segment(
                link, PROXY_NODE, OUR_SRC, access, seq0, 1, seq_retx
            ),
        )
    await settle()
    assert "replay" not in caplog.text
    assert [m.access_pdu for m in recorder.messages] == [ONOFF_STATUS_ON, access]
    assert recorder.messages[-1].seq == seq_retx
    assert link.sent_acks()[-1] == (OUR_SRC, PROXY_NODE, seq0 & 0x1FFF, 0b111)
    assert attached.state.rpl[PROXY_NODE] == (
        0,
        seq_retx,
    )  # the list never goes backwards
    # a genuine replay of the completed message (its segments, after the reassembly context expired) is refused
    attached._segments[PROXY_NODE, seq0 & 0x1FFF]["t"] -= 11
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        for p in pdus:
            link.deliver(PROXY_NETWORK_PDU, p)
    await settle()
    assert (
        f"replay from 0148 seq {seq0:06X} ignored (segment of SeqAuth {seq0:06X})"
        in caplog.text
    )
    assert len(recorder.messages) == 2
    assert (PROXY_NODE, seq0 & 0x1FFF) not in attached._segments
    assert link.sent_acks()[-1] == (
        OUR_SRC,
        PROXY_NODE,
        seq0 & 0x1FFF,
        0b111,
    )  # no new ack


async def test_two_interleaved_segmented_messages_are_both_delivered(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
):
    """Message B (later SeqAuth) completes before message A: A is still delivered when its last segment lands."""
    seq_a, pdus_a = link.access_pdus(PROXY_NODE, OUR_SRC, bytes(20))
    seq_b, pdus_b = link.access_pdus(PROXY_NODE, OUR_SRC, bytes(range(20)))
    assert seq_b > seq_a
    link.deliver(PROXY_NETWORK_PDU, pdus_a[0])
    link.deliver(PROXY_NETWORK_PDU, pdus_b[0])
    link.deliver(PROXY_NETWORK_PDU, pdus_b[1])
    assert [m.access_pdu for m in recorder.messages] == [bytes(range(20))]
    link.deliver(PROXY_NETWORK_PDU, pdus_a[1])
    assert [m.access_pdu for m in recorder.messages] == [bytes(range(20)), bytes(20)]
    assert attached.state.rpl[PROXY_NODE] == (
        0,
        seq_b + 1,
    )  # B's last segment: the highest number accepted
    await settle()
    assert (OUR_SRC, PROXY_NODE, seq_a & 0x1FFF, 0b11) in link.sent_acks()


async def test_segment_whose_seq_zero_lies_before_the_iv_index_is_dropped(
    attached: ProxyClient, link: FakeBleak, caplog: pytest.LogCaptureFixture
):
    """`seq_auth_from` refuses a negative SeqAuth (ValueError): the segment is dropped quietly, no context opens."""
    upper = upper_encrypt_app(link.ak, 0, 0x1FFE, PROXY_NODE, OUR_SRC, bytes(20))
    seg = lower_segments_access(link.ak.aid, 0x1FFE, upper)[0]  # SeqZero 0x1FFE
    pdu = network_encrypt(
        link.nk, 0, False, 3, 5, PROXY_NODE, OUR_SRC, seg
    )  # sent as seq 5
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        link.deliver(PROXY_NETWORK_PDU, pdu)
    await settle()
    assert "segment dropped: SeqZero" in caplog.text
    assert attached._segments == {}
    assert link.sent_acks() == []
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_access_pdu_without_an_opcode_is_dropped_before_the_replay_list_moves(
    attached: ProxyClient,
    link: FakeBleak,
    recorder: Recorder,
    caplog: pytest.LogCaptureFixture,
):
    """An upper transport PDU that is only a TransMIC decodes to nothing (§3.7.3): dropped at DEBUG, counted
    nowhere, and the replay list is not advanced by a PDU that was never a message. Unsegmented, it is too short
    for the lower transport already (CRY-05); segmented, it authenticates and fails the access decode."""
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        seq = link.send_access(PROXY_NODE, OUR_SRC, b"")
        link.send_access(PROXY_NODE, OUR_SRC, b"", szmic=1)
    assert (
        "malformed lower transport PDU dropped: unsegmented access PDU" in caplog.text
    )
    assert "malformed access PDU dropped: empty access PDU" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert recorder.messages == []
    assert PROXY_NODE not in attached.state.rpl
    link.send_access(
        PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON, seq=seq
    )  # the number is still free
    assert len(recorder.messages) == 1
    assert attached.state.rpl[PROXY_NODE] == (0, seq)


async def test_block_ack_zero_cancels_the_segmented_message(
    attached: ProxyClient, link: FakeBleak, fast: FastAsyncio
):
    """§3.5.3.3: BlockAck 0 means the receiver cannot take the message; the sender cancels instead of retransmitting
    every segment for four rounds."""

    def refuse(n: Any) -> None:
        if n.ctl or not n.transport_pdu[0] & 0x80:
            return
        seq_zero = (int.from_bytes(n.transport_pdu[1:4], "big") >> 10) & 0x1FFF
        asyncio.get_running_loop().call_soon(link.send_ack, n.dst, n.src, seq_zero, 0)

    link.responders.append(refuse)
    with pytest.raises(TimeoutError, match="cancelled by the receiver"):
        await attached.send_access(PROXY_NODE, bytes(20))
    assert (
        len([n for n in link.net_pdus if not n.ctl]) == 2
    )  # one round, no retransmission
    assert attached._ack_waiters == {}
    assert (
        attached.state.seq == 3
    )  # filter + the two segments: nothing burnt on retries


async def test_attach_over_a_still_attached_client_releases_the_old_link(
    attached: ProxyClient, link: FakeBleak, cdb: CDB
):
    """A transport that reports `is_connected` False without its callback: the next `attach` releases the old
    client (its waiters fail, it is disconnected) instead of carrying its state into the new link."""
    pending = asyncio.ensure_future(
        attached.request(
            PROXY_NODE, M.generic_onoff_get(), M.GEN_ONOFF_STATUS, retries=1
        )
    )
    await settle()
    link.is_connected = False
    assert not attached.connected
    other = FakeBleak(cdb, address="11:22:33:44:55:66", proxy_node=LIGHT_2G)
    await attached.attach(other)
    assert attached.client is other
    assert attached.proxy_addr == LIGHT_2G
    assert link.disconnect_calls == 1
    with pytest.raises(ConnectionError, match="re-attached"):
        await pending
    assert attached._waiters == []
    # re-attaching the very client that is attached releases the link state but does not disconnect it
    await attached.attach(other)
    assert other.disconnect_calls == 0
    assert attached.client is other
    # a previous client whose disconnect fails is logged and forgotten all the same
    other.disconnect_error = RuntimeError("gone already")
    third = FakeBleak(cdb)
    await attached.attach(third)
    assert other.disconnect_calls == 1
    assert attached.client is third


async def test_a_released_or_replaced_client_is_no_longer_listened_to(
    attached: ProxyClient, link: FakeBleak, cdb: CDB, recorder: Recorder
):
    """Whatever a client we let go of still delivers is dropped: its proxy's Filter Status must not name the
    proxy of the current link, its messages must not reach the application or advance the replay list."""
    rpl = dict(attached.state.rpl)
    other = FakeBleak(cdb, address="11:22:33:44:55:66", proxy_node=LIGHT_2G)
    await attached.attach(other)
    assert attached.proxy_addr == LIGHT_2G
    link.deliver(PROXY_CONFIG, link.filter_status_pdu())  # the replaced client
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)
    assert attached.proxy_addr == LIGHT_2G
    assert (recorder.messages, attached.state.rpl) == ([], rpl)
    # re-attaching the same client unsubscribes it first; its old subscription's handler goes quiet too
    stale = other.notify_cb
    await attached.attach(other)
    assert other.stop_notify_calls == 1
    assert stale is not None
    assert stale is not other.notify_cb
    other.notify_cb = stale
    other.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)
    assert recorder.messages == []
    # a client detached without disconnecting is unsubscribed (a failure there is only logged) and ignored
    await settle()
    other.stop_notify_error = RuntimeError("not subscribed")
    await attached.detach(disconnect=False)
    assert (other.stop_notify_calls, other.disconnect_calls) == (2, 0)
    other.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)
    assert recorder.messages == []
    await attached.detach(disconnect=False)  # nothing attached any more
    assert other.stop_notify_calls == 2


async def test_a_gatt_write_that_never_completes_is_a_connection_error(
    attached: ProxyClient, link: FakeBleak, monkeypatch: pytest.MonkeyPatch
):
    """A write without response that never returns (a stalled transport) fails after `GATT_TIMEOUT` instead of
    holding the write and send locks — and every request queued behind them — for good."""
    monkeypatch.setattr(client_mod, "GATT_TIMEOUT", 0.01)
    stalled = asyncio.Event()

    async def hang(char: str, data: bytes, response: bool | None = None) -> None:
        await stalled.wait()

    monkeypatch.setattr(link, "write_gatt_char", hang)
    with pytest.raises(ConnectionError, match=r"not completed within 0\.01s"):
        await attached.send_access(PROXY_NODE, M.generic_onoff_get())
    assert not attached._write_lock.locked()
    assert not attached._send_lock.locked()


async def test_a_subscription_that_never_completes_fails_the_attach(
    proxy: ProxyClient, link: FakeBleak, monkeypatch: pytest.MonkeyPatch
):
    """Review-4 R4-11: a `start_notify` that never returns held the caller's connection loop, and the connection
    slot, for good. It fails the attach after `GATT_TIMEOUT`, and the connection is released like any failure."""
    monkeypatch.setattr(client_mod, "GATT_TIMEOUT", 0.01)
    stalled = asyncio.Event()

    async def hang(char: str, cb: Any) -> None:
        await stalled.wait()

    monkeypatch.setattr(link, "start_notify", hang)
    with pytest.raises(TimeoutError):
        await proxy.attach(link)
    assert proxy.client is None
    assert link.disconnect_calls == 1


async def test_a_disconnect_that_never_completes_is_left_behind(
    attached: ProxyClient,
    link: FakeBleak,
    cdb: CDB,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """Review-4 R4-11: `detach` (and an attach over a client still attached) gives a transport's disconnect
    `GATT_TIMEOUT`, then logs a warning and carries on: the link is released already."""
    monkeypatch.setattr(client_mod, "GATT_TIMEOUT", 0.01)
    stalled = asyncio.Event()
    hung: list[str] = []

    async def hang(self: FakeBleak) -> None:
        hung.append(self.address)
        await stalled.wait()

    monkeypatch.setattr(FakeBleak, "disconnect", hang)
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        await attached.detach()
    assert attached.client is None
    assert hung == [link.address]
    assert f"disconnecting {link.address} did not complete within 0.01s" in caplog.text
    other = FakeBleak(cdb, address="11:22:33:44:55:66")
    await attached.attach(other)
    other.is_connected = False  # gone without its callback
    third = FakeBleak(cdb)
    await attached.attach(third)
    assert attached.client is third
    assert hung == [link.address, other.address]


async def test_a_failing_callback_does_not_break_the_notification_stream(
    attached: ProxyClient, link: FakeBleak, caplog: pytest.LogCaptureFixture
):
    """A bug in an application callback is logged with its traceback (that one *is* ours) and the next
    notification is handled normally."""

    def boom(_addr: int) -> None:
        raise RuntimeError("handler bug")

    attached.on_filter_status = boom
    with caplog.at_level(logging.ERROR, logger="jhmesh"):
        link.deliver(PROXY_CONFIG, link.filter_status_pdu())
    assert "error handling proxy PDU" in caplog.text
    assert "handler bug" in caplog.text
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)
    assert attached.state.rpl[PROXY_NODE][1] == link.seq


async def test_collect_propagates_a_failed_segmented_unicast_send(
    attached: ProxyClient, link: FakeBleak, fast: FastAsyncio
):
    """A segmented message to a unicast that never acknowledges raises out of `collect` instead of returning an
    empty list as if the window had merely elapsed."""
    with pytest.raises(TimeoutError, match="not acknowledged"):
        await attached.collect(PROXY_NODE, bytes(20), M.GEN_ONOFF_STATUS, window=0.01)
    assert attached._waiters == []


# ============================================================================= P3 cleanups (review 1)


async def test_link_loss_during_a_segmented_send_is_a_connection_error(
    attached: ProxyClient,
    link: FakeBleak,
    state: LocalState,
    fast: FastAsyncio,
    monkeypatch: pytest.MonkeyPatch,
):
    """CLI-06: a lost link wakes the sender like an ack would; it must fail with ConnectionError, not report
    "not acknowledged" on its last round, and take no sequence numbers for rounds that cannot go out."""
    segments = [0]

    def drop_after_the_first_round(n: NetworkPDU) -> None:
        if n.dst == PROXY_NODE and not n.ctl:
            segments[0] += 1
            if segments[0] == 2:  # the last segment of the first round
                asyncio.get_running_loop().call_soon(attached.handle_disconnected, link)

    link.responders.append(drop_after_the_first_round)
    monkeypatch.setattr(client_mod, "SEGMENT_RETRIES", 1)
    with pytest.raises(ConnectionError):
        await attached.send_access(PROXY_NODE, bytes(20))

    monkeypatch.setattr(client_mod, "SEGMENT_RETRIES", 4)
    await attached.attach(link)
    segments[0] = 0
    seq0 = state.seq
    with pytest.raises(ConnectionError):
        await attached.send_access(PROXY_NODE, bytes(20))
    assert state.seq == seq0 + 2  # the first round's two, nothing for a retransmission


async def test_link_loss_between_the_rounds_of_a_group_send_takes_no_numbers(
    attached: ProxyClient, link: FakeBleak, state: LocalState, fast: FastAsyncio
):
    """CLI-06: a group message is sent twice without acks; a link lost in between fails before the second round
    reserves its sequence numbers."""
    segments = [0]

    def drop_after_the_first_round(n: NetworkPDU) -> None:
        if n.dst == GROUP_LIVING and not n.ctl:
            segments[0] += 1
            if segments[0] == 2:
                asyncio.get_running_loop().call_soon(attached.handle_disconnected, link)

    link.responders.append(drop_after_the_first_round)
    seq0 = state.seq
    with pytest.raises(ConnectionError, match="lost during the segmented message"):
        await attached.send_access(GROUP_LIVING, bytes(20))
    assert state.seq == seq0 + 2


async def test_collect_sees_a_status_that_also_answered_an_older_request(
    attached: ProxyClient, link: FakeBleak
):
    """CLI-07: a status resolves only the oldest request it fits, but a `collect()` queued behind that request
    must still see it (its predicate is how it gathers)."""
    req = asyncio.create_task(
        attached.request(
            PROXY_NODE, M.generic_onoff_get(), M.GEN_ONOFF_STATUS, timeout=1.0
        )
    )
    await settle()
    col = asyncio.create_task(
        attached.collect(
            GROUP_LIVING, M.generic_onoff_get(), M.GEN_ONOFF_STATUS, window=0.5
        )
    )
    await settle()
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)
    assert (await req).src == PROXY_NODE
    assert [m.src for m in await col] == [PROXY_NODE]


async def test_a_write_queued_behind_a_lost_link_fails_cleanly(
    proxy: ProxyClient, cdb: CDB
):
    """CLI-09: a write waiting for the write lock binds the client it will use inside the lock: after a link
    loss it fails as "not connected", never with an AttributeError on None, nor split across two links."""
    link = YieldingBleak(cdb, mtu_size=23)
    await proxy.attach(link)
    a = asyncio.create_task(proxy.send_access(PROXY_NODE, M.generic_onoff_get()))
    b = asyncio.create_task(proxy._write(PROXY_NETWORK_PDU, bytes(29)))
    await asyncio.sleep(0)
    proxy.handle_disconnected(link)
    results = await asyncio.gather(a, b, return_exceptions=True)
    assert all(isinstance(r, ConnectionError) for r in results)
    assert "NoneType" not in str(results[1])


async def test_a_write_split_across_a_new_link_is_refused(proxy: ProxyClient, cdb: CDB):
    """CLI-09: frames of one proxy PDU never go out on two links."""
    first, second = YieldingBleak(cdb, mtu_size=23), YieldingBleak(cdb, mtu_size=23)
    await proxy.attach(first)
    b = asyncio.create_task(proxy._write(PROXY_NETWORK_PDU, bytes(60)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    proxy.client = (
        second  # a new link installed mid-write (as attach() does after a release)
    )
    with pytest.raises(ConnectionError, match="link changed during the write"):
        await b
    assert second.writes == []


async def test_truncated_filter_status_is_dropped_quietly(
    attached: ProxyClient, link: FakeBleak, caplog: pytest.LogCaptureFixture
):
    """CLI-11: a Filter Status shorter than its 4 octets is malformed: dropped at DEBUG, not an ERROR traceback,
    and it names no proxy node."""
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        link.deliver(
            PROXY_CONFIG,
            network_encrypt(
                link.nk,
                0,
                ctl=True,
                ttl=0,
                seq=link.next_seq(),
                src=0x0150,
                dst=0,
                transport_pdu=b"\x03",
                proxy=True,
            ),
        )
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "too short" in caplog.text
    assert attached.proxy_addr == PROXY_NODE


async def test_segment_with_another_seq_auth_does_not_join_the_reassembly(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
):
    """CLI-12: a segment of an older message with the same SeqZero (SeqAuth 8192 lower) must not replace a part
    of the reassembly in progress, falsely completing it."""
    _seq_a, a = link.access_pdus(PROXY_NODE, OUR_SRC, bytes(20), seq=0x012000)
    _seq_b, b = link.access_pdus(PROXY_NODE, OUR_SRC, bytes(20), seq=0x012000 - 0x2000)
    link.deliver(PROXY_NETWORK_PDU, a[0])
    link.deliver(PROXY_NETWORK_PDU, b[1])
    link.deliver(PROXY_NETWORK_PDU, a[1])
    assert len(recorder.messages) == 1
    assert attached.rx_undecryptable == 0


async def test_done_reassembly_is_kept_ten_seconds_after_completion(
    attached: ProxyClient, link: FakeBleak, monkeypatch: pytest.MonkeyPatch
):
    """CLI-13: the reassembly timer counts from the last segment (§3.5.3.4), so a message that took 9 s to
    complete still answers a retransmission 6 s later with its ack instead of dropping it as a replay."""
    clock = [0.0]
    monkeypatch.setattr(client_mod, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    _seq, pdus = link.access_pdus(PROXY_NODE, OUR_SRC, bytes(20))
    link.deliver(PROXY_NETWORK_PDU, pdus[0])
    clock[0] = 9.0
    link.deliver(PROXY_NETWORK_PDU, pdus[1])  # completes
    await settle()
    clock[0] = 15.0
    link.deliver(PROXY_NETWORK_PDU, pdus[1])  # the sender never saw our ack
    await settle()
    assert len(link.sent_acks()) == 2


# ============================================================================= re-review of Phases 3-5


def _resent_segments(link: FakeBleak, seq0: int, access: bytes) -> list[bytes]:
    """The segments of the message with SeqAuth `seq0`, as its sender retransmits them: fresh sequence numbers."""
    upper = upper_encrypt_app(link.ak, 0, seq0, PROXY_NODE, OUR_SRC, access, 0)
    return [
        network_encrypt(
            link.nk, 0, False, 3, link.next_seq(), PROXY_NODE, OUR_SRC, lower
        )
        for lower in lower_segments_access(link.ak.aid, seq0, upper)
    ]


async def test_a_control_pdu_before_the_first_seen_segment_does_not_lock_the_message_out(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder, fast: FastAsyncio
):
    """Every first-round segment is lost, then the node sends a Heartbeat (the replay list moves past the message's
    SeqAuth); the retransmitted segments carry fresh numbers and must still be taken (§3.8.8 checks each PDU)."""
    access = h("8245000100") + bytes(16)
    seq0, _lost = link.access_pdus(PROXY_NODE, OUR_SRC, access)
    link.send_ctl(PROXY_NODE, 0xC000, bytes([0x0A, 5, 0, 0]))
    await settle()
    for pdu in _resent_segments(link, seq0, access):
        link.deliver(PROXY_NETWORK_PDU, pdu)
    await settle()
    assert len(recorder.messages) == 1


async def test_segments_replayed_with_their_old_numbers_are_still_dropped(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder, fast: FastAsyncio
):
    """A recorded segmented message replayed later carries its original sequence numbers, below the list."""
    access = h("8245000100") + bytes(16)
    _seq0, pdus = link.access_pdus(PROXY_NODE, OUR_SRC, access)
    for pdu in pdus:
        link.deliver(PROXY_NETWORK_PDU, pdu)
    await settle()
    assert len(recorder.messages) == 1
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)  # the node moved on
    attached._segments.clear()  # as after the reassembly memory expired
    for pdu in pdus:
        link.deliver(PROXY_NETWORK_PDU, pdu)
    await settle()
    assert len(recorder.messages) == 2  # the unsegmented one only


async def test_a_completed_message_retransmitted_later_is_not_delivered_twice(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder, fast: FastAsyncio
):
    """The sender missed our ack and retransmits after the reassembly entry expired: fresh numbers, but a message
    already delivered (its SeqAuth is the last one completed from that source)."""
    access = h("8245000100") + bytes(16)
    seq0, pdus = link.access_pdus(PROXY_NODE, OUR_SRC, access)
    for pdu in pdus:
        link.deliver(PROXY_NETWORK_PDU, pdu)
    await settle()
    attached._segments.clear()
    for pdu in _resent_segments(link, seq0, access):
        link.deliver(PROXY_NETWORK_PDU, pdu)
    await settle()
    assert len(recorder.messages) == 1


async def test_a_fully_acknowledged_message_survives_a_link_loss_in_the_grace_period(
    attached: ProxyClient, link: FakeBleak, fast: FastAsyncio
):
    """The first ack covers only part of the message; the rest is acknowledged while the link drops during the
    grace for late acks: delivered, so the send succeeds (the caller must not resend a Set that took)."""
    real_write = link.write_gatt_char
    writes = 0

    async def write(char: str, data: bytes, response: bool | None = None) -> None:
        nonlocal writes
        await real_write(char, data, response)
        writes += 1
        if (
            writes == 2
        ):  # the first segment is acknowledged alone, the second a moment later
            seq_zero = (link.net_pdus[-1].seq - 1) & 0x1FFF
            link.send_ctl(PROXY_NODE, OUR_SRC, segment_ack(seq_zero, 0b01))
            asyncio.get_running_loop().call_soon(
                link.send_ctl, PROXY_NODE, OUR_SRC, segment_ack(seq_zero, 0b11)
            )

    link.write_gatt_char = write  # type: ignore[method-assign]
    real_sleep = fast.sleep

    async def sleep(delay: float, result: Any = None) -> Any:
        out = await real_sleep(delay, result)  # the late ack arrives ...
        if delay == 0.25:
            attached.handle_disconnected(
                link
            )  # ... then the link goes, still in the grace
        return out

    fast.sleep = sleep  # type: ignore[method-assign]
    seq0 = attached.state.seq
    assert await attached.send_access(PROXY_NODE, h("8245") + bytes(range(18))) == seq0


async def test_ready_is_not_left_over_from_an_attach_whose_link_dropped(
    proxy: ProxyClient, cdb: CDB, fast: FastAsyncio
):
    """A link lost during attach's settle must not leave `ready` set for the next attach, before its
    notifications and filter exist."""
    first = FakeBleak(cdb)
    real_sleep = fast.sleep

    async def sleep(delay: float, result: Any = None) -> Any:
        if delay == 0.3 and proxy.client is first:
            proxy.handle_disconnected(first)
        return await real_sleep(delay, result)

    fast.sleep = sleep  # type: ignore[method-assign]
    await proxy.attach(first)
    assert proxy.client is None
    assert not proxy.ready
    fast.sleep = real_sleep  # type: ignore[method-assign]
    second = FakeBleak(cdb)
    seen: list[bool] = []

    async def start_notify(char: str, cb: Any) -> None:
        seen.append(proxy.ready)
        second.notify_cb = cb

    second.start_notify = start_notify  # type: ignore[method-assign]
    await proxy.attach(second)
    assert seen == [False]
    assert proxy.ready


async def test_a_request_queued_behind_two_others_gets_its_reply(
    attached: ProxyClient, link: FakeBleak
):
    """Review-3 T1: a request registered its waiter on the list bound when it *started* waiting for the send
    lock, and every finished request rebound `_waiters` to a new list — so a third request queued behind two
    others registered on an orphaned list and its reply was never delivered (full timeout, then a resend). Every
    connect refresh (five Gets at a time) hit this."""
    sources = [DIMMER, PROXY_NODE, LIGHT_2G]
    link.responders.append(
        lambda n: asyncio.get_running_loop().call_soon(
            link.send_access, n.dst, OUR_SRC, ONOFF_STATUS_ON
        )
    )
    write = link.write_gatt_char

    async def slow_write(char: str, data: bytes, response: bool | None = None) -> None:
        await write(char, data, response)
        await settle()  # a real GATT write yields: the reply to it lands while the send lock is still held

    link.write_gatt_char = slow_write  # type: ignore[method-assign]
    got = await asyncio.gather(
        *(
            attached.request(
                src, M.generic_onoff_get(), M.GEN_ONOFF_STATUS, timeout=0.5, retries=1
            )
            for src in sources
        )
    )
    assert [m.src for m in got] == sources
    assert len(link.net_pdus) == 3  # no resend
    assert attached._waiters == []


async def test_attach_sends_a_held_back_filter_once_the_store_catches_up(
    cdb: CDB, fast: FastAsyncio, caplog: pytest.LogCaptureFixture
):
    """Review-3 T2: `HAState` holds sequence numbers back until its store's save lands (a connect-time beacon
    that moves the IV index forces one). `attach()` took that back-pressure for real exhaustion and never sent
    the filter: the proxy stayed on its default whitelist and every group publication was lost for the link."""

    class StalledOnce(LocalState):
        refused = 0

        def reserve_seq(self, count: int) -> int:
            if self.refused == 0:
                self.refused += 1
                raise SequenceStalled("store not written yet")
            return super().reserve_seq(count)

    link = FakeBleak(cdb)
    proxy = ProxyClient(cdb, StalledOnce(None, OUR_SRC))
    with caplog.at_level(logging.INFO, logger="jhmesh"):
        await proxy.attach(link)
        assert proxy.ready  # attached, the filter follows in the background
        assert link.config_pdus == []
        await settle(30)
    assert [p.transport_pdu for p in link.config_pdus] == [b"\x00\x01"]
    assert proxy.proxy_addr == PROXY_NODE
    assert "proxy filter held back" in caplog.text
    assert proxy._tasks == set()


async def test_an_unanswered_filter_request_is_sent_again(
    proxy: ProxyClient,
    link: FakeBleak,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """The Filter Status is the proxy's acknowledgement of Set Filter Type: without it the request is repeated
    (`FILTER_SET_TRIES` in all), then given up on with a warning — the application's own watchdog decides."""
    monkeypatch.setattr(client_mod, "FILTER_ACK_TIMEOUT", 0.01)
    link.answer_filter = False
    with caplog.at_level(logging.INFO, logger="jhmesh"):
        await proxy.attach(link)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not proxy._tasks:
                break
    assert len(link.config_pdus) == client_mod.FILTER_SET_TRIES
    assert "no Filter Status from the proxy" in caplog.text
    assert "answered none of 3 proxy filter requests" in caplog.text
    assert proxy.connected


async def test_a_filter_answered_on_the_second_try_stops_the_retries(
    proxy: ProxyClient, link: FakeBleak, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(client_mod, "FILTER_ACK_TIMEOUT", 0.01)
    link.answer_filter = False
    await proxy.attach(link)
    link.answer_filter = True
    for _ in range(50):
        await asyncio.sleep(0.01)
        if not proxy._tasks:
            break
    assert len(link.config_pdus) == 2
    assert proxy.proxy_addr == PROXY_NODE


async def test_filter_resend_gives_up_at_once_on_real_exhaustion(
    cdb: CDB, fast: FastAsyncio, caplog: pytest.LogCaptureFixture
):
    """Only back-pressure (`SequenceStalled`) is worth waiting for; the end of the sequence space is not."""

    class Exhausted(LocalState):
        armed = False

        def reserve_seq(self, count: int) -> int:
            if self.armed:
                raise SequenceExhausted("at the end of the 24-bit space")
            return super().reserve_seq(count)

    state = Exhausted(None, OUR_SRC)
    link = FakeBleak(cdb)
    proxy = ProxyClient(cdb, state)
    await proxy.attach(link)
    state.armed = True
    link.iv_index = 1
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        link.send_beacon(iv_index=1)
        await settle(30)
    assert client_mod.FILTER_RESEND_WAIT not in fast.sleeps
    assert "proxy filter re-send failed: at the end of the 24-bit space" in caplog.text
    assert proxy._tasks == set()


async def test_sends_describe_their_message_only_when_debug_is_logged(
    attached: ProxyClient,
    link: FakeBleak,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fast: FastAsyncio,
):
    """Review-3 T9: the TX log line decoded every message it sent, whatever the log level."""
    calls: list[bytes] = []
    real = client_mod.describe

    def counting(pdu: bytes, **kw: Any) -> str:
        calls.append(pdu)
        return real(pdu, **kw)

    monkeypatch.setattr(client_mod, "describe", counting)
    link.auto_ack()
    with caplog.at_level(logging.INFO, logger="jhmesh"):
        await attached.send_access(LIGHT_2G, M.generic_onoff_get())
        await attached.send_access(PROXY_NODE, bytes(20))
    assert calls == []
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        await attached.send_access(LIGHT_2G, M.generic_onoff_get())
    assert set(calls) == {
        M.generic_onoff_get()
    }  # rendered by each handler that emits it
    assert "Generic OnOff Get" in caplog.text


async def test_a_cancelled_send_still_writes_every_frame_of_its_pdu(
    proxy: ProxyClient, cdb: CDB, caplog: pytest.LogCaptureFixture
):
    """Review-3 T8: a send cancelled between the SAR frames of its proxy PDU left the proxy reassembling it, and
    the proxy took the next PDU's frames for the rest — both lost. The frames go out whatever the caller does;
    a failure after the caller left is only logged."""
    link = FakeBleak(cdb, mtu_size=23)
    await proxy.attach(link)
    write = link.write_gatt_char

    async def slow_write(char: str, data: bytes, response: bool | None = None) -> None:
        await asyncio.sleep(0)
        await write(char, data, response)

    link.write_gatt_char = slow_write  # type: ignore[method-assign]
    link.writes.clear()
    task = asyncio.create_task(
        proxy.send_access(PROXY_NODE, M.light_ctl_set(1, 2700, tid=1))
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await settle(10)
    await proxy.send_access(LIGHT_2G, M.generic_onoff_get())
    assert [a[1] for a in link.sent_access()] == [PROXY_NODE, LIGHT_2G]  # both whole
    # the link goes during an orphaned write: its error is read and logged, not left unretrieved
    task = asyncio.create_task(
        proxy.send_access(PROXY_NODE, M.light_ctl_set(1, 2700, tid=2))
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    link.write_error = OSError("adapter gone")
    with (
        caplog.at_level(logging.DEBUG, logger="jhmesh"),
        pytest.raises(asyncio.CancelledError),
    ):
        await task
    await settle(10)
    assert "proxy write finished after its caller left" in caplog.text


# ============================================================================= key refresh, followed (review-3 N2b)

NEW_NET_KEY = bytes(range(0x10, 0x20))
OTHER_NET_KEY = bytes(range(0x30, 0x40))


def phone_config(
    link: FakeBleak, cdb: CDB, node: int, access: bytes, src: int = PHONE
) -> None:
    """The provisioner (the app on the phone) sends `node` a Config message sealed with the node's device key."""
    n = cdb.node_by_addr(node)
    assert n is not None
    link.send_devkey(src, node, access, n.dev_key)


def node_answers(
    link: FakeBleak, node: int, access: bytes, src: int | None = None
) -> None:
    """`node` answers the provisioner, sealed with its own device key (from `src`, its primary element by default)."""
    link.send_devkey(node if src is None else src, PHONE, access, link.dev_key(node))


def phase_status(phase: int, status: int = 0, net_key_index: int = 0) -> bytes:
    """Config Key Refresh Phase Status: `[status][netKeyIndex u16][phase]`."""
    return (
        encode_opcode(C.CONFIG_KEY_REFRESH_PHASE_STATUS)
        + bytes([status])
        + net_key_index.to_bytes(2, "little")
        + bytes([phase])
    )


def netkey_status(status: int = 0, net_key_index: int = 0) -> bytes:
    """Config NetKey Status: `[status][netKeyIndex u16]`."""
    return (
        encode_opcode(C.CONFIG_NETKEY_STATUS)
        + bytes([status])
        + net_key_index.to_bytes(2, "little")
    )


def refresh_record(
    key: bytes, phase: int, proof: str | None = None
) -> KeyRefreshRecord:
    return KeyRefreshRecord(key, phase, proof)


async def test_a_key_refresh_by_the_provisioner_is_followed(
    attached: ProxyClient,
    link: FakeBleak,
    cdb: CDB,
    state: LocalState,
    fast: FastAsyncio,
    caplog: pytest.LogCaptureFixture,
):
    """The app refreshes the NetKey (NetKey Update to every node, Phase Set 2, Phase Set 3), all sealed with the
    nodes' device keys the export gives us. Unfollowed, the link went deaf at Phase 2 and dead at Phase 3.

    Review 4 (D4): the requests alone no longer move it — the proxy's own Phase Status does (Phase 2), and the
    proxy's beacon under the new key without the flag (Phase 3)."""
    old = attached.nk
    phases: list[tuple[int, bytes]] = []
    attached.on_key_refresh = lambda phase, key: phases.append((phase, key.key))
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        phone_config(link, cdb, PROXY_NODE, C.netkey_update(NEW_NET_KEY))
    assert "following it (phase 1)" in caplog.text
    assert NEW_NET_KEY.hex() not in caplog.text
    assert attached.key_refresh_phase == 1
    assert attached.nk is old  # phase 1: still transmitting with the old key ...
    assert state.key_refresh == KeyRefreshRecord(
        NEW_NET_KEY, 1, nodes=frozenset({PROXY_NODE})
    )
    new = NetKeyMaterial.derive(NEW_NET_KEY)
    link.nk = new  # ... and the nodes' traffic under the new one is heard
    link.send_access(LIGHT_2G, OUR_SRC, ONOFF_STATUS_ON)
    assert attached.state.rpl[LIGHT_2G][1] == link.seq
    phone_config(
        link, cdb, PROXY_NODE, C.netkey_update(NEW_NET_KEY)
    )  # repeated: no change
    node_answers(
        link, PROXY_NODE, netkey_status()
    )  # the proxy took it (phase 1 confirmed)
    link.nk = old
    phone_config(link, cdb, PROXY_NODE, C.key_refresh_phase_set(2))
    await settle(10)
    assert (attached.key_refresh_phase, attached.nk) == (
        1,
        old,
    )  # a request is no proof
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        node_answers(
            link, PROXY_NODE, phase_status(2)
        )  # the proxy node says it is in phase 2
    await settle(10)
    assert (
        "key refresh phase 2 (proof: Key Refresh Phase Status from the proxy node 0148)"
        in caplog.text
    )
    assert attached.key_refresh_phase == 2
    assert attached.nk.key == NEW_NET_KEY  # phase 2: transmitting with the new key
    link.nk = new
    await attached.send_access(LIGHT_2G, M.generic_onoff_get())
    assert link.net_pdus[-1].dst == LIGHT_2G  # the fake opened it with the new key
    assert (
        link.config_pdus[-1].transport_pdu[0] == 0x00
    )  # the proxy filter again, under the new key
    assert state.key_refresh is not None
    assert (state.key_refresh.phase, state.key_refresh.proof) == (2, PROOF_PROXY)
    # a beacon under the new key without the flag: phase 3, the old key is revoked
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        link.send_beacon(iv_index=0, key_refresh=False)
    assert (
        "key refresh complete (proof: the proxy's beacon under the new key)"
        in caplog.text
    )
    assert attached.key_refresh_phase == 0
    assert attached.rx_net_keys == (attached.nk,)
    assert attached.nk.key == NEW_NET_KEY
    assert state.key_refresh is not None  # kept until the export holds the new key
    assert (state.key_refresh.key, state.key_refresh.phase) == (NEW_NET_KEY, 3)
    assert state.key_refresh.proof == PROOF_BEACON
    # phase 1 twice: learnt, then proven by the proxy's own NetKey Status (review-4 D11)
    assert phases == [
        (1, NEW_NET_KEY),
        (1, NEW_NET_KEY),
        (2, NEW_NET_KEY),
        (0, NEW_NET_KEY),
    ]
    # a restart before the export has the new key: the new key alone, no refresh in progress
    restarted = ProxyClient(cdb, state)
    assert restarted.rx_net_keys == (restarted.nk,)
    assert restarted.nk.key == NEW_NET_KEY
    assert restarted.key_refresh_phase == 0
    assert attached.classify_service_data(b"\x00" + new.network_id) == (
        "network-id",
        None,
    )


async def test_advert_verdicts_do_not_outlive_a_key_change(
    attached: ProxyClient, link: FakeBleak, cdb: CDB
):
    """Review-4 R4-8: a kept verdict is for the keys accepted when it was made — the new key's proxies are ours
    the moment a key refresh accepts it, though their advert was another network's a moment before."""
    advert = b"\x00" + NetKeyMaterial.derive(NEW_NET_KEY).network_id
    assert attached.classify_service_data(advert) is None
    phone_config(link, cdb, PROXY_NODE, C.netkey_update(NEW_NET_KEY))
    assert attached.key_refresh_phase == 1
    assert attached.classify_service_data(advert) == ("network-id", None)


async def test_a_config_request_under_the_old_key_while_transmitting_with_the_new(
    attached: ProxyClient, link: FakeBleak, cdb: CDB, fast: FastAsyncio
):
    """Review-4 D11: in a proven Phase 2 the client transmits with the new key, which a node still waiting for its
    NetKey Update does not hold; `old_net_key` seals a Config request with the old one — unsegmented and
    segmented — while every other message keeps the new one. `key_refresh_target` names a proven phase only."""
    old = attached.nk
    assert attached.key_refresh_target is None
    phone_config(link, cdb, PROXY_NODE, C.netkey_update(NEW_NET_KEY))
    assert attached.key_refresh_target is None  # learnt from one Update: not proven
    link.nk = NetKeyMaterial.derive(
        NEW_NET_KEY
    )  # the proxy re-filters under the new key
    node_answers(link, PROXY_NODE, phase_status(2))
    await settle(10)
    assert attached.key_refresh_target == (2, NEW_NET_KEY)
    assert attached.nk.key == NEW_NET_KEY
    link.nk = old  # a node that holds the old key only
    link.auto_ack()
    link.auto_config(
        lambda _node, access: (
            netkey_status()
            if access == C.netkey_update(NEW_NET_KEY)
            else phase_status(0)
        )
    )
    reply = await attached.request_config(
        LIGHT_2G,
        C.key_refresh_phase_get(),
        C.CONFIG_KEY_REFRESH_PHASE_STATUS,
        timeout=1.0,
        old_net_key=True,
    )
    assert reply.params == bytes([0, 0, 0, 0])
    reply = await attached.request_config(
        LIGHT_2G,
        C.netkey_update(NEW_NET_KEY),
        C.CONFIG_NETKEY_STATUS,
        timeout=1.0,
        old_net_key=True,
    )  # 20 bytes: two segments, each under the old key
    assert reply.params == bytes(3)
    assert [m[4] for m in link.sent_config()][-2:] == [
        C.key_refresh_phase_get(),
        C.netkey_update(NEW_NET_KEY),
    ]
    assert attached.nk.key == NEW_NET_KEY  # the rest still goes out under the new key


async def test_a_key_refresh_beacon_moves_to_phase_two_and_messages_that_are_not_ours_do_not_count(
    attached: ProxyClient, link: FakeBleak, cdb: CDB, fast: FastAsyncio
):
    old = attached.nk
    phone_config(
        link, cdb, PROXY_NODE, C.netkey_update(old.key)
    )  # the key we have: nothing to follow
    phone_config(link, cdb, PROXY_NODE, C.netkey_update(NEW_NET_KEY, net_key_index=1))
    phone_config(
        link, cdb, PROXY_NODE, C.key_refresh_phase_set(2)
    )  # no refresh known: ignored
    phone_config(link, cdb, PROXY_NODE, C.key_refresh_phase_set(2, net_key_index=1))
    link.send_access(
        PHONE, PROXY_NODE, C.netkey_update(NEW_NET_KEY)
    )  # AppKey-sealed: not the provisioner
    phone_config(
        link, cdb, PHONE, C.netkey_update(NEW_NET_KEY)
    )  # to a phone: not a device
    node_answers(link, PROXY_NODE, phase_status(2))  # nothing to confirm yet
    assert attached.key_refresh_phase == 0
    assert attached.rx_net_keys == (old,)
    phone_config(link, cdb, PROXY_NODE, C.netkey_update(NEW_NET_KEY))
    link.nk = NetKeyMaterial.derive(NEW_NET_KEY)
    link.send_beacon(
        iv_index=0, key_refresh=True
    )  # the proxy in phase 2 beacons with the new key and the flag
    assert attached.key_refresh_phase == 2
    link.send_beacon(iv_index=0, key_refresh=True)  # again: no change
    assert attached.key_refresh_phase == 2
    link.nk = old
    link.send_beacon(
        iv_index=0, key_refresh=True
    )  # the old key's beacon: parsed, no phase change
    phone_config(  # a phase that does not move it on (the builder refuses 1: raw bytes)
        link, cdb, PROXY_NODE, C.key_refresh_phase_set(2)[:-1] + b"\x01"
    )
    assert attached.key_refresh_phase == 2
    link.deliver(PROXY_BEACON, b"\x00" + bytes(22))  # not a secure network beacon
    # a second, different new key during a proven phase 2 (review 4): accepted too, the switched key stays
    phone_config(link, cdb, PROXY_NODE, C.netkey_update(OTHER_NET_KEY))
    assert attached.key_refresh_phase == 2
    assert attached.nk.key == NEW_NET_KEY
    assert [k.key for k in attached.rx_net_keys] == [
        old.key,
        NEW_NET_KEY,
        OTHER_NET_KEY,
    ]


async def test_a_private_beacon_under_the_new_key_proves_the_key_refresh(
    attached: ProxyClient, link: FakeBleak, cdb: CDB, state: LocalState
):
    """Review-4 P I-4: a proxy with privacy on proves Phase 2 and 3 with Mesh Private beacons under the new key."""
    old = attached.nk
    phone_config(link, cdb, PROXY_NODE, C.netkey_update(NEW_NET_KEY))
    assert attached.key_refresh_phase == 1
    link.send_private_beacon(key_refresh=True)  # the old key's: no proof
    assert attached.key_refresh_phase == 1
    link.nk = NetKeyMaterial.derive(NEW_NET_KEY)
    link.send_private_beacon(
        key_refresh=True
    )  # the old key does not open it, the new one does
    assert attached.key_refresh_phase == 2
    assert attached.nk.key == NEW_NET_KEY
    assert state.key_refresh is not None
    assert (state.key_refresh.phase, state.key_refresh.proof) == (2, PROOF_BEACON)
    link.send_private_beacon(key_refresh=False)  # phase 3: the old key is revoked
    assert attached.key_refresh_phase == 0
    assert attached.rx_net_keys == (attached.nk,)
    assert old not in attached.rx_net_keys
    assert (state.key_refresh.phase, state.key_refresh.proof) == (3, PROOF_BEACON)


async def test_a_forged_key_refresh_from_one_node_is_not_followed(
    attached: ProxyClient,
    link: FakeBleak,
    cdb: CDB,
    state: LocalState,
    fast: FastAsyncio,
    caplog: pytest.LogCaptureFixture,
):
    """Review 4 (D4, P4-1): a node holds its own device key and the NetKey. Sending itself (src = dst = itself,
    its own device key) NetKey Update with a key of its choice, Phase Set 2 and Phase Set 3 moved Home Assistant
    onto that key, dropped the real one and persisted it — deaf and mute, across restarts."""
    old = attached.nk
    phases: list[int] = []
    attached.on_key_refresh = lambda phase, _key: phases.append(phase)
    dev = link.dev_key(LIGHT_2G)
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        for pdu in (
            C.netkey_update(NEW_NET_KEY),
            C.key_refresh_phase_set(2),
            C.key_refresh_phase_set(3),
        ):
            link.send_devkey(LIGHT_2G, LIGHT_2G, pdu, dev)
        await settle(10)
    assert "Config NetKey Update 0232→0232 not followed" in caplog.text
    assert NEW_NET_KEY.hex() not in caplog.text
    assert attached.key_refresh_phase == 0
    assert attached.nk is old
    assert attached.rx_net_keys == (old,)
    assert state.key_refresh is None  # nothing persisted
    # from another element of its own, or to another element of its own: no better
    link.send_devkey(LIGHT_2G + 1, LIGHT_2G, C.netkey_update(NEW_NET_KEY), dev)
    link.send_devkey(PHONE, LIGHT_2G + 1, C.netkey_update(NEW_NET_KEY), dev)
    assert attached.key_refresh_phase == 0
    # posing as the phone it looks like the app: a candidate, accepted and never used ...
    link.send_devkey(PHONE, LIGHT_2G, C.netkey_update(NEW_NET_KEY), dev)
    for transition in (2, 3):
        link.send_devkey(PHONE, LIGHT_2G, C.key_refresh_phase_set(transition), dev)
    # ... and its own statuses, whatever phase they claim, are one node's word
    node_answers(link, LIGHT_2G, netkey_status())
    node_answers(link, LIGHT_2G, phase_status(2))
    node_answers(link, LIGHT_2G, phase_status(0))
    # from its other elements, or in other nodes' names sealed with its own key: not counted at all
    node_answers(link, LIGHT_2G, phase_status(2), src=LIGHT_2G + 1)
    link.send_devkey(SOCKET, PHONE, phase_status(2), dev)
    link.send_devkey(SOCKET, LIGHT_2G, phase_status(2), dev)
    await settle(10)
    assert attached.key_refresh_phase == 1
    assert attached.nk is old  # still transmitting with the real key
    assert [k.key for k in attached.rx_net_keys] == [old.key, NEW_NET_KEY]
    assert state.key_refresh is not None
    assert (state.key_refresh.phase, state.key_refresh.proof) == (1, None)
    assert phases == [1]
    # a restart: the export's key, the forged one only accepted
    restarted = ProxyClient(cdb, state)
    assert restarted.nk.key == old.key
    assert restarted.key_refresh_phase == 1


async def test_a_key_refresh_proven_by_two_nodes_statuses(
    attached: ProxyClient,
    link: FakeBleak,
    cdb: CDB,
    state: LocalState,
    fast: FastAsyncio,
    caplog: pytest.LogCaptureFixture,
):
    """Phase Status from two distinct nodes, each under its own device key, prove Phase 2; after Phase Set 3,
    phase 0 from two nodes that held the key proves Phase 3."""
    phases: list[int] = []
    attached.on_key_refresh = lambda phase, _key: phases.append(phase)
    for node in (LIGHT_2G, SOCKET, DIMMER):
        phone_config(link, cdb, node, C.netkey_update(NEW_NET_KEY))
    node_answers(link, LIGHT_2G, netkey_status())
    node_answers(
        link, SOCKET, netkey_status(status=0x0B)
    )  # Cannot Update: no confirmation
    node_answers(link, SOCKET, phase_status(2, status=0x0B))  # a failure: not counted
    node_answers(link, SOCKET, phase_status(2, net_key_index=1))  # another subnet
    node_answers(link, SOCKET, netkey_status(net_key_index=1))
    for node in (LIGHT_2G, SOCKET, DIMMER):
        phone_config(link, cdb, node, C.key_refresh_phase_set(2))
    node_answers(link, LIGHT_2G, phase_status(2))
    assert attached.key_refresh_phase == 1  # one node: no proof yet
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        node_answers(link, SOCKET, phase_status(2))
    assert "Key Refresh Phase Status from nodes 0172, 0232" in caplog.text
    assert attached.key_refresh_phase == 2
    assert attached.nk.key == NEW_NET_KEY
    assert state.key_refresh is not None
    assert state.key_refresh.proof == PROOF_STATUSES
    node_answers(link, DIMMER, phase_status(2))  # a third: nothing more to do
    link.nk = NetKeyMaterial.derive(
        NEW_NET_KEY
    )  # the nodes transmit with the new key now
    node_answers(
        link, LIGHT_2G, phase_status(0)
    )  # phase 0 before any Phase Set 3 means nothing
    for node in (LIGHT_2G, SOCKET, DIMMER):
        phone_config(link, cdb, node, C.key_refresh_phase_set(3))
    node_answers(link, LIGHT_2G, phase_status(0))
    assert attached.key_refresh_phase == 2
    node_answers(link, SOCKET, phase_status(0))
    assert attached.key_refresh_phase == 0
    assert attached.rx_net_keys == (attached.nk,)
    assert attached.nk.key == NEW_NET_KEY
    assert state.key_refresh is not None
    assert (state.key_refresh.phase, state.key_refresh.proof) == (3, PROOF_STATUSES)
    assert phases == [1, 2, 0]


async def test_a_key_refresh_completed_by_the_proxys_status(
    attached: ProxyClient,
    link: FakeBleak,
    cdb: CDB,
    state: LocalState,
    fast: FastAsyncio,
):
    """The proxy node's own word is enough (as its beacon is): Phase Status 0 after Phase Set 3, from phase 1."""
    phone_config(link, cdb, PROXY_NODE, C.netkey_update(NEW_NET_KEY))
    node_answers(link, PROXY_NODE, phase_status(1))  # Phase Get: it holds the key
    phone_config(link, cdb, PROXY_NODE, C.key_refresh_phase_set(3))
    link.nk = NetKeyMaterial.derive(NEW_NET_KEY)
    node_answers(link, PROXY_NODE, phase_status(0))
    assert attached.key_refresh_phase == 0
    assert attached.nk.key == NEW_NET_KEY
    assert state.key_refresh is not None
    assert (state.key_refresh.phase, state.key_refresh.proof) == (3, PROOF_PROXY)


async def test_an_aborted_key_refresh_restarted_with_another_key(
    attached: ProxyClient,
    link: FakeBleak,
    cdb: CDB,
    state: LocalState,
    fast: FastAsyncio,
):
    """The app aborts a refresh to phase 0 when a node lags (`transport-provisioning.md` §4.2) and may start again
    with another key: Home Assistant followed the requests and moved on its own. Now neither key is dropped, and
    nothing moves without proof."""
    old = attached.nk
    for node in (PROXY_NODE, LIGHT_2G):
        phone_config(link, cdb, node, C.netkey_update(NEW_NET_KEY))
        phone_config(link, cdb, node, C.key_refresh_phase_set(2))
    node_answers(
        link, LIGHT_2G, phase_status(2)
    )  # one node moved, the other lags: the app aborts
    for node in (PROXY_NODE, LIGHT_2G):
        phone_config(link, cdb, node, C.netkey_update(OTHER_NET_KEY))
        phone_config(link, cdb, node, C.key_refresh_phase_set(3))
    node_answers(link, LIGHT_2G, phase_status(0))
    assert attached.key_refresh_phase == 1
    assert attached.nk is old
    assert [k.key for k in attached.rx_net_keys] == [
        old.key,
        NEW_NET_KEY,
        OTHER_NET_KEY,
    ]
    # the proxy's beacon says which one the mesh took
    link.nk = NetKeyMaterial.derive(OTHER_NET_KEY)
    link.send_beacon(iv_index=0, key_refresh=True)
    assert attached.key_refresh_phase == 2
    assert attached.nk.key == OTHER_NET_KEY
    assert NEW_NET_KEY in [
        k.key for k in attached.rx_net_keys
    ]  # still accepted until a proven phase 3


async def test_a_forged_update_during_a_real_refresh_does_not_take_its_statuses(
    attached: ProxyClient, link: FakeBleak, cdb: CDB, fast: FastAsyncio
):
    """A node forging an Update of its own key during the app's refresh moves only its own vote: the statuses of the
    other nodes count for the key the app sent them."""
    for node in (PROXY_NODE, SOCKET, DIMMER):
        phone_config(link, cdb, node, C.netkey_update(NEW_NET_KEY))
    phone_config(
        link, cdb, LIGHT_2G, C.netkey_update(OTHER_NET_KEY)
    )  # the forger, to itself
    node_answers(link, LIGHT_2G, phase_status(2))
    node_answers(link, SOCKET, phase_status(2))
    node_answers(link, DIMMER, phase_status(2))
    assert attached.key_refresh_phase == 2
    assert attached.nk.key == NEW_NET_KEY


async def test_a_key_refresh_in_progress_survives_a_restart(
    cdb: CDB, tmp_path: Path, fast: FastAsyncio
):
    path = tmp_path / "state.json"
    state = LocalState(path, OUR_SRC)
    state.set_key_refresh(
        KeyRefreshRecord(
            NEW_NET_KEY,
            2,
            PROOF_STATUSES,
            frozenset({LIGHT_2G, SOCKET}),
            {2: frozenset({LIGHT_2G, SOCKET})},
        )
    )
    assert json.loads(path.read_text())["key_refresh"] == {
        "key": NEW_NET_KEY.hex(),
        "phase": 2,
        "proof": "statuses",
        "nodes": ["0172", "0232"],
        "confirmed": {"2": ["0172", "0232"]},
    }
    assert "key_refresh" not in state.to_dict()  # never in diagnostics
    state.close()
    resumed = LocalState(path, OUR_SRC)
    proxy = ProxyClient(cdb, resumed)
    assert proxy.key_refresh_phase == 2
    assert proxy.nk.key == NEW_NET_KEY
    resumed.close()
    # the export already has the new key (the user exported again): nothing left to follow
    raw = json.loads(path.read_text())
    raw["key_refresh"]["key"] = cdb.net_keys[0].key.hex()
    path.write_text(json.dumps(raw))
    again = LocalState(path, OUR_SRC)
    proxy = ProxyClient(cdb, again)
    assert proxy.key_refresh_phase == 0
    assert again.key_refresh is None
    again.close()
    # a record whose key refresh is not one is unusable
    raw["key_refresh"] = {"key": "00", "phase": 1}
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="16 bytes"):
        LocalState.parse_record({**raw, "src": "0D00"})


@pytest.mark.parametrize("phase", [2, 3])
def test_a_stored_phase_without_proof_is_only_a_candidate(cdb: CDB, phase: int) -> None:
    """A record written before review 4 (no proof field) or by a forged refresh: the key is accepted, not used, and
    the export's key stays — a real completed refresh is picked up again by the proxy's first beacon."""
    old = cdb.net_keys[0]
    state = LocalState(None, OUR_SRC)
    state.set_key_refresh(KeyRefreshRecord(NEW_NET_KEY, phase))
    proxy = ProxyClient(cdb, state)
    assert proxy.key_refresh_phase == 1
    assert proxy.nk is old
    assert [k.key for k in proxy.rx_net_keys] == [old.key, NEW_NET_KEY]


async def test_a_failing_key_refresh_handler_is_logged(
    attached: ProxyClient, link: FakeBleak, cdb: CDB, caplog: pytest.LogCaptureFixture
):
    def boom(phase: int, key: NetKeyMaterial) -> None:
        raise RuntimeError("handler broke")

    attached.on_key_refresh = boom
    with caplog.at_level(logging.ERROR, logger="jhmesh"):
        phone_config(link, cdb, PROXY_NODE, C.netkey_update(NEW_NET_KEY))
    assert "on_key_refresh handler failed" in caplog.text
    assert attached.key_refresh_phase == 1


def test_a_completed_key_refresh_already_in_the_export_is_left_alone(cdb: CDB) -> None:
    """Phase 3 with the export's own key: the caller put the new key in (`async_apply_followed_key_refresh`);
    the record stays for the next setup, and setting the same refresh again writes nothing."""
    state = LocalState(None, OUR_SRC)
    done = KeyRefreshRecord(cdb.net_keys[0].key, 3, PROOF_BEACON)
    state.set_key_refresh(done)
    state.set_key_refresh(done)  # unchanged: no persist
    proxy = ProxyClient(cdb, state)
    assert state.key_refresh == done
    assert proxy.key_refresh_phase == 0
    assert proxy.rx_net_keys == (cdb.net_keys[0],)


@pytest.mark.parametrize("phase", [1, 2])
async def test_an_export_written_mid_key_refresh_is_followed_from_its_phase(
    cdb: CDB, state: LocalState, fast: FastAsyncio, phase: int
):
    """Mesh CDB `netKeys[].phase` / `oldKey`: in phase 1 the network still transmits with the old key, from phase 2
    with the new one (`key`), and accepts both until phase 3 (§3.10.4). Taking `key` alone, a phase 1 export put
    Home Assistant on a key no node transmitted with yet, and missed the proxies advertising the old one."""
    old = cdb.net_keys[0]
    refreshing = refreshing_cdb(NEW_NET_KEY, phase)
    new = refreshing.net_keys[0]
    proxy = ProxyClient(refreshing, state)
    assert proxy.key_refresh_phase == phase
    tx = old if phase == 1 else new
    assert proxy.nk.key == tx.key
    assert [k.key for k in proxy.rx_net_keys] == [old.key, NEW_NET_KEY]
    rnd = bytes(range(8))
    for nk in (old, new):
        assert proxy.classify_service_data(b"\x00" + nk.network_id) == (
            "network-id",
            None,
        )
        identity = b"\x01" + nk.node_identity_hash(rnd, PROXY_NODE) + rnd
        assert proxy.classify_service_data(identity) == ("node-identity", PROXY_NODE)
    link = FakeBleak(refreshing)
    link.nk = tx  # the proxy opens what we send only under the key of the phase
    await proxy.attach(link)
    assert proxy.proxy_addr == PROXY_NODE
    await proxy.send_access(LIGHT_2G, M.generic_onoff_get())
    assert link.net_pdus[-1].dst == LIGHT_2G
    for nk in (old, new):  # the nodes' traffic under either key is heard
        link.nk = nk
        link.send_access(LIGHT_2G, OUR_SRC, ONOFF_STATUS_ON)
        assert proxy.state.rpl[LIGHT_2G][1] == link.seq
    # the new key's beacon without the flag: phase 3, the old key is revoked
    link.nk = new
    link.send_beacon(iv_index=0, key_refresh=False)
    assert proxy.key_refresh_phase == 0
    assert proxy.rx_net_keys == (new,)
    assert state.key_refresh is not None
    assert (state.key_refresh.key, state.key_refresh.phase) == (NEW_NET_KEY, 3)


async def test_statuses_count_for_the_refresh_the_export_was_written_in(
    cdb: CDB, state: LocalState, fast: FastAsyncio
):
    """No NetKey Update heard (it was sent before the export): the nodes' statuses count for the export's own
    refresh, and phase 0 after Phase Set 3 completes it."""
    proxy = ProxyClient(refreshing_cdb(NEW_NET_KEY, 2), state)
    link = FakeBleak(refreshing_cdb(NEW_NET_KEY, 2))
    await proxy.attach(link)
    for node in (LIGHT_2G, SOCKET):
        phone_config(link, cdb, node, C.key_refresh_phase_set(3))
        node_answers(link, node, phase_status(0))
    assert proxy.key_refresh_phase == 0
    assert [k.key for k in proxy.rx_net_keys] == [NEW_NET_KEY]


async def test_a_truncated_netkey_update_teaches_nothing(
    attached: ProxyClient, link: FakeBleak, cdb: CDB, fast: FastAsyncio
):
    phone_config(link, cdb, PROXY_NODE, C.netkey_update(NEW_NET_KEY)[:12])
    assert attached.key_refresh_phase == 0


def test_a_proof_before_the_link_is_up_sends_no_filter(
    cdb: CDB, state: LocalState
) -> None:
    """A beacon handled before `attach` set up the proxy filter moves the refresh; nothing is sent."""
    proxy = ProxyClient(refreshing_cdb(NEW_NET_KEY, 1), state)
    link = FakeBleak(refreshing_cdb(NEW_NET_KEY, 1))  # its `nk` is the export's new key
    link.notify_cb = proxy._on_notify
    link.send_beacon(iv_index=0, key_refresh=True)
    assert proxy.key_refresh_phase == 2
    assert not link.writes


def test_a_stored_key_refresh_meets_the_exports_own(cdb: CDB) -> None:
    """The refresh the client stored (`LocalState.key_refresh`) against the one the export was written in: the
    stored one is later, except that a phase never goes backwards for the same new key. Review 4: an unproven
    stored key no longer replaces the export's refresh — it joins it."""
    old = cdb.net_keys[0]
    state = LocalState(None, OUR_SRC)
    state.set_key_refresh(
        refresh_record(NEW_NET_KEY, 2, PROOF_BEACON)
    )  # followed further than the export was written
    proxy = ProxyClient(refreshing_cdb(NEW_NET_KEY, 1), state)
    assert (proxy.key_refresh_phase, proxy.nk.key) == (2, NEW_NET_KEY)
    state.set_key_refresh(
        refresh_record(NEW_NET_KEY, 1)
    )  # a record older than the export
    proxy = ProxyClient(refreshing_cdb(NEW_NET_KEY, 2), state)
    assert (proxy.key_refresh_phase, proxy.nk.key) == (2, NEW_NET_KEY)
    state.set_key_refresh(
        refresh_record(NEW_NET_KEY, 3, PROOF_BEACON)
    )  # completed since
    proxy = ProxyClient(refreshing_cdb(NEW_NET_KEY, 1), state)
    assert proxy.key_refresh_phase == 0
    assert [k.key for k in proxy.rx_net_keys] == [NEW_NET_KEY]
    state.set_key_refresh(
        refresh_record(OTHER_NET_KEY, 1)
    )  # a key the export does not know: one more candidate
    proxy = ProxyClient(refreshing_cdb(NEW_NET_KEY, 2), state)
    assert (proxy.key_refresh_phase, proxy.nk.key) == (2, NEW_NET_KEY)
    assert [k.key for k in proxy.rx_net_keys] == [old.key, NEW_NET_KEY, OTHER_NET_KEY]
    state.set_key_refresh(refresh_record(OTHER_NET_KEY, 2, PROOF_PROXY))  # proven later
    proxy = ProxyClient(refreshing_cdb(NEW_NET_KEY, 1), state)
    assert (proxy.key_refresh_phase, proxy.nk.key) == (2, OTHER_NET_KEY)


def test_access_message_repr_never_shows_a_decrypted_key():
    # the sniffer decrypts device-key traffic: an AppKey Add carries the AppKey in the clear, and neither a `%r`
    # nor `str()` of the message may print it
    app_key = bytes(range(0xA0, 0xB0))
    access = C.appkey_add(app_key)
    msg = AccessMessage(
        0x0001, 0x0100, 5, 7, C.CONFIG_APPKEY_ADD, None, access[1:], access, "dev:0100"
    )
    for text in (repr(msg), str(msg)):
        assert app_key.hex() not in text.lower()
        assert (
            repr(app_key)[2:-1] not in text
        )  # the dataclass default printed the bytes' own repr
    assert "src=1" in repr(msg)
    assert "key='dev:0100'" in repr(msg)


# ============================================================================= own-source detection (review-4 S I2)


def _from_us(link: FakeBleak, seq: int, iv_index: int | None = None) -> None:
    """Deliver an access message from our own address, as a relay echoes ours or another client sends one."""
    link.send_access(OUR_SRC, GROUP_WC, ONOFF_STATUS_ON, seq=seq, iv_index=iv_index)


async def test_a_number_we_never_sent_from_our_address_is_reported_once_per_interval(
    attached: ProxyClient,
    link: FakeBleak,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    clock = [1000.0]
    monkeypatch.setattr(client_mod, "_now", lambda: clock[0])
    seen: list[tuple[int, int]] = []
    attached.on_foreign_own_source = lambda iv, seq: seen.append((iv, seq))
    assert attached.state.seq == 1  # the filter request took number 0

    _from_us(link, 0)  # our own filter request's number, echoed: ignored as always
    assert (seen, attached.foreign_own_source) == ([], None)

    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        _from_us(link, 500)  # a number we never handed out: another client
    assert seen == [(0, 500)]
    assert attached.foreign_own_source == (0, 500)
    assert "another client uses this address" in caplog.text
    _from_us(link, 600)
    _from_us(link, 550)
    assert seen == [(0, 500)]  # within the interval: noted, not reported again
    assert attached.foreign_own_source == (0, 600)
    clock[0] += client_mod.FOREIGN_SOURCE_REPORT_INTERVAL
    _from_us(link, 560)
    assert seen == [(0, 500), (0, 600)]  # the highest seen on this link
    # never delivered, never in the replay list: the PDUs are dropped as our echoes were
    assert recorder.messages == []
    assert OUR_SRC not in attached.state.rpl
    assert attached.state.seq == 1  # nothing was sent or skipped by the library

    # a new link starts over: its first sighting is reported at once
    await attached.detach()
    await attached.attach(link)
    assert attached.foreign_own_source is None
    _from_us(link, 700)
    assert seen[-1] == (0, 700)


async def test_a_failing_or_missing_own_source_handler_only_logs(
    attached: ProxyClient, link: FakeBleak, caplog: pytest.LogCaptureFixture
):
    def boom(_iv: int, _seq: int) -> None:
        raise RuntimeError("handler bug")

    _from_us(link, 300)  # no handler: the warning alone
    assert attached.foreign_own_source == (0, 300)
    await attached.detach()
    await attached.attach(link)
    attached.on_foreign_own_source = boom
    with caplog.at_level(logging.ERROR, logger="jhmesh"):
        _from_us(link, 400)
    assert "on_foreign_own_source handler failed" in caplog.text


async def test_our_own_pdus_around_an_iv_update_are_echoes(
    attached: ProxyClient, link: FakeBleak
):
    """Echoes under the index we transmitted before an IV Update completed stay ignored; numbers above them, or under
    the new index while we still transmit under the old one, prove another client."""
    seen: list[tuple[int, int]] = []
    attached.on_foreign_own_source = lambda iv, seq: seen.append((iv, seq))
    state = attached.state
    # IV Update in progress: the mesh is at 1, we (and every node) still transmit under 0
    state.iv_index, state.iv_update_active, state.seq = 1, True, 40
    link.iv_index = 1
    _from_us(link, 39, iv_index=0)  # ours
    assert seen == []
    _from_us(link, 0, iv_index=1)  # an index we never transmitted under
    assert seen == [(1, 0)]

    # the update completed: we restarted at 0 under 1, the 40 numbers sent under 0 are the peak
    await attached.detach()
    await attached.attach(link)
    seen.clear()
    state.iv_update_active, state.seq_peak, state.seq_peak_from = False, 41, 0
    state.seq = 5
    _from_us(
        link, 40, iv_index=0
    )  # our last PDU under the old index, echoed after the switch
    _from_us(link, 4, iv_index=1)
    assert seen == []
    _from_us(link, 41, iv_index=0)  # above everything we sent under 0
    assert seen == [(0, 41)]


def test_handed_out_follows_the_counter_peak_and_guard(cdb: CDB):
    state = LocalState(None, OUR_SRC)
    proxy = ProxyClient(cdb, state)
    state.iv_index, state.seq, state.seq_peak, state.seq_peak_from = 5, 100, 300, 4
    assert proxy._handed_out(5, 99)
    assert not proxy._handed_out(5, 100)
    # an older index: below the counter and the peak, from the peak's index on; nothing known before it
    assert proxy._handed_out(4, 299)
    assert not proxy._handed_out(4, 300)
    assert proxy._handed_out(3, 0xFFFFFF)
    state.seq = 400  # the counter carried on under a guard: above the peak
    assert proxy._handed_out(4, 399)
    # a newer index: never ours, unless the guard says the counter carried on under it
    state.iv_index, state.iv_update_active = 6, True  # transmitting under 5
    assert not proxy._handed_out(6, 0)
    state.seq_guard = 5
    assert not proxy._handed_out(6, 0)
    state.seq_guard = 6
    assert proxy._handed_out(6, 399)
    assert not proxy._handed_out(6, 400)
    state.seq_guard = client_mod.SEQ_GUARD_FIRST_BEACON
    assert proxy._handed_out(6, 0)


# ============================================================================= proxy configuration checks (P4-7)


def _proxy_config(
    link: FakeBleak,
    *,
    ctl: bool = True,
    dst: int = 0x0000,
    seq: int | None = None,
    src: int = PROXY_NODE,
) -> bytes:
    return network_encrypt(
        link.nk,
        link.iv_index,
        ctl,
        0,
        link.next_seq() if seq is None else seq,
        src,
        dst,
        h("03010000"),
        proxy=True,
    )


async def test_proxy_configuration_needs_its_header_and_is_replay_protected(
    attached: ProxyClient, link: FakeBleak
):
    statuses: list[int] = []
    attached.on_filter_status = statuses.append
    attached.proxy_addr = None
    assert attached.rx_proxy_config_dropped == 0
    link.deliver(PROXY_CONFIG, _proxy_config(link, ctl=False))  # CTL=0
    link.deliver(PROXY_CONFIG, _proxy_config(link, dst=LIGHT_2G))  # DST not unassigned
    link.deliver(PROXY_CONFIG, h("00") * 20)  # not ours at all
    assert (attached.proxy_addr, statuses) == (None, [])
    assert attached.rx_proxy_config_dropped == 3

    recorded = _proxy_config(link, src=LIGHT_2G)
    link.deliver(PROXY_CONFIG, recorded)
    assert (attached.proxy_addr, statuses) == (LIGHT_2G, [LIGHT_2G])
    # played back, or an older one: dropped, the proxy node stays
    attached.proxy_addr = None
    attached._filter_acked.clear()
    link.deliver(PROXY_CONFIG, recorded)
    link.deliver(PROXY_CONFIG, _proxy_config(link, seq=5))
    assert attached.proxy_addr is None
    assert not attached._filter_acked.is_set()
    assert statuses == [LIGHT_2G]
    assert attached.rx_proxy_config_dropped == 5
    link.deliver(PROXY_CONFIG, _proxy_config(link))  # a newer one
    assert attached.proxy_addr == PROXY_NODE

    # the next link starts over: its proxy's numbers have nothing to do with this one's
    await attached.detach()
    link.seq = 0x100
    await attached.attach(link)
    assert attached.proxy_addr == PROXY_NODE
    assert attached.rx_proxy_config_dropped == 0


# ============================================================================= review-4 D32


async def test_a_status_that_does_not_show_a_lost_set_answers_the_get_out_with_it(
    attached: ProxyClient, link: FakeBleak
):
    """D32: a Set lost on the air while a Get to the same element is out. The Get's Status shows the old state at
    rest; it answers the Get, not the older Set, which keeps waiting — and takes the Status that shows its state."""
    switch_on = asyncio.create_task(
        attached.request(
            PROXY_NODE,
            M.generic_onoff_set(True, transition=0),
            M.GEN_ONOFF_STATUS,
            timeout=1.0,
            retries=1,
        )
    )
    await settle()
    get = asyncio.create_task(
        attached.request(
            PROXY_NODE, M.generic_onoff_get(), M.GEN_ONOFF_STATUS, timeout=1.0
        )
    )
    await settle()
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_OFF)  # the Get's answer
    assert (await get).params == b"\x00"
    assert not switch_on.done()
    link.send_access(
        PROXY_NODE, ELEMENT_GROUP_148, ONOFF_STATUS_ON
    )  # the Set's publication
    assert (await switch_on).params == b"\x01"


async def test_a_lost_set_passed_over_is_sent_again(
    attached: ProxyClient, link: FakeBleak
):
    """D32: the Set whose wait the Get's Status no longer ends goes out again (same PDU, same TID) within its
    attempts, and the load's answer to that confirms it."""
    access = M.generic_onoff_set(True, transition=0)
    asked: list[bytes] = []

    def answer(n: NetworkPDU) -> None:
        if n.dst == PROXY_NODE and not n.ctl:
            asked.append(n.transport_pdu)
            if len(asked) == 2:  # the Get; the Set before it was lost
                link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_OFF)
            if len(asked) == 3:  # the Set again
                link.send_access(PROXY_NODE, ELEMENT_GROUP_148, ONOFF_STATUS_ON)

    link.responders.append(answer)
    switch_on = asyncio.create_task(
        attached.request(PROXY_NODE, access, M.GEN_ONOFF_STATUS, timeout=0.05)
    )
    await settle()
    get = await attached.request(PROXY_NODE, M.generic_onoff_get(), M.GEN_ONOFF_STATUS)
    assert get.params == b"\x00"
    assert (await switch_on).params == b"\x01"
    assert [m[4] for m in link.sent_access()] == [
        access,
        M.generic_onoff_get(),
        access,
    ]


@pytest.mark.parametrize(
    ("access", "get_pdu", "status", "shown"),
    [
        # a transition under way: the target is the requested one
        (
            M.light_lightness_set(0x8000),
            M.light_lightness_get(),
            M.LIGHT_LIGHTNESS_STATUS,
            h("00100080" + "05"),
        ),
        # the load's own step: within one percent of the requested lightness
        (
            M.light_lightness_set(0x8000),
            M.light_lightness_get(),
            M.LIGHT_LIGHTNESS_STATUS,
            h("2882"),
        ),
        (
            M.generic_level_set(-0x1000),
            M.generic_level_get(),
            M.GEN_LEVEL_STATUS,
            h("00f0"),
        ),
        (
            M.light_ctl_set(0x8000, 3000),
            M.light_ctl_get(),
            M.LIGHT_CTL_STATUS,
            h("0080b80b"),
        ),
        # the CTL Temperature Status's delta UV is the light's own, not compared
        (
            M.light_ctl_temperature_set(4000),
            M.light_ctl_temperature_get(),
            M.LIGHT_CTL_TEMP_STATUS,
            h("a00f0500"),
        ),
        # a Set with a transition time (review-4 F4-1): the light still at its old level, fading to the target
        (
            M.light_lightness_set(0x8000, transition=M.encode_transition(3)),
            M.light_lightness_get(),
            M.LIGHT_LIGHTNESS_STATUS,
            h("0010" + "0080" + "1e"),
        ),
        (
            M.light_ctl_set(0x8000, 3000, transition=M.encode_transition(3)),
            M.light_ctl_get(),
            M.LIGHT_CTL_STATUS,
            h("0010a00f" + "0080b80b" + "1e"),
        ),
    ],
    ids=[
        "target",
        "step",
        "level",
        "ctl",
        "ctl-temperature",
        "lightness-transition",
        "ctl-transition",
    ],  # a Set's TID is random
)
async def test_a_status_that_shows_a_sets_state_answers_the_older_set(
    attached: ProxyClient,
    link: FakeBleak,
    access: bytes,
    get_pdu: bytes,
    status: int,
    shown: bytes,
):
    """D32: a Status that shows the requested state (present, or target while a transition runs) answers the
    oldest Set waiting, as before; the Get out to the element waits for the next one."""
    opcode = encode_opcode(status)
    put = asyncio.create_task(
        attached.request(PROXY_NODE, access, status, timeout=1.0, retries=1)
    )
    await settle()
    get = asyncio.create_task(
        attached.request(PROXY_NODE, get_pdu, status, timeout=1.0, retries=1)
    )
    await settle()
    link.send_access(PROXY_NODE, ELEMENT_GROUP_148, opcode + shown)
    assert (await put).params == shown
    assert not get.done()
    link.send_access(PROXY_NODE, OUR_SRC, opcode + shown)
    assert (await get).params == shown


async def test_a_status_no_other_request_takes_still_answers_the_set(
    attached: ProxyClient, link: FakeBleak
):
    """D32: a load that clamps the value (a lightness under its range minimum) answers with a state the Set did not
    ask for; with nothing else waiting for it, that Status still confirms the Set, and of two Sets the older."""
    first = asyncio.create_task(
        attached.request(
            PROXY_NODE, M.light_lightness_set(1), M.LIGHT_LIGHTNESS_STATUS, timeout=1.0
        )
    )
    await settle()
    second = asyncio.create_task(
        attached.request(
            PROXY_NODE, M.light_lightness_set(2), M.LIGHT_LIGHTNESS_STATUS, timeout=1.0
        )
    )
    await settle()
    clamped = encode_opcode(M.LIGHT_LIGHTNESS_STATUS) + h("000d")
    link.send_access(PROXY_NODE, ELEMENT_GROUP_148, clamped)
    assert (await first).params == h("000d")
    assert not second.done()
    link.send_access(PROXY_NODE, OUR_SRC, clamped)
    assert (await second).params == h("000d")
