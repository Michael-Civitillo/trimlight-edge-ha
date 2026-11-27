"""Tests for the Trimlight light entity."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_EFFECT, ATTR_HS_COLOR
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from custom_components.trimlight.api import TrimlightApiError, TrimlightAuthError
from custom_components.trimlight.const import (
    DOMAIN,
    EFFECT_CATEGORY_BUILTIN,
    EFFECT_CATEGORY_CUSTOM,
    HA_COLOR_EFFECT_NAME,
)
from custom_components.trimlight.light import (
    _api_color_to_hs,
    _effect_category,
    _hs_to_api_color,
    _match_running_effect,
    _running_color,
    _now_in_window,
    _scheduled_effect_ids,
    _timer_schedule_is_on,
    _time_to_minutes,
)

from .conftest import (
    MOCK_COORDINATOR_DATA,
    MOCK_DEVICE_ID,
    MOCK_DEVICE_LIST,
    MOCK_DEVICE_NAME,
)

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
    assert _time_to_minutes({"hours": 24, "minutes": 1}) is None


def test_time_to_minutes_reads_midnight_at_the_end_of_the_day():
    """24:00 is midnight, not a broken value.

    Regression test: the API documents hours as [1, 24], so a timer ending at
    midnight reports 24:00. Rejecting it threw the whole window away, which
    left Home Assistant reporting the light on all day.
    """
    assert _time_to_minutes({"hours": 24, "minutes": 0}) == 0
    # A carried 23:60 is the same instant.
    assert _time_to_minutes({"hours": 23, "minutes": 60}) == 0
    # Minutes of 60 roll into the next hour the same way.
    assert _time_to_minutes({"hours": 10, "minutes": 60}) == 11 * 60
    # And the ordinary values still parse as before.
    assert _time_to_minutes({"hours": 0, "minutes": 0}) == 0
    assert _time_to_minutes({"hours": 23, "minutes": 59}) == 23 * 60 + 59


def test_timer_window_ending_at_midnight_is_honoured():
    """An evening schedule ending at 24:00 must drive on/off, not be dropped."""
    data = {"daily": [_daily_entry((18, 0), (24, 0))]}
    assert _timer_schedule_is_on(data, datetime(2026, 6, 24, 12, 0)) is False
    assert _timer_schedule_is_on(data, datetime(2026, 6, 24, 17, 59)) is False
    assert _timer_schedule_is_on(data, datetime(2026, 6, 24, 18, 0)) is True
    assert _timer_schedule_is_on(data, datetime(2026, 6, 24, 23, 59)) is True
    # Midnight is the end of the window, and the end is exclusive.
    assert _timer_schedule_is_on(data, datetime(2026, 6, 24, 0, 0)) is False


async def test_timer_mode_off_outside_a_window_ending_at_midnight(hass, mock_api):
    """The entity reports off outside an evening window that ends at 24:00.

    Before this, such a window was discarded, so timer mode fell back to
    reporting the light on around the clock.
    """
    data = _timer_device([_daily_entry((18, 0), (24, 0))])
    with patch(
        "custom_components.trimlight.light.dt_util.now",
        return_value=datetime(2026, 6, 24, 12, 0),
    ):
        await _setup_integration(
            hass, mock_api, coordinator_data=data,
            entry_id="test_entry_midnight", unique_id="test_client_id_midnight",
        )
        assert hass.states.get(_entity_id()).state == STATE_OFF


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


def _command_order(mock_api):
    """Return the names of the commands sent to the controller, in order."""
    return [
        c[0] for c in mock_api.mock_calls
        if c[0] in ("set_switch_state", "save_effect", "view_effect")
    ]


async def test_plain_turn_on_always_powers_on(hass, mock_api):
    """A plain turn_on must power the device on even when HA thinks it is on.

    Regression test: HA's belief comes from a poll up to 30 seconds old, or
    from optimistic state, so trusting it meant a plain turn_on could send
    nothing at all. Users saw the lights stay off while HA showed them on,
    and only picking an effect or a colour actually turned them on.

    The power-on is all a light that is already on is sent, though. Viewing
    the effect HA has for it again would restart its animation, put a
    brightness dimmed in the Trimlight app back to the saved value, and pin
    one member of a running rotation.
    """
    await _setup_integration(hass, mock_api)
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": _entity_id()}, blocking=True
    )
    mock_api.set_switch_state.assert_called_once_with(MOCK_DEVICE_ID, 1)
    mock_api.view_effect.assert_not_called()
    mock_api.save_effect.assert_not_called()


async def test_plain_turn_on_while_off_reactivates_the_last_effect(hass, mock_api):
    """A plain turn_on must tell the controller what to show, not just power on.

    Regression test: a plain turn_on only set switchState=MANUAL and trusted
    the controller to resume its last pattern. On real hardware the cloud
    accepted that, Home Assistant showed the light on, and the strip stayed
    dark; a brightness change did not help either, and only selecting an
    effect lit it. Selecting an effect is the same power-on followed by
    view_effect, so a plain turn_on now ends the same way.

    The effect is the one the controller last showed rather than the first in
    the list: forcing the first one is what once left strips on a wrong or
    static pattern.
    """
    device = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]
    independence_day = device["effects"][1]
    off_data = {
        MOCK_DEVICE_ID: {
            **device,
            "switchState": 0,
            "currentEffect": {
                k: v for k, v in independence_day.items() if k not in ("id", "name")
            },
        }
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
    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 2)
    mock_api.save_effect.assert_not_called()
    # Power on first: switching to MANUAL afterwards would make the controller
    # fall back to whatever it has persisted, overriding the effect.
    assert _command_order(mock_api) == ["set_switch_state", "view_effect"]
    state = hass.states.get(_entity_id())
    assert state.state == STATE_ON
    assert state.attributes.get(ATTR_EFFECT) == "INDEPENDENCE DAY"


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
    # Regression test for #23: the cloud accepts only 0 (built-in) and
    # 1 (custom) and rejects the 2 that used to be sent with error 12008.
    assert payload["category"] == EFFECT_CATEGORY_CUSTOM == 1
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

    # Slot deleted on the device — the cloud answers with a result code, which
    # surfaces to the caller instead of reporting a silent success.
    mock_api.save_effect.side_effect = TrimlightApiError(
        "API error 12004: effect not found", code=12004
    )
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


async def test_transient_save_failure_keeps_the_colour_slot(hass, mock_api):
    """A timeout must not make the next colour pick create a second slot.

    Regression test: the cached slot id used to be dropped on any error,
    including a transport failure that says nothing about whether the slot
    still exists. With no poll in between there is no way to re-resolve it by
    name, so the retry created a duplicate "HA Color" effect — the
    duplicate-name condition that breaks the cloud's parsing.
    """
    await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_transient", unique_id="test_client_id_transient",
    )
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": _entity_id(), ATTR_HS_COLOR: (0.0, 100.0)}, blocking=True,
    )
    assert mock_api.save_effect.call_args[0][1]["id"] == -1

    # A transport failure carries no API result code.
    mock_api.save_effect.side_effect = TrimlightApiError(
        "Request to /path failed: TimeoutError"
    )
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "light", "turn_on",
            {"entity_id": _entity_id(), ATTR_HS_COLOR: (120.0, 100.0)},
            blocking=True,
        )

    mock_api.save_effect.side_effect = None
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": _entity_id(), ATTR_HS_COLOR: (240.0, 100.0)}, blocking=True,
    )
    # Still the known slot, not a blind create.
    assert mock_api.save_effect.call_args[0][1]["id"] == 99


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


# A built-in (category 0) effect as the device reports it: the pattern is
# identified by ``mode`` and sized by ``pixelLen``/``reverse``. There is no
# ``pixels`` array, unlike the custom (category 1) effects in the fixture.
_BUILTIN_EFFECT = {
    "id": 3,
    "name": "Green Cyan Wave",
    "category": EFFECT_CATEGORY_BUILTIN,
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
        "category": EFFECT_CATEGORY_BUILTIN,
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


def test_effect_category_derived_from_effect_shape():
    """The save category comes from the effect's shape, not the reported number.

    Regression test for #23: devices have reported custom effects as
    category 2, which the cloud rejects on save (only 0 built-in / 1 custom
    are valid), so re-saving such an effect for a brightness change failed.
    """
    custom = {"category": 2, "pixels": [{"index": 0, "count": 1, "color": 255}]}
    builtin = {"category": 1, "pixelLen": 30, "reverse": False}
    assert _effect_category(custom) == EFFECT_CATEGORY_CUSTOM
    assert _effect_category(builtin) == EFFECT_CATEGORY_BUILTIN
    # Already-correct numbering is left as is.
    assert _effect_category({**custom, "category": 1}) == EFFECT_CATEGORY_CUSTOM
    assert _effect_category({**builtin, "category": 0}) == EFFECT_CATEGORY_BUILTIN
    # An ambiguous shape (both markers, or neither) falls back to what the
    # device reported, and to nothing when it reported nothing.
    assert _effect_category({"category": 7, "pixels": [{}], "pixelLen": 30}) == 7
    assert _effect_category({"category": 1, "pixels": []}) == 1
    assert _effect_category({"name": "bare"}) is None


def _with_legacy_categories():
    """Coordinator data whose effects use the old 1=built-in / 2=custom numbering."""
    device = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]
    return {
        MOCK_DEVICE_ID: {
            **device,
            "effects": [
                {**device["effects"][1], "category": 2},
                {**_BUILTIN_EFFECT, "category": 1},
            ],
        }
    }


async def test_resave_sends_category_the_cloud_accepts(hass, mock_api):
    """Re-saving an effect for a brightness change never echoes a rejected category.

    Regression test for #23: with the device reporting the old numbering, a
    brightness change on a saved effect forwarded category 2 (custom) or
    1 (built-in, which the cloud now reads as custom) and failed with 12008.
    """
    await _setup_integration(
        hass, mock_api,
        coordinator_data=_with_legacy_categories(),
        entry_id="test_entry_legacy_cat",
        unique_id="test_client_id_legacy_cat",
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
    assert payload["category"] == EFFECT_CATEGORY_CUSTOM
    expected_pixels = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]["effects"][1]["pixels"]
    assert payload["pixels"] == expected_pixels

    await hass.services.async_call(
        "light",
        "turn_on",
        {
            "entity_id": _entity_id(),
            ATTR_EFFECT: "Green Cyan Wave",
            ATTR_BRIGHTNESS: 42,
        },
        blocking=True,
    )
    payload = mock_api.save_effect.call_args[0][1]
    assert payload["category"] == EFFECT_CATEGORY_BUILTIN
    assert payload["pixelLen"] == 30
    assert "pixels" not in payload


def _list_only_data():
    """Coordinator data for a device whose detail fetch has never succeeded."""
    return {MOCK_DEVICE_ID: dict(MOCK_DEVICE_LIST[0])}


async def test_color_without_effect_list_does_not_create_a_slot(hass, mock_api):
    """No "HA Color" effect is created blind while the effect list is unknown.

    Regression test for #22: while the cloud can't parse the device's stored
    data the detail never loads, so there's no telling whether a color slot
    already exists. Creating one anyway could leave duplicate effects on the
    controller, which users report is itself one of the things that breaks
    the cloud's parsing, so the command fails cleanly instead.
    """
    await _setup_integration(
        hass, mock_api,
        coordinator_data=_list_only_data(),
        entry_id="test_entry_no_detail",
        unique_id="test_client_id_no_detail",
    )
    with pytest.raises(HomeAssistantError, match="has not loaded"):
        await hass.services.async_call(
            "light",
            "turn_on",
            {"entity_id": _entity_id(), ATTR_HS_COLOR: (0.0, 100.0)},
            blocking=True,
        )
    # The optimistic write was rolled back: HA doesn't claim a color applied.
    state = hass.states.get(_entity_id())
    # A device list carries no switchState, so with no detail the on/off state
    # is genuinely unknown rather than guessed at.
    assert state.state == STATE_UNKNOWN
    assert state.attributes.get(ATTR_EFFECT) is None

    # Brightness alone has nothing to act on either: with no detail there is
    # nothing to re-save but the color slot, and that would repaint the strip.
    # The light is not known to be on, though, so it is still powered on, as
    # it would be by a plain turn-on, before the call fails.
    mock_api.set_switch_state.reset_mock()
    with pytest.raises(HomeAssistantError, match="does not know"):
        await hass.services.async_call(
            "light",
            "turn_on",
            {"entity_id": _entity_id(), ATTR_BRIGHTNESS: 100},
            blocking=True,
        )
    mock_api.set_switch_state.assert_called_once_with(MOCK_DEVICE_ID, 1)
    mock_api.save_effect.assert_not_called()
    mock_api.view_effect.assert_not_called()
    # The power-on landed, so the light is on, as the error itself says. Still
    # no effect is claimed for it.
    state = hass.states.get(_entity_id())
    assert state.state == STATE_ON
    assert state.attributes.get(ATTR_EFFECT) is None


async def test_color_with_known_slot_works_without_effect_list(hass, mock_api):
    """A slot already resolved keeps working even if the detail stops loading."""
    coordinator = await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_known_slot",
        unique_id="test_client_id_known_slot",
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_HS_COLOR: (0.0, 100.0)},
        blocking=True,
    )
    assert mock_api.save_effect.call_args[0][1]["id"] == -1

    coordinator.async_set_updated_data(_list_only_data())
    await hass.async_block_till_done()
    mock_api.save_effect.reset_mock()
    mock_api.view_effect.reset_mock()

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_HS_COLOR: (120.0, 100.0)},
        blocking=True,
    )
    assert mock_api.save_effect.call_args[0][1]["id"] == 99
    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 99)


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
    # Detail is carried forward and flagged stale, which is what happens while
    # a detail fetch is failing: there is nothing live to contradict the cache.
    stale = {
        MOCK_DEVICE_ID: {**MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID], "_detail_stale": True}
    }
    await _setup_integration(
        hass, mock_api, coordinator_data=stale,
        entry_id="test_entry_restore",
        unique_id="test_client_id_restore",
    )
    # With no live detail to contradict it, the restored state stands.
    state = hass.states.get(_entity_id())
    assert state.attributes.get(ATTR_EFFECT) == "NEW YEAR"
    assert state.attributes.get(ATTR_BRIGHTNESS) == 42
    assert tuple(state.attributes.get(ATTR_HS_COLOR)) == (120.0, 100.0)


async def test_device_state_beats_a_stale_restore(hass, mock_api):
    """What the controller reports now wins over values saved before a restart.

    The lights can be changed from the Trimlight app while Home Assistant is
    down, so a restored brightness or colour can be simply wrong. The restore
    is only a fallback for what the controller does not report.
    """
    mock_restore_cache(
        hass,
        [
            State(
                _entity_id(),
                STATE_ON,
                {
                    ATTR_EFFECT: "INDEPENDENCE DAY",
                    ATTR_HS_COLOR: [240.0, 100.0],
                    ATTR_BRIGHTNESS: 42,
                },
            )
        ],
    )
    await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_restore_live",
        unique_id="test_client_id_restore_live",
    )
    state = hass.states.get(_entity_id())
    # The fixture's controller reports NEW YEAR running at brightness 255.
    assert state.attributes.get(ATTR_EFFECT) == "NEW YEAR"
    assert state.attributes.get(ATTR_BRIGHTNESS) == 255


# --------------------------------------------------------------------------- #
# Review regressions: stale brightness, null effect lists, startup state       #
# --------------------------------------------------------------------------- #


async def test_brightness_back_to_reported_value_still_resaves(hass, mock_api):
    """Setting brightness back to what the device last reported must re-save.

    The effects list in coordinator data only refreshes on the next poll, so
    right after a brightness change it still carries the old value. Comparing
    the requested brightness against that stale value skipped the re-save, so
    255 -> 100 -> 255 within one poll interval left the device at 100 while
    HA showed 255.
    """
    await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_stale_bri",
        unique_id="test_client_id_stale_bri",
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_EFFECT: "NEW YEAR", ATTR_BRIGHTNESS: 100},
        blocking=True,
    )
    assert mock_api.save_effect.call_args[0][1]["brightness"] == 100
    mock_api.save_effect.reset_mock()

    # No poll has run: the fixture still reports NEW YEAR at brightness 255.
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_BRIGHTNESS: 255},
        blocking=True,
    )
    mock_api.save_effect.assert_called_once()
    payload = mock_api.save_effect.call_args[0][1]
    assert payload["name"] == "NEW YEAR"
    assert payload["brightness"] == 255


async def test_null_effect_list_does_not_break_the_entity(hass, mock_api):
    """A device reporting its effects as null is a loaded device with none.

    The effect list used to be iterated as is, so a null broke every state
    write and the entity never appeared. Picking a color must still work:
    the detail did load, there is simply no slot yet to reuse.
    """
    data = {
        MOCK_DEVICE_ID: {**MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID], "effects": None}
    }
    await _setup_integration(
        hass, mock_api,
        coordinator_data=data,
        entry_id="test_entry_null_effects",
        unique_id="test_client_id_null_effects",
    )
    state = hass.states.get(_entity_id())
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes.get("effect_list") == []

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": _entity_id(), ATTR_HS_COLOR: (0.0, 100.0)},
        blocking=True,
    )
    assert mock_api.save_effect.call_args[0][1]["id"] == -1
    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 99)


async def test_brightness_reflects_device_at_setup(hass, mock_api):
    """The entity shows the device's brightness from the start, not 255."""
    device = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]
    data = {
        MOCK_DEVICE_ID: {
            **device,
            "currentEffect": {**device["currentEffect"], "brightness": 100},
        }
    }
    await _setup_integration(
        hass, mock_api,
        coordinator_data=data,
        entry_id="test_entry_setup_bri",
        unique_id="test_client_id_setup_bri",
    )
    assert hass.states.get(_entity_id()).attributes.get(ATTR_BRIGHTNESS) == 100


