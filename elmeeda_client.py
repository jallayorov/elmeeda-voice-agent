"""Elmeeda Fleet Management API client with automatic auth token caching and refresh."""

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class ElmeedaClient:
    """Async HTTP client for the Elmeeda API.

    Handles OAuth2-style token acquisition via POST /auth/token (form-encoded
    username/password) and transparently refreshes expired tokens.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        token_refresh_margin: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token_refresh_margin = token_refresh_margin
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)

    async def close(self):
        """Shut down the underlying HTTP client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def _authenticate(self):
        """Obtain a fresh access token via form-encoded POST."""
        logger.info("Authenticating with Elmeeda API at %s/auth/token", self.base_url)
        resp = await self._client.post(
            "/auth/token",
            data={"username": self._username, "password": self._password},
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
        self._token_expires_at = (
            time.monotonic() + expires_in - self._token_refresh_margin
        )
        logger.info("Authenticated — token valid for %ds", expires_in)

    async def _ensure_token(self):
        """Ensure a valid access token exists, refreshing if needed."""
        if self._access_token and time.monotonic() < self._token_expires_at:
            return
        async with self._lock:
            if self._access_token and time.monotonic() < self._token_expires_at:
                return
            await self._authenticate()

    # ------------------------------------------------------------------
    # Generic request with auto-retry on 401
    # ------------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        """Make an authenticated request, retrying once on 401."""
        await self._ensure_token()
        headers = {"Authorization": f"Bearer {self._access_token}"}
        resp = await self._client.request(method, path, headers=headers, **kwargs)

        if resp.status_code == 401:
            logger.warning("Got 401 — refreshing token and retrying")
            self._access_token = None
            await self._ensure_token()
            headers = {"Authorization": f"Bearer {self._access_token}"}
            resp = await self._client.request(method, path, headers=headers, **kwargs)

        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    async def get_warranty_status(self, unit_number: str) -> dict[str, Any]:
        """Look up warranty coverage for a fleet unit."""
        logger.info("Fetching warranty status for unit %s", unit_number)
        return await self._request("GET", f"/units/{unit_number}/warranty")

    async def get_claim_status(self, claim_id: str) -> dict[str, Any]:
        """Check the status of an existing warranty claim."""
        logger.info("Fetching claim status for %s", claim_id)
        return await self._request("GET", f"/claims/{claim_id}")

    async def evaluate_repair_coverage(
        self, unit_number: str, repair_code: str, symptoms: str
    ) -> dict[str, Any]:
        """Evaluate whether a repair is covered under warranty."""
        logger.info(
            "Evaluating coverage: unit=%s repair_code=%s", unit_number, repair_code
        )
        return await self._request(
            "POST",
            f"/units/{unit_number}/coverage-evaluation",
            json={"repair_code": repair_code, "symptoms": symptoms},
        )

    async def schedule_callback(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Schedule a callback from a warranty specialist."""
        logger.info("Scheduling callback: %s", payload.get("phone", "unknown"))
        return await self._request("POST", "/callbacks", json=payload)
