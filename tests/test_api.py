"""Tests for the Trimlight API client."""

import base64
import hashlib
import hmac
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.trimlight.api import TrimlightApi, TrimlightApiError


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
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(
            return_value={"code": 10001, "desc": "auth error"}
        )
        api._session.request = AsyncMock(return_value=mock_resp)

        with pytest.raises(TrimlightApiError, match="10001"):
            await api._request("GET", "/some/path")

    @pytest.mark.asyncio
    async def test_returns_payload_on_success(self, api):
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(
            return_value={"code": 0, "desc": "success", "payload": {"key": "value"}}
        )
        api._session.request = AsyncMock(return_value=mock_resp)

        result = await api._request("GET", "/some/path")
        assert result == {"key": "value"}

    @pytest.mark.asyncio
    async def test_returns_none_when_no_payload(self, api):
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"code": 0, "desc": "success"})
        api._session.request = AsyncMock(return_value=mock_resp)

        result = await api._request("POST", "/some/path")
        assert result is None


class TestGetDevices:
    """Tests for the get_devices method."""

    @pytest.mark.asyncio
    async def test_returns_device_list(self, api):
        devices = [{"deviceId": "abc", "name": "Front"}]
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(
            return_value={"code": 0, "payload": {"data": devices}}
        )
        api._session.request = AsyncMock(return_value=mock_resp)

        result = await api.get_devices()
        assert result == devices


class TestNotifyUpdateShadow:
    """Tests for the notify_update_shadow method."""

    @pytest.mark.asyncio
    async def test_swallows_api_error(self, api):
        """notify_update_shadow should not propagate errors — it's best-effort."""
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(
            return_value={"code": 10001, "desc": "error"}
        )
        api._session.request = AsyncMock(return_value=mock_resp)

        # Should not raise.
        await api.notify_update_shadow("device_123")


class TestSaveEffect:
    """Tests for the save_effect method."""

    @pytest.mark.asyncio
    async def test_returns_saved_id(self, api):
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(
            return_value={"code": 0, "payload": {"id": 42}}
        )
        api._session.request = AsyncMock(return_value=mock_resp)

        result = await api.save_effect("device_123", {"id": -1, "name": "Test"})
        assert result == {"id": 42}
