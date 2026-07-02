"""Tests for the Trimlight coordinator."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.trimlight.api import (
    TrimlightApi,
    TrimlightApiError,
    TrimlightAuthError,
)
from custom_components.trimlight.const import DOMAIN
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


def _make_coordinator(hass, api):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    return TrimlightCoordinator(hass, entry, api)


@pytest.fixture
def coordinator(hass, api):
    return _make_coordinator(hass, api)


async def test_list_failure_raises_update_failed(hass, api):
    """A failure listing devices must surface as UpdateFailed."""
    api.get_devices = AsyncMock(side_effect=TrimlightApiError("502 Bad Gateway"))
    coordinator = _make_coordinator(hass, api)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_auth_failure_raises_config_entry_auth_failed(hass, api):
    """Rejected credentials must trigger HA's reauth flow, not a retry loop."""
    api.get_devices = AsyncMock(
        side_effect=TrimlightAuthError("API auth error 10001: auth error")
    )
    coordinator = _make_coordinator(hass, api)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_detail_auth_failure_raises_config_entry_auth_failed(coordinator, api):
    """An auth rejection on the detail call is not a per-device outage."""
    api.get_device = AsyncMock(
        side_effect=TrimlightAuthError("API auth error 10001: auth error")
    )
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_merges_list_and_detail(coordinator):
    """A healthy poll merges list-level and detail-level fields."""
    data = await coordinator._async_update_data()
    assert data[DEVICE_ID]["name"] == "Front"
    assert data[DEVICE_ID]["effects"][0]["name"] == "NEW YEAR"


async def test_malformed_list_entry_is_skipped(coordinator, api):
    """One list entry without a deviceId must not fail the whole update."""
    api.get_devices = AsyncMock(
        return_value=[{"name": "ghost"}, dict(LIST_ENTRY)]
    )
    data = await coordinator._async_update_data()
    assert list(data) == [DEVICE_ID]


async def test_detail_none_treated_as_failure(coordinator, api):
    """A success response without a payload must not crash the merge."""
    coordinator.data = await coordinator._async_update_data()

    api.get_device = AsyncMock(return_value=None)
    data = await coordinator._async_update_data()
    assert data[DEVICE_ID]["_detail_stale"] is True
    # Last-known detail survives, same as any other detail failure.
    assert data[DEVICE_ID]["effects"][0]["name"] == "NEW YEAR"


async def test_offline_device_skips_detail_fetch(coordinator, api):
    """An offline device keeps last-known detail without extra requests."""
    coordinator.data = await coordinator._async_update_data()

    api.get_devices = AsyncMock(return_value=[{**LIST_ENTRY, "connectivity": 0}])
    api.notify_update_shadow.reset_mock()
    api.get_device.reset_mock()

    data = await coordinator._async_update_data()
    api.notify_update_shadow.assert_not_called()
    api.get_device.assert_not_called()
    assert data[DEVICE_ID]["connectivity"] == 0
    assert data[DEVICE_ID]["_detail_stale"] is True
    assert data[DEVICE_ID]["effects"][0]["name"] == "NEW YEAR"


async def test_detail_failure_preserves_previous_detail(coordinator, api):
    """A failing detail fetch keeps its last-known detail; list fields refresh."""
    coordinator.data = await coordinator._async_update_data()

    api.get_device = AsyncMock(
        side_effect=TrimlightApiError("API error 10008: Device is offline")
    )

    data = await coordinator._async_update_data()
    # ...the effect list survives so the entity doesn't blank out.
    assert data[DEVICE_ID]["effects"][0]["name"] == "NEW YEAR"
    assert data[DEVICE_ID]["_detail_stale"] is True


async def test_detail_failure_without_prior_detail_keeps_list(coordinator, api):
    """If detail never succeeded, fall back to list-only data (no crash)."""
    api.get_device = AsyncMock(side_effect=TrimlightApiError("boom"))
    data = await coordinator._async_update_data()
    assert data[DEVICE_ID]["name"] == "Front"
    assert "effects" not in data[DEVICE_ID]


async def test_detail_failure_warns_once(coordinator, api, caplog):
    """Repeated detail failures must warn once, not on every poll."""
    api.get_device = AsyncMock(side_effect=TrimlightApiError("boom"))
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
    api.get_device = AsyncMock(side_effect=TrimlightApiError("boom"))
    coordinator.data = await coordinator._async_update_data()
    assert DEVICE_ID in coordinator._detail_failures

    api.get_device = AsyncMock(return_value=dict(DETAIL))
    await coordinator._async_update_data()
    assert DEVICE_ID not in coordinator._detail_failures


async def test_detail_failure_marks_data_stale(coordinator, api):
    """Carried-forward detail is flagged stale so consumers don't trust it."""
    coordinator.data = await coordinator._async_update_data()
    assert "_detail_stale" not in coordinator.data[DEVICE_ID]

    api.get_device = AsyncMock(side_effect=TrimlightApiError("boom"))
    data = await coordinator._async_update_data()
    assert data[DEVICE_ID]["_detail_stale"] is True

    # A successful poll clears the stale marker again.
    api.get_device = AsyncMock(return_value=dict(DETAIL))
    coordinator.data = data
    data = await coordinator._async_update_data()
    assert "_detail_stale" not in data[DEVICE_ID]
