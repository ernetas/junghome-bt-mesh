"""The HA-level fake proxy link itself: its teardown check and the sequence numbers its nodes send with."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from custom_components.junghome_ble.jhmesh.client import MESH_PROXY_DATA_IN
from custom_components.junghome_ble.jhmesh.pdu import (
    PROXY_NETWORK_PDU,
    ProxyReassembler,
    network_decrypt,
    proxy_frame,
)

from .conftest import SRC_SEQ_BASE, FakeProxyLink, check_link_teardown
from .helpers import LIGHT_CTL, LIGHT_SWITCH, onoff_status

if TYPE_CHECKING:
    from custom_components.junghome_ble.jhmesh.cdb import CDB

GROUP = 0xC061


async def test_teardown_fails_on_a_pdu_the_mesh_could_not_open(cdb: CDB) -> None:
    """One undecryptable write fails the teardown, naming its position and length but not its bytes."""
    link = FakeProxyLink(cdb)
    garbage = bytes(range(0xA0, 0xB4))  # 20 octets no NetKey opens
    for frame in proxy_frame(PROXY_NETWORK_PDU, garbage, link.mtu_size - 3):
        await link.write_gatt_char(MESH_PROXY_DATA_IN, frame)
    assert [(layer, i) for layer, i, _ in link.undecryptable] == [("network", 0)]
    with pytest.raises(pytest.fail.Exception) as err:
        check_link_teardown(link)
    assert "[(0, 'network', 20)]" in str(err.value)
    assert garbage.hex() not in str(err.value)
    assert repr(garbage) not in str(err.value)
    link.expect_undecryptable = True
    check_link_teardown(link)


def test_teardown_fails_on_a_replay(cdb: CDB) -> None:
    link = FakeProxyLink(cdb)
    link.replayed.append((0x0D00, 0, 7))
    with pytest.raises(AssertionError, match="replayed"):
        check_link_teardown(link)
    link.expect_replays = True
    check_link_teardown(link)


def test_each_source_counts_its_own_sequence_numbers(cdb: CDB) -> None:
    """Two nodes injected alternately keep independent, increasing counters, as elements do on air (§3.4.4.3)."""
    link = FakeProxyLink(cdb)
    frames: list[bytes] = []
    link._notify = lambda _char, data: frames.append(bytes(data))
    for on in (True, False, True):
        link.inject(LIGHT_SWITCH, GROUP, onoff_status(on))
        link.inject(LIGHT_CTL, GROUP, onoff_status(on))
    link.inject_heartbeat(LIGHT_SWITCH, GROUP)
    reasm = ProxyReassembler()
    seen: dict[int, list[int]] = {}
    for frame in frames:
        r = reasm.feed(frame)
        assert r is not None
        n = network_decrypt(link.nk, link.iv_index, r[1])
        assert n is not None
        seen.setdefault(n.src, []).append(n.seq)
    start = {src: SRC_SEQ_BASE + (src << 8) for src in (LIGHT_SWITCH, LIGHT_CTL)}
    assert seen == {
        LIGHT_SWITCH: [start[LIGHT_SWITCH] + i for i in (1, 2, 3, 4)],
        LIGHT_CTL: [start[LIGHT_CTL] + i for i in (1, 2, 3)],
    }
    assert link.seq == 0x100000  # the proxy's own counter is left alone
