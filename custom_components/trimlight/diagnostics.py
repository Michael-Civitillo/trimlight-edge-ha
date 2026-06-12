"""Diagnostics support for Trimlight Edge."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, DOMAIN

# Keys that must never appear in diagnostics output.
#
# - CONF_CLIENT_SECRET: the HMAC private key.
# - CONF_CLIENT_ID: the account identifier; also an HMAC input, so it
#   is redacted for defense in depth.
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

    Adds the "Download Diagnostics" option to the integration's
    settings page (Home Assistant only offers the download when a
    handler exists). Account credentials and device identifiers are
    redacted so the resulting JSON is safe to attach to bug reports.
    """
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # coordinator.data is keyed by deviceId, and async_redact_data only
    # redacts values — re-key the devices positionally so the device
    # IDs stay out of the output entirely.
    coordinator_data = {
        f"device_{index}": async_redact_data(device_data, TO_REDACT)
        for index, device_data in enumerate(coordinator.data.values())
    }

    return {
        "entry": {
            # entry.title is deliberately omitted: the default title
            # for this integration embeds the client_id.
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "coordinator_data": coordinator_data,
    }