async def test_first_polls_sync_on_a_freshly_booted_host(hass, mock_api):
    """Polls must be applied before any command, whatever the monotonic clock says.

    time.monotonic() is typically time since boot. The cooldown used 0.0 as
    "no command yet", so on a host up for less than the cooldown every poll in
    that window looked like it was inside a cooldown and was ignored.
    """
    with patch("custom_components.trimlight.light.time") as time_mock:
        time_mock.monotonic.return_value = 10.0
        coordinator = await _setup_integration(
            hass, mock_api,
            entry_id="test_entry_young_clock",
            unique_id="test_client_id_young_clock",
        )
        assert hass.states.get(_entity_id()).state == STATE_ON

        coordinator.async_set_updated_data(
            {MOCK_DEVICE_ID: {**MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID], "switchState": 0}}
        )
        await hass.async_block_till_done()
        assert hass.states.get(_entity_id()).state == STATE_OFF


# --------------------------------------------------------------------------- #
# Reading the running effect and its colour back from the device              #
# --------------------------------------------------------------------------- #


def test_api_colour_round_trips():
    """The inverse conversion must return the hue and saturation sent."""
    for hs in ((0.0, 100.0), (120.0, 100.0), (240.0, 100.0), (30.0, 50.0)):
        assert _api_color_to_hs(_hs_to_api_color(hs)) == pytest.approx(hs, abs=0.75)
    assert _api_color_to_hs(0xFF0000) == pytest.approx((0.0, 100.0), abs=0.01)
    assert _api_color_to_hs(0x00FF00) == pytest.approx((120.0, 100.0), abs=0.01)


def test_running_colour_only_for_a_solid_pattern():
    """A multi-colour pattern has no single colour to report."""
    solid = {"pixels": [{"index": 0, "count": 1, "color": 0xFF0000},
                        {"index": 1, "count": 0, "color": 0}]}
    assert _running_color(solid) == 0xFF0000
    multi = {"pixels": [{"index": 0, "count": 5, "color": 0xFF0000},
                        {"index": 1, "count": 5, "color": 0x0000FF}]}
    assert _running_color(multi) is None
    assert _running_color({}) is None
    assert _running_color({"pixels": []}) is None


def test_match_running_effect_needs_an_unambiguous_answer():
    """Content matching identifies the running effect, but never guesses."""
    custom = {"id": 1, "name": "NEW YEAR", "category": 1, "mode": 0, "speed": 127,
              "brightness": 255, "pixels": [{"index": 0, "count": 1, "color": 9}]}
    builtin = {"id": 3, "name": "Wave", "category": 0, "mode": 3, "speed": 100,
               "brightness": 255, "pixelLen": 30, "reverse": False}
    effects = [custom, builtin]

    # A controller that reports no id still identifies itself by content.
    running = {k: v for k, v in custom.items() if k not in ("id", "name")}
    assert _match_running_effect(running, effects) is custom
    running_builtin = {k: v for k, v in builtin.items() if k not in ("id", "name")}
    assert _match_running_effect(running_builtin, effects) is builtin

    # Two effects identical apart from their name are not guessed between.
    twin = {**custom, "id": 7, "name": "NEW YEAR copy"}
    assert _match_running_effect(running, [custom, twin]) is None

    # Nothing recognisable, and an empty list, both mean "do not know".
    assert _match_running_effect({"category": 1, "mode": 9}, effects) is None
    assert _match_running_effect(running, []) is None

    # An id is used directly when the controller does report one.
    assert _match_running_effect({"id": 3}, effects) is builtin


async def test_running_effect_is_recovered_from_the_device(hass, mock_api):
    """HA learns what is running from currentEffect, with no id reported.

    This is what keeps a later brightness-only command from falling through to
    the colour slot and repainting the strip.
    """
    await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_identify", unique_id="test_client_id_identify",
    )
    state = hass.states.get(_entity_id())
    assert state.attributes.get(ATTR_EFFECT) == "NEW YEAR"

    # So a brightness-only command re-saves that effect rather than a colour.
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": _entity_id(), ATTR_BRIGHTNESS: 90}, blocking=True,
    )
    payload = mock_api.save_effect.call_args[0][1]
    assert payload["name"] == "NEW YEAR"
    assert payload["brightness"] == 90


async def test_colour_picker_follows_the_device(hass, mock_api):
    """The colour shown is the one on the strip, not the last one HA sent."""
    device = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]
    data = {
        MOCK_DEVICE_ID: {
            **device,
            "currentEffect": {
                **device["currentEffect"],
                "pixels": [
                    {"index": 0, "count": 1, "color": 0x00FF00, "disable": False},
                    *[{"index": i, "count": 0, "color": 0, "disable": False}
                      for i in range(1, 30)],
                ],
            },
        }
    }
    await _setup_integration(
        hass, mock_api, coordinator_data=data,
        entry_id="test_entry_colourback", unique_id="test_client_id_colourback",
    )
    hs = hass.states.get(_entity_id()).attributes.get(ATTR_HS_COLOR)
    assert tuple(hs) == pytest.approx((120.0, 100.0), abs=0.01)


