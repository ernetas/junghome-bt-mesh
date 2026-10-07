"""Segmentation and reassembly of access messages (Mesh Protocol 1.1 §3.5.3): the lower transport's SAR layer.

`Segmentation` is the part of the `ProxyClient` that splits an access message longer than 11 bytes into segments,
writes them round by round and waits for the receiver's Segment Acknowledgment (`_send_segmented`), and that
reassembles the segments of a message sent to us and acknowledges them (`_on_segment`, with the SAR
Acknowledgment and SAR Discard timers). It shares the client's link, sequence numbers and send lock: the client
is built on it (`class ProxyClient(Segmentation)`) and `jhmesh.client` re-exports its constants.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from .messages import describe
from .pdu import (
    NONCE_DEVICE,
    PROXY_NETWORK_PDU,
    NetworkPDU,
    SegmentInfo,
    is_unicast,
    lower_segments_access,
    network_encrypt,
    segment_ack,
    seq_auth_from,
    upper_encrypt,
)

if TYPE_CHECKING:
    from .crypto import NetKeyMaterial
    from .state import LocalState
    from .stats import LinkStats

__all__ = [
    "SAR_ACK_DELAY_INCREMENT",
    "SAR_ACK_RETRANSMISSIONS",
    "SAR_DISCARD_TIMEOUT",
    "SAR_SEGMENTS_THRESHOLD",
    "SAR_SEGMENT_INTERVAL",
    "SEGMENT_ACK_TIMEOUT",
    "SEGMENT_RESTARTS",
    "SEGMENT_RETRIES",
]

log = logging.getLogger("jhmesh")
trace = logging.getLogger("jhmesh.trace")

SEGMENT_SIZE = 12
SEGMENT_ACK_TIMEOUT = 1.5
SEGMENT_RETRIES = 4
SEGMENT_RESTARTS = (
    2  # how often a segmented message starts over because the IV index moved mid-way
)
# The SAR Receiver state (Mesh Protocol 1.1 §4.2.49) at the spec's defaults — a proxy client has no Configuration
# Server to set it: acknowledgment delay increment 2.5 (state 0b001, §4.2.49.2), one transmission of each Segment
# Acknowledgment (retransmissions count 0b00, §4.2.49.3) for messages of more than 3 segments (§4.2.49.1), a
# reassembly discarded 10 s after its last new segment (0b0001, §4.2.49.4), a segment reception interval of 60 ms
# (0b0101, §4.2.49.5). `Segmentation._on_segment` applies them (§3.5.3.4)
SAR_ACK_DELAY_INCREMENT = 2.5
SAR_ACK_RETRANSMISSIONS = 0
SAR_SEGMENTS_THRESHOLD = 3
SAR_DISCARD_TIMEOUT = 10.0
SAR_SEGMENT_INTERVAL = 0.060  # seconds


class _Lazy:
    """A log argument rendered only when the record is (`describe` ran for every send at any level)."""

    __slots__ = ("render",)

    def __init__(self, render: Callable[[], str]) -> None:
        self.render = render

    def __str__(self) -> str:
        return self.render()


@dataclass
class _AckState:
    block: int = 0
    cancelled: bool = False  # BlockAck 0: the receiver cannot take the message (§3.5.3.3), stop sending it
    event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class _TxKey:
    """The upper-transport key of an outgoing message: AppKey 0, or the device key of the node addressed.

    Everything else on the send path (sequence numbers, IV index, segmentation, acks) is the same for both;
    only the nonce type and the AKF/AID bits of the lower transport header differ (§3.5.2, §3.8.5).
    """

    key: bytes = field(repr=False)  # key material never in logs or error text
    nonce: int  # NONCE_APP | NONCE_DEVICE
    akf: bool
    aid: int
    label: str  # 'app0' | 'dev:<addr>', as in AccessMessage.key
    # the NetKey the network layer is sealed with; None = the one we transmit with when it is written (`nk`)
    net: NetKeyMaterial | None = field(default=None, repr=False)

    @property
    def devkey(self) -> bool:
        """Whether this is a device key (the message may carry key material: `describe` hides what it cannot decode)."""
        return self.nonce == NONCE_DEVICE


class Segmentation:
    """The SAR layer of a `ProxyClient`: segmented sends with their acknowledgments, reassembly with ours.

    The client holds the state (`_segments`, `_seq_auth_done`, `_ack_waiters`, `_sar_locks`) and provides the
    link: `_send_lock`, `_write`, `_spawn`, the replay check and the delivery of a reassembled message.
    """

    if TYPE_CHECKING:
        state: LocalState
        client: Any
        ttl: int
        _stats: LinkStats
        _send_lock: asyncio.Lock
        _sar_locks: dict[int, asyncio.Lock]
        _ack_waiters: dict[tuple[int, int], _AckState]
        _segments: dict[tuple[int, int], dict[str, Any]]
        _seq_auth_done: dict[int, tuple[int, int]]
        _tasks: set[asyncio.Task[None]]

        @property
        def nk(self) -> NetKeyMaterial: ...

        def _refuse_unlinked(self) -> None: ...

        async def _write(self, msg_type: int, payload: bytes) -> None: ...

        def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]: ...

        def _is_replay(self, src: int, iv: int, seq: int) -> bool: ...

        def _deliver(
            self,
            n: NetworkPDU,
            akf: bool,
            aid: int,
            upper: bytes,
            szmic: int,
            seq_auth: int,
            segmented: bool = False,
        ) -> None: ...

    # ------------------------------------------------------------------ sending
    def _sar_lock(self, dst: int) -> asyncio.Lock:
        """Return the lock that keeps segmented messages to `dst` one at a time (one SeqZero per destination)."""
        lock = self._sar_locks.get(dst)
        if lock is None:
            lock = self._sar_locks[dst] = asyncio.Lock()
        return lock

    async def _send_segmented(
        self,
        dst: int,
        access_pdu: bytes,
        ttl: int,
        key: _TxKey,
        on_locked: Callable[[], None] | None = None,
    ) -> int:
        """Send a segmented access message; returns its SeqAuth (the first sequence number).

        Every retransmission round re-reads the transmit IV index: when an IV Update completes in between, the
        upper transport PDU (encrypted under the old index and SeqAuth) can no longer be verified by anyone, so
        the message starts over under the new index with a fresh SeqAuth.
        """
        n_segments = -(
            -(len(access_pdu) + 4) // SEGMENT_SIZE
        )  # 4-byte TransMIC (SZMIC 0)
        for attempt in range(SEGMENT_RESTARTS + 1):
            seq0 = await self._send_segments_once(
                dst,
                access_pdu,
                ttl,
                key,
                n_segments,
                on_locked if attempt == 0 else None,
            )
            if seq0 is not None:
                return seq0
            if attempt < SEGMENT_RESTARTS:
                log.info(
                    "IV index changed during the segmented message to %04X, starting it over",
                    dst,
                )
        # the index keeps moving: something is badly wrong with what we hear
        raise TimeoutError(
            f"segmented message to {dst:04X} not acknowledged (IV index kept changing)"
        )

    async def _send_segments_once(
        self,
        dst: int,
        access_pdu: bytes,
        ttl: int,
        key: _TxKey,
        n_segments: int,
        on_locked: Callable[[], None] | None = None,
    ) -> int | None:
        """One segmented transmission with its retransmission rounds; None when the IV index moved in between.

        Each round reserves and writes its segments under `_send_lock` and waits for the acknowledgement
        outside it. `ConnectionError` when the link goes away meanwhile: `_release_link` wakes the ack wait like
        an ack would, and neither a "not acknowledged" nor another round's sequence numbers would be right for
        that.
        """
        link = self.client
        # released by `_segment_rounds` once the first round is written
        await self._send_lock.acquire()
        try:
            self._refuse_unlinked()
            if on_locked is not None:
                on_locked()
            iv = self.state.tx_iv_index
            # all of the first round's numbers, or SequenceExhausted
            seq0 = self.state.reserve_seq(n_segments)
            upper = upper_encrypt(
                key.key, key.nonce, iv, seq0, self.state.src, dst, access_pdu
            )
            segments = lower_segments_access(
                key.aid, seq0, upper, SEGMENT_SIZE, akf=key.akf
            )
            if len(segments) != n_segments:  # pragma: no cover — same arithmetic twice
                raise RuntimeError(
                    "segment count differs from the reserved sequence numbers"
                )
        except BaseException:
            self._send_lock.release()
            raise
        trace.debug(
            "TX %04X→%04X seq=%06X [%s] segmented x%d %s",
            self.state.src,
            dst,
            seq0,
            key.label,
            n_segments,
            _Lazy(partial(describe, access_pdu, devkey=key.devkey)),
        )
        return await self._segment_rounds(dst, ttl, segments, link, iv, seq0, key.net)

    async def _segment_rounds(
        self,
        dst: int,
        ttl: int,
        segments: list[bytes],
        link: Any,
        iv: int,
        seq0: int,
        net: NetKeyMaterial | None = None,
    ) -> int | None:
        """Run the rounds of `_send_segments_once`; entered holding `_send_lock`, released after the first writes."""
        seq_zero = seq0 & 0x1FFF
        pending = set(range(len(segments)))
        ack_key = (dst, seq_zero)
        acked = _AckState()
        if is_unicast(dst):
            self._ack_waiters[ack_key] = acked
        try:
            for attempt in range(SEGMENT_RETRIES):
                if attempt:
                    await self._send_lock.acquire()
                try:
                    if attempt and self.state.tx_iv_index != iv:
                        return None
                    # before any number is reserved for a round
                    if self.client is not link:
                        raise ConnectionError(
                            "proxy link lost during the segmented message"
                        )
                    order = sorted(pending)
                    # one synchronous reservation: every number of this round belongs to `iv`'s sequence space,
                    # whatever beacon arrives while the writes below yield to the loop
                    if attempt == 0:
                        seqs = [seq0 + i for i in order]
                    else:
                        first = self.state.reserve_seq(len(order))
                        seqs = [first + k for k in range(len(order))]
                        # a group's second round is no retransmission: it always goes twice
                        if is_unicast(dst):
                            self._stats.segment_retransmissions += len(order)
                    acked.event.clear()  # before the writes: an ack may land while the last one is in flight
                    await self._write_segments(dst, ttl, iv, segments, order, seqs, net)
                finally:
                    self._send_lock.release()
                if not is_unicast(dst):
                    if attempt == 1:
                        return seq0  # groups: send everything twice, no acks
                    await asyncio.sleep(0.3)
                    continue
                await self._wait_for_acks(acked, pending, dst, attempt)
                if acked.cancelled:
                    raise TimeoutError(
                        f"segmented message to {dst:04X} cancelled by the receiver (BlockAck 0: busy or out of resources)"
                    )
                # whatever came in — during the writes, the wait or the grace period — counts
                pending = {i for i in pending if not (acked.block >> i) & 1}
                if not pending:
                    return seq0  # delivered, even if the link went away during the grace period
                if self.client is not link:
                    raise ConnectionError(
                        "proxy link lost during the segmented message"
                    )
        finally:
            self._ack_waiters.pop(ack_key, None)
        raise TimeoutError(f"segmented message to {dst:04X} not acknowledged")

    async def _write_segments(
        self,
        dst: int,
        ttl: int,
        iv: int,
        segments: list[bytes],
        order: list[int],
        seqs: list[int],
        net_key: NetKeyMaterial | None = None,
    ) -> None:
        """Write the segments `order` names, each with its sequence number from `seqs` (under `net_key`, else `nk`)."""
        for i, seq in zip(order, seqs, strict=True):
            net = network_encrypt(
                self.nk if net_key is None else net_key,
                iv,
                False,
                ttl,
                seq,
                self.state.src,
                dst,
                segments[i],
            )
            await self._write(PROXY_NETWORK_PDU, net)

    @staticmethod
    async def _wait_for_acks(
        acked: _AckState, pending: set[int], dst: int, attempt: int
    ) -> None:
        """Wait for a round's acknowledgement; when it leaves segments open, a short grace for late ones."""
        try:
            await asyncio.wait_for(acked.event.wait(), SEGMENT_ACK_TIMEOUT)
            if any(not (acked.block >> i) & 1 for i in pending):
                # grace: acks for later segments usually follow within ~50 ms
                await asyncio.sleep(0.25)
        except asyncio.TimeoutError:
            trace.debug("no segment ack from %04X (attempt %d)", dst, attempt + 1)

    # ------------------------------------------------------------------ receiving
    def _on_segment(self, n: NetworkPDU, s: SegmentInfo) -> None:
        """Reassemble a segmented message and acknowledge it as Mesh Protocol 1.1 §3.5.3.4 (Reassembly behavior) says.

        A reassembly is kept per (source, SeqZero). A new segment (First / Next Segment) of a message to our address
        starts the SAR Acknowledgment timer again — min(SegN + 0.5, `SAR_ACK_DELAY_INCREMENT`) segment reception
        intervals (`SAR_SEGMENT_INTERVAL`) — and when it fires the segments received so far are acknowledged
        (`_ack_timer`); the segment that completes the message stops it and acknowledges every segment at once (Last
        Segment). A segment already received (Repeated Segment) changes nothing. The SAR Discard timer: a reassembly
        with no new segment for `SAR_DISCARD_TIMEOUT` is dropped, segments and all, when the next segment arrives
        (nothing can complete it meanwhile). A segment of the message last completed from that source (Most Recent
        SeqAuth) is acknowledged again as complete — the sender did not hear our acknowledgment — at most once per
        `SAR_ACK_DELAY_INCREMENT` intervals, and is not delivered again. Only messages to our own address are
        acknowledged: a group's or another node's never.
        """
        key = (n.src, s.seq_zero)
        now = time.monotonic()
        for k in [
            k for k, v in self._segments.items() if now - v["t"] > SAR_DISCARD_TIMEOUT
        ]:
            self._drop_reassembly(k)
        if s.seg_o > s.seg_n:
            trace.debug(
                "RX %04X→%04X segment %d of %d: SegO beyond SegN, dropped",
                n.src,
                n.dst,
                s.seg_o,
                s.seg_n + 1,
            )
            return
        try:
            seq_auth = seq_auth_from(n.seq, s.seq_zero)
        except ValueError as err:
            trace.debug("RX %04X→%04X segment dropped: %s", n.src, n.dst, err)
            return
        st = self._segments.get(key)
        if st is None:
            st = self._start_reassembly(n, s, key, seq_auth, now)
        if st is None or not self._fits_reassembly(n, s, st, seq_auth):
            return
        ours = n.dst == self.state.src
        if st["done"]:
            st["t"] = now  # keeps the entry: it answers the sender's retransmissions
            if ours:  # Most Recent SeqAuth: the sender did not see our ack; repeat it
                self._ack_complete(n.src, s.seq_zero, st, now)
            return
        if s.seg_o in st["parts"]:
            return  # Repeated Segment: ignored, no timer restarts
        st["t"] = now  # the SAR Discard timer starts again with every new segment
        st["parts"][s.seg_o] = s.data
        if (
            len(st["parts"]) == s.seg_n + 1
        ):  # every index 0..SegN is present (SegO was range-checked)
            data = b"".join(st["parts"][i] for i in range(s.seg_n + 1))
            st["done"], st["parts"] = True, {}
            self._stop_ack_timer(st)
            self._seq_auth_done[n.src] = max(
                self._seq_auth_done.get(n.src, (-1, -1)),
                (n.iv_index, st["seq_auth"]),
            )
            if ours:
                self._ack_complete(n.src, s.seq_zero, st, now)
            self._deliver(
                n, s.akf, s.aid, data, s.szmic, st["seq_auth"], segmented=True
            )
        elif ours:
            self._stop_ack_timer(st)
            delay = min(s.seg_n + 0.5, SAR_ACK_DELAY_INCREMENT) * SAR_SEGMENT_INTERVAL
            st["ack_task"] = self._spawn(self._ack_timer(key, st, delay))

    def _start_reassembly(
        self,
        n: NetworkPDU,
        s: SegmentInfo,
        key: tuple[int, int],
        seq_auth: int,
        now: float,
    ) -> dict[str, Any] | None:
        """Start the reassembly a segment opens, or None when the segment is a replay (`_on_segment`).

        The replay check belongs to the *start* of a reassembly (checking the completed message would drop a
        late-completing one after acknowledging it). It is on the segment's own sequence number, as §3.8.8
        checks every PDU: a recorded segment replayed later carries its old number, below the list, while
        a retransmission carries a fresh one — checking the message's SeqAuth instead would lock a message
        out for good once anything the node sent after it (a Heartbeat, a Segment Ack) moved the list past
        that SeqAuth. A message already delivered is recognised by its SeqAuth instead (`_seq_auth_done`): an
        older one is a replay, the last one a retransmission to acknowledge again (Most Recent SeqAuth, §3.5.3.4).
        """
        done = self._seq_auth_done.get(n.src, (-1, -1))
        if self._is_replay(n.src, n.iv_index, n.seq) or (n.iv_index, seq_auth) < done:
            self._stats.replays_dropped += 1
            trace.debug(
                "replay from %04X seq %06X ignored (segment of SeqAuth %06X)",
                n.src,
                n.seq,
                seq_auth,
            )
            return None
        st: dict[str, Any] = {
            "parts": {},
            "n": s.seg_n,
            "t": now,
            "done": (n.iv_index, seq_auth) == done,
            "seq_auth": seq_auth,
            "acked_at": None,  # monotonic time of the last complete acknowledgment
            "ack_task": None,  # the SAR Acknowledgment timer running
        }
        self._segments[key] = st
        return st

    @staticmethod
    def _fits_reassembly(
        n: NetworkPDU, s: SegmentInfo, st: dict[str, Any], seq_auth: int
    ) -> bool:
        """Whether a segment belongs to the reassembly of its (source, SeqZero): same SegN, same SeqAuth."""
        if s.seg_n != st["n"]:
            # a different SegN under the same SeqZero: not the message being assembled
            trace.debug(
                "RX %04X→%04X segment %d of %d contradicts the %d segments announced, dropped",
                n.src,
                n.dst,
                s.seg_o,
                s.seg_n + 1,
                st["n"] + 1,
            )
            return False
        if seq_auth != st["seq_auth"]:
            # same SeqZero, another message (8192·k earlier): not a part of this one
            trace.debug(
                "RX %04X→%04X segment of SeqAuth %06X does not belong to reassembly %06X, dropped",
                n.src,
                n.dst,
                seq_auth,
                st["seq_auth"],
            )
            return False
        return True

    def _drop_reassembly(self, key: tuple[int, int]) -> None:
        """Discard a reassembly whose SAR Discard timer ran out, with its acknowledgment timer (§3.5.3.4)."""
        self._stop_ack_timer(self._segments.pop(key))

    def _stop_ack_timer(self, st: dict[str, Any]) -> None:
        """Stop a reassembly's SAR Acknowledgment timer, if one is running."""
        task, st["ack_task"] = st["ack_task"], None
        if task is not None:
            task.cancel()
            self._tasks.discard(task)

    async def _ack_timer(
        self, key: tuple[int, int], st: dict[str, Any], delay: float
    ) -> None:
        """Run a reassembly's SAR Acknowledgment timer: when it fires, acknowledge the segments received so far."""
        await asyncio.sleep(delay)
        # fired: a new segment starts a timer of its own rather than cancel this acknowledgment
        st["ack_task"] = None
        block = 0
        for i in st["parts"]:
            block |= 1 << i
        await self._send_ack(key[0], key[1], block, self._ack_transmissions(st["n"]))

    def _ack_complete(
        self, src: int, seq_zero: int, st: dict[str, Any], now: float
    ) -> None:
        """Acknowledge every segment of a completed message, at most once per `SAR_ACK_DELAY_INCREMENT` intervals."""
        last = st["acked_at"]
        if (
            last is not None
            and now - last < SAR_ACK_DELAY_INCREMENT * SAR_SEGMENT_INTERVAL
        ):
            return
        st["acked_at"] = now
        self._spawn(
            self._send_ack(
                src,
                seq_zero,
                (1 << (st["n"] + 1)) - 1,
                self._ack_transmissions(st["n"]),
            )
        )

    @staticmethod
    def _ack_transmissions(seg_n: int) -> int:
        """How often a Segment Acknowledgment goes: once, or 1 + the retransmissions count above the threshold (§3.5.3.4)."""
        if seg_n + 1 > SAR_SEGMENTS_THRESHOLD:
            return SAR_ACK_RETRANSMISSIONS + 1
        return 1

    async def _send_ack(
        self, dst: int, seq_zero: int, block: int, transmissions: int = 1
    ) -> None:
        """Acknowledge a segmented message; its sequence number is reserved and written under `_send_lock`.

        Taken outside the lock, the number could land in the middle of a segmented round, whose numbers are all
        reserved before its first segment is written: the ack reached the air ahead of the round's remaining
        segments with a higher number, and the nodes' replay protection dropped those. `transmissions` > 1 repeats
        it, each with a new sequence number, `SAR_SEGMENT_INTERVAL` apart (§3.5.3.4).
        """
        if not is_unicast(dst):
            return
        for sent in range(transmissions):
            if sent:
                await asyncio.sleep(SAR_SEGMENT_INTERVAL)
            try:
                async with self._send_lock:
                    self._refuse_unlinked()
                    pdu = network_encrypt(
                        self.nk,
                        self.state.tx_iv_index,
                        ctl=True,
                        ttl=self.ttl,
                        seq=self.state.next_seq(),
                        src=self.state.src,
                        dst=dst,
                        transport_pdu=segment_ack(seq_zero, block),
                    )
                    await self._write(PROXY_NETWORK_PDU, pdu)
            except (
                ConnectionError
            ):  # link gone, or the sequence space exhausted: nothing we can send
                return
