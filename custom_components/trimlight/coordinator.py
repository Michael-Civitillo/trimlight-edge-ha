"""DataUpdateCoordinator for Trimlight Edge."""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TrimlightApi, TrimlightApiError
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class TrimlightCoordinator(DataUpdateCoordinator[dict]):
    """Polls all devices for a single Trimlight account.

    coordinator.data is a dict keyed by deviceId, each value being a merged
    dict of the list-level fields (name, switchState, connectivity, state,
    fwVersionName) and the detail-level fields (effects, currentEffect, …).
    """

    def __init__(self, hass: HomeAssistant, api: TrimlightApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.api = api

    async def _async_update_data(self) -> dict:
        try:
            devices = await self.api.get_devices()
        except TrimlightApiError as err:
            raise UpdateFailed(f"Failed to list Trimlight devices: {err}") from err

        result: dict = {}
        for device in devices:
            device_id = device["deviceId"]
            _LOGGER.debug("Device list-level data: %s", device)
            try:
                with open("/config/trimlight_debug.json", "w") as f:
                    json.dump(device, f, indent=2)
            except Exception:
                pass
            # Notify the device to push fresh shadow data, then fetch detail.
            await self.api.notify_update_shadow(device_id)
            try:
                detail = await self.api.get_device(device_id)
                merged = {**device, **detail}
                _LOGGER.debug("Device %s detail data: %s", device_id, detail)
                result[device_id] = merged
            except TrimlightApiError as err:
                _LOGGER.warning(
                    "Could not fetch detail for device %s: %s", device_id, err
                )
                result[device_id] = device

        return result