async def test_selecting_an_effect_shows_its_brightness_at_once(hass, mock_api):
    """Activating an effect adopts its stored brightness, so HA must show it.

    Measured against a real controller once both the cloud shadow and the
    entity had settled: selecting an effect stored at 120 left the controller
    reporting 120. Without this, the slider keeps the previous value until the
    optimistic window expires and a poll lands, up to about 90 seconds.
    """
    device = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]
    data = {
        MOCK_DEVICE_ID: {
            **device,
            "effects": [
                device["effects"][0],
                {**device["effects"][1], "brightness": 120},
            ],
        }
    }
    await _setup_integration(
        hass, mock_api, coordinator_data=data,
        entry_id="test_entry_fxbright", unique_id="test_client_id_fxbright",
    )
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": _entity_id(), ATTR_EFFECT: "INDEPENDENCE DAY"}, blocking=True,
    )
    state = hass.states.get(_entity_id())
    assert state.attributes.get(ATTR_EFFECT) == "INDEPENDENCE DAY"
    assert state.attributes.get(ATTR_BRIGHTNESS) == 120


# --------------------------------------------------------------------------- #
# Turning on must leave the strip lit, whatever HA knows about it             #
# --------------------------------------------------------------------------- #

# A currentEffect that matches none of the saved effects, so Home Assistant
# cannot tell what the strip was showing.
_UNMATCHED_CURRENT_EFFECT = {
    "category": 1,
    "mode": 0,
    "speed": 127,
    "brightness": 255,
    "pixels": [
        {"index": i, "count": 0, "color": 0, "disable": False} for i in range(30)
    ],
}


def _unidentified_device(*, switch_state=0, **overrides):
    """Coordinator data for a device whose last effect HA cannot identify."""
    return {
        MOCK_DEVICE_ID: {
            **MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID],
            "switchState": switch_state,
            "currentEffect": _UNMATCHED_CURRENT_EFFECT,
            **overrides,
        }
    }


async def _turn_on(hass, **service_data):
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": _entity_id(), **service_data},
        blocking=True,
    )


async def _turn_off(hass):
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": _entity_id()}, blocking=True
    )


class _ControllerThatDoesNotResume:
    """Stands in for the controller behind the original report.

    It accepts a switch from off to MANUAL but shows nothing until an effect
    is activated. Switching it off darkens it. MANUAL sent to a controller
    that is already in manual mode changes nothing, which is what sending it
    ahead of every command has relied on since 1.3.0.
    """

    def __init__(self):
        self.switch_state = 0
        self.showing = None  # The effect id on the strip; None while dark.
        self.stored_brightness = {}  # What each saved effect was last saved at.

    def attach(self, mock_api):
        mock_api.set_switch_state.side_effect = self._set_switch_state
        mock_api.view_effect.side_effect = self._view_effect
        mock_api.save_effect.side_effect = self._save_effect

    async def _set_switch_state(self, device_id, state):
        if state != self.switch_state:
            self.showing = None
        self.switch_state = state

    async def _view_effect(self, device_id, effect_id):
        self.showing = effect_id

    async def _save_effect(self, device_id, payload):
        effect_id = 99 if payload["id"] == -1 else payload["id"]
        self.stored_brightness[effect_id] = payload["brightness"]
        return {"id": effect_id}


async def test_reported_sequence_lights_a_controller_that_does_not_resume(
    hass, mock_api
):
    """The original report, step by step, must end with the strip lit each time.

    "I turned it on and it said it was on but didn't go on, I changed the
    brightness and it still didn't go on, but once I changed the effect it
    finally came on." Home Assistant did not know the effect, as after a
    restart while the lights were off.
    """
    controller = _ControllerThatDoesNotResume()
    controller.attach(mock_api)
    await _setup_integration(
        hass, mock_api, coordinator_data=_unidentified_device(),
        entry_id="test_entry_report", unique_id="test_client_id_report",
    )

    # Step one used to leave the strip dark while HA showed the light on.
    await _turn_on(hass)
    assert hass.states.get(_entity_id()).state == STATE_ON
    assert controller.showing is not None

    # Step two used to be refused, because HA did not know the effect, so it
    # sent nothing. Here the turn-on above has settled what is showing.
    await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})
    assert controller.showing is not None
    assert controller.stored_brightness[controller.showing] == 100

    await _turn_on(hass, **{ATTR_EFFECT: "INDEPENDENCE DAY"})
    assert controller.showing == 2

    # And the round trip that follows: off, then on again, brings it back.
    await _turn_off(hass)
    assert controller.showing is None
    await _turn_on(hass)
    assert controller.showing == 2

    # A turn_on of the light once it is lit leaves it as it is.
    await _turn_on(hass)
    assert controller.showing == 2


def test_scheduled_effect_ids_rank_the_likeliest_first():
    """The timer's effects come back most relevant first, for a Monday 20:00."""
    lit_now = {**_daily_entry((18, 0), (23, 0)), "effectId": 5}
    later_today = {**_daily_entry((21, 0), (23, 0)), "effectId": 6}
    weekend_only = {**_daily_entry((18, 0), (23, 0), repetition=3), "effectId": 7}
    disabled = {**_daily_entry((18, 0), (23, 0), enable=False), "effectId": 8}
    event_lit_now = {
        **_calendar_entry((6, 1), (6, 30), (19, 0), (22, 0)), "effectId": 9
    }
    event_other_season = {
        **_calendar_entry((12, 1), (12, 25), (17, 0), (23, 0)), "effectId": 10
    }
    data = {
        "daily": [weekend_only, later_today, lit_now, disabled],
        "calendar": [event_other_season, event_lit_now],
    }
    # Open windows first (the calendar event ahead of the daily entry), then
    # what else runs today, then daily entries for other days. A disabled
    # entry and an event outside its date range are not candidates at all.
    assert _scheduled_effect_ids(data, _MONDAY) == [9, 5, 6, 7]


def test_scheduled_effect_ids_survive_malformed_schedules():
    """Entries the schedule maths cannot read are skipped, not fatal."""
    assert _scheduled_effect_ids({}, _MONDAY) == []
    data = {"daily": None, "calendar": ["junk", {"enable": True}, {"effectId": None}]}
    assert _scheduled_effect_ids(data, _MONDAY) == []


def test_scheduled_effect_ids_expand_the_combined_effect():
    """A schedule running the combined effect stands for the effects in it.

    The API fixes the combined effect's id at 200 (docs section 16). It is a
    rotation through saved effects, not a saved effect itself, so the id on
    its own would match nothing and the schedule would be passed over.
    """
    rotating = {**_daily_entry((18, 0), (23, 0)), "effectId": 200}
    single = {**_daily_entry((21, 0), (23, 0)), "effectId": 6}
    data = {
        "daily": [single, rotating],
        "combinedEffect": {"effectIds": [4, 2, 9], "interval": 5},
    }
    # The rotation is lit at 20:00, so its effects lead, in playing order.
    assert _scheduled_effect_ids(data, _MONDAY) == [4, 2, 9, 6]
    # With nothing saying what the rotation holds, the entry contributes
    # nothing rather than an id that names no effect.
    for combined in (None, {}, {"effectIds": None}):
        assert _scheduled_effect_ids(
            {"daily": [single, rotating], "combinedEffect": combined}, _MONDAY
        ) == [6]


async def test_turn_on_with_unknown_effect_uses_the_timer_schedule(hass, mock_api):
    """When HA has no idea what was showing, the timer's effect is shown.

    Regression test for the case the original report most likely hit: Home
    Assistant had been restarted while the lights were off, so it no longer
    knew the effect, and the power-on alone left the strip dark. The schedule
    is the one place the device says which pattern the lights are meant to
    show, so it beats the first effect in the list.
    """
    data = _unidentified_device(
        daily=[{**_daily_entry((18, 0), (23, 0)), "effectId": 2}]
    )
    await _setup_integration(
        hass, mock_api, coordinator_data=data,
        entry_id="test_entry_sched_fx", unique_id="test_client_id_sched_fx",
    )
    assert hass.states.get(_entity_id()).state == STATE_OFF

    await _turn_on(hass)

    assert _command_order(mock_api) == ["set_switch_state", "view_effect"]
    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 2)
    state = hass.states.get(_entity_id())
    assert state.state == STATE_ON
    # HA now knows what is showing, so it can show it and act on it.
    assert state.attributes.get(ATTR_EFFECT) == "INDEPENDENCE DAY"


async def test_turn_on_with_unknown_effect_uses_the_scheduled_rotation(
    hass, mock_api
):
    """A timer running the combined effect still decides what turn-on shows.

    The schedule names id 200, the rotation, which is not a saved effect. The
    first effect in the rotation that Home Assistant can name is shown, here
    skipping an id the device no longer has.
    """
    data = _unidentified_device(
        daily=[{**_daily_entry((18, 0), (23, 0)), "effectId": 200}],
        combinedEffect={"effectIds": [41, 2, 1], "interval": 5},
    )
    await _setup_integration(
        hass, mock_api, coordinator_data=data,
        entry_id="test_entry_rotation_fx", unique_id="test_client_id_rotation_fx",
    )
    await _turn_on(hass)

    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 2)
    state = hass.states.get(_entity_id())
    assert state.attributes.get(ATTR_EFFECT) == "INDEPENDENCE DAY"


async def test_turn_on_with_unknown_effect_falls_back_to_first_saved(hass, mock_api):
    """With no schedule either, the first effect HA can name beats a dark strip."""
    device = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]
    data = _unidentified_device(
        # An effect with no name cannot be shown in HA, and one with no id
        # cannot be activated, so neither is a candidate.
        effects=[{"id": 7}, {"name": "No id"}, *device["effects"]],
    )
    await _setup_integration(
        hass, mock_api, coordinator_data=data,
        entry_id="test_entry_first_fx", unique_id="test_client_id_first_fx",
    )
    await _turn_on(hass)

    assert _command_order(mock_api) == ["set_switch_state", "view_effect"]
    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 1)
    assert hass.states.get(_entity_id()).attributes.get(ATTR_EFFECT) == "NEW YEAR"


async def test_plain_turn_on_leaves_an_unidentified_running_pattern_alone(
    hass, mock_api
):
    """A light that is already on with a pattern HA cannot name keeps it.

    The strip may be showing something played from the Trimlight app that was
    never saved. An automation calling turn_on on a light that is already on
    must not replace that with an effect picked for it.
    """
    await _setup_integration(
        hass, mock_api, coordinator_data=_unidentified_device(switch_state=1),
        entry_id="test_entry_keep_fx", unique_id="test_client_id_keep_fx",
    )
    assert hass.states.get(_entity_id()).state == STATE_ON

    await _turn_on(hass)

    assert _command_order(mock_api) == ["set_switch_state"]
    assert hass.states.get(_entity_id()).attributes.get(ATTR_EFFECT) is None


