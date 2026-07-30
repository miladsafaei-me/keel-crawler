"""Mihomo (Clash.Meta) REST client: advance the AUTO proxy group between crawl retries.

Extracted from Revenika's ``crawler_mihomo_api`` and made config-first: a
``MihomoClient`` is built from ``KEEL_CRAWLER["mihomo"]`` (with env fallback for the
usual ``.env`` secrets). The local proxy URL that Chromium points at never changes;
only the *upstream* outbound behind the group is switched, so no worker restart is
needed. Score recording delegates to an injected :class:`ProxyScoreStore`, so it
honours the store's disable/reset behaviour.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests

from keel_crawler.proxy.scores import ProxyScoreStore, default_score_store

logger = logging.getLogger(__name__)

_TRUE = ("1", "true", "yes", "on")


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _resolve_api_base(raw: str) -> str:
    """Rewrite ``host.docker.internal`` -> ``127.0.0.1`` when not inside Docker."""
    raw = (raw or "").strip().rstrip("/")
    if not raw or Path("/.dockerenv").is_file():
        return raw
    if re.search(r"(?i)host\.docker\.internal", raw):
        return re.sub(r"(?i)host\.docker\.internal", "127.0.0.1", raw)
    return raw


class MihomoClient:
    """Thin control-API client for one proxy group."""

    def __init__(
        self,
        *,
        api_url: str,
        secret: str,
        group: str = "AUTO",
        score_store: ProxyScoreStore | None = None,
        enabled: bool = True,
    ) -> None:
        self._api_base = _resolve_api_base(api_url)
        self._secret = (secret or "").strip()
        self._group = (group or "").strip() or "AUTO"
        self._scores = score_store if score_store is not None else default_score_store()
        self._enabled = bool(enabled)

    def is_configured(self) -> bool:
        """True when rotation is allowed (enabled + API base + secret)."""
        return bool(self._enabled and self._api_base and self._secret)

    @property
    def api_base(self) -> str:
        return self._api_base

    @property
    def group(self) -> str:
        return self._group

    @property
    def score_store(self) -> ProxyScoreStore:
        return self._scores

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._secret}", "Content-Type": "application/json"}

    def _group_segment(self) -> str:
        return quote(self._group, safe="")

    def _get_group(self, *, timeout_sec: float) -> dict[str, Any] | None:
        try:
            r = requests.get(
                f"{self._api_base}/proxies/{self._group_segment()}",
                headers=self._headers(),
                timeout=timeout_sec,
            )
            if not r.ok:
                return None
            data = r.json()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def active_outbound(self, *, timeout_sec: float = 5.0) -> str:
        """The group's current ``now`` outbound, or ``""``."""
        if not self.is_configured():
            return ""
        data = self._get_group(timeout_sec=timeout_sec)
        return (data or {}).get("now", "").strip() if data else ""

    def record_success(self, *, timeout_sec: float = 5.0) -> None:
        """Bump the score of the group's active outbound after a good crawl."""
        if not self.is_configured():
            return
        now = self.active_outbound(timeout_sec=timeout_sec)
        if now:
            self._scores.bump_success(now)

    def record_failure(self, *, timeout_sec: float = 5.0) -> None:
        """Lower the score of the group's active outbound after a bad crawl/probe."""
        if not self.is_configured():
            return
        now = self.active_outbound(timeout_sec=timeout_sec)
        if now:
            self._scores.bump_failure(now)

    def cycle_next(self, *, timeout_sec: float = 8.0) -> tuple[bool, str]:
        """Select the next-best member after ``now`` (score desc, delay asc, stable index).

        Returns ``(True, detail)`` on success, ``(False, reason)`` otherwise.
        """
        if not self.is_configured():
            return False, "disabled (need mihomo api_url + secret; and not turned off)"
        data = self._get_group(timeout_sec=timeout_sec)
        if data is None:
            return False, f"GET /proxies/{self._group} failed"

        names = [x.strip() for x in (data.get("all") or []) if isinstance(x, str) and x.strip()]
        now = (data.get("now") or "").strip()
        if len(names) < 2:
            return False, "group has fewer than 2 members"

        ordered = self._scores.sort_by_rank(names)
        try:
            i = ordered.index(now) if now else -1
        except ValueError:
            i = -1
        nxt = ordered[(i + 1) % len(ordered)]

        for method, caller in (("PATCH", requests.patch), ("PUT", requests.put)):
            try:
                resp = caller(
                    f"{self._api_base}/proxies/{self._group_segment()}",
                    headers=self._headers(),
                    data=json.dumps({"name": nxt}),
                    timeout=timeout_sec,
                )
                if resp.ok:
                    host_hint = ""
                    try:
                        host_hint = urlparse(self._api_base).hostname or ""
                    except Exception:
                        pass
                    return True, f"{method} -> {nxt!r} (from {now!r}, api={host_hint})"
            except Exception as exc:
                logger.debug("Mihomo %s /proxies/%s: %s", method, self._group, exc)
                continue
        return False, f"PATCH/PUT failed for next={nxt!r}"


def mihomo_from_config(score_store: ProxyScoreStore | None = None) -> MihomoClient:
    """Build a :class:`MihomoClient` from ``KEEL_CRAWLER["mihomo"]`` + env fallback.

    Config keys: ``api_url``, ``secret``, ``group``. Env fallback:
    ``MIHOMO_API_URL``, ``MIHOMO_SECRET``, ``MIHOMO_PROXY_GROUP``. Rotation is
    forced off by env ``KEEL_CRAWLER_DISABLE_MIHOMO=1``.
    """
    from keel_crawler.config import crawler_subconfig

    cfg = crawler_subconfig("mihomo")
    api_url = (cfg.get("api_url") or _env("MIHOMO_API_URL")).strip()
    secret = (cfg.get("secret") or _env("MIHOMO_SECRET")).strip()
    group = (cfg.get("group") or _env("MIHOMO_PROXY_GROUP") or "AUTO").strip()
    disabled = _env("KEEL_CRAWLER_DISABLE_MIHOMO").lower() in _TRUE
    return MihomoClient(
        api_url=api_url,
        secret=secret,
        group=group,
        score_store=score_store,
        enabled=not disabled,
    )
