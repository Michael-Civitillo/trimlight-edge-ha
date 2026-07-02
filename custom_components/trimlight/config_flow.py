"""Config flow for Trimlight Edge."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import TrimlightApi, TrimlightApiError, TrimlightAuthError
from .const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, DOMAIN

_SECRET_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLIENT_ID): str,
        vol.Required(CONF_CLIENT_SECRET): _SECRET_SELECTOR,
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLIENT_SECRET): _SECRET_SELECTOR,
    }
)


class TrimlightConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Trimlight Edge."""

    VERSION = 1

    async def _async_validate(
        self, client_id: str, client_secret: str
    ) -> str | None:
        """Try the credentials against the API; return an error key or None."""
        api = TrimlightApi(
            client_id, client_secret, async_get_clientsession(self.hass)
        )
        try:
            await api.get_devices()
        except TrimlightAuthError:
            return "invalid_auth"
        except TrimlightApiError:
            return "cannot_connect"
        except Exception:
            return "unknown"
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step: collect clientId and clientSecret."""
        errors: dict[str, str] = {}

        if user_input is not None:
            error = await self._async_validate(
                user_input[CONF_CLIENT_ID], user_input[CONF_CLIENT_SECRET]
            )
            if error is not None:
                errors["base"] = error
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

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth when the API rejects the stored credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect a new client secret and revalidate."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if entry is None:
            return self.async_abort(reason="reauth_failed")

        if user_input is not None:
            error = await self._async_validate(
                entry.data[CONF_CLIENT_ID], user_input[CONF_CLIENT_SECRET]
            )
            if error is not None:
                errors["base"] = error
            else:
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, **user_input}
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={
                CONF_CLIENT_ID: entry.data[CONF_CLIENT_ID]
            },
            errors=errors,
        )
