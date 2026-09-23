"""How unusual a video is, measured against its own channel.

Raw view counts compare a large channel with a small one and say nothing a host can
act on: a million views on a channel that always gets a million is a normal Tuesday,
and fifty thousand on a channel that usually gets two is the finding. Every function
here therefore takes a baseline drawn from the same channel and returns a multiple of
it.

Two separate questions are answered, and confusing them is the trap this module
exists to avoid. **How well did it end up doing** compares lifetime views with the
channel's typical lifetime views, and only settles after weeks. **How fast is it
moving now** compares views per hour since publication with the channel's own early
pace, and is the only one of the two that can see a trend while it is still forming.
A fresh video always loses the first comparison, because it has not had time to win it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)

"""YouTube accepts uploads of up to three minutes into the Shorts experience, so
duration alone is a usable signal and an imperfect one: a three-minute landscape
video is not a Short, and the API does not state which surface a video was published
to. Treat the answer as a hint to confirm, never as a fact to publish."""
SHORT_MAX_SECONDS = 181


def parse_duration_seconds(iso_duration: str) -> int:
    """``PT4M13S`` -> 253. Returns 0 for the live/unknown shapes the API also emits."""
    match = ISO_DURATION.match((iso_duration or "").strip())
    if not match:
        return 0
    parts = {key: int(value or 0) for key, value in match.groupdict().items()}
    return (
        parts["days"] * 86_400
        + parts["hours"] * 3_600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def looks_like_short(duration_seconds: int) -> bool:
    return 0 < duration_seconds < SHORT_MAX_SECONDS


def median(values: list[float]) -> float:
    kept = sorted(value for value in values if value > 0)
    if not kept:
        return 0.0
    middle = len(kept) // 2
    if len(kept) % 2:
        return float(kept[middle])
    return (kept[middle - 1] + kept[middle]) / 2


def channel_baseline(view_counts: list[int], *, window: int = 20) -> float:
    """The channel's typical video, as a median of its most recent ``window`` uploads.

    A median and not a mean, because one runaway video would otherwise raise the bar
    it is supposed to be measured against and hide every later outlier on that channel.
    """
    return median([float(v) for v in view_counts[:window]])


def outlier_multiplier(view_count: int, baseline: float) -> float:
    """How many times its channel's typical video this one has reached.

    Zero when there is no baseline yet: a channel we have watched for a day cannot
    say what normal is, and inventing a multiplier there would rank every new
    channel's every video as a discovery.
    """
    if baseline <= 0:
        return 0.0
    return round(view_count / baseline, 2)


def hours_since(published_at: datetime, *, now: datetime | None = None) -> float:
    moment = now or datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    elapsed = (moment - published_at).total_seconds() / 3600.0
    return max(elapsed, 0.25)


def views_per_hour(view_count: int, published_at: datetime, *, now: datetime | None = None) -> float:
    return round(view_count / hours_since(published_at, now=now), 2)


@dataclass(frozen=True)
class VelocityReading:
    """One video's current pace, and how that pace compares with its channel's."""

    video_id: str
    views_per_hour: float
    baseline_views_per_hour: float
    multiplier: float
    age_hours: float

    @property
    def is_breaking(self) -> bool:
        """Moving fast enough, and young enough, that the topic is still open.

        Both halves matter. A high multiplier on a three-week-old video is an
        evergreen finding and belongs in a different queue; the same multiplier at
        eighteen hours old is a story that has not been told yet.
        """
        return self.multiplier >= 3.0 and self.age_hours <= 72


def early_pace_baseline(samples: list[tuple[int, datetime]], *, now: datetime | None = None) -> float:
    """The channel's usual views-per-hour, from videos old enough to have settled.

    Only videos published between three and sixty days ago are counted: younger ones
    are still accelerating and would raise the bar, older ones have flattened and
    would lower it.
    """
    moment = now or datetime.now(timezone.utc)
    paces: list[float] = []
    for view_count, published_at in samples:
        age_days = hours_since(published_at, now=moment) / 24.0
        if 3 <= age_days <= 60:
            paces.append(views_per_hour(view_count, published_at, now=moment))
    return median(paces)


def read_velocity(
    video_id: str,
    view_count: int,
    published_at: datetime,
    baseline_pace: float,
    *,
    now: datetime | None = None,
) -> VelocityReading:
    moment = now or datetime.now(timezone.utc)
    pace = views_per_hour(view_count, published_at, now=moment)
    multiplier = round(pace / baseline_pace, 2) if baseline_pace > 0 else 0.0
    return VelocityReading(
        video_id=video_id,
        views_per_hour=pace,
        baseline_views_per_hour=round(baseline_pace, 2),
        multiplier=multiplier,
        age_hours=round(hours_since(published_at, now=moment), 1),
    )


def parse_published_at(value: str) -> datetime:
    """The API's RFC3339 timestamps, as aware datetimes."""
    text = (value or "").replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def window_start(days: int, *, now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(days=days)
