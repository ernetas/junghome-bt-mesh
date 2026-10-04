"""The hub's lifecycle (`hub/lifecycle.py`): the registry its stop cancels through, in order, and the back-off."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from custom_components.junghome_ble.hub.lifecycle import (
    KEYED,
    TASKS,
    TIMERS,
    Backoff,
    async_cancel_task,
)

from .conftest import (
    FakeProxyLink,
    settle,
    setup_entry,
    wait_for_link,
)
from .test_coordinator import (
    answering_link as answering_link,  # noqa: PLC0414  # the fixture
)
from .test_coordinator import hub_of
from .test_coordinator import (
    no_property_reads as no_property_reads,  # noqa: PLC0414  # the autouse fixture
)
from .test_coordinator import (
    refresh_gate as refresh_gate,  # noqa: PLC0414  # the fixture
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_stop_cancels_a_running_refresh(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    answering_link: FakeProxyLink,
    refresh_gate: asyncio.Event,
) -> None:
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    hub = hub_of(mock_config_entry)
    task = hub.lifecycle.task("refresh")
    assert task is not None
    assert not task.done()
    await hub.async_stop()
    assert task.cancelled()
    assert hub.lifecycle.task("refresh") is None
    assert hub.lifecycle.task("link") is None
    await hub.link._watch_link()  # a link that is already gone: returns at once


async def test_stop_is_idempotent(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    hub = hub_of(init_integration)
    await hub.async_stop()
    assert not hub.connected
    assert not fake_link.is_connected
    assert mock_bluetooth_env["callbacks"] == []
    await hub.async_stop()


async def test_stop_disconnects_even_when_a_background_task_failed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """HAC-04: `_cancel`'s `task.cancel()` is a no-op on a task that has already finished with an exception, and
    `await task` then re-raises it — an error `async_stop` had no `try/finally` to survive, so it used to skip
    the disconnect and the counter close and leave the BLE connection (and its adapter/proxy slot) open."""
    hub = hub_of(init_integration)

    async def boom() -> None:
        raise ValueError("x")

    hub.lifecycle.set_task("heartbeats", hass.async_create_task(boom()))
    await (
        hass.async_block_till_done()
    )  # the task is done with the exception before async_stop cancels it

    await hub.async_stop()  # must not raise
    assert not fake_link.is_connected
    assert hub.lifecycle.task("heartbeats") is None


async def test_cancel_still_propagates_a_cancellation_of_its_own_task(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """HAC-04: `async_cancel_task` swallows the `CancelledError` its own `task.cancel()` produces, but must not
    swallow one that means its own caller (here, the task running it) is itself being cancelled — that would
    turn a cancelled `async_stop` into one that quietly finishes instead of stopping partway as asked."""
    release = asyncio.Event()

    async def stubborn() -> None:
        try:
            await release.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(
                0.01
            )  # still running when the wrapper below is cancelled
            raise

    task = hass.async_create_task(stubborn())
    await asyncio.sleep(0)

    wrapper = hass.async_create_task(async_cancel_task(task))
    await asyncio.sleep(0)  # let it call task.cancel() and reach `await task`
    wrapper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wrapper


async def test_stop_cancels_every_registered_handle_in_order(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Review-4 A4-13: `async_stop` cancels everything registered through the registry, in the hub's order of old.

    Every name holds a handle here: the running hub's own ones are wrapped (and still cancelled for real), the
    free ones get a stand-in. The timers go first in their fixed order, then the timers per node, the gestures
    pending, the tasks, and the key refresh's task last; nothing is left registered afterwards.
    """
    hub = hub_of(init_integration)
    lifecycle = hub.lifecycle
    order: list[str] = []

    def timer(name: str, real: Callable[[], None] | None) -> Callable[[], None]:
        def cancel() -> None:
            order.append(name)
            if real is not None:
                real()

        return cancel

    async def forever() -> None:
        await asyncio.Event().wait()

    def task(name: str, real: asyncio.Task[None] | None) -> asyncio.Task[None]:
        if real is None or real.done():
            real = hass.loop.create_task(forever())
        real.add_done_callback(lambda _task: order.append(name))
        return real

    for name in TIMERS:
        lifecycle.set_timer(name, timer(name, lifecycle.timer(name)))
    for name in KEYED:
        lifecycle.keyed(name)[0x0148] = timer(name, None)
    for name in TASKS:
        lifecycle.set_task(name, task(name, lifecycle.task(name)))
    hub.vault_refresh.task = task("vault_refresh", hub.vault_refresh.task)
    with patch.object(
        hub.gestures, "cancel_all", side_effect=lambda: order.append("gestures")
    ):
        await hub.async_stop()

    assert order == [
        "adv",
        "time",
        "energy",
        "heartbeats",
        "seq_check",
        "grace",
        "ha_stop",
        "echo",
        "offset_change",
        "seq_stall",
        "export_refresh",
        "filter_watch",
        "recheck",
        "transition_reread",
        "locating",
        "gestures",
        "link",
        "refresh",
        "energy",
        "heartbeats",
        "vault_refresh",
    ]
    assert lifecycle.pending() == []
    assert hub.vault_refresh.task is None


def test_a_doubling_backoff_grows_to_its_cap_and_starts_over() -> None:
    backoff = Backoff(2.0, 60.0)
    assert [backoff.grow() for _ in range(7)] == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    assert backoff.failures == 7
    backoff.reset()
    assert (backoff.delay, backoff.failures) == (2.0, 0)


def test_a_scheduled_backoff_repeats_its_last_delay() -> None:
    backoff = Backoff(schedule=(60.0, 300.0, 900.0))
    assert [backoff.grow() for _ in range(5)] == [60.0, 300.0, 900.0, 900.0, 900.0]
    backoff.reset()
    assert backoff.grow() == 60.0
