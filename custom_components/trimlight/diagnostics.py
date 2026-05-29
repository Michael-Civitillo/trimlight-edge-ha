"""Diagnostics support for Trimlight Edge."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, DOMAIN

# Keys that must never appear in diagnostics output.
#
# - CONF_CLIENT_SECRET: the HMAC private key. Catastrophic if leaked.
# - CONF_CLIENT_ID: the account identifier; also an HMAC input and an
#   account-level identifier, so we redact it for defense in depth.
# - deviceId / device_id / mac / macAddress: per-controller identifiers
#   that — combined with leaked credentials — allow an attacker to
#   target a specific physical controller. Not credentials themselves
#   but worth redacting in the same blast radius.
TO_REDACT = {
    CONF_CLIENT_SECRET,
    CONF_CLIENT_ID,
    "deviceId",
    "device_id",
    "mac",
    "macAddress",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry.

    Called by Home Assistant when the user clicks "Download
    Diagnostics" on the integration's settings page. Without a
    handler, HA falls back to dumping ``entry.data`` raw — which
    for this integration includes ``client_secret``.
    """
    coordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "coordinator_data": async_redact_data(coordinator.data, TO_REDACT),
    }
