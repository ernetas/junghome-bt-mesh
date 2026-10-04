"""The segmented-message receiver acknowledges as Mesh Protocol 1.1 §3.5.3.4 says (review-4 P I-8).

The SAR Acknowledgment timer acknowledges the segments received so far, started again by every new segment; the
segment that completes a message acknowledges it whole; a segment of the message last completed is acknowledged as
complete again, at most every 150 ms; group destinations are never acknowledged; a reassembly the SAR Discard timer
ended takes its acknowledgment timer with it. The SAR Receiver state is the spec's defaults (§4.2.49).
"""

from __future__ import annotations

import asyncio

import pytest

from jhmesh import client as client_mod
from jhmesh.client import (
    SAR_ACK_DELAY_INCREMENT,
    SAR_DISCARD_TIMEOUT,
    SAR_SEGMENT_INTERVAL,
    SAR_SEGMENTS_THRESHOLD,
    ProxyClient,
)
from jhmesh.pdu import (
    PROXY_NETWORK_PDU,
    lower_segments_access,
    network_encrypt,
    upper_encrypt_app,
)

from .conftest import (
    GROUP_WC,
    LIGHT_2G,
    OUR_SRC,
    PROXY_NODE,
    FakeBleak,
    FastAsyncio,
    Recorder,
)


async def settle(turns: int = 3) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


def retransmission(
    link: FakeBleak, src: int, dst: int, access: bytes, seq0: int, index: int
) -> bytes:
    """Segment `index` of the message `seq0` started, sent again with a fresh sequence number (same SeqAuth)."""
    upper = upper_encrypt_app(link.ak, link.iv_index, seq0, src, dst, access)
    segments = lower_segments_access(link.ak.aid, seq0, upper)
    return network_encrypt(
        link.nk, link.iv_index, False, 3, link.next_seq(), src, dst, segments[index]
    )


def test_the_defaults_are_the_specs() -> None:
    """§4.2.49: delay increment 0b001 + 1.5, threshold 3, discard (1 + 1) * 5 s, interval (5 + 1) * 10 ms."""
    assert (SAR_ACK_DELAY_INCREMENT, SAR_SEGMENTS_THRESHOLD, SAR_DISCARD_TIMEOUT) == (
        2.5,
        3,
        10.0,
    )
    assert pytest.approx(0.06) == SAR_SEGMENT_INTERVAL
    assert (
        client_mod.SAR_ACK_RETRANSMISSIONS == 0
    )  # one transmission of each acknowledgment


async def test_the_timer_acknowledges_what_arrived_and_restarts_with_each_new_segment(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder, fast: FastAsyncio
) -> None:
    seq0, pdus = link.access_pdus(PROXY_NODE, OUR_SRC, bytes(20))  # 2 segments: SegN 1
    key = (PROXY_NODE, seq0 & 0x1FFF)
    link.deliver(PROXY_NETWORK_PDU, pdus[1])
    first = attached._segments[key]["ack_task"]
    assert first is not None
    link.deliver(
        PROXY_NETWORK_PDU, pdus[1]
    )  # Repeated Segment: ignored, the timer runs on
    assert attached._segments[key]["ack_task"] is first
    fast.sleeps.clear()
    await settle()
    assert fast.sleeps == [
        pytest.approx(1.5 * SAR_SEGMENT_INTERVAL)
    ]  # min(SegN + 0.5, 2.5) intervals
    assert link.sent_acks() == [(OUR_SRC, PROXY_NODE, seq0 & 0x1FFF, 0b10)]
    assert recorder.messages == []
    link.deliver(
        PROXY_NETWORK_PDU, pdus[0]
    )  # Last Segment: everything at once, delivered
    await settle()
    assert link.sent_acks()[-1] == (OUR_SRC, PROXY_NODE, seq0 & 0x1FFF, 0b11)
    assert len(recorder.messages) == 1
    assert attached._tasks == set()


async def test_a_new_segment_cancels_the_running_timer(
    attached: ProxyClient, link: FakeBleak
) -> None:
    seq0, pdus = link.access_pdus(PROXY_NODE, OUR_SRC, bytes(40))  # 4 segments
    key = (PROXY_NODE, seq0 & 0x1FFF)
    link.deliver(PROXY_NETWORK_PDU, pdus[0])
    first = attached._segments[key]["ack_task"]
    link.deliver(PROXY_NETWORK_PDU, pdus[2])
    await settle(1)
    assert first.cancelled()
    assert first not in attached._tasks
    await settle()
    assert link.sent_acks() == [
        (OUR_SRC, PROXY_NODE, seq0 & 0x1FFF, 0b0101)
    ]  # one, for both


