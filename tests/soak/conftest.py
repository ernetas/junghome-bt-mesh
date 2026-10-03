"""Tests on virtual time: Home Assistant, the hub and the simulated mesh on a `VirtualTimeLoop` (`tests/sim/clock.py`).

pytest-asyncio builds every test's loop (and the loop its async fixtures, `hass` among them, run on) from the
factory this directory's `pytest_asyncio_loop_factories` names, so here — and only here, the hook being this
conftest's — minutes of link flapping and reconnection back-offs take the milliseconds their Python takes. The
clock stands still while an executor job runs, and `loop.time()` stays virtual although Home Assistant's fixtures
bind `time.monotonic` to it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.sim import VirtualTimeLoop

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import pytest


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], VirtualTimeLoop]]:
    """Every test of this directory runs on virtual time."""
    return {"virtual": VirtualTimeLoop}
