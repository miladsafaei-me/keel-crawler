"""The YouTube Data API's daily budget, accounted for before a call is made.

The API grants 10,000 units a day per project and refuses everything once they are
gone, until midnight Pacific. The refusal is indistinguishable from a broken key in
a log line, so a run that overspends looks like an outage the next morning.

What makes overspending easy is that the costs are not uniform: ``search.list``
costs 100 units and ``playlistItems.list`` costs 1, so one careless search loop
spends a hundred polls' worth of budget. This module is the one place that knows
the prices, and :class:`QuotaLedger` is what a caller charges before it spends, so
a batch stops on its own budget rather than on the API's refusal.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Published costs, per call and not per item returned. A call that asks for more
# `part` values costs the same as one that asks for fewer, which is why callers
# batch ids rather than parts.
QUOTA_COSTS: dict[str, int] = {
    "channels.list": 1,
    "playlistItems.list": 1,
    "videos.list": 1,
    "commentThreads.list": 1,
    "search.list": 100,
}

DEFAULT_DAILY_LIMIT = 10_000


class QuotaExhausted(RuntimeError):
    """Raised before a call that the caller's own budget cannot pay for."""


@dataclass
class QuotaLedger:
    """What this run has spent, and what it is allowed to spend.

    It is deliberately per-run and in-memory rather than persisted: the real budget
    lives at Google and resets on their clock, so a stored counter would drift from
    it and give false confidence. This ledger answers a narrower and honest
    question -- has *this* batch spent more than it was told it could.
    """

    limit: int = DEFAULT_DAILY_LIMIT
    spent: int = 0
    calls: dict[str, int] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    def charge(self, endpoint: str, count: int = 1) -> int:
        """Record ``count`` calls to ``endpoint``; raise rather than overspend."""
        unit = QUOTA_COSTS.get(endpoint)
        if unit is None:
            raise KeyError(f"unknown YouTube endpoint: {endpoint}")
        cost = unit * count
        if self.spent + cost > self.limit:
            raise QuotaExhausted(
                f"{endpoint} needs {cost} units, {self.remaining} left of {self.limit}"
            )
        self.spent += cost
        self.calls[endpoint] = self.calls.get(endpoint, 0) + count
        return cost

    def can_afford(self, endpoint: str, count: int = 1) -> bool:
        return self.spent + QUOTA_COSTS[endpoint] * count <= self.limit

    def summary(self) -> str:
        parts = ", ".join(f"{name}x{n}" for name, n in sorted(self.calls.items()))
        return f"{self.spent}/{self.limit} units ({parts or 'nothing spent'})"
