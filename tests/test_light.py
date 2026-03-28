"""Tests for the Trimlight light entity."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_EFFECT, ATTR_HS_COLOR
from homeassistant.const import STATE_OFF, STATE_ON

from custom_components.trimlight.const import DOMAIN, HA_COLOR_EFFECT_NAME

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


def _entity_id() -> str:
    return f"light.{MOCK_DEVICE_NAME.lower().replace(' ', '_')}"


@pytest.mark.asyncio
async def test_light_is_on(hass, mock_api):
    """Light should be ON when switchState is 1 (manual)."""
    await _setup_integration(hass, mock_api)
    state = hass.states.get(_entity_id())
    assert state is not None
    assert state.state == STATE_ON


@pytest.mark.asyncio
async def test_light_is_off(hass, mock_api):
    """Light should be OFF when switchState is 0."""
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

    state = hass.states.get(_entity_id())
    assert state is not None
    assert state.state == STATE_OFF


@pytest.mark.asyncio
async def test_effect_list_populated(hass, mock_api):
    """All saved effects should appear in the effect_list attribute."""
    await _setup_integration(hass, mock_api)
    state = hass.states.get(_entity_id())
    effect_list = state.attributes.get("effect_list", [])
    assert "NEW YEAR" in effect_list
    assert "INDEPENDENCE DAY" in effect_list


@pytest.mark.asyncio
async def test_turn_on_activates_first_effect(hass, mock_api):
    """Plain turn_on should activate the first saved effect via view_effect."""
    await _setup_integration(hass, mock_api)
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": _entity_id()}, blocking=True
    )
    mock_api.view_effect.assert_called_with(MOCK_DEVICE_ID, 1)


@pytest.mark.asyncio
async def test_turn_on_with_effect(hass, mock_api):
    """Turn on with ATTR_EFFECT should call view_effect with the correct ID."""
    await _setup_integration(hass, mock_api)
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_EFFECT: "INDEPENDENCE DAY"},
        blocking=True,
    )
    mock_api.view_effect.assert_called_with(MOCK_DEVICE_ID, 2)


@pytest.mark.asyncio
async def test_turn_on_with_color(hass, mock_api):
    """Turn on with ATTR_HS_COLOR should save and view a color effect."""
    await _setup_integration(hass, mock_api)
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_HS_COLOR: (0.0, 100.0)},
        blocking=True,
    )
    # Should save_effect with the color and then view_effect.
    mock_api.save_effect.assert_called_once()
    save_args = mock_api.save_effect.call_args
    payload = save_args[0][1]
    assert payload["name"] == HA_COLOR_EFFECT_NAME
    assert payload["category"] == 2
    assert payload["mode"] == 0
    assert payload["pixels"][0]["color"] == 16711680  # pure red

    mock_api.view_effect.assert_called_with(MOCK_DEVICE_ID, 99)


@pytest.mark.asyncio
async def test_turn_on_with_brightness(hass, mock_api):
    """Turn on with ATTR_BRIGHTNESS should save the color effect with that brightness."""
    await _setup_integration(hass, mock_api)
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_BRIGHTNESS: 128},
        blocking=True,
    )
    mock_api.save_effect.assert_called_once()
    save_args = mock_api.save_effect.call_args
    assert save_args[0][1]["brightness"] == 128


@pytest.mark.asyncio
async def test_turn_off(hass, mock_api):
    """Turn off should call set_switch_state with OFF."""
    await _setup_integration(hass, mock_api)
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": _entity_id()}, blocking=True
    )
    mock_api.set_switch_state.assert_called_once_with(MOCK_DEVICE_ID, 0)


@pytest.mark.asyncio
async def test_rapid_color_changes_skip_view(hass, mock_api):
    """After the first color set, subsequent changes should skip view_effect."""
    await _setup_integration(hass, mock_api)

    # First color — should call both save_effect and view_effect.
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_HS_COLOR: (0.0, 100.0)},
        blocking=True,
    )
    assert mock_api.view_effect.call_count == 1
    assert mock_api.save_effect.call_count == 1

    # Second color — should call save_effect but NOT view_effect again.
    mock_api.view_effect.reset_mock()
    mock_api.save_effect.reset_mock()
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_HS_COLOR: (120.0, 100.0)},
        blocking=True,
    )
    assert mock_api.save_effect.call_count == 1
    assert mock_api.view_effect.call_count == 0
