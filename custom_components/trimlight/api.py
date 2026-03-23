"""Trimlight Edge API client."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

import aiohttp
import async_timeout

from .const import API_BASE_URL


class TrimlightApiError(Exception):
    """Raised when the Trimlight API returns an error."""


class TrimlightApi:
    """Client for the Trimlight V2 OAuth API."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        session: aiohttp.ClientSession,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._session = session

    def _build_headers(self) -> dict[str, str]:
        """Build auth headers per the HMAC-SHA256 scheme.

        Token = base64(HMAC-SHA256("Trimlight|<clientId>|<timestamp>", clientSecret))
        """
        timestamp = int(time.time() * 1000)
        message = f"Trimlight|{self._client_id}|{timestamp}"
        sig = hmac.new(
            self._client_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).digest()
        token = base64.b64encode(sig).decode()
        return {
            "authorization": token,
            "S-ClientId": self._client_id,
            "S-Timestamp": str(timestamp),
            "Content-Type": "application/json",
        }

    async def _request(
        self, method: str, path: str, data: dict[str, Any] | None = None
    ) -> Any:
        """Make an authenticated request.

        Note: The API uses JSON bodies even for GET requests, as documented.
        """
        url = f"{API_BASE_URL}{path}"
        headers = self._build_headers()
        async with async_timeout.timeout(10):
            resp = await self._session.request(
                method, url, headers=headers, json=data
            )
            result = await resp.json()
        code = result.get("code")
        if code != 0:
            raise TrimlightApiError(
                f"API error {code}: {result.get('desc', 'unknown')}"
            )
        return result.get("payload")

    # ------------------------------------------------------------------ #
    # Device discovery                                                     #
    # ------------------------------------------------------------------ #

    async def get_devices(self) -> list[dict]:
        """Return all devices in the account."""
        payload = await self._request(
            "GET", "/v1/oauth/resources/devices", {"page": 0}
        )
        return payload["data"]

    async def get_device(self, device_id: str) -> dict:
        """Return full detail for a single device (effects, schedules, etc.)."""
        return await self._request(
            "POST",
            "/v1/oauth/resources/device/get",
            {"deviceId": device_id},
        )

    async def notify_update_shadow(self, device_id: str) -> None:
        """Ask the device to push its latest shadow data before polling."""
        try:
            await self._request(
                "GET",
                "/v1/oauth/resources/device/notify-update-shadow",
                {"deviceId": device_id},
            )
        except TrimlightApiError:
            # Non-critical; best-effort
            pass

    # ------------------------------------------------------------------ #
    # Device control                                                       #
    # ------------------------------------------------------------------ #

    async def set_switch_state(self, device_id: str, state: int) -> None:
        """Set the switch state: 0=off, 1=manual, 2=timer."""
        await self._request(
            "POST",
            "/v1/oauth/resources/device/update",
            {"deviceId": device_id, "payload": {"switchState": state}},
        )

    async def view_effect(self, device_id: str, effect_id: int) -> None:
        """Activate a saved effect by its ID."""
        await self._request(
            "POST",
            "/v1/oauth/resources/device/effect/view",
            {"deviceId": device_id, "payload": {"id": effect_id}},
        )

    async def preview_effect(self, device_id: str, effect_payload: dict) -> None:
        """Preview (play) an effect without saving it.

        Used for real-time brightness / colour adjustments.
        effect_payload must match the build-in or custom effect schema.
        """
        await self._request(
            "POST",
            "/v1/oauth/resources/device/effect/preview",
            {"deviceId": device_id, "payload": effect_payload},
        )
