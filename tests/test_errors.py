"""`errors.mesh_errors`: transport errors to translated `HomeAssistantError`s, everything else unchanged."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.junghome_ble.const import DOMAIN
from custom_components.junghome_ble.errors import mesh_errors


def _raised(error: BaseException, **kwargs: Any) -> HomeAssistantError:
    with pytest.raises(HomeAssistantError) as caught, mesh_errors(**kwargs):
        raise error
    assert caught.value.translation_domain == DOMAIN
    assert caught.value.__cause__ is error
    return caught.value


@pytest.mark.parametrize(
    "error",
    [TimeoutError(), ConnectionError("lost"), BrokenPipeError(), OSError(5, "io")],
    ids=["timeout", "connection", "connection-subclass", "os"],
)
def test_without_a_timeout_key_every_transport_error_is_send_failed(
    error: OSError,
) -> None:
    err = _raised(error)
    assert err.translation_key == "send_failed"
    assert err.translation_placeholders is None


def test_a_timeout_takes_the_timeout_key_without_placeholders() -> None:
    err = _raised(TimeoutError(), timeout_key="no_answer")
    assert err.translation_key == "no_answer"
    assert err.translation_placeholders is None


def test_a_timeout_takes_the_timeout_key_with_its_placeholders() -> None:
    err = _raised(
        TimeoutError(),
        timeout_key="device_not_reachable",
        placeholders={"entity": "light.kitchen"},
    )
    assert err.translation_key == "device_not_reachable"
    assert err.translation_placeholders == {"entity": "light.kitchen"}


def test_callable_placeholders_are_read_when_the_error_is_raised() -> None:
    names = ["light.before"]

    def rename_then_time_out() -> None:
        names.append("light.after")
        raise TimeoutError

    with (
        pytest.raises(HomeAssistantError) as caught,
        mesh_errors(
            timeout_key="device_not_reachable",
            placeholders=lambda: {"entity": names[-1]},
        ),
    ):
        rename_then_time_out()
    assert caught.value.translation_placeholders == {"entity": "light.after"}


@pytest.mark.parametrize(
    "error", [ConnectionError(), OSError()], ids=["connection", "os"]
)
def test_other_transport_errors_stay_send_failed_with_a_timeout_key(
    error: OSError,
) -> None:
    err = _raised(error, timeout_key="no_answer", placeholders={"entity": "x"})
    assert err.translation_key == "send_failed"
    assert err.translation_placeholders is None


@pytest.mark.parametrize(
    "error",
    [ValueError("bad"), HomeAssistantError("already"), KeyError("k")],
    ids=["value", "home-assistant", "key"],
)
def test_an_unrelated_exception_passes_unchanged(error: Exception) -> None:
    with pytest.raises(type(error)) as caught, mesh_errors(timeout_key="no_answer"):
        raise error
    assert caught.value is error
    assert caught.value.__cause__ is None


def test_a_block_that_succeeds_raises_nothing() -> None:
    with mesh_errors(timeout_key="no_answer"):
        done = True
    assert done
