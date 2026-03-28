"""Trimlight Edge light platform."""

from __future__ import annotations

import colorsys
import logging
import time
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from homeassistant.config_entries import ConfigEntry

from .const import (
    DOMAIN,
    EFFECT_CATEGORY_CUSTOM,
    EFFECT_MODE_STATIC,
    HA_COLOR_EFFECT_NAME,
    SWITCH_STATE_MANUAL,
    SWITCH_STATE_OFF,
)
from .coordinator import TrimlightCoordinator

_LOGGER = logging.getLogger(__name__)

# Seconds to hold optimistic state after a command before trusting API state.
_COMMAND_COOLDOWN = 60

# Maximum pixel entries in a custom effect (API docs: index range [0, 29]).
_MAX_PIXEL_ENTRIES = 30


def _hs_to_api_color(hs: tuple[float, float]) -> int:
    """Convert HA hs_color (hue 0-360, sat 0-100) to API decimal RGB integer."""
    h, s = hs
    r, g, b = colorsys.hsv_to_rgb(h / 360.0, s / 100.0, 1.0)
    return (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)


def _build_solid_color_pixels(color_int: int) -> list[dict[str, Any]]:
    """Build a 30-entry pixel array for a solid color effect.

    Uses count=1 for the first pixel entry (the pattern repeats across the
    strip in STATIC mode). Remaining entries are zeroed.
    """
    return [
        {
            "index": i,
            "count": 1 if i == 0 else 0,
            "color": color_int if i == 0 else 0,
            "disable": False,
        }
        for i in range(_MAX_PIXEL_ENTRIES)
    ]


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
      - on / off
      - HS color picker (saves a static custom effect then activates it)
      - brightness control (mapped to the effect brightness field)
      - effect selection from the effects saved on the device
    """

    _attr_supported_color_modes = {ColorMode.HS}
    _attr_color_mode = ColorMode.HS
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self, coordinator: TrimlightCoordinator, device_id: str
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = device_id
        self._active_effect_name: str | None = None
        self._last_command_time: float = 0.0
        self._color_effect_id: int | None = None
        # Defaults so HA's frontend renders the color picker on first load.
        self._attr_hs_color = (0.0, 0.0)
        self._attr_brightness = 255

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    @property
    def _data(self) -> dict[str, Any]:
        """Return the coordinator data for this device."""
        return self.coordinator.data.get(self._device_id, {})

    @property
    def _effects(self) -> list[dict[str, Any]]:
        """Return the saved effects list for this device."""
        return self._data.get("effects", [])

    def _effect_by_name(self, name: str) -> dict[str, Any] | None:
        """Find a saved effect by name."""
        return next((e for e in self._effects if e.get("name") == name), None)

    # ------------------------------------------------------------------ #
    # HA entity properties                                                 #
    # ------------------------------------------------------------------ #

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for the HA device registry."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=self._data.get("name", self._device_id),
            manufacturer="Trimlight",
            model="Edge",
            sw_version=self._data.get("fwVersionName"),
        )

    @property
    def available(self) -> bool:
        """Return True if the device is online."""
        return self._data.get("connectivity", 0) == 1

    def _handle_coordinator_update(self) -> None:
        """Sync state from coordinator, respecting the optimistic cooldown."""
        if time.monotonic() - self._last_command_time > _COMMAND_COOLDOWN:
            switch_state = self._data.get("switchState")
            if switch_state is not None:
                self._attr_is_on = switch_state != SWITCH_STATE_OFF
        self.async_write_ha_state()

    @property
    def effect_list(self) -> list[str]:
        """Return the list of available effect names."""
        return [e["name"] for e in self._effects]

    @property
    def effect(self) -> str | None:
        """Return the currently active effect name."""
        return self._active_effect_name

    # ------------------------------------------------------------------ #
    # Commands                                                             #
    # ------------------------------------------------------------------ #

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light.

        Supports:
          - Plain toggle:   activates first saved effect (or HA Color if set)
          - ATTR_EFFECT:    activates the named saved effect
          - ATTR_HS_COLOR:  saves a solid static color effect then activates it
          - ATTR_BRIGHTNESS: updates the brightness of the current color effect
        """
        api = self.coordinator.api
        effect_name: str | None = kwargs.get(ATTR_EFFECT)
        hs_color: tuple[float, float] | None = kwargs.get(ATTR_HS_COLOR)
        brightness: int | None = kwargs.get(ATTR_BRIGHTNESS)

        _LOGGER.debug("turn_on on %s — kwargs: %s", self._device_id, kwargs)

        # Optimistic update — holds for _COMMAND_COOLDOWN seconds.
        self._last_command_time = time.monotonic()
        self._attr_is_on = True
        if hs_color:
            self._attr_hs_color = hs_color
            self._active_effect_name = None
        if brightness is not None:
            self._attr_brightness = brightness
        self.async_write_ha_state()

        if effect_name is not None:
            await self._activate_effect(effect_name)
        elif hs_color is not None or brightness is not None:
            await self._set_color(
                hs_color if hs_color is not None else self._attr_hs_color,
                brightness if brightness is not None else self._attr_brightness,
            )
        else:
            await self._plain_turn_on()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        self._last_command_time = time.monotonic()
        self._attr_is_on = False
        self.async_write_ha_state()

        try:
            await self.coordinator.api.set_switch_state(
                self._device_id, SWITCH_STATE_OFF
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to turn off %s", self._device_id)

    # ------------------------------------------------------------------ #
    # Internal command helpers                                             #
    # ------------------------------------------------------------------ #

    async def _activate_effect(self, effect_name: str) -> None:
        """Activate a named saved effect via view_effect."""
        effect = self._effect_by_name(effect_name)
        if effect is None:
            _LOGGER.error(
                "Effect '%s' not found on device %s", effect_name, self._device_id
            )
            return
        try:
            await self.coordinator.api.view_effect(self._device_id, effect["id"])
            self._active_effect_name = effect_name
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to activate effect on %s", self._device_id)

    async def _set_color(
        self, hs: tuple[float, float], brightness: int
    ) -> None:
        """Save a solid-color effect on the device, then activate it."""
        api = self.coordinator.api
        color_int = _hs_to_api_color(hs)
        pixels = _build_solid_color_pixels(color_int)

        # Reuse cached "HA Color" slot or find it in the effects list.
        if self._color_effect_id is None:
            existing = self._effect_by_name(HA_COLOR_EFFECT_NAME)
            if existing:
                self._color_effect_id = existing["id"]

        effect_id = self._color_effect_id if self._color_effect_id is not None else -1

        try:
            result = await api.save_effect(
                self._device_id,
                {
                    "id": effect_id,
                    "name": HA_COLOR_EFFECT_NAME,
                    "category": EFFECT_CATEGORY_CUSTOM,
                    "mode": EFFECT_MODE_STATIC,
                    "speed": 127,
                    "brightness": brightness,
                    "pixels": pixels,
                },
            )
            saved_id = (result or {}).get("id", effect_id)
            if not saved_id or saved_id == -1:
                _LOGGER.error("save_effect returned invalid id: %s", saved_id)
                return

            self._color_effect_id = saved_id

            # Only call view_effect the first time to activate the slot.
            # After that, save_effect alone updates the running pattern,
            # halving the API calls for rapid color changes.
            if self._active_effect_name != HA_COLOR_EFFECT_NAME:
                await api.view_effect(self._device_id, saved_id)
            self._active_effect_name = HA_COLOR_EFFECT_NAME
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to set color on %s", self._device_id)

    async def _plain_turn_on(self) -> None:
        """Turn on with no specific color or effect requested."""
        api = self.coordinator.api

        # Re-activate the current HA Color if one was previously set.
        if self._color_effect_id is not None:
            try:
                await api.view_effect(self._device_id, self._color_effect_id)
                return
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to re-activate HA Color on %s", self._device_id)

        # Otherwise activate the first saved effect.
        effects = self._effects
        if effects:
            try:
                await api.view_effect(self._device_id, effects[0]["id"])
                return
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to view effect on %s", self._device_id)

        # Last resort: just set switch state to manual.
        try:
            await api.set_switch_state(self._device_id, SWITCH_STATE_MANUAL)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to turn on %s", self._device_id)