async def test_turn_on_does_not_put_a_remembered_effect_over_what_is_showing(
    hass, mock_api
):
    """What HA remembers must not replace what an already-on light is showing.

    HA identified NEW YEAR, then an unsaved pattern was started from the
    Trimlight app. HA cannot name it, so it goes on showing NEW YEAR as the
    effect. An automation calling turn_on on that light must not treat the
    stale name as what to show and bring NEW YEAR back.
    """
    coordinator = await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_stale_fx", unique_id="test_client_id_stale_fx",
    )
    assert hass.states.get(_entity_id()).attributes.get(ATTR_EFFECT) == "NEW YEAR"

    # A poll reports the app's pattern, which the API documentation says
    # carries id -1 while it is an unsaved preview.
    preview = _unidentified_device(switch_state=1)
    preview[MOCK_DEVICE_ID]["currentEffect"] = {**_UNMATCHED_CURRENT_EFFECT, "id": -1}
    coordinator.async_set_updated_data(preview)
    await hass.async_block_till_done()
    state = hass.states.get(_entity_id())
    assert state.state == STATE_ON
    assert state.attributes.get(ATTR_EFFECT) == "NEW YEAR"

    await _turn_on(hass)

    assert _command_order(mock_api) == ["set_switch_state"]


async def test_failed_reactivation_is_retried_by_the_next_turn_on(hass, mock_api):
    """An "on" left by a turn-on that showed nothing is not taken as settled.

    The re-activation is best-effort, so when it fails HA still shows the
    light on. That is exactly the state in which a turn-on sends the power-on
    alone, so without a note that the re-activation is still owed, repeating
    the turn-on would never try again and the strip would stay dark until the
    light was switched off and on.
    """
    controller = _ControllerThatDoesNotResume()
    controller.attach(mock_api)
    await _setup_integration(
        hass, mock_api, coordinator_data=_unidentified_device(),
        entry_id="test_entry_owed", unique_id="test_client_id_owed",
    )
    mock_api.view_effect.side_effect = TrimlightApiError(
        "Request to /path failed: TimeoutError"
    )
    await _turn_on(hass)  # Succeeds: the power-on landed.
    assert hass.states.get(_entity_id()).state == STATE_ON
    assert controller.showing is None

    controller.attach(mock_api)  # The cloud answers again.
    mock_api.reset_mock()
    await _turn_on(hass)
    assert _command_order(mock_api) == ["set_switch_state", "view_effect"]
    assert controller.showing == 1

    # Now that the strip was told what to show, "on" means on again.
    mock_api.reset_mock()
    await _turn_on(hass)
    assert _command_order(mock_api) == ["set_switch_state"]
    assert controller.showing == 1


async def test_resume_keeps_the_brightness_home_assistant_set(hass, mock_api):
    """Turning back on must not publish a brightness older than HA's own.

    The stored brightness of an effect is read from the last poll, so right
    after HA re-saved the effect it is out of date. Slider to 100, off, on
    used to leave HA showing the old 255 while the controller had 100, and
    the next colour pick then sent that 255 to the controller.
    """
    await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_keep_bri", unique_id="test_client_id_keep_bri",
    )
    await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})
    await _turn_off(hass)
    await _turn_on(hass)

    state = hass.states.get(_entity_id())
    assert state.attributes.get(ATTR_EFFECT) == "NEW YEAR"
    assert state.attributes.get(ATTR_BRIGHTNESS) == 100

    await _turn_on(hass, **{ATTR_HS_COLOR: (0.0, 100.0)})
    assert mock_api.save_effect.call_args[0][1]["brightness"] == 100


async def test_resume_shows_the_stored_brightness_after_dimming_outside_ha(
    hass, mock_api
):
    """Turning back on shows the brightness the controller goes back to.

    NEW YEAR is saved at 255 and was dimmed to 50 from the Trimlight app, so
    HA shows 50. Activating the effect puts the strip back at the saved 255,
    and HA has to say so: left at 50, the slider was wrong until a poll landed
    after the cooldown, and a colour picked in that window was sent at 50.
    """
    device = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]
    data = {
        MOCK_DEVICE_ID: {
            **device,
            "currentEffect": {**device["currentEffect"], "brightness": 50},
        }
    }
    await _setup_integration(
        hass, mock_api, coordinator_data=data,
        entry_id="test_entry_dimmed", unique_id="test_client_id_dimmed",
    )
    assert hass.states.get(_entity_id()).attributes.get(ATTR_BRIGHTNESS) == 50

    await _turn_off(hass)
    await _turn_on(hass)
    assert hass.states.get(_entity_id()).attributes.get(ATTR_BRIGHTNESS) == 255


async def test_reselecting_the_running_effect_shows_its_stored_brightness(
    hass, mock_api
):
    """Activating the effect that is already running publishes its brightness too."""
    device = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]
    data = {
        MOCK_DEVICE_ID: {
            **device,
            "currentEffect": {**device["currentEffect"], "brightness": 50},
        }
    }
    await _setup_integration(
        hass, mock_api, coordinator_data=data,
        entry_id="test_entry_reselect", unique_id="test_client_id_reselect",
    )
    await _turn_on(hass, **{ATTR_EFFECT: "NEW YEAR"})
    assert hass.states.get(_entity_id()).attributes.get(ATTR_BRIGHTNESS) == 255


async def test_own_brightness_gives_way_to_a_freshly_polled_one(hass, mock_api):
    """What HA saved stops being the newest record once a fresh list arrives.

    HA set NEW YEAR to 100. Long afterwards a poll brings a newly fetched
    list in which it is stored at 180, changed from the Trimlight app, and
    that list is now the one to believe.
    """
    device = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]
    with patch("custom_components.trimlight.light.time") as time_mock:
        time_mock.monotonic.return_value = 1000.0
        coordinator = await _setup_integration(
            hass, mock_api,
            entry_id="test_entry_own_bri", unique_id="test_client_id_own_bri",
        )
        await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})

        time_mock.monotonic.return_value = 1500.0
        coordinator.async_set_updated_data(
            {
                MOCK_DEVICE_ID: {
                    **device,
                    "effects": [
                        {**device["effects"][0], "brightness": 180},
                        device["effects"][1],
                    ],
                    "currentEffect": {**device["currentEffect"], "brightness": 180},
                }
            }
        )
        await hass.async_block_till_done()
        await _turn_off(hass)
        await _turn_on(hass)

    assert hass.states.get(_entity_id()).attributes.get(ATTR_BRIGHTNESS) == 180


async def test_a_fresh_list_keeps_superseding_after_a_carried_one(hass, mock_api):
    """A save superseded by a fresh list stays superseded.

    HA set NEW YEAR to 100; hours of fresh polls then showed it at 200, set
    from the Trimlight app. The poll in hand when the light is turned back on
    happens to be one that came back without an effects list, so the old list
    was carried over. That must not bring HA's long-superseded 100 back.
    """
    device = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]

    def poll(brightness, **flags):
        return {
            MOCK_DEVICE_ID: {
                **device,
                "effects": [
                    {**device["effects"][0], "brightness": brightness},
                    device["effects"][1],
                ],
                "currentEffect": {**device["currentEffect"], "brightness": brightness},
                **flags,
            }
        }

    with patch("custom_components.trimlight.light.time") as time_mock:
        time_mock.monotonic.return_value = 1000.0
        coordinator = await _setup_integration(
            hass, mock_api,
            entry_id="test_entry_superseded", unique_id="test_client_id_superseded",
        )
        await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})

        for now in (5000.0, 6000.0, 7000.0):
            time_mock.monotonic.return_value = now
            coordinator.async_set_updated_data(poll(200))
            await hass.async_block_till_done()
        await _turn_off(hass)

        time_mock.monotonic.return_value = 9000.0
        coordinator.async_set_updated_data(poll(200, _effects_carried=True, switchState=0))
        await hass.async_block_till_done()
        await _turn_on(hass)

    assert hass.states.get(_entity_id()).attributes.get(ATTR_BRIGHTNESS) == 200


async def test_owed_reactivation_does_not_outlive_its_moment(hass, mock_api):
    """A failed re-activation must not shape a turn-on hours later.

    The note that one is owed makes the next turn-on act as a real one, which
    is right for someone pressing "on" again because the strip stayed dark.
    Kept for good, it made a turn_on long afterwards replace a pattern that
    had since been started from the Trimlight app, on a light that was lit.
    """
    with patch("custom_components.trimlight.light.time") as time_mock:
        time_mock.monotonic.return_value = 1000.0
        coordinator = await _setup_integration(
            hass, mock_api, coordinator_data=_unidentified_device(),
            entry_id="test_entry_owed_old", unique_id="test_client_id_owed_old",
        )
        mock_api.view_effect.side_effect = TrimlightApiError(
            "Request to /path failed: TimeoutError"
        )
        await _turn_on(hass)  # The power-on lands; the re-activation does not.
        mock_api.view_effect.side_effect = None
        await _turn_off(hass)

        # Hours later the strip is lit from the Trimlight app with a pattern
        # HA cannot name, and a poll reports the light on.
        time_mock.monotonic.return_value = 9000.0
        coordinator.async_set_updated_data(_unidentified_device(switch_state=1))
        await hass.async_block_till_done()
        assert hass.states.get(_entity_id()).state == STATE_ON

        mock_api.reset_mock()
        await _turn_on(hass)
        assert _command_order(mock_api) == ["set_switch_state"]
        with pytest.raises(ServiceValidationError, match="cannot tell which effect"):
            await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})
        mock_api.save_effect.assert_not_called()


async def test_turn_on_that_had_nothing_to_show_is_retried(hass, mock_api):
    """A turn-on with no effect to show yet leaves the same note as a failed one.

    Home Assistant started while the detail fetch was failing, so it knew no
    effects and could only power the controller on. Once the effects load, a
    second turn-on has to show one even though HA already shows the light on.
    """
    controller = _ControllerThatDoesNotResume()
    controller.attach(mock_api)
    coordinator = await _setup_integration(
        hass, mock_api, coordinator_data=_list_only_data(),
        entry_id="test_entry_owed_none", unique_id="test_client_id_owed_none",
    )
    await _turn_on(hass)
    assert _command_order(mock_api) == ["set_switch_state"]
    assert controller.showing is None

    coordinator.async_set_updated_data(_unidentified_device(switch_state=1))
    await hass.async_block_till_done()
    mock_api.reset_mock()
    await _turn_on(hass)

    assert _command_order(mock_api) == ["set_switch_state", "view_effect"]
    assert controller.showing == 1


async def test_failed_effect_turn_on_from_off_is_retried_by_a_plain_one(
    hass, mock_api
):
    """Any turn-on from off that showed nothing is owed a re-activation.

    Selecting an effect on an off light powers the controller on first. When
    the effect then fails to activate, the next poll reports the light on, and
    a plain turn-on must not take that "on" to mean the strip is lit.
    """
    off_data = {
        MOCK_DEVICE_ID: {**MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID], "switchState": 0}
    }
    coordinator = await _setup_integration(
        hass, mock_api, coordinator_data=off_data,
        entry_id="test_entry_owed_fx", unique_id="test_client_id_owed_fx",
    )
    mock_api.view_effect.side_effect = TrimlightApiError(
        "Request to /path failed: TimeoutError"
    )
    with pytest.raises(HomeAssistantError):
        await _turn_on(hass, **{ATTR_EFFECT: "INDEPENDENCE DAY"})
    mock_api.view_effect.side_effect = None

    coordinator.async_set_updated_data(MOCK_COORDINATOR_DATA)
    await hass.async_block_till_done()
    assert hass.states.get(_entity_id()).state == STATE_ON

    mock_api.reset_mock()
    await _turn_on(hass)
    assert _command_order(mock_api) == ["set_switch_state", "view_effect"]


