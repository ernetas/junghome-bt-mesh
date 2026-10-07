"""The integration's own texts as Home Assistant cached them for the server's language, where no translation hook is.

A logbook line (`logbook.py`) and what a stopped plan applied (`configurator.store.applied_message`, a placeholder's
value) are worded in the backend: Home Assistant translates a message by its key, never a placeholder's value nor a
describer's return. Both come from the translations Home Assistant cached for the server's language when it set the
integration up, English where that language has none. So do the words of the *Mesh topology* picture
(`common.topology_*`, `cached_texts`), drawn in the backend as an SVG.
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
    key no version wrote lacks one. A text the placeholders do not fit (a logbook row an older version stored, of a
    text that gained a placeholder since) is passed over as one it lacks, as `topology_svg._say` does: a describer
    that raised would fail every such row of the logbook.
    """
    path = f"component.{DOMAIN}.{category}.{key}"
    for language in (hass.config.language, "en"):
        text = async_get_cached_translations(hass, language, category, DOMAIN)
        if path in text:
            try:
                return text[path].format_map(placeholders or {})
            except (KeyError, IndexError, ValueError):
                continue
    return None


@callback
def cached_texts(hass: HomeAssistant, category: str, prefix: str) -> dict[str, str]:
    """Return the integration's texts `<category>.<prefix>…` by the rest of their key, as written (not filled in).

    English, the server's language over it: a language whose translations are not cached yet (just chosen) reads as
    English until they are.
    """
    start = f"component.{DOMAIN}.{category}.{prefix}"
    found: dict[str, str] = {}
    for language in ("en", hass.config.language):
        text = async_get_cached_translations(hass, language, category, DOMAIN)
        found.update(
            {
                path.removeprefix(start): value
                for path, value in text.items()
                if path.startswith(start)
            }
        )
    return found
