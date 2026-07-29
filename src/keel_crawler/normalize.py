"""URL normalization shared by the fetch cache and the per-host throttle."""
from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def normalize_request_url(url: str) -> tuple[str, str]:
    """Return ``(normalized_url, hostname_lower)`` for cache keys and throttling.

    Drops the fragment, lower-cases scheme + host, elides default ports, and
    ensures a ``/`` path so ``https://x`` and ``https://x/`` share one cache row.
    """
    raw = (url or "").strip()
    try:
        p = urlparse(raw)
    except ValueError:
        return raw, ""
    scheme = (p.scheme or "https").lower()
    host = (p.hostname or "").lower()
    if not host:
        return raw, ""
    port = p.port
    if port and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    path = p.path if p.path else "/"
    normalized = urlunparse((scheme, netloc, path, p.params, p.query, ""))
    return normalized, host
