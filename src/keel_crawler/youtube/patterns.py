"""What shape a set of video titles is packaged in, measured rather than judged.

A host watching competitors accumulates two different findings, and only one of them
is a topic. **What** a video is about is the topic, and it needs the host's own
vocabulary to read. **How** it is packaged -- a question, a comparison, a number, a
first-person test, a series with a fixed opening -- is a form, and a form is visible
in the words of the title alone, with no vocabulary at all.

This module reads the second one, and it reads it against a performance number the
caller supplies. Nothing here knows what that number is: a multiple of a channel's
median, a view count, a click-through rate all work, and the output is always the same
shape -- how many titles carry the pattern, what their median metric is, and how that
compares with the corpus the rows came from.

**Masking is what turns a title into a form.** ``FTMO Payout Proof 2026: $8,400 in 9
Days`` and ``Apex Payout Proof 2026: $2,100 in 4 Days`` are one form, and no pattern
miner that compares them literally can say so. Replace the brand, the year, the money
and the counts with their placeholders and both become ``{FIRM} payout proof {YEAR}
{MONEY} in {N} days``, which is a form with a support of two.

**No model is called and none is needed.** Every reading here is a regular expression,
a dictionary lookup and a median, so it runs in a management command on a timer under
a rule that forbids a model in an unattended path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

FIRM_TOKEN = "{FIRM}"
MONEY_TOKEN = "{MONEY}"
YEAR_TOKEN = "{YEAR}"
NUMBER_TOKEN = "{N}"

_MONEY = re.compile(r"\$\s?\d[\d,.]*\s?[km]?\b")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_NUMBER = re.compile(r"(?<![a-z0-9{])\d[\d,.]*\s?%?")
_KEEP = re.compile(r"[^a-zA-Z0-9$%{}\s]+")
_SPACES = re.compile(r"\s+")


def normalise(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Punctuation is dropped rather than kept because it is the least stable part of a
    title: the same series prints an em dash one week and a colon the next, and two
    spellings of one form halve its support. A feature that genuinely depends on
    punctuation is measured by :func:`feature_lift` against the raw title instead.
    """
    return _squash((title or "").lower())


def _squash(text: str) -> str:
    """Drop punctuation and collapse whitespace, leaving the case alone.

    Separate from :func:`normalise` because :func:`mask_title` must not lowercase its
    own output: the placeholders are upper case on purpose, so that a form printed on a
    page cannot be mistaken for a word somebody typed.
    """
    return _SPACES.sub(" ", _KEEP.sub(" ", text)).strip()


def _joined_digits(text: str) -> str:
    """One number stays one token: ``$8,400`` must not become ``$8`` and ``400``.

    Stripping punctuation first looked harmless and was not. The separator inside a
    thousands group is punctuation by every other measure, and dropping it split every
    large amount in two -- the money pass then masked the first half and the number
    pass masked the second, so ``$8,400 in 9 days`` produced two placeholders where the
    title has one amount, and no two titles of the same series masked alike.
    """
    return re.sub(r"(?<=\d)[,\u066c\u2009 ](?=\d\d\d(?!\d))", "", text)


def mask_title(title: str, entities: Iterable[str] = ()) -> str:
    """The title as a form: brands, years, money and counts replaced by placeholders.

    ``entities`` are masked **first** and longest-first, because several of them carry
    digits of their own -- ``The 5ers``, ``E8 Markets``, ``Top One Futures`` -- and a
    number pass that ran before them would turn a brand into ``e{N} markets`` and
    split one form into as many as there are brands.
    """
    text = _joined_digits((title or "").lower())
    for entity in sorted({e.strip().lower() for e in entities if e}, key=len, reverse=True):
        if entity:
            text = text.replace(entity, f" {FIRM_TOKEN} ")
    text = _YEAR.sub(f" {YEAR_TOKEN} ", text)
    text = _MONEY.sub(f" {MONEY_TOKEN} ", text)
    text = _NUMBER.sub(f" {NUMBER_TOKEN} ", text)
    return _squash(text)


@dataclass(frozen=True)
class TitleRow:
    """One video, as this module needs it.

    ``title`` is the title as published and is what :func:`feature_lift` reads, because
    a feature may well be a dollar sign or a pair of brackets that masking removes.
    ``skeleton`` is the masked form and is what :func:`mine_templates` reads. Both are
    carried on one row so a caller masks once, with its own entity list, and both
    readings then agree about which videos they are describing.
    """

    title: str
    metric: float
    skeleton: str = ""
    label: str = ""

    @property
    def form(self) -> str:
        return self.skeleton or mask_title(self.title)


