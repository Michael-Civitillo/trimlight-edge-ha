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
        # Cached ID of the "HA Color" effect slot on the device.
        # Populated on first save so rapid color changes reuse the same slot
        # without waiting for a coordinator poll.
        self._color_effect_id: int | None = None

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
        return self._data.get("connectivity", 0) == 1

    def _handle_coordinator_update(self) -> None:
        """Sync state from coordinator, holding optimistic state after commands."""
        seconds_since_command = time.monotonic() - self._last_command_time
        if seconds_since_command > _COMMAND_COOLDOWN:
            switch_state = self._data.get("switchState")
            if switch_state is not None:
                self._attr_is_on = switch_state != SWITCH_STATE_OFF
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

        if effect_name is not None:
            # Effect takes highest priority — even if HA also sends hs_color.
            # view_effect both activates the effect AND turns the device on;
            # no separate set_switch_state call needed (it would clear the effect).
            effect = self._effect_by_name(effect_name)
            if effect is None:
                _LOGGER.error("Effect '%s' not found on device %s", effect_name, self._device_id)
            else:
                try:
                    await api.view_effect(self._device_id, effect["id"])
                    self._active_effect_name = effect_name
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error("Failed to activate effect on %s: %s", self._device_id, err)

        elif hs_color is not None:
            # Solid color: save/update an "HA Color" effect on the device,
            # then activate it via view_effect. preview_effect is broken on
            # this firmware. view_effect also turns the device on.
            #
            # API docs: pixel index range [0, 29], count range [0, 60].
            # Category 1 = custom effect, mode 0 = STATIC.
            # STATIC mode does NOT repeat — it plays the pattern once. So we
            # fill ALL 30 entries × 60 count = 1800 pixels of solid color,
            # enough to cover any residential strip.
            color_int = _hs_to_api_color(hs_color)

            pixels = [
                {"index": i, "count": 60, "color": color_int, "disable": False}
                for i in range(30)
            ]

            # Reuse the cached "HA Color" slot, falling back to the effects
            # list (after a coordinator poll), then creating a new slot.
            # The in-memory cache means rapid color changes never create
            # duplicate effects regardless of coordinator timing.
            if self._color_effect_id is None:
                existing = self._effect_by_name("HA Color")
                if existing:
                    self._color_effect_id = existing["id"]

            effect_id = self._color_effect_id if self._color_effect_id is not None else -1

            try:
                result = await api.save_effect(
                    self._device_id,
                    {
                        "id": effect_id,
                        "name": "HA Color",
                        "category": 1,
                        "mode": 0,
                        "speed": 127,
                        "brightness": 255,
                        "pixels": pixels,
                    },
                )
                saved_id = (result or {}).get("id", effect_id)
                if saved_id and saved_id != -1:
                    self._color_effect_id = saved_id  # cache for future calls
                    await api.view_effect(self._device_id, saved_id)
                    self._active_effect_name = "HA Color"
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Failed to set color on %s: %s", self._device_id, err)

        else:
            # Plain turn-on: activate first saved effect (which also turns on
            # the device). Only fall back to set_switch_state if no effects
            # are saved — view_effect is preferred because set_switch_state
            # alone puts the device in a blank manual state with no pattern.
            effects = self._effects
            if effects:
                try:
                    await api.view_effect(self._device_id, effects[0]["id"])
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error("Failed to view effect on %s: %s", self._device_id, err)
            else:
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
