"""OAuth 2.0 for the two APIs that a plain API key cannot read.

The YouTube Data API works with an API key, but YouTube Analytics and YouTube
Reporting are both channel-owner data and Google gates them behind an
installed-app OAuth grant: a one-time consent (`consent.py`), then a refresh
token that this module turns into short-lived access tokens forever after.

The refresh token is the only credential a host stores. Google returns no new
refresh token on a normal refresh, so this class never rotates or persists
one -- it exchanges the token it was given for an access token, caches that
token in memory, and refreshes again when it is close to expiry.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"

# Refresh this many seconds before the token's own expiry, so a call in flight
# never races the clock and gets a 401 mid-request.
EXPIRY_MARGIN_SECONDS = 60


class OAuthError(RuntimeError):
    """A non-2xx answer from Google's token endpoint, with its own reason attached."""


@dataclass
class OAuthCredentials:
    """One refresh token, exchanged for access tokens as needed.

    ``session`` is accepted for the same reason the rest of the toolkit accepts
    one: tests hand it a fake, and a host that already pools connections can
    reuse its own.
    """

    client_id: str
    client_secret: str
    refresh_token: str
    session: requests.Session | None = None
    timeout: int = 20
    _token: str = field(default="", init=False, repr=False)
    _expires_at: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.client_id or not self.client_secret or not self.refresh_token:
            raise ValueError("client_id, client_secret and refresh_token are all required")
        if self.session is None:
            self.session = requests.Session()

    def access_token(self) -> str:
        """A live bearer token, refreshed only when the cached one is stale."""
        now = time.monotonic()
        if self._token and now < self._expires_at - EXPIRY_MARGIN_SECONDS:
            return self._token
        self._refresh()
        return self._token

    def _refresh(self) -> None:
        response = self.session.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise OAuthError(_describe_failure(response))
        payload = response.json()
        token = payload.get("access_token", "")
        if not token:
            raise OAuthError(f"token refresh answered 200 with no access_token: {payload}")
        self._token = token
        self._expires_at = time.monotonic() + float(payload.get("expires_in", 3600))


def _describe_failure(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:200]}"
    error = payload.get("error", "")
    description = payload.get("error_description", "")
    if error == "invalid_grant":
        return (
            "invalid_grant: the refresh token expired or was revoked. "
            "If the Google Cloud OAuth consent screen is still in 'Testing', "
            "every refresh token it issues dies after 7 days -- move the app to "
            "'In production' (Google Cloud Console > APIs & Services > OAuth "
            "consent screen) and re-run consent.py to mint a token that does not "
            f"expire on its own. ({description or 'no further detail from Google'})"
        )
    return f"HTTP {response.status_code} {error}: {description or payload}"
