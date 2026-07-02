"""Trimlight Edge light platform."""

from __future__ import annotations

import asyncio
import colorsys
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import TrimlightApiError
from .const import (
    DOMAIN,
    EFFECT_CATEGORY_CUSTOM,
    EFFECT_MODE_STATIC,
    HA_COLOR_EFFECT_NAME,
    SCHEDULE_REPETITION_EVERYDAY,
    SCHEDULE_REPETITION_TODAY,
    SCHEDULE_REPETITION_WEEKDAYS,
    SCHEDULE_REPETITION_WEEKEND,
    SWITCH_STATE_MANUAL,
    SWITCH_STATE_OFF,
    SWITCH_STATE_TIMER,
)
from .coordinator import TrimlightCoordinator

if TYPE_CHECKING:
    from . import TrimlightConfigEntry

_LOGGER = logging.getLogger(__name__)

# All commands go through the API client's shared lock anyway; run entity
# updates one at a time so commands don't pile up behind each other.
PARALLEL_UPDATES = 1

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
    ``hour``/``minute``; both are accepted. Returns None for missing or
    out-of-range values so the caller can fall back to mode-based state
    instead of treating garbage as a real window.
    """
    if not isinstance(obj, dict):
        return None
    hours = obj.get("hours", obj.get("hour"))
    minutes = obj.get("minutes", obj.get("minute"))
    if hours is None or minutes is None:
        return None
    try:
        hours = int(hours)
        minutes = int(minutes)
    except (TypeError, ValueError):
        return None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes


def _now_in_window(now_minutes: int, start: int, end: int) -> bool:
    """Return True if now falls inside the [start, end) on-window.

    Handles windows that wrap past midnight (e.g. 18:00 -> 02:00).
    """
    # Zero-length windows are normally filtered out by ``_window_minutes``
    # before this is reached; this guard only matters for direct callers/tests.
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


def _daily_applies_today(entry: dict[str, Any], now: datetime) -> bool:
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
    if repetition in (SCHEDULE_REPETITION_EVERYDAY, SCHEDULE_REPETITION_TODAY):
        return True
    return True  # Unrecognised repetition: assume it applies.


def _calendar_active_today(entry: dict[str, Any], now: datetime) -> bool:
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


def _entry_vote(
    entry: dict[str, Any],
    now: datetime,
    now_minutes: int,
    applies: Callable[[dict[str, Any], datetime], bool],
) -> bool | None:
    """Vote on whether one schedule entry has the lights on right now.

    ``applies`` is a callable ``(entry, ref_date) -> bool`` returning True if the
    schedule runs on that date (day-of-week repetition for daily entries, the
    date range for calendar events).

    Returns:
        True  — now is inside this entry's on-window and it runs today.
        False — this entry is positively not lighting anything right now:
                either now is outside its window, or the window is same-day
                and the schedule doesn't run today at all.
        None  — the entry contributes nothing (disabled, no usable window, or
                a wrapping window whose day attribution is uncertain).

    A window that wraps past midnight (e.g. 22:00 -> 02:00) is split into a head
    segment that belongs to today and a tail segment that belongs to the
    schedule that started *yesterday*. When now is inside such a window but the
    segment's day doesn't apply, the vote is None rather than False: this
    assumes the device attributes a wrapping window to its start day (the night
    it begins); if the firmware instead keyed it to the end day the head/tail
    day checks would be inverted, and a False vote could report an on light as
    off. Same-day windows carry no such ambiguity, so a repetition that
    positively excludes today votes False — otherwise a weekdays-only schedule
    would leave the light reported on for the whole weekend.
    """
    if not entry.get("enable", True):
        return None
    window = _window_minutes(entry)
    if window is None:
        return None
    start, end = window
    inside = _now_in_window(now_minutes, start, end)

    if start < end:
        # Same-day window: lit only when now is inside it and the schedule
        # runs today.
        return inside and applies(entry, now)

    # Window wraps midnight.
    yesterday = now - timedelta(days=1)
    if inside:
        # The head segment (now >= start) belongs to today; the tail segment
        # (now < end) belongs to the run that started yesterday.
        ref = now if now_minutes >= start else yesterday
        return True if applies(entry, ref) else None
    # Outside the window entirely: this entry's lights are off right now no
    # matter which day the window is keyed to.
    return False


def _timer_schedule_is_on(data: dict[str, Any], now: datetime) -> bool | None:
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
    now_minutes = now.hour * 60 + now.minute
    decided = False
    for source, applies in (
        ("daily", _daily_applies_today),
        ("calendar", _calendar_active_today),
    ):
        entries = data.get(source)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            vote = _entry_vote(entry, now, now_minutes, applies)
            if vote is None:
                continue
            if vote:
                return True
            decided = True

    # No window said "on". If at least one applicable window said "off" the
    # schedule positively determines off; otherwise it's indeterminate.
    return False if decided else None


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
    entry: TrimlightConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one light entity per Trimlight device.

    Devices added to the account later are picked up from coordinator updates,
    so a new controller appears without reloading the integration.
    """
    coordinator = entry.runtime_data
    known_ids: set[str] = set()

    def _add_new_devices() -> None:
        new_ids = set(coordinator.data or {}) - known_ids
        if new_ids:
            known_ids.update(new_ids)
            async_add_entities(
                TrimlightLight(coordinator, device_id)
                for device_id in sorted(new_ids)
            )

    _add_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))


