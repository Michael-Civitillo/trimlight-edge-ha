"""Shared fixtures for Trimlight Edge tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.trimlight.api import TrimlightApi
from custom_components.trimlight.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    DOMAIN,
    HA_COLOR_EFFECT_NAME,
)

MOCK_DEVICE_ID = "device_abc123"
MOCK_DEVICE_NAME = "Front House"

MOCK_DEVICE_LIST = [
    {
        "deviceId": MOCK_DEVICE_ID,
        "name": MOCK_DEVICE_NAME,
        "switchState": 1,
        "connectivity": 1,
        "state": 0,
        "fwVersionName": "1.17.4171",
    }
]

MOCK_DEVICE_DETAIL = {
    "name": MOCK_DEVICE_NAME,
    "switchState": 1,
    "connectivity": 1,
    "state": 0,
    "colorOrder": 0,
    "ic": 0,
    "fwVersionName": "1.17.4171",
    "ports": [
        {"id": 1, "start": 1, "end": 76},
        {"id": 2, "start": 77, "end": 92},
    ],
    "effects": [
        {
            "id": 1,
            "name": "NEW YEAR",
            "category": 2,
            "mode": 0,
            "speed": 127,
            "brightness": 255,
            "pixels": [
                {"index": 0, "count": 1, "color": 14509076, "disable": False},
                *[
                    {"index": i, "count": 0, "color": 0, "disable": False}
                    for i in range(1, 30)
                ],
            ],
        },
        {
            "id": 2,
            "name": "INDEPENDENCE DAY",
            "category": 2,
            "mode": 0,
            "speed": 127,
            "brightness": 255,
            "pixels": [
                {"index": 0, "count": 5, "color": 16711680, "disable": False},
                {"index": 1, "count": 5, "color": 16777215, "disable": False},
                {"index": 2, "count": 5, "color": 255, "disable": False},
                *[
                    {"index": i, "count": 0, "color": 0, "disable": False}
                    for i in range(3, 30)
                ],
            ],
        },
    ],
    "currentEffect": {
        "category": 2,
        "mode": 0,
        "speed": 127,
        "brightness": 255,
    },
    "combinedEffect": {"effectIds": [], "interval": 5},
    "daily": [],
    "calendar": [],
}

MOCK_COORDINATOR_DATA = {
    MOCK_DEVICE_ID: {**MOCK_DEVICE_LIST[0], **MOCK_DEVICE_DETAIL}
}


@pytest.fixture
def mock_api():
    """Return a fully mocked TrimlightApi."""
    api = MagicMock(spec=TrimlightApi)
    api.get_devices = AsyncMock(return_value=MOCK_DEVICE_LIST)
    api.get_device = AsyncMock(return_value=MOCK_DEVICE_DETAIL)
    api.notify_update_shadow = AsyncMock(return_value=None)
    api.set_switch_state = AsyncMock(return_value=None)
    api.view_effect = AsyncMock(return_value=None)
    api.save_effect = AsyncMock(return_value={"id": 99})
    return api


@pytest.fixture
def mock_config_entry_data():
    """Return valid config entry data."""
    return {CONF_CLIENT_ID: "test_client_id", CONF_CLIENT_SECRET: "test_secret"}