def median(values: Sequence[float]) -> float:
    """A plain median, zeros and negatives included.

    Deliberately not :func:`keel_crawler.youtube.velocity.median`, which drops
    non-positive values because a view count of zero there means "not measured". Here
    zero is a measurement: a video that reached nothing is exactly the evidence that
    its form does not work.
    """
    kept = sorted(float(v) for v in values)
    if not kept:
        return 0.0
    middle = len(kept) // 2
    if len(kept) % 2:
        return round(kept[middle], 3)
    return round((kept[middle - 1] + kept[middle]) / 2, 3)


@dataclass(frozen=True)
class Template:
    """One recurring title form, with what the videos carrying it actually did."""

    text: str
    support: int
    median_metric: float
    lift: float
    examples: tuple[str, ...]

    @property
    def is_series(self) -> bool:
        """Whether this looks like somebody's fixed series rather than a loose habit.

        A form repeated five times or more with a stable opening is a programme: one
        channel publishing the same shape every week. That is the finding a host wants
        from a competitor -- a series can be answered with a series -- and it reads
        differently from a phrasing three channels happen to share.
        """
        return self.support >= 5


def mine_templates(
    rows: Sequence[TitleRow],
    *,
    min_support: int = 4,
    lengths: Sequence[int] = (3, 4, 5),
    limit: int = 25,
) -> list[Template]:
    """Recurring openings in the masked titles, ranked by support and then by median.

    The opening rather than any n-gram inside the title, because a form is a *shape a
    video is announced in* and the announcement is at the front. A middle n-gram
    miner finds ``prop firm`` in half the corpus and calls it a pattern.

    **A shorter key whose support a longer key matches exactly is dropped.** Measured
    on a 601-title corpus: ``best {N} proprietary`` and ``best {N} proprietary
    trading`` both had a support of fourteen, which is one form printed twice. The
    longer key is the more specific true statement, so it is the one kept.
    """
    if not rows:
        return []
    corpus = median([row.metric for row in rows])
    buckets: dict[str, list[TitleRow]] = {}
    for row in rows:
        tokens = row.form.split()
        for length in lengths:
            if len(tokens) >= length:
                buckets.setdefault(" ".join(tokens[:length]), []).append(row)

    kept = {key: members for key, members in buckets.items() if len(members) >= min_support}
    redundant = {
        shorter
        for shorter in kept
        for longer in kept
        if longer != shorter
        and longer.startswith(shorter + " ")
        and len(kept[longer]) == len(kept[shorter])
    }

    templates = []
    for key, members in kept.items():
        if key in redundant:
            continue
        metric = median([row.metric for row in members])
        templates.append(
            Template(
                text=key,
                support=len(members),
                median_metric=metric,
                lift=round(metric / corpus, 2) if corpus else 0.0,
                examples=tuple(
                    (row.label or row.title)
                    for row in sorted(members, key=lambda r: -r.metric)[:3]
                ),
            )
        )
    templates.sort(key=lambda t: (-t.support, -t.median_metric))
    return templates[:limit]


@dataclass(frozen=True)
class FeatureLift:
    """One packaging feature, and the difference between titles that carry it and not."""

    name: str
    matched: int
    median_with: float
    median_without: float
    lift: float

    @property
    def is_thin(self) -> bool:
        """Whether the sample is small enough that the lift is a hint and not a result.

        Named on the row rather than filtered out, because a reader deciding what to
        film is better served by "promising, eighteen videos" than by silence.
        """
        return self.matched < 20


def feature_lift(
    rows: Sequence[TitleRow],
    features: Mapping[str, str | Callable[[str], bool]],
    *,
    min_matched: int = 8,
) -> list[FeatureLift]:
    """How each packaging feature's videos did against the ones without it.

    A feature is a regular expression matched case-insensitively against the **raw**
    title, or any callable taking the raw title. Raw, because the features worth
    measuring include the ones masking destroys: a dollar amount, a bracketed suffix,
    a shouted word.

    A feature matching fewer than ``min_matched`` titles is left out entirely. Two
    videos are not a finding, and a table of one-sample lifts is read as if it were.
    """
    out: list[FeatureLift] = []
    for name, test in features.items():
        if callable(test):
            predicate = test
        else:
            pattern = re.compile(test, re.IGNORECASE)
            predicate = lambda title, _p=pattern: bool(_p.search(title or ""))
        with_, without = [], []
        for row in rows:
            (with_ if predicate(row.title) else without).append(row.metric)
        if len(with_) < min_matched or not without:
            continue
        median_with, median_without = median(with_), median(without)
        out.append(
            FeatureLift(
                name=name,
                matched=len(with_),
                median_with=median_with,
                median_without=median_without,
                lift=round(median_with / median_without, 2) if median_without else 0.0,
            )
        )
    out.sort(key=lambda f: -f.lift)
    return out
