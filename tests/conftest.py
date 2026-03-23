"""Shared fixtures for Trimlight Edge tests."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.trimlight.api import TrimlightApi
from custom_components.trimlight.const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, DOMAIN

MOCK_DEVICE_ID = "device_abc123"
MOCK_DEVICE_NAME = "Front House"

MOCK_DEVICE_LIST = [
    {
        "deviceId": MOCK_DEVICE_ID,
        "name": MOCK_DEVICE_NAME,
        "switchState": 1,
        "connectivity": 1,
        "state": 0,
        "fwVersionName": "1.1.1",
    }
]

MOCK_DEVICE_DETAIL = {
    "name": MOCK_DEVICE_NAME,
    "switchState": 1,
    "connectivity": 1,
    "state": 0,
    "colorOrder": 0,
    "ic": 0,
    "fwVersionName": "1.1.1",
    "effects": [
        {
            "id": 0,
            "name": "New Year",
            "category": 0,
            "mode": 0,
            "speed": 100,
            "brightness": 200,
            "pixelLen": 30,
            "reverse": False,
        },
        {
            "id": 1,
            "name": "Christmas",
            "category": 0,
            "mode": 5,
            "speed": 80,
            "brightness": 255,
            "pixelLen": 20,
            "reverse": False,
        },
    ],
    "currentEffect": {
        "category": 0,
        "mode": 0,
        "speed": 100,
        "brightness": 200,
        "pixelLen": 30,
        "reverse": False,
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
    api.preview_effect = AsyncMock(return_value=None)
    return api


@pytest.fixture
def mock_config_entry_data():
    return {CONF_CLIENT_ID: "test_client_id", CONF_CLIENT_SECRET: "test_secret"}
