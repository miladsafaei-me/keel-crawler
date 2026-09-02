"""Persist per-outbound proxy scores for probe order and rotation (+success / -failure).

A lock-guarded JSON store, extracted from Revenika's ``mihomo_proxy_scores`` and
made configurable + business-blind. Two capabilities were added on top of the
original:

* **Disable** — when scoring is off (``KEEL_CRAWLER["proxy_scoring_enabled"] = False``
  or env ``KEEL_CRAWLER_DISABLE_PROXY_SCORING=1``), every mutating call is a no-op
  and ranking falls back to the input order. Nothing is written to disk.
* **Reset** — ``reset()`` clears all scores (and delays); ``reset_outbound(name)``
  clears one. Both are also exposed via the ``crawler_proxy_scores`` command.

Ranking order for fallback/rotation: higher score first, then lower last-probe
delay (ms), then stable input index.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from keel_crawler.proxy.jsonstore import FdHandle, PlainHandle, dumps, locked

logger = logging.getLogger(__name__)

_MIN_SCORE = -1_000_000
_MAX_SCORE = 1_000_000
_MISSING_DELAY_SORT = 10**9

_TRUE = ("1", "true", "yes", "on")


def _clamp_score(v: int) -> int:
    return max(_MIN_SCORE, min(_MAX_SCORE, int(v)))


def _coerce_scores(data: object) -> dict[str, int]:
    out: dict[str, int] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if not isinstance(k, str) or not k.strip():
                continue
            try:
                out[k.strip()] = _clamp_score(int(v))
            except (TypeError, ValueError):
                continue
    return out


def _xdg_scores_dir() -> Path:
    xdg = (os.environ.get("XDG_DATA_HOME") or "").strip()
    root = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return root / "keel_crawler"


class ProxyScoreStore:
    """Score persistence for one outbound-name space (one scores + one delays file)."""

    def __init__(self, *, scores_dir: str | os.PathLike | None = None, enabled: bool = True) -> None:
        base = Path(scores_dir).expanduser() if scores_dir else _xdg_scores_dir()
        self._scores_path = base / "proxy_scores.json"
        self._delays_path = base / "proxy_last_delays.json"
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool) -> None:
        """Toggle scoring at runtime; when off, all writes become no-ops."""
        self._enabled = bool(value)

    @property
    def scores_path(self) -> Path:
        return self._scores_path

    @property
    def delays_path(self) -> Path:
        return self._delays_path

    def load_scores(self) -> dict[str, int]:
        return self._read_json(self._scores_path, _coerce_scores)

    def load_delays(self) -> dict[str, int]:
        def coerce(data: object) -> dict[str, int]:
            out: dict[str, int] = {}
            if isinstance(data, dict):
                for k, v in data.items():
                    if not isinstance(k, str) or not k.strip():
                        continue
                    try:
                        out[k.strip()] = int(v)
                    except (TypeError, ValueError):
                        continue
            return out

        return self._read_json(self._delays_path, coerce)

    @staticmethod
    def _read_json(path: Path, coerce) -> dict[str, int]:
        if not path.is_file():
            return {}
        try:
            raw = path.read_text(encoding="utf-8")
            if len(raw.encode("utf-8")) > _MAX_READ_BYTES:
                return {}
            return coerce(json.loads(raw))
        except Exception:
            return {}

    def sort_by_rank(
        self,
        names: list[str],
        *,
        scores: dict[str, int] | None = None,
        delays: dict[str, int] | None = None,
    ) -> list[str]:
        """Higher score first, then lower delay, then stable index.

        When scoring is disabled the input order is returned unchanged.
        """
        if not self._enabled:
            return list(names)
        s = self.load_scores() if scores is None else scores
        d = self.load_delays() if delays is None else delays

        def delay_key(n: str) -> int:
            v = d.get(n)
            return _MISSING_DELAY_SORT if v is None or v < 0 else int(v)

        indexed = list(enumerate(names))
        indexed.sort(key=lambda iv: (-s.get(iv[1], 0), delay_key(iv[1]), iv[0]))
        return [iv[1] for iv in indexed]

    def adjust(self, outbound_name: str, delta: int) -> None:
        """Add ``delta`` to one outbound's score (locked read-modify-write)."""
        if not self._enabled:
            return
        name = (outbound_name or "").strip()
        d = int(delta)
        if not name or d == 0:
            return
        self.apply_deltas({name: d})

    def bump_success(self, outbound_name: str, *, delta: int = 1) -> None:
        self.adjust(outbound_name, max(1, int(delta)))

    def bump_failure(self, outbound_name: str, *, delta: int = 1) -> None:
        self.adjust(outbound_name, -max(1, int(delta)))

    def apply_deltas(self, deltas: dict[str, int]) -> None:
        """Apply several score adjustments in one locked read-modify-write."""
        if not self._enabled:
            return
        filtered = {k.strip(): int(v) for k, v in deltas.items() if k.strip() and int(v) != 0}
        if not filtered:
            return
        self._scores_path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(self._scores_path) as handle:
            scores = _coerce_scores(handle.read())
            for k, v in filtered.items():
                scores[k] = _clamp_score(scores.get(k, 0) + v)
            handle.write(scores)

    def save_delays(self, delays_ms: dict[str, int]) -> None:
        """Persist last measured delay per outbound (ms); use -1 for a failed probe."""
        if not self._enabled:
            return
        clean: dict[str, int] = {}
        for k, v in delays_ms.items():
            ks = (k or "").strip()
            if not ks:
                continue
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv < -1 or iv >= 10**8:
                continue
            clean[ks] = iv
        self._delays_path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(self._delays_path) as handle:
            handle.write(clean)

    def reset(self) -> None:
        """Clear ALL scores and last-probe delays (best-effort; ignores absent files)."""
        for path in (self._scores_path, self._delays_path):
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                logger.debug("proxy score reset: could not remove %s", path, exc_info=True)

    def reset_outbound(self, outbound_name: str) -> None:
        """Clear score + delay for a single outbound, leaving the rest intact."""
        name = (outbound_name or "").strip()
        if not name:
            return
        if self._scores_path.is_file():
            self._scores_path.parent.mkdir(parents=True, exist_ok=True)
            with _locked(self._scores_path) as handle:
                scores = _coerce_scores(handle.read())
                scores.pop(name, None)
                handle.write(scores)
        if self._delays_path.is_file():
            with _locked(self._delays_path) as handle:
                delays = handle.read()
                if isinstance(delays, dict):
                    delays.pop(name, None)
                    handle.write(delays)


