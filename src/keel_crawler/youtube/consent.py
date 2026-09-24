#!/usr/bin/env python3
"""One-time consent for a channel's Analytics + Reporting OAuth grant.

Run directly, on a machine with a browser, by whoever owns the channel --
never imported by the rest of this package and never run unattended:

    python3 src/keel_crawler/youtube/consent.py \\
        --client-id YOUR_CLIENT_ID.apps.googleusercontent.com \\
        --client-secret YOUR_CLIENT_SECRET \\
        [--port 8765] [--write-env /path/to/.env]

It walks the installed-app OAuth flow (a loopback redirect on
``http://127.0.0.1:<port>/``, not a registered web redirect) for the scopes
``yt-analytics.readonly`` and ``youtube.readonly``, exchanges the code for a
refresh token, confirms which channel was authorized, and prints the four
``.env`` lines a host needs (``--write-env`` also writes them to a file,
replacing any existing copies of the same four keys; nothing is written
anywhere without that flag).

**The 7-day trap:** a Google Cloud OAuth consent screen left in "Testing"
issues refresh tokens that stop working after 7 days, silently, with no
warning at consent time -- ``oauth.py`` only discovers it later, as an
``invalid_grant`` error. Before running this script, set the consent screen to
"In production" (Google Cloud Console > APIs & Services > OAuth consent
screen); a token minted while still in "Testing" must be re-consented after
moving to "In production", the expiry is not retroactively lifted.

Deliberately stdlib-only: this is a script someone runs once from a laptop,
not a module the rest of the package imports, so it carries no dependency on
``requests`` or on the rest of this repo.
"""
from __future__ import annotations

import argparse
import http.server
import json
import sys
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
SCOPES = (
    "https://www.googleapis.com/auth/yt-analytics.readonly "
    "https://www.googleapis.com/auth/youtube.readonly"
)

ENV_KEYS = (
    "YOUTUBE_OAUTH_CLIENT_ID",
    "YOUTUBE_OAUTH_CLIENT_SECRET",
    "YOUTUBE_OAUTH_REFRESH_TOKEN",
    "YOUTUBE_CHANNEL_ID",
)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Catches the single redirect the consent screen sends back, then stops."""

    def do_GET(self) -> None:  # noqa: N802 - name required by http.server
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.auth_code = params.get("code", [""])[0]  # type: ignore[attr-defined]
        self.server.auth_error = params.get("error", [""])[0]  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if self.server.auth_code:  # type: ignore[attr-defined]
            self.wfile.write(b"Consent received. You can close this tab.")
        else:
            self.wfile.write(b"No authorization code received. Check the terminal.")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # the terminal output below is the log; http.server's own is noise


def _wait_for_code(port: int) -> str:
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.auth_code = ""  # type: ignore[attr-defined]
    server.auth_error = ""  # type: ignore[attr-defined]
    server.handle_request()  # blocks for exactly one request, then returns
    if server.auth_error:  # type: ignore[attr-defined]
        raise RuntimeError(f"consent was refused: {server.auth_error}")  # type: ignore[attr-defined]
    if not server.auth_code:  # type: ignore[attr-defined]
        raise RuntimeError("no authorization code arrived on the redirect")
    return server.auth_code  # type: ignore[attr-defined]


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_json(url: str, params: dict, token: str) -> dict:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def run_consent(client_id: str, client_secret: str, port: int) -> dict:
    """Walk the flow end to end; return the four env values plus the channel title."""
    redirect_uri = f"http://127.0.0.1:{port}/"
    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(auth_params)}"
    print("Open this URL, signed into the channel's own Google account:")
    print(auth_url)
    print()
    print(f"Waiting for the redirect on {redirect_uri} ...")
    try:
        webbrowser.open(auth_url)
    except Exception:  # noqa: BLE001 - headless boxes have no browser to open
        pass

    code = _wait_for_code(port)

    token_payload = _post_form(
        TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    refresh_token = token_payload.get("refresh_token", "")
    access_token = token_payload.get("access_token", "")
    if not refresh_token:
        raise RuntimeError(
            "Google returned no refresh_token. This happens when this Google "
            "account already granted this app consent before, without revoking "
            "it -- revoke access at https://myaccount.google.com/permissions "
            "and run this script again."
        )
    if not access_token:
        raise RuntimeError(f"token exchange answered with no access_token: {token_payload}")

    channel_payload = _get_json(CHANNELS_URL, {"part": "id,snippet", "mine": "true"}, access_token)
    channels = channel_payload.get("items", [])
    if not channels:
        raise RuntimeError(f"the authorized account has no channel: {channel_payload}")
    channel = channels[0]

    return {
        "YOUTUBE_OAUTH_CLIENT_ID": client_id,
        "YOUTUBE_OAUTH_CLIENT_SECRET": client_secret,
        "YOUTUBE_OAUTH_REFRESH_TOKEN": refresh_token,
        "YOUTUBE_CHANNEL_ID": channel.get("id", ""),
        "_channel_title": channel.get("snippet", {}).get("title", ""),
    }


def _write_env(path: str, values: dict) -> None:
    """Append or replace the four keys in an existing ``.env``; never touch the rest."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        lines = []
    kept = [line for line in lines if not any(line.startswith(f"{key}=") for key in ENV_KEYS)]
    for key in ENV_KEYS:
        kept.append(f"{key}={values[key]}")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(kept) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-time consent for a channel's Analytics + Reporting OAuth grant."
    )
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--write-env", default="")
    args = parser.parse_args(argv)

    try:
        result = run_consent(args.client_id, args.client_secret, args.port)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print()
    print(f"Authorized channel: {result['_channel_title']} ({result['YOUTUBE_CHANNEL_ID']})")
    print()
    print("Add these to .env:")
    for key in ENV_KEYS:
        print(f"{key}={result[key]}")

    if args.write_env:
        _write_env(args.write_env, result)
        print()
        print(f"Written to {args.write_env}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
