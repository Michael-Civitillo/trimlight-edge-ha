"""Config flow for Trimlight Edge."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TrimlightApi, TrimlightApiError
from .const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, DOMAIN

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLIENT_ID): str,
        vol.Required(CONF_CLIENT_SECRET): str,
    }
)


class TrimlightConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Trimlight Edge."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None):
        """Handle the initial step: collect clientId and clientSecret."""
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            api = TrimlightApi(
                user_input[CONF_CLIENT_ID],
                user_input[CONF_CLIENT_SECRET],
                session,
            )
            try:
                await api.get_devices()
            except TrimlightApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(user_input[CONF_CLIENT_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Trimlight ({user_input[CONF_CLIENT_ID]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )
