"""Pluggable captcha / challenge-solver seam.

keel-crawler does NOT ship a captcha solver — solving Turnstile/reCAPTCHA needs a
paid third-party service (2Captcha, CapSolver) or a sidecar (FlareSolverr), which is
a per-project cost decision. Instead the engine calls a host-provided callable as a
last resort when the egress/proxy ladder has exhausted and the page still looks like
a challenge.

Configure it with a dotted path::

    KEEL_CRAWLER = {"captcha_solver": "myapp.crawl.solve_challenge"}

The callable receives ``(url: str, page: CrawledPage)`` and returns a solved
``CrawledPage`` (``.ok()`` True) or ``None`` to give up. Any exception is swallowed
and treated as "unsolved", so a flaky solver never crashes a crawl.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def resolve_captcha_solver() -> Optional[Callable]:
    """Return the configured solver callable, or ``None`` when unset."""
    from keel_crawler.config import crawler_setting

    dotted = crawler_setting("captcha_solver")
    if not dotted:
        return None
    try:
        from django.utils.module_loading import import_string

        return import_string(dotted)
    except Exception:
        logger.warning("keel-crawler: could not import captcha_solver %r", dotted, exc_info=True)
        return None


def try_solve(url: str, page):
    """Invoke the solver if configured; return a solved page or ``None`` (never raises)."""
    solver = resolve_captcha_solver()
    if solver is None:
        return None
    try:
        result = solver(url, page)
    except Exception:
        logger.warning("keel-crawler: captcha_solver raised for %s", url[:120], exc_info=True)
        return None
    if result is not None and getattr(result, "ok", lambda: False)():
        return result
    return None