# The lock-guarded JSON file underneath this store now lives in
# keel_crawler.proxy.jsonstore, because keel_crawler.proxy.pool needs the same
# primitive. Two copies of a concurrency helper diverge quietly and are then very
# hard to debug, so it was extracted rather than duplicated. These aliases keep
# this module's existing call sites unchanged.
_locked = locked
# Kept at this module's original value: the shared helper allows a larger file,
# and a score file that big means something is wrong rather than something big.
_MAX_READ_BYTES = 2_000_000
_dumps = dumps
_FdHandle = FdHandle
_PlainHandle = PlainHandle


def _scoring_enabled_from_config() -> bool:
    from keel_crawler.config import crawler_setting

    if (os.environ.get("KEEL_CRAWLER_DISABLE_PROXY_SCORING") or "").strip().lower() in _TRUE:
        return False
    return bool(crawler_setting("proxy_scoring_enabled"))


def default_score_store() -> ProxyScoreStore:
    """Build a store from ``KEEL_CRAWLER`` (``proxy_scores_dir`` + ``proxy_scoring_enabled``).

    Env override: ``KEEL_CRAWLER_PROXY_SCORES_DIR`` sets the directory;
    ``KEEL_CRAWLER_DISABLE_PROXY_SCORING=1`` forces scoring off.
    """
    from keel_crawler.config import crawler_setting

    env_dir = (os.environ.get("KEEL_CRAWLER_PROXY_SCORES_DIR") or "").strip()
    scores_dir = env_dir or crawler_setting("proxy_scores_dir")
    return ProxyScoreStore(scores_dir=scores_dir, enabled=_scoring_enabled_from_config())
