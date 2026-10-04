"""jhmesh.stats and the trace logger (review-4 A4-14): what a link carried and dropped, counted; traffic logged apart."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import fields
from typing import TYPE_CHECKING

import pytest

from jhmesh import messages as M
from jhmesh.crypto import NetKeyMaterial
from jhmesh.pdu import PROXY_CONFIG, PROXY_NETWORK_PDU, network_encrypt
from jhmesh.stats import LinkStats

from .conftest import (
    GROUP_WC,
    OUR_SRC,
    PROXY_NODE,
    FakeBleak,
    FastAsyncio,
    Recorder,
)

if TYPE_CHECKING:
    from jhmesh.cdb import CDB
    from jhmesh.client import ProxyClient

h = bytes.fromhex
ONOFF_STATUS_ON = h("820401")
HEARTBEAT = bytes(
    [0x0A, 5, 0x00, 0x03]
)  # Heartbeat control PDU: InitTTL 5, relay and proxy features


def _filter_status(link: FakeBleak, seq: int) -> bytes:
    """A Filter Status from the proxy with sequence number `seq` (a replay when `seq` was taken already)."""
    return network_encrypt(
        link.nk,
        link.iv_index,
        True,
        0,
        seq,
        PROXY_NODE,
        0x0000,
        h("03010000"),
        proxy=True,
    )


def test_link_stats_add_field_by_field() -> None:
    names = [f.name for f in fields(LinkStats)]
    one = LinkStats(**{n: i for i, n in enumerate(names)})
    two = LinkStats(**dict.fromkeys(names, 10))
    assert one + two == LinkStats(**{n: i + 10 for i, n in enumerate(names)})
    assert LinkStats() + LinkStats() == LinkStats()


async def test_received_traffic_moves_its_counters(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
) -> None:
    """Each kind of PDU the proxy forwards moves its own counter, and only that one (with `rx` for network PDUs)."""
    start = attached.link_stats
    assert (start.tx, start.rx) == (
        1,
        0,
    )  # the filter request; its Filter Status is no network PDU

    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON, seq=100)  # to us
    link.send_access(PROXY_NODE, GROUP_WC, ONOFF_STATUS_ON, seq=101)  # to a group
    stats = attached.link_stats
    assert (stats.rx, stats.messages, stats.messages_to_us) == (2, 2, 1)

    link.send_access(
        PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON, seq=101
    )  # unsegmented, a replay
    seq = link.next_seq()
    heartbeat = network_encrypt(
        link.nk, 0, True, 3, seq, PROXY_NODE, OUR_SRC, HEARTBEAT
    )
    link.deliver(PROXY_NETWORK_PDU, heartbeat)
    link.deliver(PROXY_NETWORK_PDU, heartbeat)  # control, a replay
    # segmented, under numbers below the last one taken from the node: both segments are replays
    _, pdus = link.access_pdus(PROXY_NODE, OUR_SRC, bytes(20), seq=50)
    for pdu in pdus:
        link.deliver(PROXY_NETWORK_PDU, pdu)
    stats = attached.link_stats
    assert (stats.rx, stats.replays_dropped, stats.messages) == (7, 4, 2)
    assert len(recorder.messages) == 2

    foreign = NetKeyMaterial.derive(bytes(16))
    link.deliver(
        PROXY_NETWORK_PDU,
        network_encrypt(
            foreign, 0, False, 3, 1, PROXY_NODE, OUR_SRC, h("660102030405")
        ),
    )
    link.send_beacon(valid=False)
    link.send_beacon()  # authenticated: not counted
    link.deliver(
        PROXY_CONFIG, _filter_status(link, 1)
    )  # below the one attach took: a replay
    link.deliver(PROXY_CONFIG, h("00") * 20)  # not ours
    assert link.notify_cb is not None
    link.notify_cb(None, bytearray(b"\x40" + bytes(100)))
    link.notify_cb(None, bytearray(b"\x80" + bytes(100)))  # outgrew the proxy PDU limit
    stats = attached.link_stats
    assert stats == LinkStats(
        tx=1,
        rx=8,
        messages=2,
        messages_to_us=1,
        undecryptable=1,
        beacons_unauthenticated=1,
        garbage=1,
        replays_dropped=4,
        proxy_config_dropped=2,
        proxy_config_replays=1,
    )
    # the compatible names read the same counts
    assert (
        attached.rx_undecryptable,
        attached.rx_garbage,
        attached.rx_proxy_config_dropped,
    ) == (1, 1, 2)


async def test_sent_traffic_moves_its_counters(
    attached: ProxyClient, link: FakeBleak, fast: FastAsyncio
) -> None:
    """`tx` counts every PDU written, `segment_retransmissions` the unicast segments sent again, `request_timeouts`
    every unanswered attempt; a group's second round is no retransmission."""
    await attached.send_access(PROXY_NODE, M.generic_onoff_get())
    assert attached.link_stats.tx == 2

    link.auto_ack(drop_once={1})
    await attached.send_access(
        PROXY_NODE, bytes(20)
    )  # two segments, the second lost once
    for _ in range(3):  # the acks in flight
        await asyncio.sleep(0)
    assert (attached.link_stats.tx, attached.link_stats.segment_retransmissions) == (
        5,
        1,
    )

    await attached.send_access(GROUP_WC, bytes(20))  # two segments, twice
    assert (attached.link_stats.tx, attached.link_stats.segment_retransmissions) == (
        9,
        1,
    )

    with pytest.raises(TimeoutError):
        await attached.request(
            PROXY_NODE,
            M.generic_onoff_get(),
            M.GEN_ONOFF_STATUS,
            timeout=0.01,
            retries=2,
        )
    assert (attached.link_stats.tx, attached.link_stats.request_timeouts) == (11, 2)


