"""The translated error a command reports when the mesh did not carry it.

Every command an entity sends can end in the same transport errors: the node did not answer (`TimeoutError`) or the
link could not carry the message (`ConnectionError` / `OSError`). `mesh_errors` turns them into the translated
`HomeAssistantError` an action shows, chained to the original. `TimeoutError` is an `OSError`, so it is told apart
first; a site with no timeout text of its own reports it as `send_failed` like the other transport errors.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN

type Placeholders = Mapping[str, str] | Callable[[], Mapping[str, str]]


@contextmanager
def mesh_errors(
    *, timeout_key: str | None = None, placeholders: Placeholders | None = None
) -> Iterator[None]:
    """Map a transport error of the block to its translated `HomeAssistantError`; anything else passes unchanged.

    `TimeoutError` raises `timeout_key` with `placeholders` when one is given, else `send_failed`; `ConnectionError`
    and every other `OSError` raise `send_failed`. `placeholders` may be a callable, read only when the error is
    raised (an entity's `entity_id` as it is then).
    """
    try:
        yield
    except TimeoutError as err:
        if timeout_key is None:
            raise _send_failed() from err
        values = placeholders() if callable(placeholders) else placeholders
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=timeout_key,
            translation_placeholders=dict(values) if values is not None else None,
        ) from err
    except (ConnectionError, OSError) as err:
        raise _send_failed() from err


def _send_failed() -> HomeAssistantError:
    return HomeAssistantError(translation_domain=DOMAIN, translation_key="send_failed")
