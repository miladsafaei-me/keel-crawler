"""URL normalization shared by the fetch cache and the per-host throttle."""
from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def hostname_of(url: str) -> str:
    """Lowercased hostname with a leading ``www.`` stripped, or ``""``.

    The single source of truth for the per-host key used by pacing, throttling, and
    the anti-bot ladder — previously duplicated as ``_hostname`` in several modules.
    """
    try:
        host = (urlparse((url or "").strip()).netloc or "").lower()
    except Exception:
        return ""
    if "@" in host:  # strip any userinfo
        host = host.rsplit("@", 1)[-1]
    if ":" in host:  # strip port
        host = host.split(":", 1)[0]
    return host[4:] if host.startswith("www.") else host


def origin_of(url: str) -> str:
    """``scheme://netloc`` for a URL, or ``""`` when either part is missing."""
    try:
        p = urlparse((url or "").strip())
    except Exception:
        return ""
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


def normalize_request_url(url: str) -> tuple[str, str]:
    """Return ``(normalized_url, hostname_lower)`` for cache keys and throttling.

    Drops the fragment, lower-cases scheme + host, elides default ports, and
    ensures a ``/`` path so ``https://x`` and ``https://x/`` share one cache row.

    This is the **HTTP cache** identity: it keeps ``www.``, the scheme, and the raw
    query verbatim because those change the bytes the server returns. It is deliberately
    distinct from :func:`keel_crawler.browser.harvest.canonical_url_key` (the
    *discovery* identity, which folds ``www.``/scheme and strips tracking params) — do
    not substitute one for the other.
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