async def test_turn_on_while_the_timer_has_the_light_lit_shows_the_effect_again(
    hass, mock_api
):
    """Taking a controller off its timer must not leave a lit strip dark.

    HA shows a light its timer has lit as on, but the power-on a turn_on sends
    is still a change of mode, from timer to manual, and a controller that
    shows nothing after a change of mode until told what to show went dark
    while HA went on showing it on. The effect the controller itself reports
    running is activated again after the power-on.
    """
    controller = _ControllerThatDoesNotResume()
    controller.switch_state = 2
    controller.showing = 1
    controller.attach(mock_api)
    data = _timer_device([_daily_entry((18, 0), (23, 0))])
    with patch(
        "custom_components.trimlight.light.dt_util.now", return_value=_MONDAY
    ):
        await _setup_integration(
            hass, mock_api, coordinator_data=data,
            entry_id="test_entry_timer_lit", unique_id="test_client_id_timer_lit",
        )
        assert hass.states.get(_entity_id()).state == STATE_ON

        await _turn_on(hass)
        assert _command_order(mock_api) == ["set_switch_state", "view_effect"]
        assert controller.showing == 1

        # It is in manual mode now, whatever the last poll still says, so a
        # further turn_on is the power-on alone again.
        mock_api.reset_mock()
        await _turn_on(hass)
        assert _command_order(mock_api) == ["set_switch_state"]
        assert controller.showing == 1


async def test_failed_re_show_under_the_timer_is_retried(hass, mock_api):
    """A re-activation tried after leaving the timer, and failed, is retried.

    The timer has NEW YEAR lit, which HA identifies, so a turn_on shows it
    again after the power-on. When that fails, a controller that shows
    nothing after a change of mode is left dark while HA shows it on, so the
    next turn_on tries again, as it would after a turn-on from off.
    """
    controller = _ControllerThatDoesNotResume()
    controller.switch_state = 2
    controller.showing = 1
    controller.attach(mock_api)
    data = _timer_device([_daily_entry((18, 0), (23, 0))])
    with patch(
        "custom_components.trimlight.light.dt_util.now", return_value=_MONDAY
    ):
        await _setup_integration(
            hass, mock_api, coordinator_data=data,
            entry_id="test_entry_timer_retry", unique_id="test_client_id_timer_retry",
        )
        mock_api.view_effect.side_effect = TrimlightApiError(
            "Request to /path failed: TimeoutError"
        )
        await _turn_on(hass)  # The power-on lands; showing NEW YEAR does not.
        assert controller.showing is None

        controller.attach(mock_api)  # The cloud answers again.
        mock_api.reset_mock()
        await _turn_on(hass)

    assert _command_order(mock_api) == ["set_switch_state", "view_effect"]
    assert controller.showing == 1


async def test_timer_lit_pattern_nothing_could_name_is_never_replaced(
    hass, mock_api
):
    """A second turn_on of a timer-lit light still picks nothing for it.

    The timer has the light showing a pattern HA cannot identify, so a
    turn_on can only power it on. A controller that resumes keeps showing
    something there, and a second turn_on soon after (a scene, a group, a
    double tap) must not replace it with an effect HA picked. A controller
    that goes dark instead is lit again by turning the light off and on.
    """
    controller = _ControllerThatDoesNotResume()
    controller.switch_state = 2
    controller.showing = "pattern from the timer"
    controller.attach(mock_api)
    data = _timer_device([_daily_entry((18, 0), (23, 0))])
    data[MOCK_DEVICE_ID]["currentEffect"] = _UNMATCHED_CURRENT_EFFECT
    with patch(
        "custom_components.trimlight.light.dt_util.now", return_value=_MONDAY
    ):
        await _setup_integration(
            hass, mock_api, coordinator_data=data,
            entry_id="test_entry_timer_dark", unique_id="test_client_id_timer_dark",
        )
        await _turn_on(hass)
        assert _command_order(mock_api) == ["set_switch_state"]

        mock_api.reset_mock()
        await _turn_on(hass)
        assert _command_order(mock_api) == ["set_switch_state"]
        with pytest.raises(ServiceValidationError):
            await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})
        mock_api.save_effect.assert_not_called()

        # Off and on again is a turn-on from off, which does pick an effect:
        # here the one the timer schedule uses.
        await _turn_off(hass)
        mock_api.reset_mock()
        await _turn_on(hass)

    assert _command_order(mock_api) == ["set_switch_state", "view_effect"]
    assert controller.showing == 1


async def test_turn_on_under_the_timer_picks_nothing_for_a_lit_light(
    hass, mock_api
):
    """Only what the controller reports running is shown again, never a guess.

    HA remembers INDEPENDENCE DAY, but the timer has the light showing a
    pattern HA cannot identify. Neither the remembered effect nor a fallback
    may replace it just because a turn_on took the controller off its timer.
    """
    mock_restore_cache(
        hass,
        [State(_entity_id(), STATE_ON, {ATTR_EFFECT: "INDEPENDENCE DAY"})],
    )
    data = _timer_device([_daily_entry((18, 0), (23, 0))])
    data[MOCK_DEVICE_ID]["currentEffect"] = _UNMATCHED_CURRENT_EFFECT
    with patch(
        "custom_components.trimlight.light.dt_util.now", return_value=_MONDAY
    ):
        await _setup_integration(
            hass, mock_api, coordinator_data=data,
            entry_id="test_entry_timer_keep", unique_id="test_client_id_timer_keep",
        )
        state = hass.states.get(_entity_id())
        assert state.state == STATE_ON
        assert state.attributes.get(ATTR_EFFECT) == "INDEPENDENCE DAY"

        await _turn_on(hass)

    assert _command_order(mock_api) == ["set_switch_state"]


# The colour slot as a controller lists it once it exists: solid blue.
_COLOUR_SLOT = {
    "id": 50,
    "name": HA_COLOR_EFFECT_NAME,
    "category": 1,
    "mode": 0,
    "speed": 127,
    "brightness": 255,
    "pixels": [
        {"index": 0, "count": 1, "color": 0x0000FF, "disable": False},
        *[{"index": i, "count": 0, "color": 0, "disable": False} for i in range(1, 30)],
    ],
}


async def test_fallback_never_picks_the_colour_slot(hass, mock_api):
    """The colour slot is not an effect to fall back on.

    HA would show it with whatever colour HA happens to hold, white on a
    fresh start, and the next brightness change would re-save the slot with
    that colour: the strip repainted a colour nobody picked.
    """
    device = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]
    data = _unidentified_device(effects=[_COLOUR_SLOT, *device["effects"]])
    await _setup_integration(
        hass, mock_api, coordinator_data=data,
        entry_id="test_entry_no_slot", unique_id="test_client_id_no_slot",
    )
    await _turn_on(hass)

    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 1)
    assert hass.states.get(_entity_id()).attributes.get(ATTR_EFFECT) == "NEW YEAR"


async def test_colour_slot_alone_is_not_fallen_back_on(hass, mock_api):
    """With nothing but the colour slot saved, the power-on is all there is."""
    data = _unidentified_device(effects=[_COLOUR_SLOT])
    await _setup_integration(
        hass, mock_api, coordinator_data=data,
        entry_id="test_entry_only_slot", unique_id="test_client_id_only_slot",
    )
    await _turn_on(hass)

    assert _command_order(mock_api) == ["set_switch_state"]


async def test_brightness_on_the_colour_slot_resaves_the_colour_ha_holds(
    hass, mock_api
):
    """Brightness on a solid colour must keep the colour HA last set.

    The polled copy of the slot lags a colour change by up to one poll, so
    re-saving from it would put the previous colour back on the strip.
    """
    device = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]
    data = {MOCK_DEVICE_ID: {**device, "effects": [*device["effects"], _COLOUR_SLOT]}}
    await _setup_integration(
        hass, mock_api, coordinator_data=data,
        entry_id="test_entry_slot_bri", unique_id="test_client_id_slot_bri",
    )

    async def save(device_id, payload):
        return {"id": payload["id"]}  # An update keeps the effect's id.

    mock_api.save_effect.side_effect = save
    await _turn_on(hass, **{ATTR_HS_COLOR: (0.0, 100.0)})  # Red; the list says blue.
    mock_api.reset_mock()

    await _turn_on(hass, **{ATTR_BRIGHTNESS: 80})

    payload = mock_api.save_effect.call_args[0][1]
    assert payload["id"] == 50
    assert payload["name"] == HA_COLOR_EFFECT_NAME
    assert payload["brightness"] == 80
    assert payload["pixels"][0]["color"] == 0xFF0000
    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 50)


async def test_brightness_from_off_with_nothing_to_show_still_powers_on(
    hass, mock_api
):
    """A brightness that cannot be applied must not stop an off light turning on.

    While the effect list has never loaded (the cloud cannot parse the
    controller's data) there is nothing to apply a brightness to. Turning on
    still works in that state, so a turn-on that also names a brightness sends
    the power-on before it fails, rather than nothing at all.
    """
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(_entity_id(), STATE_OFF),
                {"effect": "NEW YEAR", "hs_color": [0.0, 0.0], "brightness": 255},
            )
        ],
    )
    list_only = {MOCK_DEVICE_ID: {**MOCK_DEVICE_LIST[0], "switchState": 0}}
    await _setup_integration(
        hass, mock_api, coordinator_data=list_only,
        entry_id="test_entry_bri_no_list", unique_id="test_client_id_bri_no_list",
    )
    assert hass.states.get(_entity_id()).state == STATE_OFF

    with pytest.raises(HomeAssistantError, match="could not set its brightness"):
        await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})

    assert _command_order(mock_api) == ["set_switch_state"]
    mock_api.set_switch_state.assert_called_once_with(MOCK_DEVICE_ID, 1)
    # The error says the light was turned on, and HA must agree with it.
    state = hass.states.get(_entity_id())
    assert state.state == STATE_ON
    # The brightness that was not applied is not shown as if it had been.
    assert state.attributes.get(ATTR_BRIGHTNESS) == 255


async def test_partly_landed_turn_on_is_not_undone_by_a_lagging_poll(
    hass, mock_api
):
    """A light that was powered on stays on in HA through a poll that lags.

    The brightness could not be applied, but the power-on did land. Like any
    other command that landed, HA holds that state while the cloud catches
    up, rather than letting the next poll, still reporting the light off,
    show it off again.
    """
    data = _unidentified_device(effects=[_COLOUR_SLOT])  # Nothing to fall back on.
    coordinator = await _setup_integration(
        hass, mock_api, coordinator_data=data,
        entry_id="test_entry_lag", unique_id="test_client_id_lag",
    )
    assert hass.states.get(_entity_id()).state == STATE_OFF

    with pytest.raises(HomeAssistantError, match="could not set its brightness"):
        await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})
    assert hass.states.get(_entity_id()).state == STATE_ON

    coordinator.async_set_updated_data(_unidentified_device(effects=[_COLOUR_SLOT]))
    await hass.async_block_till_done()
    assert hass.states.get(_entity_id()).state == STATE_ON


