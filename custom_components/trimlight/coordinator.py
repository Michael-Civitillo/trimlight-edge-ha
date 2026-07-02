"""DataUpdateCoordinator for Trimlight Edge."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TrimlightApi, TrimlightApiError, TrimlightAuthError
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class TrimlightCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls all devices for a single Trimlight account.

    coordinator.data is a dict keyed by deviceId, each value being a merged
    dict of the list-level fields (name, switchState, connectivity, state,
    fwVersionName) and the detail-level fields (effects, currentEffect, etc.).
    """

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, api: TrimlightApi
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.api = api
        # Device IDs whose detail fetch is currently failing, so the warning is
        # logged once per outage rather than on every poll.
        self._detail_failures: set[str] = set()

    def _carry_forward(self, device: dict[str, Any], previous: dict[str, Any]) -> dict:
        """Merge fresh list-level fields over last-known detail, flagged stale.

        Keeps the effects list and timer schedule so the entity doesn't blank
        out while detail is unavailable; the list-level fields (notably
        connectivity) still refresh. The key is underscore-prefixed so it
        can't collide with a real API field or leak as a device attribute.
        """
        return {
            **previous.get(device["deviceId"], {}),
            **device,
            "_detail_stale": True,
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the Trimlight API."""
        try:
            devices = await self.api.get_devices()
        except TrimlightAuthError as err:
            raise ConfigEntryAuthFailed(
                f"Trimlight rejected the API credentials: {err}"
            ) from err
        except TrimlightApiError as err:
            raise UpdateFailed(
                f"Failed to list Trimlight devices: {err}"
            ) from err

        previous = self.data or {}
        result: dict[str, Any] = {}
        for device in devices:
            if not isinstance(device, dict) or "deviceId" not in device:
                # One malformed list entry must not fail the whole update.
                _LOGGER.warning("Skipping malformed device entry: %s", device)
                continue
            device_id: str = device["deviceId"]
            _LOGGER.debug("Device list-level data: %s", device)

            # An offline device can't push fresh shadow data, so skip the two
            # detail requests and keep the last-known detail. The entity is
            # unavailable while connectivity is 0 anyway.
            if device.get("connectivity") != 1:
                result[device_id] = self._carry_forward(device, previous)
                _LOGGER.debug(
                    "Device %s is offline; skipping detail fetch", device_id
                )
                continue

            # Notify the device to push fresh shadow data, then fetch detail.
            await self.api.notify_update_shadow(device_id)
            try:
                detail = await self.api.get_device(device_id)
                if not isinstance(detail, dict):
                    raise TrimlightApiError(
                        f"Unexpected device detail payload: {detail!r}"
                    )
            except TrimlightAuthError as err:
                raise ConfigEntryAuthFailed(
                    f"Trimlight rejected the API credentials: {err}"
                ) from err
            except TrimlightApiError as err:
                # Warn once per outage instead of on every 30s poll.
                result[device_id] = self._carry_forward(device, previous)
                if device_id not in self._detail_failures:
                    self._detail_failures.add(device_id)
                    _LOGGER.warning(
                        "Could not fetch detail for device %s: %s", device_id, err
                    )
                else:
                    _LOGGER.debug(
                        "Still cannot fetch detail for device %s: %s", device_id, err
                    )
                continue

            if device_id in self._detail_failures:
                self._detail_failures.discard(device_id)
                _LOGGER.info("Device %s detail fetch recovered", device_id)

            merged = {**device, **detail}
            _LOGGER.debug("Device %s merged data keys: %s", device_id, list(merged.keys()))
            _LOGGER.debug(
                "Device %s switchState=%s daily=%s calendar=%s",
                device_id,
                merged.get("switchState"),
                merged.get("daily"),
                merged.get("calendar"),
            )
            result[device_id] = merged

        return result
