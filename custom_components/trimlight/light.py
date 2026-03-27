"""Trimlight Edge light platform."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SWITCH_STATE_MANUAL, SWITCH_STATE_OFF
from .coordinator import TrimlightCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one light entity per Trimlight device."""
    coordinator: TrimlightCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        TrimlightLight(coordinator, device_id)
        for device_id in coordinator.data
    )


class TrimlightLight(CoordinatorEntity[TrimlightCoordinator], LightEntity):
    """Represents a single Trimlight device as a HA light entity.

    Capabilities:
      • on / off
      • brightness (maps to effect brightness 0-255)
      • effect selection from the effects saved on the device
    """

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_has_entity_name = True
    _attr_name = None  # device name is used as entity name via has_entity_name

    def __init__(
        self, coordinator: TrimlightCoordinator, device_id: str
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = device_id
        # Track the name of the currently active effect locally.
        # The API's currentEffect object doesn't include name/id, so we
        # remember the last effect the user selected via HA.
        self._active_effect_name: str | None = None

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    @property
    def _data(self) -> dict:
        return self.coordinator.data.get(self._device_id, {})

    @property
    def _effects(self) -> list[dict]:
        return self._data.get("effects", [])

    def _effect_by_name(self, name: str) -> dict | None:
        return next((e for e in self._effects if e["name"] == name), None)

    # ------------------------------------------------------------------ #
    # HA entity properties                                                 #
    # ------------------------------------------------------------------ #

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=self._data.get("name", self._device_id),
            manufacturer="Trimlight",
            model="Edge",
            sw_version=self._data.get("fwVersionName"),
        )

    @property
    def available(self) -> bool:
        """Device is available only when online."""
        return self._data.get("connectivity", 0) == 1

    @property
    def is_on(self) -> bool:
        return self._data.get("switchState", 0) != SWITCH_STATE_OFF

    @property
    def brightness(self) -> int | None:
        """Return brightness from the currently running effect (0-255)."""
        current = self._data.get("currentEffect")
        if current:
            return current.get("brightness")
        return None

    @property
    def effect_list(self) -> list[str]:
        return [e["name"] for e in self._effects]

    @property
    def effect(self) -> str | None:
        """Return the last effect selected through HA."""
        return self._active_effect_name

    # ------------------------------------------------------------------ #
    # Commands                                                             #
    # ------------------------------------------------------------------ #

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light, optionally setting an effect and/or brightness."""
        api = self.coordinator.api
        effect_name: str | None = kwargs.get(ATTR_EFFECT)
        brightness: int | None = kwargs.get(ATTR_BRIGHTNESS)

        if effect_name is not None:
            effect = self._effect_by_name(effect_name)
            if effect is None:
                _LOGGER.error("Effect '%s' not found on device %s", effect_name, self._device_id)
            else:
                await api.view_effect(self._device_id, effect["id"])
                self._active_effect_name = effect_name

        if brightness is not None:
            current = self._data.get("currentEffect", {})
            if current:
                preview: dict[str, Any] = {
                    "category": current.get("category", 0),
                    "mode": current.get("mode", 0),
                    "speed": current.get("speed", 100),
                    "brightness": brightness,
                }
                if current.get("category") == 0:
                    preview["pixelLen"] = current.get("pixelLen", 30)
                    preview["reverse"] = current.get("reverse", False)
                if current.get("category") == 0:
                    await api.preview_effect(self._device_id, preview)
            else:
                _LOGGER.debug(
                    "No currentEffect data for %s; skipping brightness preview",
                    self._device_id,
                )

        await api.set_switch_state(self._device_id, SWITCH_STATE_MANUAL)

        # Optimistically reflect the new state immediately.
        self._attr_is_on = True
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        await self.coordinator.api.set_switch_state(self._device_id, SWITCH_STATE_OFF)

        # Optimistically reflect the new state immediately.
        self._attr_is_on = False
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
