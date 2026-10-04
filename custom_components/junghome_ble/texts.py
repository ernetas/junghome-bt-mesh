"""The integration's own texts as Home Assistant cached them for the server's language, where no translation hook is.

A logbook line (`logbook.py`) and what a stopped plan applied (`configurator.store.applied_message`, a placeholder's
value) are worded in the backend: Home Assistant translates a message by its key, never a placeholder's value nor a
describer's return. Both come from the translations Home Assistant cached for the server's language when it set the
integration up, English where that language has none.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.translation import async_get_cached_translations

from .const import DOMAIN


@callback
def cached_text(
    hass: HomeAssistant,
    category: str,
    key: str,
    placeholders: dict[str, str] | None = None,
) -> str | None:
    """Return the integration's text `<category>.<key>` in the server's language (English without), filled in.

    None when neither has it: the integration's translations are cached when Home Assistant sets it up, so only a
    key no version wrote lacks one.
    """
    path = f"component.{DOMAIN}.{category}.{key}"
    for language in (hass.config.language, "en"):
        text = async_get_cached_translations(hass, language, category, DOMAIN)
        if path in text:
            return text[path].format_map(placeholders or {})
    return None
