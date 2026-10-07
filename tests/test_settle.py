"""`settle`, the suite's wait for "nothing left to happen": it waits for the hub's own work, and fails on a busy loop.

An idle loop is not the end of the hub's work: a request that times out in milliseconds (`fast_requests`) leaves the
loop idle between its attempts while the connect-time refresh goes on, and a test's next lines then race it. A loop
that never goes idle (a loop over `asyncio.sleep`, which `fast_sleep` makes instant) used to run into a silent turn
cap and only cost time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from typing import TYPE_CHECKING

import pytest

from custom_components.junghome_ble.jhmesh.client import ProxyClient

from . import conftest
from .conftest import FakeProxyLink, settle

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

FAST_TIMEOUT = 0.01  # real seconds a request waits per attempt here


@pytest.fixture
def fast_requests() -> Generator[None]:
    """Every request that names no timeout gives up after milliseconds: the refresh goes on in small real steps."""
    timeout, *rest = ProxyClient.request.__defaults__ or ()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ProxyClient.request, "__defaults__", (FAST_TIMEOUT, *rest))
        yield
    assert ProxyClient.request.__defaults__ == (timeout, *rest)


def stall(seconds: float) -> None:
    """Hold the loop without yielding to it, as a busy machine or a cold import does."""
    until = conftest._REAL_CLOCK[0]() + seconds
    while conftest._REAL_CLOCK[0]() < until:
        pass


async def test_the_connect_time_refresh_is_through_after_a_settle(
    hass: HomeAssistant,
    fast_requests: None,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Nothing the hub does by itself is left once the setup settled, however long the test then takes."""
    task = init_integration.runtime_data.lifecycle.task("refresh")
    assert task is None or task.done()
    sent = len(fake_link.sent)
    stall(0.1)
    await settle(hass)
    assert fake_link.sent[sent:] == []


async def test_a_frozen_clock_does_not_hold_settle(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    fast_requests: None,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """With the loop's clock frozen the refresh's timers wait for the test: settle comes back at once."""
    await hass.config_entries.async_reload(init_integration.entry_id)
    started = conftest._REAL_CLOCK[0]()
    await settle(hass)
    assert conftest._REAL_CLOCK[0]() - started < 1.0


async def _spin() -> None:
    """A task that never waits for anything: what the loop's turn cap is there to catch."""
    while True:  # noqa: ASYNC110  # busy on purpose
        await asyncio.sleep(0)


async def test_a_loop_that_never_goes_idle_fails_the_test(hass: HomeAssistant) -> None:
    spinner = hass.loop.create_task(_spin())
    try:
        with pytest.raises(pytest.fail.Exception, match="still busy"):
            await settle(hass)
    finally:
        spinner.cancel()


@pytest.mark.busy_ok
async def test_busy_ok_lets_a_busy_loop_through(hass: HomeAssistant) -> None:
    spinner = hass.loop.create_task(_spin())
    try:
        await settle(hass)
    finally:
        spinner.cancel()