async def test_brightness_from_off_with_unknown_effect_lights_a_picked_effect(
    hass, mock_api
):
    """Turning an off light on with a brightness lights it even when unknown.

    This used to be refused outright whenever HA did not know the effect,
    which left an automation such as "turn on at 40%" failing with the lights
    off. The light is now turned on with an effect picked the same way a
    plain turn-on picks one. The brightness is not saved into that effect,
    though: the timer runs the same saved effects, often the very one picked,
    so it would change how the timer lights them from then on. The call says
    the brightness was not applied.
    """
    await _setup_integration(
        hass, mock_api,
        coordinator_data=_unidentified_device(
            daily=[{**_daily_entry((18, 0), (23, 0)), "effectId": 2}]
        ),
        entry_id="test_entry_bri_off", unique_id="test_client_id_bri_off",
    )
    with pytest.raises(HomeAssistantError, match="left the brightness"):
        await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})

    assert _command_order(mock_api) == ["set_switch_state", "view_effect"]
    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 2)
    mock_api.save_effect.assert_not_called()
    state = hass.states.get(_entity_id())
    assert state.state == STATE_ON
    assert state.attributes.get(ATTR_EFFECT) == "INDEPENDENCE DAY"
    # The brightness the effect is saved with, which is what the strip shows.
    assert state.attributes.get(ATTR_BRIGHTNESS) == 255


async def test_brightness_from_off_applies_to_the_remembered_effect(hass, mock_api):
    """The effect a light was showing before a restart is not a guess.

    "Turn on at 40%" on a light that was off across a restart brings back the
    effect it was showing, at 40%, as it would for any effect HA knew.
    """
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(_entity_id(), STATE_OFF),
                {"effect": "INDEPENDENCE DAY", "hs_color": [0.0, 0.0], "brightness": 255},
            )
        ],
    )
    await _setup_integration(
        hass, mock_api, coordinator_data=_unidentified_device(),
        entry_id="test_entry_bri_known", unique_id="test_client_id_bri_known",
    )
    await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})

    assert _command_order(mock_api) == [
        "set_switch_state", "save_effect", "view_effect",
    ]
    payload = mock_api.save_effect.call_args[0][1]
    assert payload["name"] == "INDEPENDENCE DAY"
    assert payload["brightness"] == 100
    state = hass.states.get(_entity_id())
    assert state.attributes.get(ATTR_EFFECT) == "INDEPENDENCE DAY"
    assert state.attributes.get(ATTR_BRIGHTNESS) == 100


async def test_brightness_only_is_refused_while_on_with_unknown_effect(
    hass, mock_api
):
    """Brightness alone still cannot be applied to a pattern HA cannot name."""
    await _setup_integration(
        hass, mock_api, coordinator_data=_unidentified_device(switch_state=1),
        entry_id="test_entry_bri_unknown", unique_id="test_client_id_bri_unknown",
    )
    with pytest.raises(ServiceValidationError, match="cannot tell which effect"):
        await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})
    # Refused before anything was sent, the power-on included.
    assert _command_order(mock_api) == []


async def test_brightness_only_is_refused_when_the_effect_was_deleted(
    hass, mock_api
):
    """An effect HA remembers but the device no longer has is an unknown one.

    Regression test: this used to fall through to the colour slot, replacing
    the pattern on the strip with a solid colour because the slider moved.
    """
    mock_restore_cache(
        hass,
        [State(_entity_id(), STATE_ON, {ATTR_EFFECT: "DELETED IN THE APP"})],
    )
    await _setup_integration(
        hass, mock_api, coordinator_data=_unidentified_device(switch_state=1),
        entry_id="test_entry_gone_fx", unique_id="test_client_id_gone_fx",
    )
    assert (
        hass.states.get(_entity_id()).attributes.get(ATTR_EFFECT)
        == "DELETED IN THE APP"
    )
    with pytest.raises(
        ServiceValidationError,
        match="'DELETED IN THE APP' is no longer saved on the controller",
    ):
        await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})
    mock_api.save_effect.assert_not_called()


async def test_refused_reactivation_does_not_fail_the_turn_on(
    hass, mock_api, caplog
):
    """The power-on landing is what makes a plain turn_on a success.

    Re-activating the effect is a step this integration adds on top. An effect
    deleted in the Trimlight app since the last poll, or a cloud that cannot
    read the controller's data, must not turn a turn-on that used to work
    into one that fails. A controller that resumes by itself is lit anyway.
    """
    off_data = {
        MOCK_DEVICE_ID: {**MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID], "switchState": 0}
    }
    await _setup_integration(
        hass, mock_api, coordinator_data=off_data,
        entry_id="test_entry_soft_resume", unique_id="test_client_id_soft_resume",
    )
    mock_api.view_effect.side_effect = TrimlightApiError(
        "API error 12004: effect not found", code=12004
    )
    await _turn_on(hass)  # Must not raise.

    mock_api.set_switch_state.assert_called_once_with(MOCK_DEVICE_ID, 1)
    assert hass.states.get(_entity_id()).state == STATE_ON
    # It leaves a trace for the controllers that do need the step, with
    # advice that still works once the moment for a retry has passed.
    assert "could not re-activate 'NEW YEAR'" in caplog.text
    assert "turn them off and on again" in caplog.text


async def test_colour_slot_is_resumed_before_a_poll_lists_it(hass, mock_api):
    """Colour, off, on within one poll must bring the colour back.

    The slot was only just created, so the polled effects list does not have
    it yet. Without its cached id a plain turn-on would treat the effect as
    unknown and show some other effect in place of the colour just picked.
    """
    await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_slot_resume", unique_id="test_client_id_slot_resume",
    )
    await _turn_on(hass, **{ATTR_HS_COLOR: (0.0, 100.0)})
    await _turn_off(hass)
    mock_api.reset_mock()

    await _turn_on(hass)

    assert _command_order(mock_api) == ["set_switch_state", "view_effect"]
    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 99)
    state = hass.states.get(_entity_id())
    assert state.attributes.get(ATTR_EFFECT) == HA_COLOR_EFFECT_NAME
    assert tuple(state.attributes.get(ATTR_HS_COLOR)) == (0.0, 100.0)


async def test_colour_slot_outlasts_a_list_that_never_refreshed(hass, mock_api):
    """Time alone does not make a missing colour slot a deleted one.

    If no poll has delivered a list since the colour was picked, the list in
    hand is still the one from before the slot existed, however old it is.
    """
    with patch("custom_components.trimlight.light.time") as time_mock:
        time_mock.monotonic.return_value = 1000.0
        await _setup_integration(
            hass, mock_api,
            entry_id="test_entry_slot_old", unique_id="test_client_id_slot_old",
        )
        await _turn_on(hass, **{ATTR_HS_COLOR: (0.0, 100.0)})  # Creates slot 99.
        await _turn_off(hass)

        time_mock.monotonic.return_value = 5000.0
        mock_api.view_effect.reset_mock()
        await _turn_on(hass)
        mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 99)


async def test_a_list_fetched_while_the_cloud_catches_up_never_proves_a_slot_gone(
    hass, mock_api
):
    """It is when a list was fetched that counts, not when it is looked at.

    A list fetched 55 seconds after the colour was picked may predate the save
    reaching the cloud, so its lacking the slot proves nothing. It still
    proves nothing a minute later, when a failed refresh hands the same list
    over again and the light is turned on.
    """
    with patch("custom_components.trimlight.light.time") as time_mock:
        time_mock.monotonic.return_value = 1000.0
        coordinator = await _setup_integration(
            hass, mock_api,
            entry_id="test_entry_slot_early", unique_id="test_client_id_slot_early",
        )
        await _turn_on(hass, **{ATTR_HS_COLOR: (0.0, 100.0)})  # Creates slot 99.
        await _turn_off(hass)

        time_mock.monotonic.return_value = 1055.0
        coordinator.async_set_updated_data(_poll_without_the_colour_slot())
        await hass.async_block_till_done()

        time_mock.monotonic.return_value = 1120.0
        coordinator.async_set_updated_data(coordinator.data)
        await hass.async_block_till_done()
        mock_api.view_effect.reset_mock()
        await _turn_on(hass)

    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 99)
    assert hass.states.get(_entity_id()).attributes.get(ATTR_EFFECT) == HA_COLOR_EFFECT_NAME


async def test_a_list_delivered_again_unchanged_never_proves_a_slot_gone(
    hass, mock_api
):
    """The same list handed over again is not a list fetched since the save.

    A failed refresh notifies the entity with the data it already had, which
    here is the list from before the colour slot existed. The controller
    reports red running and the light off, so only the list could mislead.
    """
    with patch("custom_components.trimlight.light.time") as time_mock:
        time_mock.monotonic.return_value = 1000.0
        coordinator = await _setup_integration(
            hass, mock_api, coordinator_data=_poll_without_the_colour_slot(),
            entry_id="test_entry_slot_again", unique_id="test_client_id_slot_again",
        )
        await _turn_on(hass, **{ATTR_HS_COLOR: (0.0, 100.0)})  # Creates slot 99.
        await _turn_off(hass)

        time_mock.monotonic.return_value = 2000.0
        coordinator.async_set_updated_data(coordinator.data)
        await hass.async_block_till_done()

        mock_api.view_effect.reset_mock()
        await _turn_on(hass)

    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 99)


def _poll_without_the_colour_slot(**flags):
    """Coordinator data as a poll delivers it while red shows from a slot it omits.

    The light is off and its effects list is a newly built one, as a detail
    fetch produces. ``flags`` adds the markers the coordinator sets when the
    list is in fact left over from an earlier poll.
    """
    device = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]
    red = [
        {"index": 0, "count": 1, "color": 0xFF0000, "disable": False},
        *[{"index": i, "count": 0, "color": 0, "disable": False} for i in range(1, 30)],
    ]
    return {
        MOCK_DEVICE_ID: {
            **device,
            "switchState": 0,
            "effects": list(device["effects"]),
            "currentEffect": {**device["currentEffect"], "pixels": red},
            **flags,
        }
    }


