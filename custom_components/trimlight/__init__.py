"""Trimlight Edge Home Assistant Integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TrimlightApi
from .const import CONF_CLIENT_ID, CONF_CLIENT_SECRET
from .coordinator import TrimlightCoordinator

PLATFORMS: list[Platform] = [Platform.LIGHT]

type TrimlightConfigEntry = ConfigEntry[TrimlightCoordinator]


async def async_setup_entry(
    hass: HomeAssistant, entry: TrimlightConfigEntry
) -> bool:
    """Set up Trimlight Edge from a config entry."""
    session = async_get_clientsession(hass)
    api = TrimlightApi(
        entry.data[CONF_CLIENT_ID],
        entry.data[CONF_CLIENT_SECRET],
        session,
    )

    coordinator = TrimlightCoordinator(hass, entry, api)
    # Verifies connectivity (Bronze quality rule): raises
    # ConfigEntryNotReady itself if the first refresh fails.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: TrimlightConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
