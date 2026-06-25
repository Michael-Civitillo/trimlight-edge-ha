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
from homeassistant.util import dt as dt_util

from homeassistant.config_entries import ConfigEntry

from .const import (
    DOMAIN,
    EFFECT_CATEGORY_CUSTOM,
    EFFECT_MODE_STATIC,
    HA_COLOR_EFFECT_NAME,
    SCHEDULE_REPETITION_WEEKDAYS,
    SCHEDULE_REPETITION_WEEKEND,
    SWITCH_STATE_MANUAL,
    SWITCH_STATE_OFF,
    SWITCH_STATE_TIMER,
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
    return (round(r * 255) << 16) | (round(g * 255) << 8) | round(b * 255)


def _time_to_minutes(obj: Any) -> int | None:
    """Convert a schedule time object to minutes-since-midnight.

    Trimlight time objects use the same shape as the API ``currentDate``
    helper (``hours``/``minutes``). Older/alternate payloads may use
    ``hour``/``minute``; both are accepted. Returns None if the value can't
    be parsed so the caller can fall back to mode-based state.
    """
    if not isinstance(obj, dict):
        return None
    hours = obj.get("hours", obj.get("hour"))
    minutes = obj.get("minutes", obj.get("minute"))
    if hours is None or minutes is None:
        return None
    try:
        return (int(hours) % 24) * 60 + (int(minutes) % 60)
    except (TypeError, ValueError):
        return None


def _now_in_window(now_minutes: int, start: int, end: int) -> bool:
    """Return True if now falls inside the [start, end) on-window.

    Handles windows that wrap past midnight (e.g. 18:00 -> 02:00).
    """
    if start == end:
        return False
    if start < end:
        return start <= now_minutes < end
    # Wraps midnight: on from start until end the next day.
    return now_minutes >= start or now_minutes < end


def _window_minutes(entry: dict[str, Any]) -> tuple[int, int] | None:
    """Return an entry's (start, end) on-window in minutes, or None.

    A zero-length window (start == end) is ambiguous — it could mean "off" or
    an all-day "on" sentinel — so it's treated as no usable window. The caller
    then falls back to the on-state rather than risk reporting an on light as
    off.
    """
    start = _time_to_minutes(entry.get("startTime"))
    end = _time_to_minutes(entry.get("endTime"))
    if start is None or end is None or start == end:
        return None
    return start, end


def _daily_applies_today(entry: dict[str, Any], now: Any) -> bool:
    """Return True if a daily schedule's repetition includes today.

    Only the day sets we can identify positively (week days / weekend) are
    excluded; "everyday", "today only", and any unrecognised repetition value
    are treated as applying so we never report an on light as off.
    """
    repetition = entry.get("repetition")
    is_weekend = now.weekday() >= 5  # Mon=0 … Sun=6
    if repetition == SCHEDULE_REPETITION_WEEKDAYS:
        return not is_weekend
    if repetition == SCHEDULE_REPETITION_WEEKEND:
        return is_weekend
    return True


def _calendar_active_today(entry: dict[str, Any], now: Any) -> bool:
    """Return True if today's date falls within a calendar entry's range."""
    start = entry.get("startDate")
    end = entry.get("endDate")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return False
    try:
        start_md = (int(start["month"]), int(start["day"]))
        end_md = (int(end["month"]), int(end["day"]))
    except (KeyError, TypeError, ValueError):
        return False
    now_md = (now.month, now.day)
    if start_md <= end_md:
        return start_md <= now_md <= end_md
    # Range wraps the year end (e.g. Dec 31 -> Jan 1).
    return now_md >= start_md or now_md <= end_md


def _active_windows(data: dict[str, Any], now: Any) -> list[tuple[int, int]]:
    """Collect every on-window in effect today (daily + calendar)."""
    windows: list[tuple[int, int]] = []

    daily = data.get("daily")
    if isinstance(daily, list):
        for entry in daily:
            if not isinstance(entry, dict):
                continue
            if not entry.get("enable", True):
                continue
            if not _daily_applies_today(entry, now):
                continue
            window = _window_minutes(entry)
            if window is not None:
                windows.append(window)

    calendar = data.get("calendar")
    if isinstance(calendar, list):
        for entry in calendar:
            if not isinstance(entry, dict):
                continue
            if not _calendar_active_today(entry, now):
                continue
            window = _window_minutes(entry)
            if window is not None:
                windows.append(window)

    return windows


def _timer_schedule_is_on(data: dict[str, Any], now: Any) -> bool | None:
    """Determine on/off from the device's timer schedule.

    In timer mode the device's persisted ``switchState`` stays ``2`` whether
    the schedule currently has the lights lit or not, so the running state has
    to be derived from the schedule windows. Each daily schedule and each
    calendar event is a single on-window; the lights are on while now is inside
    any window in effect today.

    Returns:
        True/False when the schedule positively determines the state, or None
        when there are no usable windows so the caller can fall back to
        treating timer mode as on.

    Calendar events and daily schedules are treated as additive (on if inside
    any of them). If calendar events actually override daily on their dates,
    this can only ever leave a window on that the device turned off — it never
    reports an on light as off, which keeps the fallback safe.
    """
    windows = _active_windows(data, now)
    if not windows:
        return None

    now_minutes = now.hour * 60 + now.minute
    for start, end in windows:
        if _now_in_window(now_minutes, start, end):
            return True
    return False


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
        # Set initial on/off state from coordinator data so the entity
        # doesn't start as "unknown" before the first coordinator update.
        is_on = self._resolve_is_on(self.coordinator.data.get(device_id, {}))
        if is_on is not None:
            self._attr_is_on = is_on

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

    def _resolve_is_on(self, data: dict[str, Any]) -> bool | None:
        """Resolve the on/off state from coordinator data.

        Off and manual map directly from ``switchState``. In timer mode the
        persisted ``switchState`` stays ``2`` even after the schedule has
        turned the lights off, so the running state is derived from the
        schedule windows (falling back to "on" when it can't be determined).
        Returns None when there's no switch state to read.
        """
        switch_state = data.get("switchState")
        if switch_state is None:
            return None
        if switch_state == SWITCH_STATE_OFF:
            return False
        if switch_state == SWITCH_STATE_TIMER:
            scheduled = _timer_schedule_is_on(data, dt_util.now())
            if scheduled is not None:
                return scheduled
        return True

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
            is_on = self._resolve_is_on(self._data)
            if is_on is not None:
                self._attr_is_on = is_on
        self.async_write_ha_state()

    @property
    def effect_list(self) -> list[str]:
        """Return the list of available effect names."""
        return [e["name"] for e in self._effects if e.get("name")]

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
          - Plain toggle:   powers on; the device resumes its previous effect
          - ATTR_EFFECT:    activates the named saved effect
          - ATTR_HS_COLOR:  saves a solid static color effect then activates it
          - ATTR_BRIGHTNESS: updates the brightness of the current color effect
        """
        effect_name: str | None = kwargs.get(ATTR_EFFECT)
        hs_color: tuple[float, float] | None = kwargs.get(ATTR_HS_COLOR)
        brightness: int | None = kwargs.get(ATTR_BRIGHTNESS)

        _LOGGER.debug("turn_on on %s — kwargs: %s", self._device_id, kwargs)

        # view_effect/save_effect activate an effect but never touch the
        # device's persisted switchState, so a fresh power-on must set it to
        # MANUAL explicitly. Without it the shadow keeps reporting OFF and the
        # entity flips back to off once the optimistic cooldown expires.
        # Captured before the optimistic flag below overwrites it.
        needs_power_on = not self._attr_is_on

        # Optimistic update — holds for _COMMAND_COOLDOWN seconds.
        self._last_command_time = time.monotonic()
        self._attr_is_on = True
        if hs_color:
            self._attr_hs_color = hs_color
        if brightness is not None:
            self._attr_brightness = brightness
        self.async_write_ha_state()

        # Power on first when needed: setting switchState=MANUAL makes the
        # device resume its persisted effect, which would otherwise override an
        # effect/color activated beforehand.
        if effect_name is not None:
            if needs_power_on:
                await self._power_on()
            await self._activate_effect(effect_name)
        elif hs_color is not None or brightness is not None:
            if needs_power_on:
                await self._power_on()
            await self._set_color(
                hs_color if hs_color is not None else self._attr_hs_color,
                brightness if brightness is not None else self._attr_brightness,
            )
        elif needs_power_on:
            # Plain turn-on: powering on makes the device resume the effect it
            # was last showing. Re-issuing MANUAL while already on would revert
            # an effect activated via view_effect, so only do it when off.
            await self._power_on()

        # Republish state: the command helpers update _active_effect_name after
        # the optimistic write above, so without this the effect shown in HA
        # lags one selection behind until the next coordinator poll.
        self.async_write_ha_state()

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

            # Always re-activate the slot after saving. Re-saving the
            # "HA Color" effect updates the stored values but does not make the
            # controller repaint the running pattern — only view_effect does.
            # Skipping it meant same-slot changes (e.g. red -> blue) were
            # silently ignored on the device while HA state showed the new color.
            await api.view_effect(self._device_id, saved_id)
            self._active_effect_name = HA_COLOR_EFFECT_NAME
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to set color on %s", self._device_id)
            # The cached slot may be stale (e.g. the effect was deleted in
            # the Trimlight app). Drop it so the next attempt re-resolves
            # by name or creates a new slot instead of failing forever.
            self._color_effect_id = None

    async def _power_on(self) -> None:
        """Power the device on (manual mode).

        Activating an effect via view_effect/save_effect does not update the
        device's persisted switchState, so it must be set explicitly here or
        the device keeps reporting itself as off. Setting MANUAL also makes the
        device resume the effect it was last showing, which is the desired
        behaviour for a plain on/off toggle.
        """
        try:
            await self.coordinator.api.set_switch_state(
                self._device_id, SWITCH_STATE_MANUAL
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to power on %s", self._device_id)
