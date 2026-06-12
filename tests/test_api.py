"""Tests for the Trimlight API client."""

import base64
import hashlib
import hmac
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.trimlight.api import TrimlightApi, TrimlightApiError


def _mock_response(json_value):
    """Return a mocked aiohttp response with the given JSON body."""
    resp = AsyncMock()
    resp.raise_for_status = MagicMock()  # Sync method in aiohttp.
    resp.json = AsyncMock(return_value=json_value)
    return resp


@pytest.fixture
def api():
    """Return a TrimlightApi instance with a mocked session."""
    session = MagicMock()
    return TrimlightApi("my_client", "my_secret", session)


class TestBuildHeaders:
    """Tests for the HMAC-SHA256 auth header generation."""

    def test_contains_required_keys(self, api):
        headers = api._build_headers()
        assert "authorization" in headers
        assert "S-ClientId" in headers
        assert "S-Timestamp" in headers
        assert "Content-Type" in headers

    def test_client_id_in_header(self, api):
        headers = api._build_headers()
        assert headers["S-ClientId"] == "my_client"

    def test_timestamp_is_numeric_string(self, api):
        headers = api._build_headers()
        assert headers["S-Timestamp"].isdigit()

    def test_token_is_valid_base64(self, api):
        headers = api._build_headers()
        base64.b64decode(headers["authorization"])  # Should not raise.

    def test_token_matches_hmac_computation(self, api):
        with patch("time.time", return_value=1713166849.256):
            headers = api._build_headers()

        timestamp = headers["S-Timestamp"]
        message = f"Trimlight|my_client|{timestamp}"
        expected_sig = hmac.new(
            b"my_secret", message.encode(), hashlib.sha256
        ).digest()
        expected_token = base64.b64encode(expected_sig).decode()
        assert headers["authorization"] == expected_token


class TestRequest:
    """Tests for the underlying _request method."""

    @pytest.mark.asyncio
    async def test_raises_on_nonzero_code(self, api):
        mock_resp = _mock_response({"code": 10001, "desc": "auth error"})
        api._session.request = AsyncMock(return_value=mock_resp)

        with pytest.raises(TrimlightApiError, match="10001"):
            await api._request("GET", "/some/path")

    @pytest.mark.asyncio
    async def test_returns_payload_on_success(self, api):
        mock_resp = _mock_response(
            {"code": 0, "desc": "success", "payload": {"key": "value"}}
        )
        api._session.request = AsyncMock(return_value=mock_resp)

        result = await api._request("GET", "/some/path")
        assert result == {"key": "value"}

    @pytest.mark.asyncio
    async def test_returns_none_when_no_payload(self, api):
        mock_resp = _mock_response({"code": 0, "desc": "success"})
        api._session.request = AsyncMock(return_value=mock_resp)

        result = await api._request("POST", "/some/path")
        assert result is None

    @pytest.mark.asyncio
    async def test_wraps_network_error(self, api):
        api._session.request = AsyncMock(
            side_effect=aiohttp.ClientError("connection refused")
        )

        with pytest.raises(TrimlightApiError, match="connection refused"):
            await api._request("GET", "/some/path")

    @pytest.mark.asyncio
    async def test_wraps_timeout(self, api):
        api._session.request = AsyncMock(side_effect=TimeoutError())

        with pytest.raises(TrimlightApiError):
            await api._request("GET", "/some/path")

    @pytest.mark.asyncio
    async def test_wraps_http_error_status(self, api):
        mock_resp = _mock_response(None)
        mock_resp.raise_for_status = MagicMock(
            side_effect=aiohttp.ClientResponseError(
                request_info=MagicMock(), history=(), status=502
            )
        )
        api._session.request = AsyncMock(return_value=mock_resp)

        with pytest.raises(TrimlightApiError):
            await api._request("GET", "/some/path")

    @pytest.mark.asyncio
    async def test_wraps_invalid_json(self, api):
        mock_resp = _mock_response(None)
        mock_resp.json = AsyncMock(side_effect=ValueError("not JSON"))
        api._session.request = AsyncMock(return_value=mock_resp)

        with pytest.raises(TrimlightApiError, match="not JSON"):
            await api._request("GET", "/some/path")

    @pytest.mark.asyncio
    async def test_raises_on_non_dict_response(self, api):
        mock_resp = _mock_response(["unexpected", "list"])
        api._session.request = AsyncMock(return_value=mock_resp)

        with pytest.raises(TrimlightApiError, match="Unexpected response"):
            await api._request("GET", "/some/path")

    @pytest.mark.asyncio
    async def test_failed_request_still_stamps_rate_limit(self, api):
        api._session.request = AsyncMock(side_effect=aiohttp.ClientError("boom"))

        with pytest.raises(TrimlightApiError):
            await api._request("GET", "/some/path")
        assert api._last_request_time > 0.0


class TestCurrentDate:
    """Tests for the currentDate payload helper."""

    def test_field_mapping(self):
        # 2026-06-14 is a Sunday — API expects SUNDAY=1.
        fake_now = datetime(2026, 6, 14, 10, 30, 5)
        with patch(
            "custom_components.trimlight.api.dt_util.now", return_value=fake_now
        ):
            result = TrimlightApi._current_date()

        assert result == {
            "year": 26,
            "month": 6,
            "day": 14,
            "weekday": 1,
            "hours": 10,
            "minutes": 30,
            "seconds": 5,
        }

    def test_weekday_saturday(self):
        # 2026-06-13 is a Saturday — API expects SATURDAY=7.
        fake_now = datetime(2026, 6, 13)
        with patch(
            "custom_components.trimlight.api.dt_util.now", return_value=fake_now
        ):
            assert TrimlightApi._current_date()["weekday"] == 7

    def test_weekday_monday(self):
        # 2026-06-15 is a Monday — API expects MONDAY=2.
        fake_now = datetime(2026, 6, 15)
        with patch(
            "custom_components.trimlight.api.dt_util.now", return_value=fake_now
        ):
            assert TrimlightApi._current_date()["weekday"] == 2


class TestGetDevices:
    """Tests for the get_devices method."""

    @pytest.mark.asyncio
    async def test_returns_device_list(self, api):
        devices = [{"deviceId": "abc", "name": "Front"}]
        mock_resp = _mock_response({"code": 0, "payload": {"data": devices}})
        api._session.request = AsyncMock(return_value=mock_resp)

        result = await api.get_devices()
        assert result == devices


class TestNotifyUpdateShadow:
    """Tests for the notify_update_shadow method."""

    @pytest.mark.asyncio
    async def test_swallows_api_error(self, api):
        """notify_update_shadow should not propagate errors — it's best-effort."""
        mock_resp = _mock_response({"code": 10001, "desc": "error"})
        api._session.request = AsyncMock(return_value=mock_resp)

        # Should not raise.
        await api.notify_update_shadow("device_123")

    @pytest.mark.asyncio
    async def test_swallows_network_error(self, api):
        """A transient network failure must not abort the poll cycle."""
        api._session.request = AsyncMock(side_effect=aiohttp.ClientError("down"))

        # Should not raise.
        await api.notify_update_shadow("device_123")


class TestSaveEffect:
    """Tests for the save_effect method."""

    @pytest.mark.asyncio
    async def test_returns_saved_id(self, api):
        mock_resp = _mock_response({"code": 0, "payload": {"id": 42}})
        api._session.request = AsyncMock(return_value=mock_resp)

        result = await api.save_effect("device_123", {"id": -1, "name": "Test"})
        assert result == {"id": 42}
