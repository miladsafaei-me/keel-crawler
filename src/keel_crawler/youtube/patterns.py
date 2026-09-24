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


"""Words that carry no angle, and are dropped before any n-gram is assembled.

Deliberately short and English-only. A corpus in another language passes its own set
through ``stopwords``; the host knows what it collects and this module does not.
"""
DEFAULT_STOPWORDS = frozenset(
    """a an the of in on for to and or with my your our their this that these those
    is are was were be been am i we you it its his her as at by from into over under
    after before than then so but if not no yes do does did done get got make made
    new all any more most very just about out up down off he she they them there here
    what which who whom whose how why when where can could will would should may might
    must shall let s t re ve ll d m""".split()
)


@dataclass(frozen=True)
class AngleRow:
    """One video as the angle miner needs it.

    ``form`` is the masked skeleton, so the subject has already become a placeholder and
    the words left are what the title *says about* it. ``group`` is whoever published it
    and ``subjects`` is what the host's own vocabulary found in it -- both exist so the
    miner can refuse a phrase that only one channel uses or that has only ever been said
    about one subject, which is the difference between a format and a habit.
    """

    form: str
    metric: float
    group: str = ""
    subjects: tuple[str, ...] = ()
    title: str = ""
    label: str = ""


@dataclass(frozen=True)
class Angle:
    """One thing titles repeatedly say about their subject, and how those videos did."""

    text: str
    videos: int
    groups: int
    subjects: tuple[str, ...]
    median_metric: float
    lift: float
    examples: tuple[str, ...]

    @property
    def is_thin(self) -> bool:
        """Whether the sample is small enough to read as a hint rather than a result."""
        return self.videos < 20


def mine_angles(
    rows: Sequence[AngleRow],
    *,
    ignore: Iterable[str] = (),
    stopwords: Iterable[str] | None = None,
    min_videos: int = 8,
    min_groups: int = 3,
    min_subjects: int = 3,
    lengths: Sequence[int] = (1, 2, 3),
    limit: int = 40,
) -> list[Angle]:
    """Recurring phrases **anywhere** in a masked title, kept only where they repeat
    across publishers and across subjects.

    The sibling of :func:`mine_templates` and not a duplicate of it. That one mines the
    **opening** of a title, which is where a series announces itself, and its output is
    one channel's programme. This one mines any position, because the thing a host wants
    to reproduce is not where the words sit but what they claim: ``All Rules Explained``
    arrives at the front of one title, after a brand in the next and behind two pipes in
    the third, and a prefix miner sees three different forms.

    **Three floors, and each removes a different false positive.** ``min_videos`` removes
    the coincidence. ``min_groups`` removes one channel's house style, which is the
    failure a raw frequency count always produces -- a prolific publisher's habit
    outranks the field's actual convention. ``min_subjects`` removes the phrase that is
    really a proper noun: a brand the host's vocabulary does not hold still reads as an
    ordinary phrase, and the one thing it can never do is appear against three different
    subjects.

    **A shorter phrase is dropped only when a longer one has exactly its support.** The
    same rule :func:`mine_templates` uses, and for the same reason -- ``cheapest prop``
    and ``cheapest prop firm`` at sixteen videos each are one phrase printed twice --
    but no further than that. ``rules`` and ``rules explained`` have different supports
    and are different claims: the first is a subject, the second is a format, and
    collapsing them loses the one a host can actually reproduce.
    """
    if not rows:
        return []
    stop = frozenset(stopwords) if stopwords is not None else DEFAULT_STOPWORDS
    skip = {phrase.strip().lower() for phrase in ignore if phrase and phrase.strip()}
    corpus = median([row.metric for row in rows])

    buckets: dict[str, list[AngleRow]] = {}
    for row in rows:
        tokens = [
            token
            for token in row.form.split()
            if not token.startswith("{") and token not in stop
        ]
        seen: set[str] = set()
        for length in lengths:
            for start in range(len(tokens) - length + 1):
                phrase = " ".join(tokens[start : start + length])
                if phrase in seen or phrase in skip:
                    continue
                seen.add(phrase)
                buckets.setdefault(phrase, []).append(row)

    kept: dict[str, list[AngleRow]] = {}
    for phrase, members in buckets.items():
        if len(members) < min_videos:
            continue
        if len({member.group for member in members}) < min_groups:
            continue
        if len({subject for member in members for subject in member.subjects}) < min_subjects:
            continue
        kept[phrase] = members

    redundant = {
        shorter
        for shorter in kept
        for longer in kept
        if longer != shorter
        and _contains_phrase(longer, shorter)
        and len(kept[longer]) == len(kept[shorter])
    }

    angles: list[Angle] = []
    for phrase, members in kept.items():
        if phrase in redundant:
            continue
        metric = median([member.metric for member in members])
        angles.append(
            Angle(
                text=phrase,
                videos=len(members),
                groups=len({member.group for member in members}),
                subjects=tuple(
                    sorted({subject for member in members for subject in member.subjects})
                ),
                median_metric=metric,
                lift=round(metric / corpus, 2) if corpus else 0.0,
                examples=tuple(
                    (member.label or member.title)
                    for member in sorted(members, key=lambda r: -r.metric)[:3]
                ),
            )
        )
    angles.sort(key=lambda angle: (-angle.videos, -angle.median_metric))
    return angles[:limit]


def _contains_phrase(haystack: str, needle: str) -> bool:
    """Whether ``needle`` appears in ``haystack`` as a whole run of words.

    A plain ``in`` test would read ``rule`` inside ``rules explained`` and drop a phrase
    that is not a sub-phrase at all, which is the same word-boundary trap the column
    vocabularies hit with a substring test.
    """
    outer = haystack.split()
    inner = needle.split()
    if len(inner) >= len(outer):
        return False
    return any(
        outer[i : i + len(inner)] == inner for i in range(len(outer) - len(inner) + 1)
    )
