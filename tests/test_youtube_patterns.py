"""Tests for the pace window and the title-pattern miner.

Plain unittest, no Django and no network. Every case here is a statement about a
reading that is easy to get subtly wrong and impossible to notice afterwards: a pace
measured over twenty minutes, a first observation counted as a day's gain, a brand
whose own digits get masked as a number, or a template printed twice at two lengths.

Run: python -m unittest tests.test_youtube_patterns
"""
import unittest
from datetime import datetime, timedelta, timezone

from keel_crawler.youtube import (
    FIRM_TOKEN,
    AngleRow,
    MONEY_TOKEN,
    NUMBER_TOKEN,
    TitleRow,
    YEAR_TOKEN,
    feature_lift,
    gain_in_window,
    mine_angles,
    mask_title,
    mine_templates,
    pace_multiple,
    window_pace,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def observations(*pairs):
    """``(hours_ago, views)`` pairs, as the store would hand them over."""
    return [(NOW - timedelta(hours=hours), views) for hours, views in pairs]


class WindowPaceTests(unittest.TestCase):
    def test_the_newest_pair_that_clears_the_minimum_span_is_the_one_used(self):
        reading = window_pace(
            observations((24, 1_000), (12, 1_600), (6, 2_000), (0, 2_300)),
            min_span_hours=3.0,
        )
        assert reading is not None
        self.assertEqual(reading.span_hours, 6.0)
        self.assertEqual(reading.gained, 300)
        self.assertEqual(reading.views_per_hour, 50.0)

    def test_a_span_shorter_than_the_floor_is_refused_rather_than_extrapolated(self):
        self.assertIsNone(
            window_pace(observations((0.5, 1_000), (0, 1_004)), min_span_hours=3.0)
        )

    def test_one_observation_cannot_produce_a_pace(self):
        self.assertIsNone(window_pace(observations((0, 1_000))))

    def test_observations_older_than_the_window_are_not_reached_for(self):
        self.assertIsNone(
            window_pace(
                observations((200, 10), (0, 5_000)),
                min_span_hours=3.0,
                max_span_hours=48.0,
            )
        )

    def test_views_that_fall_are_reported_as_they_were_measured(self):
        reading = window_pace(observations((6, 2_000), (0, 1_940)), min_span_hours=3.0)
        assert reading is not None
        self.assertEqual(reading.gained, -60)
        self.assertEqual(reading.views_per_hour, -10.0)

    def test_naive_timestamps_are_read_as_utc_rather_than_crashing(self):
        rows = [(datetime(2026, 9, 24, 0, 0), 100), (datetime(2026, 9, 24, 12, 0), 340)]
        reading = window_pace(rows, min_span_hours=3.0)
        assert reading is not None
        self.assertEqual(reading.span_hours, 12.0)
        self.assertEqual(reading.gained, 240)

    def test_unordered_input_is_sorted_before_anything_is_measured(self):
        reading = window_pace(observations((0, 2_300), (12, 1_600)), min_span_hours=3.0)
        assert reading is not None
        self.assertEqual(reading.gained, 700)


class GainInWindowTests(unittest.TestCase):
    def test_the_start_is_the_newest_reading_at_or_before_the_window_opens(self):
        reading = gain_in_window(
            observations((30, 500), (26, 700), (20, 900), (0, 1_500)),
            since=NOW - timedelta(hours=24),
        )
        assert reading is not None
        self.assertEqual(reading.from_views, 700)
        self.assertEqual(reading.gained, 800)

    def test_a_video_first_seen_inside_the_window_contributes_only_what_it_gained(self):
        """Its lifetime views are not a gain, and counting them puts it top of the board."""
        reading = gain_in_window(
            observations((6, 40_000), (0, 40_900)),
            since=NOW - timedelta(hours=24),
        )
        assert reading is not None
        self.assertEqual(reading.gained, 900)
        self.assertEqual(reading.span_hours, 6.0)

    def test_a_single_reading_yields_nothing_rather_than_its_whole_view_count(self):
        self.assertIsNone(
            gain_in_window(observations((2, 40_000)), since=NOW - timedelta(hours=24))
        )


class PaceMultipleTests(unittest.TestCase):
    def test_no_reference_means_no_multiple(self):
        self.assertEqual(pace_multiple(120.0, 0.0), 0.0)

    def test_a_multiple_is_rounded_to_two_places(self):
        self.assertEqual(pace_multiple(120.0, 45.0), 2.67)


class MaskTitleTests(unittest.TestCase):
    def test_a_brand_carrying_digits_is_masked_before_the_numbers_are(self):
        self.assertEqual(
            mask_title("E8 Markets payout proof 2026: $8,400 in 9 days", ["E8 Markets"]),
            f"{FIRM_TOKEN} payout proof {YEAR_TOKEN} {MONEY_TOKEN} in {NUMBER_TOKEN} days",
        )

    def test_the_longest_entity_wins_so_a_short_brand_cannot_split_a_long_one(self):
        self.assertEqual(
            mask_title("Apex Trader Funding vs Apex", ["Apex", "Apex Trader Funding"]),
            f"{FIRM_TOKEN} vs {FIRM_TOKEN}",
        )

    def test_two_titles_of_one_series_mask_to_the_same_form(self):
        first = mask_title("FTMO Payout Proof 2026: $8,400 in 9 Days", ["FTMO"])
        second = mask_title("Apex Payout Proof 2025 — $2,100 in 4 days", ["Apex"])
        self.assertEqual(first, second)

    def test_masking_works_with_no_entities_at_all(self):
        self.assertEqual(
            mask_title("How I passed 3 challenges in 2026"),
            f"how i passed {NUMBER_TOKEN} challenges in {YEAR_TOKEN}",
        )


class MineTemplatesTests(unittest.TestCase):
    def rows(self):
        made = []
        for index in range(6):
            made.append(
                TitleRow(
                    title=f"free funded trading account number {index}",
                    metric=2.0,
                    skeleton="free funded trading account",
                )
            )
        for index in range(4):
            made.append(
                TitleRow(
                    title=f"how to pass a challenge {index}",
                    metric=0.5,
                    skeleton="how to pass a challenge",
                )
            )
        made.append(TitleRow(title="one off", metric=1.0, skeleton="one off title here"))
        return made

    def test_support_below_the_floor_is_left_out(self):
        found = {t.text for t in mine_templates(self.rows(), min_support=4)}
        self.assertNotIn("one off title", found)

    def test_a_shorter_key_with_the_same_support_as_a_longer_one_is_dropped(self):
        texts = [t.text for t in mine_templates(self.rows(), min_support=4)]
        self.assertIn("free funded trading account", texts)
        self.assertNotIn("free funded trading", texts)

    def test_lift_is_against_the_corpus_median_and_not_against_one(self):
        templates = {t.text: t for t in mine_templates(self.rows(), min_support=4)}
        # Eleven rows: six at 2.0, four at 0.5, one at 1.0 -> the median is 2.0.
        self.assertEqual(templates["free funded trading account"].lift, 1.0)
        self.assertLess(templates["how to pass a challenge"].lift, 1.0)

    def test_a_form_repeated_five_times_reads_as_a_series(self):
        templates = {t.text: t for t in mine_templates(self.rows(), min_support=4)}
        self.assertTrue(templates["free funded trading account"].is_series)
        self.assertFalse(templates["how to pass a challenge"].is_series)

    def test_examples_are_the_best_performing_titles_and_not_the_first_seen(self):
        rows = [
            TitleRow(title="weak one", metric=0.1, skeleton="the same opening here"),
            TitleRow(title="strong one", metric=9.0, skeleton="the same opening here"),
            TitleRow(title="middle one", metric=1.0, skeleton="the same opening here"),
            TitleRow(title="fourth one", metric=0.2, skeleton="the same opening here"),
        ]
        template = mine_templates(rows, min_support=4)[0]
        self.assertEqual(template.examples[0], "strong one")

    def test_an_empty_corpus_returns_nothing_rather_than_dividing_by_zero(self):
        self.assertEqual(mine_templates([], min_support=1), [])


class FeatureLiftTests(unittest.TestCase):
    def rows(self):
        strong = [TitleRow(title=f"Is {n} a scam?", metric=3.0) for n in range(10)]
        weak = [TitleRow(title=f"Best prop firm {n}", metric=1.0) for n in range(10)]
        return strong + weak

    def test_a_feature_is_matched_against_the_raw_title_punctuation_included(self):
        lifts = {f.name: f for f in feature_lift(
            self.rows(), {"question mark": r"\?"}, min_matched=8
        )}
        self.assertEqual(lifts["question mark"].matched, 10)
        self.assertEqual(lifts["question mark"].lift, 3.0)

    def test_a_feature_too_thin_to_measure_is_left_out_entirely(self):
        self.assertEqual(
            feature_lift(self.rows(), {"rare": r"unicorn"}, min_matched=8), []
        )

    def test_a_small_but_reported_sample_says_so_on_the_row(self):
        lifts = feature_lift(self.rows(), {"scam": r"scam"}, min_matched=5)
        self.assertTrue(lifts[0].is_thin)

    def test_a_callable_feature_is_accepted_beside_a_pattern(self):
        lifts = feature_lift(
            self.rows(),
            {"shouted": lambda title: title.isupper(), "scam": r"scam"},
            min_matched=8,
        )
        self.assertEqual([f.name for f in lifts], ["scam"])

    def test_features_are_ranked_by_lift_so_the_useful_one_leads(self):
        rows = self.rows() + [TitleRow(title="A middling one", metric=1.5)] * 9
        names = [f.name for f in feature_lift(
            rows, {"scam": r"scam", "best": r"best"}, min_matched=8
        )]
        self.assertEqual(names[0], "scam")


class MineAnglesTests(unittest.TestCase):
    """The angle miner, whose whole job is refusing three different false positives."""

    def rows(self, **over):
        """A corpus where one phrase is a real format and three others only look like one."""
        base = [
            # A real format: three channels, three subjects.
            AngleRow(form="{FIRM} all rules explained", metric=2.0, group="a", subjects=("f1",)),
            AngleRow(form="honest {FIRM} rules explained", metric=2.0, group="b", subjects=("f2",)),
            AngleRow(form="{FIRM} rules explained in detail", metric=2.0, group="c", subjects=("f3",)),
            # One channel's habit: same phrase, one publisher.
            AngleRow(form="{FIRM} weekly recap", metric=9.0, group="a", subjects=("f1",)),
            AngleRow(form="{FIRM} weekly recap", metric=9.0, group="a", subjects=("f2",)),
            AngleRow(form="{FIRM} weekly recap", metric=9.0, group="a", subjects=("f3",)),
            # A brand the vocabulary does not hold: three channels, one subject.
            AngleRow(form="shark funded review", metric=9.0, group="a", subjects=("f1",)),
            AngleRow(form="shark funded payout", metric=9.0, group="b", subjects=("f1",)),
            AngleRow(form="shark funded drawdown", metric=9.0, group="c", subjects=("f1",)),
        ]
        return base

    def mined(self, **over):
        kwargs = {"min_videos": 3, "min_groups": 3, "min_subjects": 3}
        kwargs.update(over)
        return {angle.text: angle for angle in mine_angles(self.rows(), **kwargs)}

    def test_a_phrase_repeated_across_channels_and_subjects_is_an_angle(self):
        self.assertIn("rules explained", self.mined())

    def test_one_channels_habit_never_becomes_a_format(self):
        """Three videos, three subjects, one publisher: a house style, not a convention."""
        self.assertNotIn("weekly recap", self.mined())

    def test_a_brand_the_vocabulary_misses_is_refused_by_the_subject_floor(self):
        """It can be said by every channel and still only ever be said about itself."""
        self.assertNotIn("shark funded", self.mined())

    def test_the_subject_floor_is_what_refuses_it_and_nothing_else(self):
        self.assertIn("shark funded", self.mined(min_subjects=1))

    def test_a_placeholder_is_never_part_of_an_angle(self):
        self.assertFalse(any("{" in text for text in self.mined(min_subjects=1)))

    def test_a_stopword_is_never_part_of_an_angle(self):
        self.assertNotIn("all", self.mined(min_subjects=1))

    def test_a_shorter_phrase_with_the_same_support_is_dropped_for_the_longer(self):
        """``rules`` and ``explained`` each cover the same three videos as the pair."""
        mined = self.mined()
        self.assertIn("rules explained", mined)
        self.assertNotIn("rules", mined)
        self.assertNotIn("explained", mined)

    def test_a_shorter_phrase_with_more_support_survives_the_longer_one(self):
        """``rules`` is a subject and ``rules explained`` a format; both are true."""
        rows = self.rows() + [
            AngleRow(form="{FIRM} rules you must know", metric=1.0, group="d", subjects=("f4",))
        ]
        texts = [a.text for a in mine_angles(rows, min_videos=3, min_groups=3, min_subjects=3)]
        self.assertIn("rules", texts)
        self.assertIn("rules explained", texts)

    def test_an_ignored_phrase_never_reaches_the_output(self):
        self.assertNotIn("rules explained", self.mined(ignore=["Rules Explained"]))

    def test_the_lift_is_the_median_against_the_corpus_median(self):
        angle = self.mined()["rules explained"]
        self.assertEqual(angle.median_metric, 2.0)
        self.assertEqual(angle.lift, round(2.0 / 9.0, 2))

    def test_the_subjects_are_reported_deduplicated_and_sorted(self):
        self.assertEqual(self.mined()["rules explained"].subjects, ("f1", "f2", "f3"))

    def test_a_phrase_counts_once_per_video_however_often_it_repeats(self):
        rows = [
            AngleRow(form="rules explained rules explained", metric=1.0, group=g, subjects=(s,))
            for g, s in (("a", "f1"), ("b", "f2"), ("c", "f3"))
        ]
        self.assertEqual(
            mine_angles(rows, min_videos=3, min_groups=3, min_subjects=3)[0].videos, 3
        )

    def test_an_empty_corpus_returns_nothing_rather_than_raising(self):
        self.assertEqual(mine_angles([]), [])

    def test_a_phrase_mostly_inside_a_longer_one_is_absorbed_only_when_asked(self):
        """``hidden`` with three of its four videos saying ``hidden rules``."""
        rows = [
            AngleRow(form=f"{{FIRM}} hidden rules {word}", metric=1.0, group=g, subjects=(s,))
            for word, g, s in (("alpha", "a", "f1"), ("beta", "b", "f2"), ("gamma", "c", "f3"))
        ] + [AngleRow(form="{FIRM} hidden truth", metric=1.0, group="d", subjects=("f4",))]
        plain = [a.text for a in mine_angles(rows, min_videos=3, min_groups=3, min_subjects=3)]
        self.assertIn("hidden", plain)
        absorbed = [a.text for a in mine_angles(
            rows, min_videos=3, min_groups=3, min_subjects=3, absorb_share=0.75
        )]
        self.assertNotIn("hidden", absorbed)
        self.assertIn("hidden rules", absorbed)

    def test_a_phrase_with_its_own_life_is_never_absorbed(self):
        """``rules`` is a subject in its own right; ``rules explained`` holds a third of it."""
        rows = [
            AngleRow(form=f"{{FIRM}} rules explained {w}", metric=1.0, group=g, subjects=(s,))
            for w, g, s in (("one", "a", "f1"), ("two", "b", "f2"), ("six", "c", "f3"))
        ] + [
            AngleRow(form=f"{{FIRM}} rules {w}", metric=1.0, group=g, subjects=(s,))
            for w, g, s in (("alpha", "a", "f1"), ("beta", "b", "f2"), ("gamma", "c", "f3"),
                            ("delta", "a", "f4"), ("omega", "b", "f5"), ("sigma", "c", "f6"))
        ]
        texts = [a.text for a in mine_angles(
            rows, min_videos=3, min_groups=3, min_subjects=3, absorb_share=0.75
        )]
        self.assertIn("rules", texts)
        self.assertIn("rules explained", texts)

    def test_a_thin_angle_says_so_on_the_row(self):
        self.assertTrue(self.mined()["rules explained"].is_thin)


if __name__ == "__main__":
    unittest.main()
