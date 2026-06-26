"""Trimlight Edge API client.

Implements the Trimlight V2 OAuth API (HMAC-SHA256 auth).
All requests are serialized via an asyncio lock — the Trimlight server
returns error 20000 when it receives concurrent requests from the same client.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time
from typing import Any

import aiohttp

from homeassistant.util import dt as dt_util

from .const import (
    API_BASE_URL,
    API_MAX_REQUEST_ATTEMPTS,
    API_REQUEST_MIN_INTERVAL,
    API_RETRY_BASE_BACKOFF,
)

_LOGGER = logging.getLogger(__name__)


class TrimlightApiError(Exception):
    """Raised when the Trimlight API returns a non-zero result code."""


def _is_retryable(err: Exception) -> bool:
    """Return True for transient transport errors worth retrying.

    Server-side 5xx and rate-limit (429) responses, timeouts, and connection
    errors are transient. Client errors (4xx) and unparseable bodies are not
    retried — they won't change on a repeat. API result-code errors (e.g. a
    device being offline) never reach here; they're raised after a successful
    HTTP exchange.
    """
    if isinstance(err, aiohttp.ClientResponseError):
        return err.status >= 500 or err.status == 429
    if isinstance(err, (TimeoutError, aiohttp.ClientError)):
        return True
    return False


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
        self._lock = asyncio.Lock()
        self._last_request_time: float = 0.0

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

        Requests are serialized via a lock and rate-limited to prevent
        the Trimlight server from returning error 20000.

        Transient transport errors (5xx, timeouts, dropped connections) are
        retried with exponential backoff; everything else fails fast.

        Raises TrimlightApiError on network failures, timeouts, HTTP errors,
        unparseable responses, and non-zero API result codes.
        """
        url = f"{API_BASE_URL}{path}"
        async with self._lock:
            result = None
            for attempt in range(API_MAX_REQUEST_ATTEMPTS):
                elapsed = time.monotonic() - self._last_request_time
                if elapsed < API_REQUEST_MIN_INTERVAL:
                    await asyncio.sleep(API_REQUEST_MIN_INTERVAL - elapsed)

                headers = self._build_headers()
                try:
                    async with asyncio.timeout(10):
                        resp = await self._session.request(
                            method, url, headers=headers, json=data
                        )
                        resp.raise_for_status()
                        result = await resp.json()
                    break
                except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                    last_attempt = attempt == API_MAX_REQUEST_ATTEMPTS - 1
                    if last_attempt or not _is_retryable(err):
                        raise TrimlightApiError(
                            f"Request to {path} failed: {err}"
                        ) from err
                    _LOGGER.debug(
                        "Transient error on %s (attempt %d/%d), retrying: %s",
                        path, attempt + 1, API_MAX_REQUEST_ATTEMPTS, err,
                    )
                    await asyncio.sleep(API_RETRY_BASE_BACKOFF * 2**attempt)
                finally:
                    # Stamp even on failure: the server may have processed the
                    # request, so the next one must still be spaced out.
                    self._last_request_time = time.monotonic()

        if not isinstance(result, dict):
            raise TrimlightApiError(f"Unexpected response from {path}: {result!r}")
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
        return (payload or {}).get("data") or []

    @staticmethod
    def _current_date() -> dict:
        """Return the current date/time dict the API expects.

        Uses Home Assistant's configured timezone rather than the host's:
        the two often differ (e.g. UTC containers), and this value drives
        the device clock and timer schedules.
        """
        now = dt_util.now()
        # API weekday: SUNDAY=1 … SATURDAY=7; Python isoweekday: MON=1 … SUN=7
        weekday = now.isoweekday() % 7 + 1
        return {
            "year": now.year - 2000,
            "month": now.month,
            "day": now.day,
            "weekday": weekday,
            "hours": now.hour,
            "minutes": now.minute,
            "seconds": now.second,
        }

    async def get_device(self, device_id: str) -> dict:
        """Return full detail for a single device (effects, schedules, etc.)."""
        return await self._request(
            "POST",
            "/v1/oauth/resources/device/get",
            {"deviceId": device_id, "currentDate": self._current_date()},
        )

    async def notify_update_shadow(self, device_id: str) -> None:
        """Ask the device to push its latest shadow data before polling."""
        try:
            await self._request(
                "GET",
                "/v1/oauth/resources/device/notify-update-shadow",
                {"deviceId": device_id, "currentDate": self._current_date()},
            )
        except TrimlightApiError:
            pass  # Non-critical; best-effort

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

    async def save_effect(self, device_id: str, effect_payload: dict) -> dict:
        """Save (create or update) an effect on the device.

        Set effect_payload["id"] = -1 to create new, or an existing ID to update.
        Returns the payload containing the saved effect's id.
        """
        return await self._request(
            "POST",
            "/v1/oauth/resources/device/effect/save",
            {"deviceId": device_id, "payload": effect_payload},
        )
