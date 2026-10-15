#!/usr/bin/env python3
"""Microsoft Entra ID client-credentials token provider (no Azure SDK)."""

import contextlib
import logging
import threading
import time
from collections.abc import Callable

import requests

logger = logging.getLogger(__name__)

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
# Tokens are refreshed this many seconds before they expire.
EXPIRY_MARGIN_S = 60
# (connect, read) timeout in seconds for every HTTP request.
HTTP_TIMEOUT: tuple[float, float] = (5.0, 30.0)


class AuthError(Exception):
    """Raised when a token cannot be obtained."""


class TokenProvider:
    """Fetches and caches an OAuth2 client-credentials access token.

    The token is cached until ``EXPIRY_MARGIN_S`` seconds before it expires.
    ``get_token`` is thread-safe: concurrent callers share one refresh.
    """

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        scope: str,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create a provider.

        Args:
            tenant_id: Entra ID tenant (GUID or domain).
            client_id: App registration (client) ID.
            client_secret: App registration client secret.
            scope: Requested scope, e.g. ``https://monitor.azure.com/.default``.
            session: Optional ``requests.Session`` (injected in tests).
            clock: Wall clock returning epoch seconds.
        """
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._session = session or requests.Session()
        self._clock = clock
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0

    def get_token(self) -> str:
        """Return a valid access token, refreshing it if needed.

        Raises:
            AuthError: If the token endpoint cannot be reached or rejects the request.
        """
        with self._lock:
            if self._token and self._clock() < self._expires_at - EXPIRY_MARGIN_S:
                return self._token
            return self._refresh()

    def invalidate(self) -> None:
        """Drop the cached token (call after a 401 from the resource)."""
        with self._lock:
            self._token = ""
            self._expires_at = 0.0

    def _refresh(self) -> str:
        url = TOKEN_URL.format(tenant=self._tenant_id)
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": self._scope,
        }
        try:
            resp = self._session.post(url, data=data, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            raise AuthError(f"token request failed: {type(e).__name__}") from e
        if resp.status_code != 200:
            err = ""
            with contextlib.suppress(ValueError):
                err = str(resp.json().get("error", ""))
            raise AuthError(f"token endpoint returned HTTP {resp.status_code} {err}".strip())
        try:
            payload = resp.json()
            token = str(payload["access_token"])
            expires_in = float(payload.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError) as e:
            raise AuthError("token response malformed") from e
        self._token = token
        self._expires_at = self._clock() + expires_in
        logger.debug("obtained token for scope %s (expires in %ds)", self._scope, expires_in)
        return token
