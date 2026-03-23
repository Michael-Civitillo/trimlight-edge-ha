"""Tests for the Trimlight light entity."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_EFFECT
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE

from custom_components.trimlight.const import DOMAIN

from .conftest import MOCK_COORDINATOR_DATA, MOCK_DEVICE_ID, MOCK_DEVICE_NAME

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def _setup_integration(hass, mock_api):
    """Helper: set up the integration with a mocked API and return the coordinator."""
    with (
        patch("custom_components.trimlight.TrimlightApi", return_value=mock_api),
        patch(
            "custom_components.trimlight.coordinator.TrimlightCoordinator._async_update_data",
            return_value=MOCK_COORDINATOR_DATA,
        ),
    ):
        from homeassistant.config_entries import ConfigEntry

        entry = ConfigEntry(
            version=1,
            minor_version=1,
            domain=DOMAIN,
            title="Trimlight (test)",
            data={"client_id": "test", "client_secret": "secret"},
            source="user",
            options={},
            entry_id="test_entry",
            unique_id="test_client_id",
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return hass.data[DOMAIN]["test_entry"]


@pytest.mark.asyncio
async def test_light_is_on(hass, mock_api):
    await _setup_integration(hass, mock_api)
    state = hass.states.get(f"light.{MOCK_DEVICE_NAME.lower().replace(' ', '_')}")
    assert state is not None
    assert state.state == STATE_ON


@pytest.mark.asyncio
async def test_light_is_off(hass, mock_api):
    off_data = {
        MOCK_DEVICE_ID: {**MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID], "switchState": 0}
    }
    with patch(
        "custom_components.trimlight.coordinator.TrimlightCoordinator._async_update_data",
        return_value=off_data,
    ):
        with patch("custom_components.trimlight.TrimlightApi", return_value=mock_api):
            from homeassistant.config_entries import ConfigEntry

            entry = ConfigEntry(
                version=1,
                minor_version=1,
                domain=DOMAIN,
                title="Trimlight (test)",
                data={"client_id": "test2", "client_secret": "secret"},
                source="user",
                options={},
                entry_id="test_entry_off",
                unique_id="test_client_id_off",
            )
            entry.add_to_hass(hass)
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

    entity_id = f"light.{MOCK_DEVICE_NAME.lower().replace(' ', '_')}"
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_OFF


@pytest.mark.asyncio
async def test_brightness_reflected(hass, mock_api):
    await _setup_integration(hass, mock_api)
    entity_id = f"light.{MOCK_DEVICE_NAME.lower().replace(' ', '_')}"
    state = hass.states.get(entity_id)
    assert state.attributes.get("brightness") == 200


@pytest.mark.asyncio
async def test_effect_list_populated(hass, mock_api):
    await _setup_integration(hass, mock_api)
    entity_id = f"light.{MOCK_DEVICE_NAME.lower().replace(' ', '_')}"
    state = hass.states.get(entity_id)
    assert "New Year" in state.attributes.get("effect_list", [])
    assert "Christmas" in state.attributes.get("effect_list", [])


@pytest.mark.asyncio
async def test_turn_on_calls_set_switch_state(hass, mock_api):
    coordinator = await _setup_integration(hass, mock_api)
    # Simulate device currently off
    coordinator.data[MOCK_DEVICE_ID]["switchState"] = 0

    entity_id = f"light.{MOCK_DEVICE_NAME.lower().replace(' ', '_')}"
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_id}, blocking=True
    )

    mock_api.set_switch_state.assert_called_once_with(MOCK_DEVICE_ID, 1)


@pytest.mark.asyncio
async def test_turn_on_with_effect(hass, mock_api):
    coordinator = await _setup_integration(hass, mock_api)

    entity_id = f"light.{MOCK_DEVICE_NAME.lower().replace(' ', '_')}"
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": entity_id, ATTR_EFFECT: "New Year"},
        blocking=True,
    )

    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 0)


@pytest.mark.asyncio
async def test_turn_on_with_brightness(hass, mock_api):
    coordinator = await _setup_integration(hass, mock_api)

    entity_id = f"light.{MOCK_DEVICE_NAME.lower().replace(' ', '_')}"
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": entity_id, ATTR_BRIGHTNESS: 128},
        blocking=True,
    )

    mock_api.preview_effect.assert_called_once()
    call_args = mock_api.preview_effect.call_args
    assert call_args[0][0] == MOCK_DEVICE_ID
    assert call_args[0][1]["brightness"] == 128


@pytest.mark.asyncio
async def test_turn_off(hass, mock_api):
    await _setup_integration(hass, mock_api)

    entity_id = f"light.{MOCK_DEVICE_NAME.lower().replace(' ', '_')}"
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": entity_id}, blocking=True
    )

    mock_api.set_switch_state.assert_called_once_with(MOCK_DEVICE_ID, 0)