async def test_colour_slot_missing_from_a_fresh_list_is_not_asked_for_forever(
    hass, mock_api
):
    """A colour slot is given up only when a fresh list shows it gone.

    The cached id outlives a slot deleted in the Trimlight app, and asking
    the controller for it would then fail on every turn-on. But a list that
    lacks the slot proves that only if it was fetched afresh, and late enough
    for the cloud to have caught up with the save. A list fetched too soon,
    or carried over because a poll came back without one (controllers have
    been seen to do that), says nothing, and giving the slot up on it put some
    other effect on the strip in place of the colour just picked.
    """
    with patch("custom_components.trimlight.light.time") as time_mock:
        time_mock.monotonic.return_value = 1000.0
        coordinator = await _setup_integration(
            hass, mock_api,
            entry_id="test_entry_slot_gone", unique_id="test_client_id_slot_gone",
        )
        await _turn_on(hass, **{ATTR_HS_COLOR: (0.0, 100.0)})  # Creates slot 99.
        await _turn_off(hass)

        # A fresh list without it, but the cloud may not have caught up yet.
        time_mock.monotonic.return_value = 1030.0
        coordinator.async_set_updated_data(_poll_without_the_colour_slot())
        await hass.async_block_till_done()
        await _turn_on(hass)
        mock_api.view_effect.assert_called_with(MOCK_DEVICE_ID, 99)
        await _turn_off(hass)

        # Much later, but the list is one carried over from an earlier poll.
        time_mock.monotonic.return_value = 1200.0
        coordinator.async_set_updated_data(
            _poll_without_the_colour_slot(_effects_carried=True)
        )
        await hass.async_block_till_done()
        await _turn_on(hass)
        mock_api.view_effect.assert_called_with(MOCK_DEVICE_ID, 99)
        await _turn_off(hass)

        # The same while the detail fetch is failing.
        time_mock.monotonic.return_value = 1400.0
        coordinator.async_set_updated_data(
            _poll_without_the_colour_slot(_detail_stale=True)
        )
        await hass.async_block_till_done()
        await _turn_on(hass)
        mock_api.view_effect.assert_called_with(MOCK_DEVICE_ID, 99)
        await _turn_off(hass)

        # A list fetched afresh, long after the save, and still no slot: it
        # was deleted, so an effect that does exist is shown in its place.
        time_mock.monotonic.return_value = 1600.0
        coordinator.async_set_updated_data(_poll_without_the_colour_slot())
        await hass.async_block_till_done()
        mock_api.view_effect.reset_mock()
        await _turn_on(hass)
        mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 1)


async def test_refused_reactivation_keeps_the_colour_slot(hass, mock_api):
    """A refusal to show the colour slot must not lead to a second slot.

    The cloud's result codes are generic (the documentation lists little more
    than "Error", and rate limiting answers with one too), so a refusal says
    nothing about whether the slot exists. Forgetting the cached id there made
    the next colour pick create another "HA Color" effect while the first was
    still on the controller, and duplicate names are a known cause of API
    error 12006.
    """
    await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_slot_kept", unique_id="test_client_id_slot_kept",
    )
    await _turn_on(hass, **{ATTR_HS_COLOR: (0.0, 100.0)})
    assert mock_api.save_effect.call_args[0][1]["id"] == -1  # Slot 99 created.
    await _turn_off(hass)

    mock_api.view_effect.side_effect = TrimlightApiError(
        "API error 20000: request too frequent", code=20000
    )
    await _turn_on(hass)  # The re-activation is refused, and that is swallowed.
    mock_api.view_effect.side_effect = None

    await _turn_on(hass, **{ATTR_HS_COLOR: (120.0, 100.0)})
    assert mock_api.save_effect.call_args[0][1]["id"] == 99


async def test_effect_is_remembered_across_a_restart_while_off(hass, mock_api):
    """Restarting while the lights are off must not forget what they showed.

    Home Assistant stores no effect for a light that is off, so the state it
    restores carries nothing. Outdoor lights are off most of the day, which is
    also when Home Assistant gets restarted, so the effect was routinely
    forgotten: the next turn-on had nothing to re-activate, and brightness
    alone was refused until an effect was picked again.
    """
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(_entity_id(), STATE_OFF),
                {
                    "effect": "INDEPENDENCE DAY",
                    "hs_color": [120.0, 100.0],
                    "brightness": 42,
                },
            )
        ],
    )
    await _setup_integration(
        hass, mock_api, coordinator_data=_unidentified_device(),
        entry_id="test_entry_remember", unique_id="test_client_id_remember",
    )
    await _turn_on(hass)

    # The remembered effect, not one picked because nothing was known.
    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 2)
    state = hass.states.get(_entity_id())
    assert state.attributes.get(ATTR_EFFECT) == "INDEPENDENCE DAY"


async def test_remembered_values_stand_in_while_detail_is_stale(hass, mock_api):
    """Colour and brightness saved while off stand in; the effect waits.

    With nothing live to go on, the saved colour and brightness are the best
    values to show. The effect is kept only to bring back when HA turns the
    light on from off: the light has been lit some other way here, so it is
    not claimed as what the strip shows.
    """
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(_entity_id(), STATE_OFF),
                {
                    "effect": "INDEPENDENCE DAY",
                    "hs_color": [120.0, 100.0],
                    "brightness": 42,
                },
            )
        ],
    )
    # Stale detail, as while a detail fetch is failing: nothing live
    # contradicts what was remembered.
    stale = {
        MOCK_DEVICE_ID: {**MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID], "_detail_stale": True}
    }
    await _setup_integration(
        hass, mock_api, coordinator_data=stale,
        entry_id="test_entry_remember_stale",
        unique_id="test_client_id_remember_stale",
    )
    state = hass.states.get(_entity_id())
    assert state.attributes.get(ATTR_EFFECT) is None
    assert state.attributes.get(ATTR_BRIGHTNESS) == 42
    assert tuple(state.attributes.get(ATTR_HS_COLOR)) == (120.0, 100.0)

    # Turned off and on from HA, it is the effect brought back.
    await _turn_off(hass)
    await _turn_on(hass)
    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 2)
    assert hass.states.get(_entity_id()).attributes.get(ATTR_EFFECT) == "INDEPENDENCE DAY"


async def test_remembered_effect_is_not_used_for_brightness_on_a_lit_light(
    hass, mock_api
):
    """A brightness change on a light lit since the restart refuses to guess.

    The light was off across a restart, and has since been lit from the
    Trimlight app with a pattern HA cannot identify. The effect it showed
    before the restart is no claim about what it shows now, so a brightness
    change must not re-save that effect over the running pattern.
    """
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(_entity_id(), STATE_OFF),
                {"effect": "NEW YEAR", "hs_color": [0.0, 0.0], "brightness": 255},
            )
        ],
    )
    coordinator = await _setup_integration(
        hass, mock_api, coordinator_data=_unidentified_device(),
        entry_id="test_entry_remember_lit", unique_id="test_client_id_remember_lit",
    )
    coordinator.async_set_updated_data(_unidentified_device(switch_state=1))
    await hass.async_block_till_done()
    assert hass.states.get(_entity_id()).state == STATE_ON

    with pytest.raises(ServiceValidationError, match="cannot tell which effect"):
        await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})
    assert _command_order(mock_api) == []


async def test_remembered_effect_survives_a_second_restart(hass, mock_api):
    """What is remembered is saved again, so two restarts in a row keep it."""
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(_entity_id(), STATE_OFF),
                {"effect": "INDEPENDENCE DAY", "hs_color": [0.0, 0.0], "brightness": 255},
            )
        ],
    )
    await _setup_integration(
        hass, mock_api, coordinator_data=_unidentified_device(),
        entry_id="test_entry_remember_twice", unique_id="test_client_id_remember_twice",
    )
    entity = hass.data["light"].get_entity(_entity_id())
    assert entity.extra_restore_state_data.as_dict()["effect"] == "INDEPENDENCE DAY"


async def test_unusable_remembered_values_are_ignored(hass, mock_api):
    """Saved data of the wrong shape must not break the entity or its state."""
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(_entity_id(), STATE_OFF),
                {"effect": 5, "hs_color": ["red", None], "brightness": True},
            )
        ],
    )
    stale = {
        MOCK_DEVICE_ID: {**MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID], "_detail_stale": True}
    }
    await _setup_integration(
        hass, mock_api, coordinator_data=stale,
        entry_id="test_entry_remember_junk",
        unique_id="test_client_id_remember_junk",
    )
    state = hass.states.get(_entity_id())
    assert state.state == STATE_ON
    assert state.attributes.get(ATTR_EFFECT) is None
    assert state.attributes.get(ATTR_BRIGHTNESS) == 255
    assert tuple(state.attributes.get(ATTR_HS_COLOR)) == (0.0, 0.0)


async def test_last_effect_is_saved_for_restore_even_when_off(hass, mock_api):
    """What the strip showed is handed to Home Assistant to keep, on or off."""
    await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_extra", unique_id="test_client_id_extra",
    )
    await _turn_on(
        hass, **{ATTR_EFFECT: "INDEPENDENCE DAY", ATTR_BRIGHTNESS: 42}
    )
    await _turn_off(hass)

    # The state itself carries none of it once the light is off...
    state = hass.states.get(_entity_id())
    assert state.state == STATE_OFF
    assert state.attributes.get(ATTR_EFFECT) is None
    assert state.attributes.get(ATTR_BRIGHTNESS) is None

    # ...which is why it is saved alongside.
    entity = hass.data["light"].get_entity(_entity_id())
    saved = entity.extra_restore_state_data.as_dict()
    assert saved["effect"] == "INDEPENDENCE DAY"
    assert saved["brightness"] == 42
    assert len(saved["hs_color"]) == 2


# --------------------------------------------------------------------------- #
# Pre-release review: colour slot, credentials and a stale schedule           #
# --------------------------------------------------------------------------- #


async def test_brightness_on_a_deleted_colour_slot_is_refused(hass, mock_api):
    """A brightness change must not re-create a colour slot deleted in the app.

    The light was showing HA's colour when HA last stopped, and the "HA Color"
    effect has since been deleted in the Trimlight app, which now has the
    strip on a pattern HA cannot identify. Re-saving the slot for a brightness
    change would create a new one and paint the strip a solid colour over the
    running pattern, so the change is refused like any for a deleted effect.
    """
    mock_restore_cache(
        hass,
        [State(_entity_id(), STATE_ON, {ATTR_EFFECT: HA_COLOR_EFFECT_NAME})],
    )
    await _setup_integration(
        hass, mock_api, coordinator_data=_unidentified_device(switch_state=1),
        entry_id="test_entry_slot_deleted", unique_id="test_client_id_slot_deleted",
    )
    assert (
        hass.states.get(_entity_id()).attributes.get(ATTR_EFFECT)
        == HA_COLOR_EFFECT_NAME
    )
    with pytest.raises(ServiceValidationError, match="no longer saved"):
        await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})
    assert _command_order(mock_api) == []


async def test_brightness_from_off_with_an_unresolvable_colour_slot_stays_on(
    hass, mock_api
):
    """A colour slot that cannot be found does not undo the power-on.

    HA remembers its colour, but the effect list has not loaded since the
    restart, so the slot cannot be found and nothing can take the brightness.
    The light is still turned on, as for any effect that cannot be found,
    rather than reported as failing to turn on and shown off again.
    """
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(_entity_id(), STATE_ON, {ATTR_EFFECT: HA_COLOR_EFFECT_NAME}),
                {"effect": HA_COLOR_EFFECT_NAME, "hs_color": [0.0, 100.0], "brightness": 255},
            )
        ],
    )
    list_only = {MOCK_DEVICE_ID: {**MOCK_DEVICE_LIST[0], "switchState": 0}}
    await _setup_integration(
        hass, mock_api, coordinator_data=list_only,
        entry_id="test_entry_slot_unloaded", unique_id="test_client_id_slot_unloaded",
    )
    assert hass.states.get(_entity_id()).state == STATE_OFF

    with pytest.raises(HomeAssistantError, match="could not set its brightness"):
        await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})

    assert _command_order(mock_api) == ["set_switch_state"]
    assert hass.states.get(_entity_id()).state == STATE_ON


