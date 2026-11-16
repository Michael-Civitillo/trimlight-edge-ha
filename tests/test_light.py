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

from custom_components.trimlight.api import TrimlightApiError
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
    """A plain turn_on must send its commands even when HA thinks it is on.

    Regression test: HA's belief comes from a poll up to 30 seconds old, or
    from optimistic state, so trusting it meant a plain turn_on could send
    nothing at all. Users saw the lights stay off while HA showed them on,
    and only picking an effect or a colour actually turned them on. The
    effect it re-activates is the one already running, so nothing is re-saved.
    """
    await _setup_integration(hass, mock_api)
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": _entity_id()}, blocking=True
    )
    mock_api.set_switch_state.assert_called_once_with(MOCK_DEVICE_ID, 1)
    # The fixture's controller reports NEW YEAR (id 1) running.
    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 1)
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
    # Brightness alone routes through the color slot on a fresh entity too.
    # Brightness alone is refused earlier still: with no detail there is
    # nothing to re-save but the color slot, and that would repaint the strip.
    with pytest.raises(ServiceValidationError, match="does not know"):
        await hass.services.async_call(
            "light",
            "turn_on",
            {"entity_id": _entity_id(), ATTR_BRIGHTNESS: 100},
            blocking=True,
        )
    mock_api.save_effect.assert_not_called()
    mock_api.view_effect.assert_not_called()
    # The optimistic write was rolled back: HA doesn't claim a color applied.
    state = hass.states.get(_entity_id())
    # A device list carries no switchState, so with no detail the on/off state
    # is genuinely unknown rather than guessed at.
    assert state.state == STATE_UNKNOWN
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

    It accepts a switch to MANUAL and reports itself on, but shows nothing
    until an effect is activated. Re-issuing MANUAL is modelled as blanking
    the strip too, the least forgiving reading of what was observed, so every
    command path has to end by saying what to show.
    """

    def __init__(self):
        self.showing = None  # The effect id on the strip; None while dark.

    def attach(self, mock_api):
        mock_api.set_switch_state.side_effect = self._set_switch_state
        mock_api.view_effect.side_effect = self._view_effect
        mock_api.save_effect.side_effect = self._save_effect

    async def _set_switch_state(self, device_id, state):
        self.showing = None

    async def _view_effect(self, device_id, effect_id):
        self.showing = effect_id

    async def _save_effect(self, device_id, payload):
        return {"id": 99 if payload["id"] == -1 else payload["id"]}


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

    await _turn_on(hass)
    assert hass.states.get(_entity_id()).state == STATE_ON
    assert controller.showing is not None  # Was dark while HA showed it on.

    await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})
    assert controller.showing is not None  # Was refused, so still dark.

    await _turn_on(hass, **{ATTR_EFFECT: "INDEPENDENCE DAY"})
    assert controller.showing == 2

    # And the round trip that follows: off, then on again, brings it back.
    await _turn_off(hass)
    assert controller.showing is None
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


async def test_brightness_from_off_with_unknown_effect_turns_on(hass, mock_api):
    """Dragging the brightness slider on an off light must light it.

    This used to be refused outright whenever HA did not know the effect,
    which left an automation such as "turn on at 40%" failing with the lights
    off. Turning on from off picks an effect the same way a plain turn-on
    does, and applies the brightness to it.
    """
    await _setup_integration(
        hass, mock_api, coordinator_data=_unidentified_device(),
        entry_id="test_entry_bri_off", unique_id="test_client_id_bri_off",
    )
    await _turn_on(hass, **{ATTR_BRIGHTNESS: 100})

    assert _command_order(mock_api) == [
        "set_switch_state", "save_effect", "view_effect",
    ]
    payload = mock_api.save_effect.call_args[0][1]
    assert payload["name"] == "NEW YEAR"
    assert payload["brightness"] == 100
    state = hass.states.get(_entity_id())
    assert state.state == STATE_ON
    assert state.attributes.get(ATTR_EFFECT) == "NEW YEAR"
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
    with pytest.raises(ServiceValidationError, match="cannot tell which effect"):
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
    # It leaves a trace for the controllers that do need the step.
    assert "could not re-activate 'NEW YEAR'" in caplog.text


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


async def test_refused_colour_slot_is_forgotten(hass, mock_api):
    """A colour slot the cloud refuses to show is not asked for again.

    The cached id outlives the slot if the effect is deleted in the Trimlight
    app. Only an answer from the cloud says so, though: a timeout says nothing
    about whether the slot exists, and forgetting it then would make the next
    colour pick create a duplicate.
    """
    await _setup_integration(
        hass, mock_api,
        entry_id="test_entry_slot_gone", unique_id="test_client_id_slot_gone",
    )
    await _turn_on(hass, **{ATTR_HS_COLOR: (0.0, 100.0)})

    # A transport failure: the slot is still asked for next time.
    await _turn_off(hass)
    mock_api.view_effect.side_effect = TrimlightApiError(
        "Request to /path failed: TimeoutError"
    )
    await _turn_on(hass)
    mock_api.view_effect.assert_called_with(MOCK_DEVICE_ID, 99)

    # The cloud answers that it will not show it: dropped.
    await _turn_off(hass)
    mock_api.view_effect.side_effect = TrimlightApiError(
        "API error 12004: effect not found", code=12004
    )
    await _turn_on(hass)
    mock_api.view_effect.assert_called_with(MOCK_DEVICE_ID, 99)

    # So the next turn-on shows an effect that does exist.
    await _turn_off(hass)
    mock_api.view_effect.side_effect = None
    mock_api.view_effect.reset_mock()
    await _turn_on(hass)
    mock_api.view_effect.assert_called_once_with(MOCK_DEVICE_ID, 1)


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
    """Colour and brightness saved while off come back with the effect."""
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
    assert state.attributes.get(ATTR_EFFECT) == "INDEPENDENCE DAY"
    assert state.attributes.get(ATTR_BRIGHTNESS) == 42
    assert tuple(state.attributes.get(ATTR_HS_COLOR)) == (120.0, 100.0)


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
