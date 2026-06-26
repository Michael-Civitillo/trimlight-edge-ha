"""Tests for the Trimlight coordinator."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.trimlight.api import TrimlightApi, TrimlightApiError
from custom_components.trimlight.coordinator import TrimlightCoordinator

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

DEVICE_ID = "dev1"
LIST_ENTRY = {
    "deviceId": DEVICE_ID,
    "name": "Front",
    "switchState": 1,
    "connectivity": 1,
}
DETAIL = {"effects": [{"id": 1, "name": "NEW YEAR"}], "daily": [], "calendar": []}


@pytest.fixture
def api():
    """Return a mocked TrimlightApi that succeeds by default."""
    api = MagicMock(spec=TrimlightApi)
    api.get_devices = AsyncMock(return_value=[dict(LIST_ENTRY)])
    api.notify_update_shadow = AsyncMock(return_value=None)
    api.get_device = AsyncMock(return_value=dict(DETAIL))
    return api


@pytest.fixture
def coordinator(hass, api):
    return TrimlightCoordinator(hass, api)


async def test_list_failure_raises_update_failed(hass, api):
    """A failure listing devices must surface as UpdateFailed."""
    api.get_devices = AsyncMock(side_effect=TrimlightApiError("502 Bad Gateway"))
    coordinator = TrimlightCoordinator(hass, api)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_merges_list_and_detail(coordinator):
    """A healthy poll merges list-level and detail-level fields."""
    data = await coordinator._async_update_data()
    assert data[DEVICE_ID]["name"] == "Front"
    assert data[DEVICE_ID]["effects"][0]["name"] == "NEW YEAR"


async def test_detail_failure_preserves_previous_detail(coordinator, api):
    """An offline device keeps its last-known detail; list fields refresh."""
    coordinator.data = await coordinator._async_update_data()

    api.get_device = AsyncMock(
        side_effect=TrimlightApiError("API error 10008: Device is offline")
    )
    api.get_devices = AsyncMock(
        return_value=[{**LIST_ENTRY, "connectivity": 0}]
    )

    data = await coordinator._async_update_data()
    # Connectivity refreshes from the fresh list (now offline)...
    assert data[DEVICE_ID]["connectivity"] == 0
    # ...but the effect list survives so the entity doesn't blank out.
    assert data[DEVICE_ID]["effects"][0]["name"] == "NEW YEAR"


async def test_detail_failure_without_prior_detail_keeps_list(coordinator, api):
    """If detail never succeeded, fall back to list-only data (no crash)."""
    api.get_device = AsyncMock(side_effect=TrimlightApiError("Device is offline"))
    data = await coordinator._async_update_data()
    assert data[DEVICE_ID]["name"] == "Front"
    assert "effects" not in data[DEVICE_ID]


async def test_detail_failure_warns_once(coordinator, api, caplog):
    """Repeated detail failures must warn once, not on every poll."""
    api.get_device = AsyncMock(side_effect=TrimlightApiError("Device is offline"))
    with caplog.at_level(
        logging.WARNING, logger="custom_components.trimlight.coordinator"
    ):
        coordinator.data = await coordinator._async_update_data()
        await coordinator._async_update_data()
        await coordinator._async_update_data()

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "Could not fetch detail" in r.getMessage()
    ]
    assert len(warnings) == 1


async def test_detail_recovers_after_failure(coordinator, api):
    """Once detail fetch recovers, the failure flag clears for next time."""
    api.get_device = AsyncMock(side_effect=TrimlightApiError("Device is offline"))
    coordinator.data = await coordinator._async_update_data()
    assert DEVICE_ID in coordinator._detail_failures

    api.get_device = AsyncMock(return_value=dict(DETAIL))
    await coordinator._async_update_data()
    assert DEVICE_ID not in coordinator._detail_failures