async def test_link_stats_start_over_per_link_and_the_totals_keep_them(
    proxy: ProxyClient, link: FakeBleak, cdb: CDB
) -> None:
    assert proxy.link_stats == proxy.total_stats == LinkStats()
    await proxy.attach(link)
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)
    assert link.notify_cb is not None
    link.notify_cb(
        None, bytearray(b"\x00" + bytes(200))
    )  # garbage: the reassembly's count goes with the link
    first = proxy.link_stats
    assert (first.tx, first.rx, first.messages, first.garbage) == (1, 1, 1, 1)
    await proxy.detach()
    assert proxy.link_stats == first  # the last link's, while none is up

    await proxy.attach(FakeBleak(cdb))
    assert proxy.link_stats == LinkStats(tx=1)  # this link's filter request
    assert proxy.total_stats == first + LinkStats(tx=1)
    # a copy: changing it changes nothing
    proxy.link_stats.tx = 99
    assert proxy.link_stats.tx == 1


async def test_only_the_trace_logger_logs_the_traffic(
    attached: ProxyClient, link: FakeBleak, cdb: CDB, caplog: pytest.LogCaptureFixture
) -> None:
    """`jhmesh.trace` at DEBUG with `jhmesh` at WARNING: every PDU is logged, nothing of the link's lifecycle."""
    with (
        caplog.at_level(logging.WARNING, logger="jhmesh"),
        caplog.at_level(logging.DEBUG, logger="jhmesh.trace"),
    ):
        await attached.detach()
        await attached.attach(FakeBleak(cdb))
        await attached.send_access(PROXY_NODE, M.generic_onoff_get())
        attached.client.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)
    traffic = [r for r in caplog.records if r.name.startswith("jhmesh")]
    assert {r.name for r in traffic} == {"jhmesh.trace"}
    messages = [r.getMessage() for r in traffic]
    assert any(m.startswith(f"TX {OUR_SRC:04X}→{PROXY_NODE:04X}") for m in messages)
    assert any(m.startswith(f"RX {PROXY_NODE:04X}→{OUR_SRC:04X}") for m in messages)
    assert not any("attached to proxy" in m for m in messages)


async def test_the_trace_logger_can_be_left_out_of_a_debug_log(
    attached: ProxyClient, link: FakeBleak, cdb: CDB, caplog: pytest.LogCaptureFixture
) -> None:
    """`jhmesh` at DEBUG with `jhmesh.trace` at INFO: the link's lifecycle without a line per PDU."""
    with (
        caplog.at_level(logging.DEBUG, logger="jhmesh"),
        caplog.at_level(logging.INFO, logger="jhmesh.trace"),
    ):
        caplog.handler.setLevel(logging.DEBUG)
        await attached.detach()
        await attached.attach(FakeBleak(cdb))
        attached.client.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)
    messages = [r.getMessage() for r in caplog.records if r.name.startswith("jhmesh")]
    assert any(m.startswith("attached to proxy") for m in messages)
    assert not any(m.startswith(("TX ", "RX ")) for m in messages)
