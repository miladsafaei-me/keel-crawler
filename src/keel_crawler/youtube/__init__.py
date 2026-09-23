"""YouTube as a research source: the Data API, autocomplete, and per-channel outliers.

Business-blind, like the rest of the toolkit. It reads a platform and reports numbers;
which channels are worth watching, and what a number means for a business, belong to
the host.

Nothing here imports Django, so the layer runs in a script, a management command or a
test with no settings configured.
"""
from keel_crawler.youtube.api import (
    MAX_IDS_PER_CALL,
    SearchNotAllowed,
    YouTubeApiError,
    YouTubeDataApi,
)
from keel_crawler.youtube.quota import (
    DEFAULT_DAILY_LIMIT,
    QUOTA_COSTS,
    QuotaExhausted,
    QuotaLedger,
)
from keel_crawler.youtube.suggest import (
    SuggestUnavailable,
    diff_snapshots,
    fetch_suggestions,
)
from keel_crawler.youtube.velocity import (
    VelocityReading,
    channel_baseline,
    early_pace_baseline,
    looks_like_short,
    outlier_multiplier,
    parse_duration_seconds,
    parse_published_at,
    read_velocity,
    views_per_hour,
)

__all__ = [
    "MAX_IDS_PER_CALL",
    "SearchNotAllowed",
    "YouTubeApiError",
    "YouTubeDataApi",
    "DEFAULT_DAILY_LIMIT",
    "QUOTA_COSTS",
    "QuotaExhausted",
    "QuotaLedger",
    "SuggestUnavailable",
    "diff_snapshots",
    "fetch_suggestions",
    "VelocityReading",
    "channel_baseline",
    "early_pace_baseline",
    "looks_like_short",
    "outlier_multiplier",
    "parse_duration_seconds",
    "parse_published_at",
    "read_velocity",
    "views_per_hour",
]