async def test_long_messages_repeat_the_acknowledgment_as_configured(
    attached: ProxyClient,
    link: FakeBleak,
    fast: FastAsyncio,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Above the SAR Segments Threshold an acknowledgment goes 1 + retransmissions count times, an interval apart."""
    monkeypatch.setattr(client_mod, "SAR_ACK_RETRANSMISSIONS", 1)
    seq0, pdus = link.access_pdus(PROXY_NODE, OUR_SRC, bytes(40))  # 4 segments: above 3
    for p in pdus:
        link.deliver(PROXY_NETWORK_PDU, p)
    fast.sleeps.clear()
    await settle(5)
    assert link.sent_acks() == [(OUR_SRC, PROXY_NODE, seq0 & 0x1FFF, 0b1111)] * 2
    assert fast.sleeps == [SAR_SEGMENT_INTERVAL]
    # each with a sequence number of its own
    seqs = [n.seq for n in link.net_pdus if n.ctl]
    assert len(set(seqs)) == 2
    # a short message: once
    seq1, short = link.access_pdus(PROXY_NODE, OUR_SRC, bytes(20))
    for p in short:
        link.deliver(PROXY_NETWORK_PDU, p)
    await settle(5)
    assert link.sent_acks()[2:] == [(OUR_SRC, PROXY_NODE, seq1 & 0x1FFF, 0b11)]


async def test_the_last_message_is_acknowledged_again_after_its_reassembly_expired(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
) -> None:
    """Most Recent SeqAuth: the sender retransmits (fresh sequence numbers) because it missed our acknowledgment."""
    access = bytes(range(20))
    seq0, pdus = link.access_pdus(PROXY_NODE, OUR_SRC, access)
    for p in pdus:
        link.deliver(PROXY_NETWORK_PDU, p)
    await settle()
    assert len(recorder.messages) == 1
    key = (PROXY_NODE, seq0 & 0x1FFF)
    attached._segments[key]["t"] -= SAR_DISCARD_TIMEOUT + 1
    # another source's segment sweeps the expired entry away
    _seq, other = link.access_pdus(LIGHT_2G, GROUP_WC, bytes(20))
    link.deliver(PROXY_NETWORK_PDU, other[0])
    assert key not in attached._segments
    dropped = attached.link_stats.replays_dropped
    link.deliver(
        PROXY_NETWORK_PDU, retransmission(link, PROXY_NODE, OUR_SRC, access, seq0, 1)
    )
    await settle()
    assert [a for a in link.sent_acks() if a[1] == PROXY_NODE] == [
        (OUR_SRC, PROXY_NODE, seq0 & 0x1FFF, 0b11)
    ] * 2
    assert len(recorder.messages) == 1  # not delivered again
    assert attached.link_stats.replays_dropped == dropped  # nor counted as a replay


async def test_an_older_message_is_a_replay(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
) -> None:
    old_access, new_access = bytes(20), bytes(range(20))
    seq_old, old = link.access_pdus(PROXY_NODE, OUR_SRC, old_access)
    _seq_new, new = link.access_pdus(PROXY_NODE, OUR_SRC, new_access)
    for p in old + new:
        link.deliver(PROXY_NETWORK_PDU, p)
    await settle()
    assert len(recorder.messages) == 2
    for key in list(attached._segments):
        attached._segments[key]["t"] -= SAR_DISCARD_TIMEOUT + 1
    _seq, other = link.access_pdus(LIGHT_2G, GROUP_WC, bytes(20))
    link.deliver(PROXY_NETWORK_PDU, other[0])
    acks = len(link.sent_acks())
    dropped = attached.link_stats.replays_dropped
    link.deliver(
        PROXY_NETWORK_PDU,
        retransmission(link, PROXY_NODE, OUR_SRC, old_access, seq_old, 0),
    )
    await settle()
    assert attached.link_stats.replays_dropped == dropped + 1
    assert len(link.sent_acks()) == acks
    assert len(recorder.messages) == 2


async def test_group_and_other_nodes_messages_are_never_acknowledged(
    attached: ProxyClient, link: FakeBleak, recorder: Recorder
) -> None:
    for dst in (GROUP_WC, LIGHT_2G):
        _seq, pdus = link.access_pdus(PROXY_NODE, dst, bytes(40))
        link.deliver(PROXY_NETWORK_PDU, pdus[0])  # no timer either
        assert attached._tasks == set()
        for p in pdus[1:]:
            link.deliver(PROXY_NETWORK_PDU, p)
    await settle()
    assert link.sent_acks() == []
    assert [m.dst for m in recorder.messages] == [GROUP_WC, LIGHT_2G]
