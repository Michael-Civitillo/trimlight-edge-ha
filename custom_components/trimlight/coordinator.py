"""DataUpdateCoordinator for Trimlight Edge."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TrimlightApi, TrimlightApiError, TrimlightAuthError
from .const import (
    API_UNPARSEABLE_DEVICE_DATA_CODE,
    DOMAIN,
    HA_COLOR_EFFECT_NAME,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# Translation key of the repair issue raised while the cloud can't parse a
# device's stored data. The per-device issue id is this key plus the device id.
UNPARSEABLE_DEVICE_DATA_ISSUE = "unparseable_device_data"


def unparseable_device_data_issue_id(device_id: str) -> str:
    """Return the repair issue id for a device whose stored data can't be parsed."""
    return f"{UNPARSEABLE_DEVICE_DATA_ISSUE}_{device_id}"


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
        # Device IDs with an "unparseable device data" repair issue raised, so
        # it can be cleared again on recovery, device removal, or unload.
        self._unparseable_issues: set[str] = set()

    async def async_shutdown(self) -> None:
        """Clear this entry's repair issues on unload.

        A reload raises them again on its first refresh if the data is still
        unparseable; removing the integration must not leave them dangling.
        """
        for device_id in list(self._unparseable_issues):
            self._clear_unparseable_issue(device_id)
        await super().async_shutdown()

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

    def _handle_detail_failure(
        self, device: dict[str, Any], err: TrimlightApiError
    ) -> None:
        """Record a failed detail fetch: warn once per outage, repair when actionable.

        Error 12006 ("Device data could not be parsed") means the cloud can't
        make sense of what the controller has stored; users have traced it to
        duplicate or oddly named saved effects. Nothing here can fix that, but
        the user can from the Trimlight app, so it's surfaced as a repair
        issue instead of being left as a log line behind an empty effect list.
        """
        device_id: str = device["deviceId"]
        unparseable = err.code == API_UNPARSEABLE_DEVICE_DATA_CODE

        if device_id in self._detail_failures:
            _LOGGER.debug(
                "Still cannot fetch detail for device %s: %s", device_id, err
            )
        else:
            self._detail_failures.add(device_id)
            hint = ""
            if unparseable:
                hint = (
                    " (Trimlight's cloud cannot parse this controller's stored"
                    " data; see Settings > Repairs for what to check in the"
                    " Trimlight app)"
                )
            _LOGGER.warning(
                "Could not fetch detail for device %s: %s%s", device_id, err, hint
            )

        if unparseable and device_id not in self._unparseable_issues:
            self._unparseable_issues.add(device_id)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                unparseable_device_data_issue_id(device_id),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=UNPARSEABLE_DEVICE_DATA_ISSUE,
                translation_placeholders={
                    "device_name": device.get("name") or device_id,
                    "color_effect": HA_COLOR_EFFECT_NAME,
                },
            )

    def _handle_detail_recovered(self, device_id: str) -> None:
        """Clear a device's outage bookkeeping once its detail fetch works again."""
        if device_id in self._detail_failures:
            self._detail_failures.discard(device_id)
            _LOGGER.info("Device %s detail fetch recovered", device_id)
        self._clear_unparseable_issue(device_id)

    def _clear_unparseable_issue(self, device_id: str) -> None:
        """Delete the device's repair issue, if one was raised."""
        if device_id in self._unparseable_issues:
            self._unparseable_issues.discard(device_id)
            ir.async_delete_issue(
                self.hass, DOMAIN, unparseable_device_data_issue_id(device_id)
            )

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
                result[device_id] = self._carry_forward(device, previous)
                self._handle_detail_failure(device, err)
                continue

            self._handle_detail_recovered(device_id)

            merged = {**device, **detail}
            _LOGGER.debug("Device %s merged data keys: %s", device_id, list(merged.keys()))
            _LOGGER.debug(
                "Device %s switchState=%s currentEffect=%s currentDatetime=%s"
                " daily=%s calendar=%s",
                device_id,
                merged.get("switchState"),
                merged.get("currentEffect"),
                merged.get("currentDatetime"),
                merged.get("daily"),
                merged.get("calendar"),
            )
            result[device_id] = merged

        # A device that dropped out of the account can't recover, so don't
        # leave its outage bookkeeping (or repair issue) dangling.
        listed = set(result)
        for device_id in self._unparseable_issues - listed:
            self._clear_unparseable_issue(device_id)
        self._detail_failures &= listed

        return result
