"""Trimlight Edge light platform."""
from __future__ import annotations

import colorsys
import logging
import time
from typing import Any

from homeassistant.components.light import (
    ATTR_EFFECT,
    ATTR_HS_COLOR,
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

# Seconds to hold optimistic state after a command before trusting API state.
_COMMAND_COOLDOWN = 60


def _hs_to_api_color(hs: tuple[float, float]) -> int:
    """Convert HA hs_color (hue 0-360, sat 0-100) to API decimal RGB integer."""
    h, s = hs
    r, g, b = colorsys.hsv_to_rgb(h / 360.0, s / 100.0, 1.0)
    return (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)


def _api_color_to_hs(color_int: int) -> tuple[float, float]:
    """Convert API decimal RGB integer to HA hs_color."""
    r = (color_int >> 16) & 0xFF
    g = (color_int >> 8) & 0xFF
    b = color_int & 0xFF
    h, s, _ = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    return h * 360.0, s * 100.0


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
      • HS color picker (sends a static custom effect preview)
      • effect selection from the effects saved on the device
    """

    _attr_supported_color_modes = {ColorMode.HS}
    _attr_color_mode = ColorMode.HS
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_has_entity_name = True
    _attr_name = None  # device name is used as entity name via has_entity_name

    def __init__(
        self, coordinator: TrimlightCoordinator, device_id: str
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = device_id
        self._active_effect_name: str | None = None
        self._last_command_time: float = 0.0

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

    def _total_pixels(self) -> int:
        """Sum pixel count across all configured ports."""
        ports = self._data.get("ports", [])
        if ports:
            return sum(p.get("end", 0) - p.get("start", 0) + 1 for p in ports)
        return 300  # safe default

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
        return self._data.get("connectivity", 0) == 1

    def _handle_coordinator_update(self) -> None:
        """Sync state from coordinator, holding optimistic state after commands."""
        seconds_since_command = time.monotonic() - self._last_command_time
        if seconds_since_command > _COMMAND_COOLDOWN:
            switch_state = self._data.get("switchState")
            if switch_state is not None:
                self._attr_is_on = switch_state != SWITCH_STATE_OFF
                # Reflect current color from device if available.
                current = self._data.get("currentEffect", {})
                pixels = current.get("pixels", [])
                if pixels and pixels[0].get("color"):
                    self._attr_hs_color = _api_color_to_hs(pixels[0]["color"])
        self.async_write_ha_state()

    @property
    def effect_list(self) -> list[str]:
        return [e["name"] for e in self._effects]

    @property
    def effect(self) -> str | None:
        return self._active_effect_name

    # ------------------------------------------------------------------ #
    # Commands                                                             #
    # ------------------------------------------------------------------ #

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light.

        Supports:
          • Plain toggle     → activates first saved effect
          • ATTR_EFFECT      → activates the named saved effect
          • ATTR_HS_COLOR    → previews a solid static color (category 1)
        """
        api = self.coordinator.api
        effect_name: str | None = kwargs.get(ATTR_EFFECT)
        hs_color: tuple[float, float] | None = kwargs.get(ATTR_HS_COLOR)

        # Optimistic update — holds for 60s so coordinator can't flip it back.
        self._last_command_time = time.monotonic()
        self._attr_is_on = True
        if hs_color:
            self._attr_hs_color = hs_color
            self._active_effect_name = None
        self.async_write_ha_state()

        if hs_color is not None:
            # Solid color via custom static effect preview (category 1, mode 0 = STATIC).
            color_int = _hs_to_api_color(hs_color)
            pixel_count = self._total_pixels()
            try:
                await api.preview_effect(
                    self._device_id,
                    {
                        "category": 1,
                        "mode": 0,
                        "speed": 128,
                        "brightness": 255,
                        "pixels": [
                            {"index": 0, "count": pixel_count, "color": color_int, "disable": False}
                        ],
                    },
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Failed to set color on %s: %s", self._device_id, err)

        elif effect_name is not None:
            effect = self._effect_by_name(effect_name)
            if effect is None:
                _LOGGER.error("Effect '%s' not found on device %s", effect_name, self._device_id)
            else:
                try:
                    await api.view_effect(self._device_id, effect["id"])
                    self._active_effect_name = effect_name
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error("Failed to activate effect on %s: %s", self._device_id, err)

        else:
            # Plain turn-on: activate first saved effect.
            effects = self._effects
            if effects:
                try:
                    await api.view_effect(self._device_id, effects[0]["id"])
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error("Failed to view effect on %s: %s", self._device_id, err)

        try:
            await api.set_switch_state(self._device_id, SWITCH_STATE_MANUAL)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to turn on %s: %s", self._device_id, err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        self._last_command_time = time.monotonic()
        self._attr_is_on = False
        self.async_write_ha_state()

        try:
            await self.coordinator.api.set_switch_state(self._device_id, SWITCH_STATE_OFF)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to turn off %s: %s", self._device_id, err)
