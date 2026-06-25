"""Tests for the Trimlight light entity."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_EFFECT, ATTR_HS_COLOR
from homeassistant.const import STATE_OFF, STATE_ON

from pytest_homeassistant_custom_component.common import MockConfigEntry

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


def _calendar_entry(start_date, end_date, start, end):
    """Build one calendar event from (month, day) and (hour, minute) tuples."""
    return {
        "effectId": 1,
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
        return hass.data[DOMAIN][entry_id]


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
    # Saturday 20:00: schedule doesn't run on weekends -> no window -> unknown.
    assert _timer_schedule_is_on(data, _SATURDAY) is None


def test_timer_schedule_weekend_repetition():
    data = {"daily": [_daily_entry((18, 0), (23, 0), repetition=3)]}
    assert _timer_schedule_is_on(data, _SATURDAY) is True
    assert _timer_schedule_is_on(data, _MONDAY) is None


def test_timer_schedule_calendar_event_in_window():
    data = {
        "daily": [],
        "calendar": [_calendar_entry((12, 24), (12, 26), (17, 0), (23, 0))],
    }
    assert _timer_schedule_is_on(data, datetime(2026, 12, 25, 18, 0)) is True
    # Same calendar date but outside the daily time window -> off.
    assert _timer_schedule_is_on(data, datetime(2026, 12, 25, 2, 0)) is False
    # Outside the date range entirely -> no window -> unknown.
    assert _timer_schedule_is_on(data, datetime(2026, 12, 20, 18, 0)) is None


def test_timer_schedule_calendar_wraps_year_end():
    data = {"calendar": [_calendar_entry((12, 31), (1, 1), (17, 0), (23, 0))]}
    assert _timer_schedule_is_on(data, datetime(2027, 1, 1, 18, 0)) is True
    assert _timer_schedule_is_on(data, datetime(2026, 12, 31, 18, 0)) is True


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

    # Slot deleted on the device — save with the cached id now fails.
    mock_api.save_effect.side_effect = TrimlightApiError("effect not found")
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
