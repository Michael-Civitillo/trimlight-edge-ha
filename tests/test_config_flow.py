"""Tests for the Trimlight Edge config flow."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.trimlight.api import TrimlightApiError
from custom_components.trimlight.const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, DOMAIN

from .conftest import MOCK_DEVICE_LIST

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

VALID_INPUT = {CONF_CLIENT_ID: "test_id", CONF_CLIENT_SECRET: "test_secret"}


@pytest.mark.asyncio
async def test_user_step_shows_form(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


@pytest.mark.asyncio
async def test_user_step_success(hass):
    with patch(
        "custom_components.trimlight.config_flow.TrimlightApi"
    ) as mock_api_cls:
        mock_api = mock_api_cls.return_value
        mock_api.get_devices = AsyncMock(return_value=MOCK_DEVICE_LIST)

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=VALID_INPUT
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Trimlight (test_id)"
    assert result["data"] == VALID_INPUT


@pytest.mark.asyncio
async def test_user_step_cannot_connect(hass):
    with patch(
        "custom_components.trimlight.config_flow.TrimlightApi"
    ) as mock_api_cls:
        mock_api = mock_api_cls.return_value
        mock_api.get_devices = AsyncMock(side_effect=TrimlightApiError("fail"))

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=VALID_INPUT
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.asyncio
async def test_user_step_unknown_error(hass):
    with patch(
        "custom_components.trimlight.config_flow.TrimlightApi"
    ) as mock_api_cls:
        mock_api = mock_api_cls.return_value
        mock_api.get_devices = AsyncMock(side_effect=Exception("boom"))

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=VALID_INPUT
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


@pytest.mark.asyncio
async def test_already_configured_aborts(hass):
    with patch(
        "custom_components.trimlight.config_flow.TrimlightApi"
    ) as mock_api_cls:
        mock_api = mock_api_cls.return_value
        mock_api.get_devices = AsyncMock(return_value=MOCK_DEVICE_LIST)

        # First setup
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=VALID_INPUT
        )

        # Second setup with same client_id
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=VALID_INPUT
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
