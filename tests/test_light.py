"""Tests for the Trimlight light entity."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_EFFECT, ATTR_HS_COLOR
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.trimlight.api import TrimlightApiError
from custom_components.trimlight.const import DOMAIN, HA_COLOR_EFFECT_NAME
from custom_components.trimlight.light import (
    _now_in_window,
    _timer_schedule_is_on,
    _time_to_minutes,
)

from .conftest import MOCK_COORDINATOR_DATA, MOCK_DEVICE_ID, MOCK_DEVICE_NAME

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _timer_device(daily, *, switch_state=2):
    """Build coordinator data for a device in timer mode with a daily schedule."""
    return {
        MOCK_DEVICE_ID: {
            **MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID],
            "switchState": switch_state,
            "daily": daily,
        }
    }


def _daily_entry(start, end, *, enable=True, repetition=1):
    """Build one daily timer on-window from (hour, minute) tuples.

    repetition: 0=today only, 1=everyday, 2=week days, 3=weekend.
    """
    return {
        "enable": enable,
        "effectId": 1,
        "repetition": repetition,
        "startTime": {"hours": start[0], "minutes": start[1]},
        "endTime": {"hours": end[0], "minutes": end[1]},
    }


def _calendar_entry(start_date, end_date, start, end, *, enable=True):
    """Build one calendar event from (month, day) and (hour, minute) tuples."""
    return {
        "effectId": 1,
        "enable": enable,
        "startDate": {"month": start_date[0], "day": start_date[1]},
        "endDate": {"month": end_date[0], "day": end_date[1]},
        "startTime": {"hours": start[0], "minutes": start[1]},
        "endTime": {"hours": end[0], "minutes": end[1]},
    }


# June 2026: 22nd is a Monday (weekday), 27th is a Saturday (weekend).
_MONDAY = datetime(2026, 6, 22, 20, 0)
_SATURDAY = datetime(2026, 6, 27, 20, 0)


async def _setup_integration(hass, mock_api, *, coordinator_data=None, entry_id="test_entry", unique_id="test_client_id"):
    """Helper: set up the integration with a mocked API and return the coordinator."""
    data = coordinator_data if coordinator_data is not None else MOCK_COORDINATOR_DATA
    with (
        patch("custom_components.trimlight.TrimlightApi", return_value=mock_api),
        patch(
            "custom_components.trimlight.async_get_clientsession",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.trimlight.coordinator.TrimlightCoordinator._async_update_data",
            return_value=data,
        ),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Trimlight (test)",
            data={"client_id": "test", "client_secret": "secret"},
            entry_id=entry_id,
            unique_id=unique_id,
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry.runtime_data


def _entity_id() -> str:
    return f"light.{MOCK_DEVICE_NAME.lower().replace(' ', '_')}"


async def test_light_is_on(hass, mock_api):
    """Light should be ON when switchState is 1 (manual)."""
    await _setup_integration(hass, mock_api)
    state = hass.states.get(_entity_id())
    assert state is not None
    assert state.state == STATE_ON


async def test_light_is_off(hass, mock_api):
    """Light should be OFF when switchState is 0."""
    off_data = {
        MOCK_DEVICE_ID: {**MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID], "switchState": 0}
    }
    await _setup_integration(
        hass, mock_api,
        coordinator_data=off_data,
        entry_id="test_entry_off",
        unique_id="test_client_id_off",
    )
    state = hass.states.get(_entity_id())
    assert state is not None
    assert state.state == STATE_OFF


# --------------------------------------------------------------------------- #
# Timer-mode schedule evaluation                                              #
# --------------------------------------------------------------------------- #


def test_time_to_minutes_parses_variants():
    assert _time_to_minutes({"hours": 18, "minutes": 30}) == 18 * 60 + 30
    assert _time_to_minutes({"hour": 6, "minute": 5}) == 6 * 60 + 5
    assert _time_to_minutes({"hours": 0, "minutes": 0}) == 0
    assert _time_to_minutes(None) is None
    assert _time_to_minutes({"hours": 9}) is None
    # Out-of-range values are rejected instead of silently wrapped into a
    # valid-looking window.
    assert _time_to_minutes({"hours": 25, "minutes": 0}) is None
    assert _time_to_minutes({"hours": 10, "minutes": 75}) is None
    assert _time_to_minutes({"hours": -1, "minutes": 0}) is None


def test_now_in_window_same_day():
    # 18:00 -> 23:00
    assert _now_in_window(20 * 60, 18 * 60, 23 * 60) is True
    assert _now_in_window(17 * 60, 18 * 60, 23 * 60) is False
    assert _now_in_window(23 * 60, 18 * 60, 23 * 60) is False  # end exclusive
    assert _now_in_window(18 * 60, 18 * 60, 23 * 60) is True  # start inclusive


def test_now_in_window_wraps_midnight():
    # 18:00 -> 02:00
    assert _now_in_window(23 * 60, 18 * 60, 2 * 60) is True
    assert _now_in_window(1 * 60, 18 * 60, 2 * 60) is True
    assert _now_in_window(12 * 60, 18 * 60, 2 * 60) is False


def test_timer_schedule_inside_window():
    data = {"daily": [_daily_entry((18, 0), (23, 0))]}
    assert _timer_schedule_is_on(data, datetime(2026, 6, 24, 20, 0)) is True


def test_timer_schedule_outside_window():
    data = {"daily": [_daily_entry((18, 0), (23, 0))]}
    assert _timer_schedule_is_on(data, datetime(2026, 6, 24, 14, 0)) is False


def test_timer_schedule_no_entries_is_indeterminate():
    assert _timer_schedule_is_on({"daily": []}, datetime(2026, 6, 24, 14, 0)) is None
    assert _timer_schedule_is_on({}, datetime(2026, 6, 24, 14, 0)) is None


def test_timer_schedule_ignores_disabled_entries():
    data = {"daily": [_daily_entry((18, 0), (23, 0), enable=False)]}
    # Only entry is disabled -> nothing to evaluate -> indeterminate.
    assert _timer_schedule_is_on(data, datetime(2026, 6, 24, 20, 0)) is None


def test_timer_schedule_multiple_windows():
    data = {
        "daily": [
            _daily_entry((6, 0), (8, 0)),
            _daily_entry((18, 0), (23, 0)),
        ]
    }
    assert _timer_schedule_is_on(data, datetime(2026, 6, 24, 7, 0)) is True
    assert _timer_schedule_is_on(data, datetime(2026, 6, 24, 20, 0)) is True
    assert _timer_schedule_is_on(data, datetime(2026, 6, 24, 12, 0)) is False


def test_timer_schedule_weekdays_repetition():
    data = {"daily": [_daily_entry((18, 0), (23, 0), repetition=2)]}
    # Monday 20:00 is inside the window on a week day.
    assert _timer_schedule_is_on(data, _MONDAY) is True
    # Saturday 20:00: a same-day weekdays schedule positively doesn't run on
    # weekends, so the lights are off — not indeterminate (which would have
    # shown the light as on all weekend).
    assert _timer_schedule_is_on(data, _SATURDAY) is False


def test_timer_schedule_weekend_repetition():
    data = {"daily": [_daily_entry((18, 0), (23, 0), repetition=3)]}
    assert _timer_schedule_is_on(data, _SATURDAY) is True
    assert _timer_schedule_is_on(data, _MONDAY) is False


def test_timer_schedule_calendar_event_in_window():
    data = {
        "daily": [],
        "calendar": [_calendar_entry((12, 24), (12, 26), (17, 0), (23, 0))],
    }
    assert _timer_schedule_is_on(data, datetime(2026, 12, 25, 18, 0)) is True
    # Same calendar date but outside the daily time window -> off.
    assert _timer_schedule_is_on(data, datetime(2026, 12, 25, 2, 0)) is False
    # Outside the date range entirely: the event positively isn't running, so
    # a same-day window reports off rather than indeterminate.
    assert _timer_schedule_is_on(data, datetime(2026, 12, 20, 18, 0)) is False


def test_timer_schedule_calendar_wraps_year_end():
    data = {"calendar": [_calendar_entry((12, 31), (1, 1), (17, 0), (23, 0))]}
    assert _timer_schedule_is_on(data, datetime(2027, 1, 1, 18, 0)) is True
    assert _timer_schedule_is_on(data, datetime(2026, 12, 31, 18, 0)) is True


def test_timer_schedule_ignores_disabled_calendar_entries():
    # A disabled calendar event must not be treated as an active on-window,
    # the same way disabled daily entries are skipped.
    data = {
        "daily": [],
        "calendar": [_calendar_entry((12, 24), (12, 26), (17, 0), (23, 0), enable=False)],
    }
    assert _timer_schedule_is_on(data, datetime(2026, 12, 25, 18, 0)) is None


def test_timer_schedule_weekend_window_wraps_midnight():
    # Weekend schedule 22:00 -> 02:00. The lit tail after midnight belongs to
    # the night that started the previous (weekend) day.
    data = {"daily": [_daily_entry((22, 0), (2, 0), repetition=3)]}
    # Sunday 23:00 — head of a weekend night -> on.
    assert _timer_schedule_is_on(data, datetime(2026, 6, 21, 23, 0)) is True
    # Monday 01:00 — tail of Sunday's weekend night -> on (not dropped).
    assert _timer_schedule_is_on(data, datetime(2026, 6, 22, 1, 0)) is True
    # Monday 23:00 — Monday is not a weekend night, so the schedule isn't
    # running -> indeterminate, not a spurious on.
    assert _timer_schedule_is_on(data, datetime(2026, 6, 22, 23, 0)) is None


def test_timer_schedule_weekday_window_wraps_midnight():
    # Weekday schedule 22:00 -> 02:00. Saturday 01:00 is the tail of Friday's
    # (week day) night, so the lights are on.
    data = {"daily": [_daily_entry((22, 0), (2, 0), repetition=2)]}
    assert _timer_schedule_is_on(data, datetime(2026, 6, 27, 1, 0)) is True
    # Saturday 23:00 is a weekend night -> schedule not running -> indeterminate.
    assert _timer_schedule_is_on(data, datetime(2026, 6, 27, 23, 0)) is None


def test_timer_schedule_everyday_window_wraps_midnight():
    data = {"daily": [_daily_entry((22, 0), (2, 0))]}
    assert _timer_schedule_is_on(data, datetime(2026, 6, 24, 1, 0)) is True
    assert _timer_schedule_is_on(data, datetime(2026, 6, 24, 12, 0)) is False


def test_timer_schedule_zero_length_window_is_indeterminate():
    # A blank/all-day slot (start == end) must not flip the light to off.
    data = {"daily": [_daily_entry((0, 0), (0, 0))]}
    assert _timer_schedule_is_on(data, datetime(2026, 6, 24, 20, 0)) is None
    # A real window alongside a zero-length one still evaluates normally.
    data = {
        "daily": [
            _daily_entry((0, 0), (0, 0)),
            _daily_entry((18, 0), (23, 0)),
        ]
    }
    assert _timer_schedule_is_on(data, datetime(2026, 6, 24, 14, 0)) is False
    assert _timer_schedule_is_on(data, datetime(2026, 6, 24, 20, 0)) is True


async def test_timer_mode_shows_off_when_schedule_off(hass, mock_api):
    """Timer mode with the lights outside their on-window must report OFF.

    Regression test for #10: the device keeps switchState=2 (timer) even after
    the schedule turns the lights off, so HA must derive off from the schedule.
    """
    data = _timer_device([_daily_entry((18, 0), (23, 0))])
    with patch(
        "custom_components.trimlight.light.dt_util.now",
        return_value=datetime(2026, 6, 24, 14, 0),
    ):
        await _setup_integration(
            hass, mock_api,
            coordinator_data=data,
            entry_id="test_entry_timer_off",
            unique_id="test_client_id_timer_off",
        )
        state = hass.states.get(_entity_id())
    assert state.state == STATE_OFF


async def test_timer_mode_shows_on_when_schedule_on(hass, mock_api):
    """Timer mode with the lights inside their on-window must report ON."""
    data = _timer_device([_daily_entry((18, 0), (23, 0))])
    with patch(
        "custom_components.trimlight.light.dt_util.now",
        return_value=datetime(2026, 6, 24, 20, 0),
    ):
        await _setup_integration(
            hass, mock_api,
            coordinator_data=data,
            entry_id="test_entry_timer_on",
            unique_id="test_client_id_timer_on",
        )
        state = hass.states.get(_entity_id())
    assert state.state == STATE_ON


async def test_timer_mode_falls_back_to_on_without_schedule(hass, mock_api):
    """Timer mode with no usable schedule must keep the previous on behaviour."""
    data = _timer_device([])
    with patch(
        "custom_components.trimlight.light.dt_util.now",
        return_value=datetime(2026, 6, 24, 14, 0),
    ):
        await _setup_integration(
            hass, mock_api,
            coordinator_data=data,
            entry_id="test_entry_timer_nosched",
            unique_id="test_client_id_timer_nosched",
        )
        state = hass.states.get(_entity_id())
    assert state.state == STATE_ON


async def test_timer_mode_stale_detail_falls_back_to_on(hass, mock_api):
    """A stale (carried-forward) schedule must not be trusted to report OFF.

    While a detail fetch is failing the schedule is last-known, not live, so
    timer mode reverts to the safe "on" reading instead of deriving off from a
    schedule that may no longer match the device.
    """
    data = _timer_device([_daily_entry((18, 0), (23, 0))])
    data[MOCK_DEVICE_ID]["_detail_stale"] = True
    with patch(
        "custom_components.trimlight.light.dt_util.now",
        return_value=datetime(2026, 6, 24, 14, 0),  # outside the on-window
    ):
        await _setup_integration(
            hass, mock_api,
            coordinator_data=data,
            entry_id="test_entry_timer_stale",
            unique_id="test_client_id_timer_stale",
        )
        state = hass.states.get(_entity_id())
    assert state.state == STATE_ON


async def test_effect_list_populated(hass, mock_api):
    """All saved effects should appear in the effect_list attribute."""
    await _setup_integration(hass, mock_api)
    state = hass.states.get(_entity_id())
    effect_list = state.attributes.get("effect_list", [])
    assert "NEW YEAR" in effect_list
    assert "INDEPENDENCE DAY" in effect_list


async def test_effect_list_skips_unnamed_effects(hass, mock_api):
    """Malformed effects without a name must not break the entity state."""
    device = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]
    data = {
        MOCK_DEVICE_ID: {
            **device,
            "effects": [*device["effects"], {"id": 3}],
        }
    }
    await _setup_integration(
        hass, mock_api,
        coordinator_data=data,
        entry_id="test_entry_unnamed",
        unique_id="test_client_id_unnamed",
    )
    state = hass.states.get(_entity_id())
    assert state.attributes["effect_list"] == ["NEW YEAR", "INDEPENDENCE DAY"]


async def test_plain_turn_on_while_on_is_noop(hass, mock_api):
    """Plain turn_on on an already-on device must not change the effect.

    Re-issuing MANUAL would revert an effect activated via view_effect, so a
    no-op is the correct behaviour here.
    """
    await _setup_integration(hass, mock_api)
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": _entity_id()}, blocking=True
    )
    mock_api.view_effect.assert_not_called()
    mock_api.set_switch_state.assert_not_called()


async def test_plain_turn_on_while_off_powers_on(hass, mock_api):
    """Plain turn_on on an off device should power it on via switchState=MANUAL.

    It must NOT force the first saved effect (the device resumes the effect it
    was last showing), which is what previously caused a wrong/static effect.
    """
    off_data = {
        MOCK_DEVICE_ID: {**MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID], "switchState": 0}
    }
    await _setup_integration(
        hass, mock_api,
        coordinator_data=off_data,
        entry_id="test_entry_plain_off",
        unique_id="test_client_id_plain_off",
    )
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": _entity_id()}, blocking=True
    )
    mock_api.set_switch_state.assert_called_once_with(MOCK_DEVICE_ID, 1)
    mock_api.view_effect.assert_not_called()


async def test_turn_on_with_effect(hass, mock_api):
    """Turn on with ATTR_EFFECT should call view_effect with the correct ID."""
    await _setup_integration(hass, mock_api)
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_EFFECT: "INDEPENDENCE DAY"},
        blocking=True,
    )
    mock_api.view_effect.assert_called_with(MOCK_DEVICE_ID, 2)


async def test_turn_on_with_effect_publishes_effect_immediately(hass, mock_api):
    """The active effect must be reflected in HA state as soon as the call returns.

    Regression test: the effect name used to lag one selection behind because
    state was only written before the effect was activated.
    """
    await _setup_integration(hass, mock_api)
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_EFFECT: "INDEPENDENCE DAY"},
        blocking=True,
    )
    state = hass.states.get(_entity_id())
    assert state.attributes.get("effect") == "INDEPENDENCE DAY"


async def test_turn_on_effect_while_off_powers_on_then_activates(hass, mock_api):
    """Activating an effect on an off device must power on, then view the effect."""
    off_data = {
        MOCK_DEVICE_ID: {**MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID], "switchState": 0}
    }
    await _setup_integration(
        hass, mock_api,
        coordinator_data=off_data,
        entry_id="test_entry_effect_off",
        unique_id="test_client_id_effect_off",
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_EFFECT: "INDEPENDENCE DAY"},
        blocking=True,
    )
    # Power on must happen before the effect is activated, otherwise MANUAL
    # would override it with the device's persisted effect.
    mock_api.set_switch_state.assert_called_once_with(MOCK_DEVICE_ID, 1)
    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 2)
    ordered = [
        c[0] for c in mock_api.mock_calls
        if c[0] in ("set_switch_state", "view_effect")
    ]
    assert ordered == ["set_switch_state", "view_effect"]


async def test_turn_on_with_color(hass, mock_api):
    """Turn on with ATTR_HS_COLOR should save and view a color effect."""
    await _setup_integration(hass, mock_api)
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_HS_COLOR: (0.0, 100.0)},
        blocking=True,
    )
    # Should save_effect with the color and then view_effect.
    mock_api.save_effect.assert_called_once()
    save_args = mock_api.save_effect.call_args
    payload = save_args[0][1]
    assert payload["name"] == HA_COLOR_EFFECT_NAME
    assert payload["category"] == 2
    assert payload["mode"] == 0
    assert payload["pixels"][0]["color"] == 16711680  # pure red

    mock_api.view_effect.assert_called_with(MOCK_DEVICE_ID, 99)


async def test_turn_on_with_brightness(hass, mock_api):
    """Turn on with ATTR_BRIGHTNESS should save the color effect with that brightness."""
    await _setup_integration(hass, mock_api)
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_BRIGHTNESS: 128},
        blocking=True,
    )
    mock_api.save_effect.assert_called_once()
    save_args = mock_api.save_effect.call_args
    assert save_args[0][1]["brightness"] == 128


async def test_turn_off(hass, mock_api):
    """Turn off should call set_switch_state with OFF."""
    await _setup_integration(hass, mock_api)
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": _entity_id()}, blocking=True
    )
    mock_api.set_switch_state.assert_called_once_with(MOCK_DEVICE_ID, 0)


async def test_color_change_always_repaints(hass, mock_api):
    """Every solid-color change must re-activate the slot so the device repaints.

    Regression test: changing from one solid color to another (same slot) used
    to call save_effect only, which updates the stored values but does not make
    the controller repaint — the new color was silently ignored on the device
    while HA state showed it. Each change must call view_effect too.
    """
    await _setup_integration(hass, mock_api)

    # First color — should call both save_effect and view_effect.
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_HS_COLOR: (0.0, 100.0)},
        blocking=True,
    )
    assert mock_api.view_effect.call_count == 1
    assert mock_api.save_effect.call_count == 1

    # Second color — same slot, but must still re-activate to repaint.
    mock_api.view_effect.reset_mock()
    mock_api.save_effect.reset_mock()
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_HS_COLOR: (120.0, 100.0)},
        blocking=True,
    )
    assert mock_api.save_effect.call_count == 1
    assert mock_api.view_effect.call_count == 1


async def test_color_recovers_after_stale_effect_id(hass, mock_api):
    """A failed save (e.g. slot deleted in the app) must not break color forever."""
    await _setup_integration(hass, mock_api)

    # First color set — caches slot id 99.
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_HS_COLOR: (0.0, 100.0)},
        blocking=True,
    )
    assert mock_api.save_effect.call_args[0][1]["id"] == -1

    # Slot deleted on the device — save with the cached id now fails, which
    # surfaces to the caller instead of reporting a silent success.
    mock_api.save_effect.side_effect = TrimlightApiError("effect not found")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "light",
            "turn_on",
            {"entity_id": _entity_id(), ATTR_HS_COLOR: (120.0, 100.0)},
            blocking=True,
        )
    assert mock_api.save_effect.call_args[0][1]["id"] == 99

    # Next attempt should create a new slot and activate it.
    mock_api.save_effect.side_effect = None
    mock_api.save_effect.return_value = {"id": 123}
    mock_api.view_effect.reset_mock()
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_HS_COLOR: (240.0, 100.0)},
        blocking=True,
    )
    assert mock_api.save_effect.call_args[0][1]["id"] == -1
    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 123)


# --------------------------------------------------------------------------- #
# Command failure handling, brightness routing, availability, late devices    #
# --------------------------------------------------------------------------- #


async def test_turn_off_failure_raises_and_reverts(hass, mock_api):
    """A failed turn_off must raise instead of reporting silent success.

    The optimistic OFF is rolled back so HA keeps showing the true state
    instead of lying for the length of the command cooldown.
    """
    await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_off_fail",
        unique_id="test_client_id_off_fail",
    )
    mock_api.set_switch_state.side_effect = TrimlightApiError("cloud down")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "light", "turn_off", {"entity_id": _entity_id()}, blocking=True
        )
    assert hass.states.get(_entity_id()).state == STATE_ON


async def test_turn_on_failure_raises_and_reverts(hass, mock_api):
    """A failed power-on must raise and roll the optimistic ON back."""
    off_data = {
        MOCK_DEVICE_ID: {**MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID], "switchState": 0}
    }
    await _setup_integration(
        hass, mock_api,
        coordinator_data=off_data,
        entry_id="test_entry_on_fail",
        unique_id="test_client_id_on_fail",
    )
    mock_api.set_switch_state.side_effect = TrimlightApiError("cloud down")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": _entity_id()}, blocking=True
        )
    assert hass.states.get(_entity_id()).state == STATE_OFF


async def test_unknown_effect_raises_validation_error(hass, mock_api):
    """Activating a nonexistent effect fails the service call up front."""
    await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_bad_effect",
        unique_id="test_client_id_bad_effect",
    )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "light",
            "turn_on",
            {"entity_id": _entity_id(), ATTR_EFFECT: "DOES NOT EXIST"},
            blocking=True,
        )
    mock_api.view_effect.assert_not_called()
    mock_api.set_switch_state.assert_not_called()


async def test_brightness_only_preserves_running_effect(hass, mock_api):
    """The brightness slider must not replace a running effect with a color.

    Regression test: a brightness-only turn_on used to fall into the color
    branch and overwrite the active saved effect with a solid 'HA Color'
    effect (white, on a fresh entity).
    """
    await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_bri_effect",
        unique_id="test_client_id_bri_effect",
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_EFFECT: "NEW YEAR"},
        blocking=True,
    )
    mock_api.save_effect.reset_mock()
    mock_api.view_effect.reset_mock()

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_BRIGHTNESS: 100},
        blocking=True,
    )
    payload = mock_api.save_effect.call_args[0][1]
    assert payload["name"] == "NEW YEAR"
    assert payload["brightness"] == 100
    state = hass.states.get(_entity_id())
    assert state.attributes.get("effect") == "NEW YEAR"
    assert state.attributes.get("brightness") == 100


async def test_turn_on_effect_with_brightness_applies_both(hass, mock_api):
    """Effect + brightness in one call applies the brightness to that effect."""
    await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_effect_bri",
        unique_id="test_client_id_effect_bri",
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {
            "entity_id": _entity_id(),
            ATTR_EFFECT: "INDEPENDENCE DAY",
            ATTR_BRIGHTNESS: 42,
        },
        blocking=True,
    )
    payload = mock_api.save_effect.call_args[0][1]
    assert payload["name"] == "INDEPENDENCE DAY"
    assert payload["brightness"] == 42
    # A custom effect is re-saved with its pixel pattern intact.
    expected_pixels = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]["effects"][1]["pixels"]
    assert payload["pixels"] == expected_pixels
    state = hass.states.get(_entity_id())
    assert state.attributes.get("effect") == "INDEPENDENCE DAY"
    assert state.attributes.get("brightness") == 42


# A built-in (category 1) effect as the device reports it: the pattern is
# identified by ``mode`` and sized by ``pixelLen``/``reverse``. There is no
# ``pixels`` array, unlike the custom (category 2) effects in the fixture.
_BUILTIN_EFFECT = {
    "id": 3,
    "name": "Green Cyan Wave",
    "category": 1,
    "mode": 3,
    "speed": 100,
    "brightness": 255,
    "pixelLen": 30,
    "reverse": False,
}


def _with_builtin_effect():
    """Coordinator data with a built-in effect saved alongside the custom ones."""
    device = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]
    return {
        MOCK_DEVICE_ID: {
            **device,
            "effects": [*device["effects"], _BUILTIN_EFFECT],
        }
    }


async def test_builtin_effect_with_brightness_resaves_pixel_len(hass, mock_api):
    """Re-saving a built-in effect must forward its pixelLen/reverse fields.

    Regression test for #18: effect + brightness on a built-in effect failed
    with "API error 20000: The parameter [pixelLen] is required" because the
    save payload only forwarded ``pixels``, which built-in effects don't have.
    """
    await _setup_integration(
        hass, mock_api,
        coordinator_data=_with_builtin_effect(),
        entry_id="test_entry_builtin_bri",
        unique_id="test_client_id_builtin_bri",
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {
            "entity_id": _entity_id(),
            ATTR_EFFECT: "Green Cyan Wave",
            ATTR_BRIGHTNESS: 204,
        },
        blocking=True,
    )
    mock_api.save_effect.assert_called_once()
    payload = mock_api.save_effect.call_args[0][1]
    assert payload == {
        "id": 3,
        "name": "Green Cyan Wave",
        "category": 1,
        "mode": 3,
        "speed": 100,
        "pixelLen": 30,
        "reverse": False,
        "brightness": 204,
    }
    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 99)
    state = hass.states.get(_entity_id())
    assert state.attributes.get("effect") == "Green Cyan Wave"
    assert state.attributes.get("brightness") == 204


async def test_brightness_only_on_builtin_effect_resaves_pixel_len(hass, mock_api):
    """The brightness slider on a running built-in effect re-saves it intact."""
    await _setup_integration(
        hass, mock_api,
        coordinator_data=_with_builtin_effect(),
        entry_id="test_entry_builtin_slider",
        unique_id="test_client_id_builtin_slider",
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_EFFECT: "Green Cyan Wave"},
        blocking=True,
    )
    # Plain activation only views the effect; nothing to re-save yet.
    mock_api.save_effect.assert_not_called()

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_BRIGHTNESS: 100},
        blocking=True,
    )
    payload = mock_api.save_effect.call_args[0][1]
    assert payload["name"] == "Green Cyan Wave"
    assert payload["brightness"] == 100
    assert payload["pixelLen"] == 30
    assert payload["reverse"] is False
    assert "pixels" not in payload


async def test_unavailable_when_cloud_update_fails(hass, mock_api):
    """A failing coordinator update must mark the entity unavailable."""
    coordinator = await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_cloud_down",
        unique_id="test_client_id_cloud_down",
    )
    assert hass.states.get(_entity_id()).state == STATE_ON

    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(_entity_id()).state == STATE_UNAVAILABLE


async def test_new_device_added_after_setup(hass, mock_api):
    """A device that appears in a later poll gets an entity without reload."""
    coordinator = await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_late_device",
        unique_id="test_client_id_late_device",
    )
    assert hass.states.get("light.back_yard") is None

    new_device = {
        **MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID],
        "deviceId": "device_late",
        "name": "Back Yard",
    }
    coordinator.async_set_updated_data(
        {**coordinator.data, "device_late": new_device}
    )
    await hass.async_block_till_done()
    assert hass.states.get("light.back_yard") is not None


async def test_restores_state_after_restart(hass, mock_api):
    """Effect, color, and brightness survive an HA restart via restore."""
    mock_restore_cache(
        hass,
        [
            State(
                _entity_id(),
                STATE_ON,
                {
                    ATTR_EFFECT: "NEW YEAR",
                    ATTR_HS_COLOR: [120.0, 100.0],
                    ATTR_BRIGHTNESS: 42,
                },
            )
        ],
    )
    await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_restore",
        unique_id="test_client_id_restore",
    )
    state = hass.states.get(_entity_id())
    assert state.attributes.get(ATTR_EFFECT) == "NEW YEAR"
    assert state.attributes.get(ATTR_BRIGHTNESS) == 42
    assert tuple(state.attributes.get(ATTR_HS_COLOR)) == (120.0, 100.0)