class TrimlightLight(
    CoordinatorEntity[TrimlightCoordinator], LightEntity, RestoreEntity
):
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
        # Serializes this entity's commands: the check-then-act on the cached
        # color slot id would otherwise race when e.g. dragging the color
        # wheel fires several service calls, creating duplicate effects.
        self._command_lock = asyncio.Lock()
        # Defaults so HA's frontend renders the color picker on first load.
        self._attr_hs_color = (0.0, 0.0)
        self._attr_brightness = 255
        # Set initial on/off state from coordinator data so the entity
        # doesn't start as "unknown" before the first coordinator update.
        is_on = self._resolve_is_on(self.coordinator.data.get(device_id, {}))
        if is_on is not None:
            self._attr_is_on = is_on

    async def async_added_to_hass(self) -> None:
        """Restore command-derived state the API can't report back.

        The device detail doesn't identify the running effect or the color
        the user last picked, so without restoring them every HA restart
        would blank the effect and reset the color slot to white.
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        effect = last.attributes.get(ATTR_EFFECT)
        if effect:
            self._active_effect_name = effect
        hs_color = last.attributes.get(ATTR_HS_COLOR)
        if isinstance(hs_color, (list, tuple)) and len(hs_color) == 2:
            self._attr_hs_color = (float(hs_color[0]), float(hs_color[1]))
        brightness = last.attributes.get(ATTR_BRIGHTNESS)
        if isinstance(brightness, (int, float)):
            self._attr_brightness = int(brightness)

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
        # Only derive the timer running-state from the schedule when the detail
        # is live. While a detail fetch is failing the carried-forward schedule
        # is stale and may no longer match the device, so fall back to "on".
        if switch_state == SWITCH_STATE_TIMER and not data.get("_detail_stale"):
            scheduled = _timer_schedule_is_on(data, dt_util.now())
            if scheduled is not None:
                return scheduled
        return True

    def _state_snapshot(self) -> tuple[Any, ...]:
        """Capture the optimistic-state fields for rollback on failure."""
        return (
            self._attr_is_on,
            self._attr_hs_color,
            self._attr_brightness,
            self._active_effect_name,
        )

    def _restore_snapshot(self, snapshot: tuple[Any, ...]) -> None:
        """Roll back a failed command's optimistic write.

        Also drops the command cooldown so the next poll re-syncs immediately
        — the command may have partially applied (e.g. power-on succeeded but
        the effect activation failed).
        """
        (
            self._attr_is_on,
            self._attr_hs_color,
            self._attr_brightness,
            self._active_effect_name,
        ) = snapshot
        self._last_command_time = 0.0
        self.async_write_ha_state()

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
        """Return True if polling works and the device is online.

        ``super().available`` carries coordinator.last_update_success, so a
        cloud outage marks the entity unavailable instead of leaving it
        "available" with frozen state (which the README explicitly promises
        not to do).
        """
        return super().available and self._data.get("connectivity", 0) == 1

    def _handle_coordinator_update(self) -> None:
        """Sync state from coordinator, respecting the optimistic cooldown."""
        if time.monotonic() - self._last_command_time > _COMMAND_COOLDOWN:
            data = self._data
            is_on = self._resolve_is_on(data)
            if is_on is not None:
                self._attr_is_on = is_on
            # Brightness is the one command-set attribute the device reports
            # back (on currentEffect), so changes made in the Trimlight app
            # flow into HA. Skip stale carried-forward detail.
            if not data.get("_detail_stale"):
                current = data.get("currentEffect")
                if isinstance(current, dict) and isinstance(
                    current.get("brightness"), int
                ):
                    self._attr_brightness = current["brightness"]
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
          - ATTR_BRIGHTNESS: re-saves whatever is showing (named effect or the
            color slot) with the new brightness — it never replaces a running
            effect with a solid color

        Raises HomeAssistantError when the cloud command fails, after rolling
        back the optimistic state, so the failure is visible in the UI instead
        of being silently swallowed.
        """
        effect_name: str | None = kwargs.get(ATTR_EFFECT)
        hs_color: tuple[float, float] | None = kwargs.get(ATTR_HS_COLOR)
        brightness: int | None = kwargs.get(ATTR_BRIGHTNESS)

        _LOGGER.debug("turn_on on %s — kwargs: %s", self._device_id, kwargs)

        # Validate before the optimistic write so a bad effect name fails the
        # service call without flipping the entity on.
        effect: dict[str, Any] | None = None
        if effect_name is not None:
            effect = self._effect_by_name(effect_name)
            if effect is None or effect.get("id") is None:
                raise ServiceValidationError(
                    f"Effect '{effect_name}' not found on device"
                    f" {self._device_id}"
                )
            if hs_color is not None:
                _LOGGER.debug(
                    "Ignoring hs_color on %s: effect '%s' takes precedence",
                    self._device_id,
                    effect_name,
                )

        async with self._command_lock:
            # view_effect/save_effect activate an effect but never touch the
            # device's persisted switchState, so a fresh power-on must set it
            # to MANUAL explicitly. Without it the shadow keeps reporting OFF
            # and the entity flips back to off once the cooldown expires.
            # Captured before the optimistic flag below overwrites it.
            needs_power_on = not self._attr_is_on
            snapshot = self._state_snapshot()

            # Optimistic update — holds for _COMMAND_COOLDOWN seconds. Only
            # the attributes the taken branch actually sends are written, so
            # HA never shows values the device was never asked to apply.
            self._last_command_time = time.monotonic()
            self._attr_is_on = True
            if brightness is not None:
                self._attr_brightness = brightness
            if hs_color is not None and effect is None:
                self._attr_hs_color = hs_color
            self.async_write_ha_state()

            try:
                # Power on first when needed: setting switchState=MANUAL makes
                # the device resume its persisted effect, which would otherwise
                # override an effect/color activated beforehand.
                if needs_power_on:
                    await self._power_on()
                if effect is not None:
                    await self._activate_effect(effect, brightness)
                elif hs_color is not None:
                    await self._set_color(
                        hs_color,
                        brightness
                        if brightness is not None
                        else self._attr_brightness,
                    )
                elif brightness is not None:
                    await self._apply_brightness(brightness)
            except TrimlightApiError as err:
                self._restore_snapshot(snapshot)
                raise HomeAssistantError(
                    f"Failed to turn on {self.entity_id}: {err}"
                ) from err

            # Republish state: the command helpers update _active_effect_name
            # after the optimistic write above, so without this the effect
            # shown in HA lags one selection behind until the next poll.
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light.

        Raises HomeAssistantError when the cloud command fails, after rolling
        back the optimistic state.
        """
        async with self._command_lock:
            snapshot = self._state_snapshot()
            self._last_command_time = time.monotonic()
            self._attr_is_on = False
            self.async_write_ha_state()

            try:
                await self.coordinator.api.set_switch_state(
                    self._device_id, SWITCH_STATE_OFF
                )
            except TrimlightApiError as err:
                self._restore_snapshot(snapshot)
                raise HomeAssistantError(
                    f"Failed to turn off {self.entity_id}: {err}"
                ) from err

    # ------------------------------------------------------------------ #
    # Internal command helpers                                             #
    # ------------------------------------------------------------------ #

    async def _activate_effect(
        self, effect: dict[str, Any], brightness: int | None = None
    ) -> None:
        """Activate a saved effect, optionally with a new brightness.

        view_effect repaints the strip but can't change brightness, so a
        brightness change re-saves the effect first. The save payload is
        rebuilt from the known effect fields rather than echoing the whole
        detail dict back at the API.
        """
        api = self.coordinator.api
        effect_id = effect["id"]
        if brightness is not None and effect.get("brightness") != brightness:
            payload = {
                key: effect[key]
                for key in ("id", "name", "category", "mode", "speed", "pixels")
                if key in effect
            }
            payload["brightness"] = brightness
            result = await api.save_effect(self._device_id, payload)
            effect_id = (result or {}).get("id", effect_id)
        await api.view_effect(self._device_id, effect_id)
        self._active_effect_name = effect.get("name")

    async def _apply_brightness(self, brightness: int) -> None:
        """Apply brightness to whatever the device is currently showing.

        A running named effect is re-saved with the new brightness; only when
        the color slot is (or is assumed to be) active does the brightness go
        through the solid-color path. The running effect must never be
        replaced by a solid color just because the brightness slider moved.
        """
        name = self._active_effect_name
        if name is not None and name != HA_COLOR_EFFECT_NAME:
            effect = self._effect_by_name(name)
            if effect is not None and effect.get("id") is not None:
                await self._activate_effect(effect, brightness)
                return
            _LOGGER.debug(
                "Active effect '%s' not found on %s; using the color slot",
                name,
                self._device_id,
            )
        await self._set_color(self._attr_hs_color or (0.0, 0.0), brightness)

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
            if existing and existing.get("id") is not None:
                self._color_effect_id = existing["id"]

        effect_id = (
            self._color_effect_id if self._color_effect_id is not None else -1
        )

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
        except TrimlightApiError:
            # The cached slot may be stale (e.g. the effect was deleted in
            # the Trimlight app). Drop it so the next attempt re-resolves
            # by name or creates a new slot instead of failing forever.
            self._color_effect_id = None
            raise

        saved_id = (result or {}).get("id", effect_id)
        if saved_id is None or saved_id == -1:
            # id 0 is accepted: only a missing id or the create sentinel is
            # invalid.
            raise TrimlightApiError(
                f"save_effect returned invalid id: {saved_id}"
            )
        self._color_effect_id = saved_id

        # Always re-activate the slot after saving. Re-saving the "HA Color"
        # effect updates the stored values but does not make the controller
        # repaint the running pattern — only view_effect does. The slot id
        # stays cached if this view fails: the successful save just proved
        # the slot is valid.
        await api.view_effect(self._device_id, saved_id)
        self._active_effect_name = HA_COLOR_EFFECT_NAME
        self._attr_hs_color = hs

    async def _power_on(self) -> None:
        """Power the device on (manual mode).

        Activating an effect via view_effect/save_effect does not update the
        device's persisted switchState, so it must be set explicitly here or
        the device keeps reporting itself as off. Setting MANUAL also makes the
        device resume the effect it was last showing, which is the desired
        behaviour for a plain on/off toggle.
        """
        await self.coordinator.api.set_switch_state(
            self._device_id, SWITCH_STATE_MANUAL
        )