async def test_rejected_credentials_fail_a_turn_on_that_re_activates(
    hass, mock_api
):
    """Credentials rejected mid turn-on are not swallowed as a minor hiccup.

    A failed re-activation is otherwise only logged, since the power-on has
    landed. Rejected credentials are different: nothing else will work
    either, so the call fails and says why.
    """
    off_data = {
        MOCK_DEVICE_ID: {**MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID], "switchState": 0}
    }
    await _setup_integration(
        hass, mock_api, coordinator_data=off_data,
        entry_id="test_entry_auth_resume", unique_id="test_client_id_auth_resume",
    )
    mock_api.view_effect.side_effect = TrimlightAuthError(
        "API auth error 12001: Invalid API authentication", code=12001
    )
    with pytest.raises(HomeAssistantError, match="12001"):
        await _turn_on(hass)


async def test_fallback_ignores_a_schedule_carried_over_from_a_failing_fetch(
    hass, mock_api
):
    """A schedule kept from before the detail fetch began failing is not used.

    It may no longer be the one on the controller, which is also why the
    on/off state is not derived from it, so the fallback takes the first
    saved effect instead of the effect that old schedule named.
    """
    data = _unidentified_device(
        daily=[{**_daily_entry((18, 0), (23, 0)), "effectId": 2}],
        _detail_stale=True,
    )
    await _setup_integration(
        hass, mock_api, coordinator_data=data,
        entry_id="test_entry_stale_sched", unique_id="test_client_id_stale_sched",
    )
    assert hass.states.get(_entity_id()).state == STATE_OFF

    await _turn_on(hass)

    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 1)


async def test_a_list_counts_from_when_it_was_fetched(hass, mock_api):
    """A list is as old as its fetch, however late its update arrives.

    The coordinator stamps each device's detail when it fetches it, and only
    notifies once every device is done, which can be over a minute later if
    another device's fetch keeps timing out. A list fetched before the colour
    was picked must not count as proof the new slot is gone because its
    update arrived late; one fetched well after it does count.
    """
    with patch("custom_components.trimlight.light.time") as time_mock:
        time_mock.monotonic.return_value = 1000.0
        coordinator = await _setup_integration(
            hass, mock_api,
            entry_id="test_entry_fetched_at", unique_id="test_client_id_fetched_at",
        )
        await _turn_on(hass, **{ATTR_HS_COLOR: (0.0, 100.0)})  # Creates slot 99.
        await _turn_off(hass)

        # Fetched just before the save, delivered 65 seconds after it.
        time_mock.monotonic.return_value = 1065.0
        early = _poll_without_the_colour_slot()
        early[MOCK_DEVICE_ID]["_fetched_at"] = 990.0
        coordinator.async_set_updated_data(early)
        await hass.async_block_till_done()
        mock_api.view_effect.reset_mock()
        await _turn_on(hass)
        mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 99)
        await _turn_off(hass)

        # Fetched well after the save: the slot really is gone.
        time_mock.monotonic.return_value = 1200.0
        late = _poll_without_the_colour_slot()
        late[MOCK_DEVICE_ID]["_fetched_at"] = 1190.0
        coordinator.async_set_updated_data(late)
        await hass.async_block_till_done()
        mock_api.view_effect.reset_mock()
        await _turn_on(hass)
        mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 1)


async def test_remembered_effect_gives_way_once_another_has_been_seen(
    hass, mock_api
):
    """The effect from before a restart stops counting once HA sees another.

    The light was showing INDEPENDENCE DAY before the restart. Since then HA
    saw NEW YEAR running, which has now been deleted in the Trimlight app.
    INDEPENDENCE DAY is not what the light was last showing any more, so a
    turn-on from off with a brightness must not save that brightness into it.
    """
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(_entity_id(), STATE_OFF),
                {"effect": "INDEPENDENCE DAY", "hs_color": [0.0, 0.0], "brightness": 255},
            )
        ],
    )
    device = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]
    coordinator = await _setup_integration(
        hass, mock_api, coordinator_data={MOCK_DEVICE_ID: {**device, "switchState": 1}},
        entry_id="test_entry_remember_seen", unique_id="test_client_id_remember_seen",
    )
    assert hass.states.get(_entity_id()).attributes.get(ATTR_EFFECT) == "NEW YEAR"

    # NEW YEAR deleted in the app and the light switched off there.
    coordinator.async_set_updated_data(
        {
            MOCK_DEVICE_ID: {
                **device,
                "switchState": 0,
                "effects": [device["effects"][1]],
                "currentEffect": _UNMATCHED_CURRENT_EFFECT,
            }
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get(_entity_id()).state == STATE_OFF

    with pytest.raises(HomeAssistantError, match="left the brightness"):
        await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})
    mock_api.save_effect.assert_not_called()


async def test_picked_effect_that_fails_to_show_still_leaves_the_light_on(
    hass, mock_api
):
    """A picked effect for a turn-on with a brightness is shown best-effort.

    The power-on landed, so the light is on, as after a plain turn-on whose
    re-activation failed. The call reports that the effect could not be
    shown, and the next turn-on tries again.
    """
    await _setup_integration(
        hass, mock_api, coordinator_data=_unidentified_device(),
        entry_id="test_entry_picked_fail", unique_id="test_client_id_picked_fail",
    )
    mock_api.view_effect.side_effect = TrimlightApiError(
        "API error 20000: request too frequent", code=20000
    )
    with pytest.raises(HomeAssistantError, match="could not show 'NEW YEAR'"):
        await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})
    assert hass.states.get(_entity_id()).state == STATE_ON
    mock_api.save_effect.assert_not_called()

    mock_api.view_effect.side_effect = None
    mock_api.reset_mock()
    await _turn_on(hass)
    assert _command_order(mock_api) == ["set_switch_state", "view_effect"]


async def test_power_on_lost_in_transit_leaves_a_note(hass, mock_api):
    """A power-on with no answer may still have reached the controller.

    HA reports the failure and shows the light off. If the next poll shows it
    on after all, the strip may be dark, so a turn-on soon after re-activates
    the effect instead of taking that "on" as settled. A power-on the cloud
    rejected outright leaves no such note.
    """
    off_data = {
        MOCK_DEVICE_ID: {**MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID], "switchState": 0}
    }
    coordinator = await _setup_integration(
        hass, mock_api, coordinator_data=off_data,
        entry_id="test_entry_lost_power", unique_id="test_client_id_lost_power",
    )
    mock_api.set_switch_state.side_effect = TrimlightApiError(
        "Request to /path failed: TimeoutError"
    )
    with pytest.raises(HomeAssistantError):
        await _turn_on(hass)
    assert hass.states.get(_entity_id()).state == STATE_OFF
    mock_api.set_switch_state.side_effect = None

    coordinator.async_set_updated_data(MOCK_COORDINATOR_DATA)  # It did land.
    await hass.async_block_till_done()
    assert hass.states.get(_entity_id()).state == STATE_ON

    mock_api.reset_mock()
    await _turn_on(hass)
    assert _command_order(mock_api) == ["set_switch_state", "view_effect"]


async def test_rejected_power_on_leaves_no_note(hass, mock_api):
    """A power-on the cloud answered with an error did not happen."""
    off_data = {
        MOCK_DEVICE_ID: {**MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID], "switchState": 0}
    }
    coordinator = await _setup_integration(
        hass, mock_api, coordinator_data=off_data,
        entry_id="test_entry_refused_power", unique_id="test_client_id_refused_power",
    )
    mock_api.set_switch_state.side_effect = TrimlightApiError(
        "API error 10001: Error", code=10001
    )
    with pytest.raises(HomeAssistantError):
        await _turn_on(hass)
    mock_api.set_switch_state.side_effect = None

    # Lit since from the Trimlight app.
    coordinator.async_set_updated_data(MOCK_COORDINATOR_DATA)
    await hass.async_block_till_done()
    mock_api.reset_mock()
    await _turn_on(hass)
    assert _command_order(mock_api) == ["set_switch_state"]


async def test_brightness_refusal_before_the_list_loads_claims_no_deletion(
    hass, mock_api
):
    """An effects list that has not loaded says nothing about deleted effects."""
    mock_restore_cache(
        hass, [State(_entity_id(), STATE_ON, {ATTR_EFFECT: "NEW YEAR"})]
    )
    device = MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID]
    no_list = {
        MOCK_DEVICE_ID: {k: v for k, v in device.items() if k != "effects"}
    }
    await _setup_integration(
        hass, mock_api, coordinator_data=no_list,
        entry_id="test_entry_no_list_yet", unique_id="test_client_id_no_list_yet",
    )
    with pytest.raises(ServiceValidationError, match="does not know") as err:
        await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})
    assert "no longer saved" not in str(err.value)


async def test_a_note_gives_way_to_a_change_made_elsewhere(hass, mock_api):
    """A failed turn-on's note does not outlast someone lighting the strip.

    HA's power-on got no answer, so it may have left the strip dark and the
    next turn-on is meant to try again. But the next poll shows the light on
    running a pattern started from the Trimlight app meanwhile, not the one
    the controller reported before. The strip is lit, so a turn_on from HA
    must leave that pattern alone.
    """
    off_data = {
        MOCK_DEVICE_ID: {**MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID], "switchState": 0}
    }
    coordinator = await _setup_integration(
        hass, mock_api, coordinator_data=off_data,
        entry_id="test_entry_note_elsewhere", unique_id="test_client_id_note_elsewhere",
    )
    mock_api.set_switch_state.side_effect = TrimlightApiError(
        "Request to /path failed: TimeoutError"
    )
    with pytest.raises(HomeAssistantError):
        await _turn_on(hass)
    mock_api.set_switch_state.side_effect = None

    coordinator.async_set_updated_data(_unidentified_device(switch_state=1))
    await hass.async_block_till_done()
    mock_api.reset_mock()
    await _turn_on(hass)

    assert _command_order(mock_api) == ["set_switch_state"]


async def test_a_note_gives_way_to_a_change_seen_during_the_cooldown(
    hass, mock_api
):
    """The same holds while HA still holds its optimistic state.

    The re-activation failed, so the note was left with the cooldown running
    and polls not yet taken into account. A poll in that time still shows
    whether the controller is running something new.
    """
    off_data = {
        MOCK_DEVICE_ID: {**MOCK_COORDINATOR_DATA[MOCK_DEVICE_ID], "switchState": 0}
    }
    coordinator = await _setup_integration(
        hass, mock_api, coordinator_data=off_data,
        entry_id="test_entry_note_cooldown", unique_id="test_client_id_note_cooldown",
    )
    mock_api.view_effect.side_effect = TrimlightApiError(
        "Request to /path failed: TimeoutError"
    )
    await _turn_on(hass)  # Powered on; re-activating NEW YEAR failed.
    mock_api.view_effect.side_effect = None

    coordinator.async_set_updated_data(_unidentified_device(switch_state=1))
    await hass.async_block_till_done()
    mock_api.reset_mock()
    await _turn_on(hass)

    assert _command_order(mock_api) == ["set_switch_state"]


def test_scheduled_effect_ids_put_a_one_off_entry_after_a_repeating_one():
    """A "today only" entry, which may be left from an old date, ranks last.

    Both entries have their window open now. The one-off is listed first,
    but the everyday entry is the better guess at what these lights show.
    """
    one_off = {**_daily_entry((18, 0), (23, 0), repetition=0), "effectId": 2}
    everyday = {**_daily_entry((18, 0), (23, 0), repetition=1), "effectId": 1}
    assert _scheduled_effect_ids({"daily": [one_off, everyday]}, _MONDAY) == [1, 2]
