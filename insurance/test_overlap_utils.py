"""
Tests for the Rate Master overlap detector (insurance/overlap_utils.py).

The predicate's whole value depends on it agreeing with process_mis_mapping's
RULE 1-6 chain, so most of these pin down a specific rule's semantics — blank
means wildcard here, 0 is a real bound there, YES and NO are disjoint — rather
than the plumbing around them. Get one of those backwards and the dashboard
either cries wolf or stays quiet about real conflicts.
"""
from datetime import date

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from insurance import overlap_utils
from insurance.models import (
    MakeModelMaster, PolicyTypeMaster, ProductMaster,
    RateGroup, RateMaster, RateOverlapPair, RateOverlapScan, RTOMaster,
    YesNoNAMaster,
)


def make_group(**overrides):
    """A group dict in load_active_groups' shape, with every axis wide open."""
    group = {
        "grid_key": 1,
        "insurance_company": "acme",
        "insurer_display": "Acme",
        "product": "private car",
        "sub_product": "comp",
        "fuel_type": "petrol",
        "make_model_class": "car",
        "is_ncb": "NA",
        "is_cpa": "NA",
        "is_zd": "NA",
        "new_vehicle_makes": frozenset(),
        "new_rto_list": frozenset(),
        "add_tnc": "",
        "row_count": 1,
        "cc_min": None, "cc_max": None,
        "sc_min": None, "sc_max": None,
        "vehicle_age_min": None, "vehicle_age_max": None,
        "tariff_min": None, "tariff_max": None,
        "from_date": None, "to_date": None,
    }
    group.update(overrides)
    return group


class GroupsConflictTests(TestCase):
    """RULE-by-RULE: what does and does not separate two rate groups."""

    def test_two_wide_open_groups_of_the_same_insurer_conflict(self):
        a = make_group(grid_key=1)
        b = make_group(grid_key=2)
        self.assertTrue(overlap_utils.groups_conflict(a, b))

    def test_rule_1_different_insurer_never_conflicts(self):
        a = make_group(grid_key=1, insurance_company="acme")
        b = make_group(grid_key=2, insurance_company="zenith")
        self.assertFalse(overlap_utils.groups_conflict(a, b))

    def test_rule_2_different_product_separates(self):
        a = make_group(grid_key=1, product="private car")
        b = make_group(grid_key=2, product="two wheeler")
        self.assertFalse(overlap_utils.groups_conflict(a, b))

    def test_rule_2_blank_product_is_a_wildcard(self):
        # check_categorical_match returns True for a blank grid value, so a
        # blank-product group is reachable by every policy.
        a = make_group(grid_key=1, product="")
        b = make_group(grid_key=2, product="two wheeler")
        self.assertTrue(overlap_utils.groups_conflict(a, b))

    def test_rule_2b_na_vehicle_class_is_a_wildcard(self):
        a = make_group(grid_key=1, make_model_class="na")
        b = make_group(grid_key=2, make_model_class="bike")
        self.assertTrue(overlap_utils.groups_conflict(a, b))

    def test_rule_2b_two_named_classes_separate(self):
        a = make_group(grid_key=1, make_model_class="car")
        b = make_group(grid_key=2, make_model_class="bike")
        self.assertFalse(overlap_utils.groups_conflict(a, b))

    def test_rule_3_disjoint_cc_ranges_separate(self):
        a = make_group(grid_key=1, cc_min=0, cc_max=1000)
        b = make_group(grid_key=2, cc_min=1001, cc_max=1500)
        self.assertFalse(overlap_utils.groups_conflict(a, b))

    def test_rule_3_consecutive_bands_sharing_a_whole_bound_do_not_conflict(self):
        # "0 - 1000" then "1000 - 1500" is how a grid writes a boundary: the
        # first band stops where the second starts.
        a = make_group(grid_key=1, cc_min=0.0, cc_max=1000.0)
        b = make_group(grid_key=2, cc_min=1000.0, cc_max=1500.0)
        self.assertFalse(overlap_utils.groups_conflict(a, b))

    def test_rule_3_consecutive_bands_sharing_an_x01_bound_do_not_conflict(self):
        # Same boundary, written the other way the grids do it - 2500.01
        # meaning "just above 2500".
        a = make_group(grid_key=1, cc_min=0.0, cc_max=2500.01)
        b = make_group(grid_key=2, cc_min=2500.01, cc_max=3500.01)
        self.assertFalse(overlap_utils.groups_conflict(a, b))

    def test_rule_3_a_shared_bound_still_conflicts_when_the_span_has_width(self):
        # Only a bare seam is excused. A genuine overlap that happens to end on
        # a shared bound is still a conflict, and still covers the fractional
        # CC values the MIS really carries (1.5, 16.08, 109.2).
        a = make_group(grid_key=1, cc_min=0.0, cc_max=2500.01)
        b = make_group(grid_key=2, cc_min=100.0, cc_max=3500.01)
        self.assertTrue(overlap_utils.groups_conflict(a, b))

    def test_rule_3_two_bands_pinning_the_same_single_value_still_conflict(self):
        # Neither band stops where the other starts - both are exactly "1000",
        # so a 1000cc policy genuinely matches both.
        a = make_group(grid_key=1, cc_min=1000.0, cc_max=1000.0)
        b = make_group(grid_key=2, cc_min=1000.0, cc_max=1000.0)
        self.assertTrue(overlap_utils.groups_conflict(a, b))

    def test_rule_3_a_band_ending_on_another_bands_only_value_still_conflicts(self):
        # [0, 1000] against the single-value band [1000, 1000]: the second
        # doesn't continue past the seam, so it isn't the next band along.
        a = make_group(grid_key=1, cc_min=0.0, cc_max=1000.0)
        b = make_group(grid_key=2, cc_min=1000.0, cc_max=1000.0)
        self.assertTrue(overlap_utils.groups_conflict(a, b))

    def test_rule_3_an_unbounded_band_meeting_another_at_its_bound_is_a_seam(self):
        # A NULL bound is unbounded, so it extends past the seam like any
        # explicit value would.
        a = make_group(grid_key=1, cc_min=None, cc_max=1000.0)
        b = make_group(grid_key=2, cc_min=1000.0, cc_max=None)
        self.assertFalse(overlap_utils.groups_conflict(a, b))

    def test_rule_3_vehicle_age_bands_sharing_a_year_bound_do_not_conflict(self):
        a = make_group(grid_key=1, vehicle_age_min=0.0, vehicle_age_max=1.0)
        b = make_group(grid_key=2, vehicle_age_min=1.0, vehicle_age_max=5.01)
        self.assertFalse(overlap_utils.groups_conflict(a, b))

    def test_rule_4_dates_touching_on_a_single_day_still_conflict(self):
        # Dates never take the X.01 rule - a one-day intersection is a real day
        # a policy can be written on.
        a = make_group(grid_key=1, from_date=date(2026, 4, 1), to_date=date(2026, 9, 30))
        b = make_group(grid_key=2, from_date=date(2026, 9, 30), to_date=date(2027, 3, 31))
        self.assertTrue(overlap_utils.groups_conflict(a, b))

    def test_rule_3_zero_is_a_real_bound_not_unset(self):
        # The CSV importer writes 0 rather than NULL for a missing bound, but
        # the engine compares it literally — a 0..0 group is not open-ended.
        a = make_group(grid_key=1, cc_min=0, cc_max=0)
        b = make_group(grid_key=2, cc_min=1000, cc_max=1500)
        self.assertFalse(overlap_utils.groups_conflict(a, b))

    def test_rule_3_null_bound_is_open_ended(self):
        a = make_group(grid_key=1, cc_min=None, cc_max=None)
        b = make_group(grid_key=2, cc_min=1000, cc_max=1500)
        self.assertTrue(overlap_utils.groups_conflict(a, b))

    def test_rule_4_non_overlapping_validity_windows_separate(self):
        a = make_group(grid_key=1, from_date=date(2026, 4, 1), to_date=date(2026, 9, 30))
        b = make_group(grid_key=2, from_date=date(2026, 10, 1), to_date=date(2027, 3, 31))
        self.assertFalse(overlap_utils.groups_conflict(a, b))

    def test_rule_4_open_ended_to_date_swallows_a_later_window(self):
        a = make_group(grid_key=1, from_date=date(2026, 4, 1), to_date=None)
        b = make_group(grid_key=2, from_date=date(2026, 10, 1), to_date=date(2027, 3, 31))
        self.assertTrue(overlap_utils.groups_conflict(a, b))

    def test_rule_5_blank_clusters_collide_with_each_other(self):
        # A blank cluster is not a wildcard, but two blanks are both reachable
        # by a policy whose own make/RTO is blank.
        a = make_group(grid_key=1, new_rto_list=frozenset())
        b = make_group(grid_key=2, new_rto_list=frozenset())
        self.assertTrue(overlap_utils.groups_conflict(a, b))

    def test_rule_5_blank_cluster_does_not_collide_with_a_populated_one(self):
        a = make_group(grid_key=1, new_rto_list=frozenset())
        b = make_group(grid_key=2, new_rto_list=frozenset({"zyx"}))
        self.assertFalse(overlap_utils.groups_conflict(a, b))

    def test_rule_5_clusters_conflict_only_when_they_share_an_item(self):
        a = make_group(grid_key=1, new_rto_list=frozenset({"mh01", "mh02"}))
        b = make_group(grid_key=2, new_rto_list=frozenset({"mh02", "mh03"}))
        c = make_group(grid_key=3, new_rto_list=frozenset({"dl01"}))
        self.assertTrue(overlap_utils.groups_conflict(a, b))
        self.assertFalse(overlap_utils.groups_conflict(a, c))

    def test_rule_6_yes_and_no_are_disjoint(self):
        a = make_group(grid_key=1, is_ncb="YES")
        b = make_group(grid_key=2, is_ncb="NO")
        self.assertFalse(overlap_utils.groups_conflict(a, b))

    def test_rule_6_na_is_a_wildcard(self):
        a = make_group(grid_key=1, is_cpa="NA")
        b = make_group(grid_key=2, is_cpa="YES")
        self.assertTrue(overlap_utils.groups_conflict(a, b))

    def test_axes_combine_with_and_not_or(self):
        # Overlapping CC but disjoint seating capacity: the engine's RULE 3
        # runs both checks in sequence, so seating alone resolves the policy.
        a = make_group(grid_key=1, cc_min=0, cc_max=1500, sc_min=2, sc_max=5)
        b = make_group(grid_key=2, cc_min=1000, cc_max=2000, sc_min=6, sc_max=10)
        self.assertFalse(overlap_utils.groups_conflict(a, b))


class ContextAxisTests(TestCase):
    """
    add_tnc is shown in the drill-down but is not a matching field. No rule in
    RULE 1-6 reads it, so letting it separate two groups would clear pairs the
    engine really does resolve to both.
    """

    def test_differing_add_tnc_does_not_separate_two_groups(self):
        a = make_group(grid_key=1, add_tnc="Excludes bolero")
        b = make_group(grid_key=2, add_tnc="Excludes scorpio")
        self.assertTrue(overlap_utils.groups_conflict(a, b))

    def test_differing_add_tnc_is_not_classified_at_all(self):
        # "Garbage Van" and "Construction Eq" are two real offers the matcher
        # is blind to - not duplicates. Neither side can be deactivated, and
        # only the insurer's own grid could separate them, so the dashboard
        # doesn't list them.
        a = make_group(grid_key=1, add_tnc="Garbage Van")
        b = make_group(grid_key=2, add_tnc="Construction Eq")
        self.assertIsNone(overlap_utils.classify_pair(a, b))

    def test_matching_add_tnc_is_a_true_duplicate(self):
        a = make_group(grid_key=1, add_tnc="Garbage Van")
        b = make_group(grid_key=2, add_tnc="Garbage Van")
        self.assertEqual(overlap_utils.classify_pair(a, b), overlap_utils.CONFLICT_EXACT_DUPLICATE)

    def test_skipping_is_about_reporting_not_about_the_engine_coping(self):
        # Both groups still match the same policy and the engine will still
        # emit MULTIPLE MATCHES - dropping them from the list doesn't pretend
        # otherwise.
        a = make_group(grid_key=1, add_tnc="Garbage Van")
        b = make_group(grid_key=2, add_tnc="Construction Eq")
        self.assertTrue(overlap_utils.groups_conflict(a, b))

    def test_skipped_pairs_are_counted_and_excluded_from_the_results(self):
        groups = [
            make_group(grid_key=1, add_tnc="Garbage Van"),
            make_group(grid_key=2, add_tnc="Construction Eq"),
        ]
        pairs, counts, _capped, tnc_differing_skipped = overlap_utils.detect_overlap_pairs(groups)
        self.assertEqual(pairs, [])
        self.assertEqual(tnc_differing_skipped, 1)
        self.assertEqual(sum(counts.values()), 0)

    def test_a_range_conflict_with_differing_tnc_is_dropped_too(self):
        # Differing T&Cs mean separate offers whatever else the pair shares.
        # Re-cutting the ranges would mean editing the insurer's rate sheet, so
        # a crossing range is no more fixable here than an identical one.
        groups = [
            make_group(grid_key=1, cc_min=0, cc_max=1500, add_tnc="Garbage Van"),
            make_group(grid_key=2, cc_min=1000, cc_max=2000, add_tnc="Construction Eq"),
        ]
        pairs, _counts, _capped, tnc_differing_skipped = overlap_utils.detect_overlap_pairs(groups)
        self.assertEqual(pairs, [])
        self.assertEqual(tnc_differing_skipped, 1)

    def test_a_range_conflict_with_matching_tnc_is_reported(self):
        groups = [
            make_group(grid_key=1, cc_min=0, cc_max=1500, add_tnc="Garbage Van"),
            make_group(grid_key=2, cc_min=1000, cc_max=2000, add_tnc="Garbage Van"),
        ]
        pairs, _counts, _capped, tnc_differing_skipped = overlap_utils.detect_overlap_pairs(groups)
        self.assertEqual(tnc_differing_skipped, 0)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["conflict_type"], overlap_utils.CONFLICT_PARTIAL)

    def test_every_listed_pair_has_matching_tnc(self):
        # The invariant the Deactivate button now relies on: a listed pair's two
        # groups are always interchangeable, so no per-pair flag is needed.
        groups = [
            make_group(grid_key=1, cc_min=0, cc_max=1500, add_tnc="Garbage Van"),
            make_group(grid_key=2, cc_min=1000, cc_max=2000, add_tnc="Garbage Van"),
            make_group(grid_key=3, cc_min=900, cc_max=1100, add_tnc="Construction Eq"),
            make_group(grid_key=4, add_tnc=""),
        ]
        pairs, _counts, _capped, _skipped = overlap_utils.detect_overlap_pairs(groups)
        self.assertTrue(pairs)
        for pair in pairs:
            tnc_row = [
                ax for ax in pair["detail"]["axes"] if ax["label"] == "Add T&C"
            ][0]
            self.assertEqual(tnc_row["overlap"], "identical")

    def test_add_tnc_appears_as_a_context_row_flagged_when_it_differs(self):
        a = make_group(grid_key=1, add_tnc="Excludes bolero")
        b = make_group(grid_key=2, add_tnc="Excludes scorpio")
        row = [ax for ax in overlap_utils.describe_pair(a, b) if ax["label"] == "Add T&C"][0]
        self.assertTrue(row["is_context"])
        self.assertEqual(row["overlap"], "differs")
        self.assertEqual(row["a"], "Excludes bolero")

    def test_add_tnc_differing_only_in_whitespace_reads_as_identical(self):
        a = make_group(grid_key=1, add_tnc="Excludes  bolero\n and scorpio")
        b = make_group(grid_key=2, add_tnc="Excludes bolero and scorpio")
        row = [ax for ax in overlap_utils.describe_pair(a, b) if ax["label"] == "Add T&C"][0]
        self.assertEqual(row["overlap"], "identical")

    def test_blank_add_tnc_renders_as_blank_not_none(self):
        a = make_group(grid_key=1, add_tnc=None)
        b = make_group(grid_key=2, add_tnc="")
        row = [ax for ax in overlap_utils.describe_pair(a, b) if ax["label"] == "Add T&C"][0]
        self.assertEqual(row["a"], "(blank)")
        self.assertEqual(row["overlap"], "identical")

    def test_context_rows_come_after_every_matching_axis(self):
        # The drill-down inserts its "not part of the matching chain" divider on
        # the first context row, so they have to be contiguous at the end.
        axes = overlap_utils.describe_pair(make_group(grid_key=1), make_group(grid_key=2))
        flags = [bool(ax.get("is_context")) for ax in axes]
        self.assertEqual(flags, sorted(flags))
        self.assertTrue(flags[-1])


class ClusterFormattingTests(TestCase):
    """
    A group's RTO cluster can hold dozens of codes. Rendering every one made the
    drill-down table overflow the page, and told nobody anything the Shared
    column doesn't already say.
    """

    def test_a_short_cluster_is_listed_in_full(self):
        self.assertEqual(
            overlap_utils.format_cluster(frozenset({"mh01", "mh02"})), "mh01, mh02"
        )

    def test_a_long_cluster_is_previewed_with_a_remainder(self):
        items = frozenset(f"rto{i:02d}" for i in range(20))
        text = overlap_utils.format_cluster(items)
        self.assertTrue(text.endswith("+12 more"))
        self.assertEqual(text.count(","), overlap_utils.CLUSTER_PREVIEW_LIMIT - 1)

    def test_long_item_names_cut_the_preview_short_of_the_item_limit(self):
        # Eight of these would be a ~370-character wall in a narrow column.
        items = frozenset(
            f"liberty_gcv_4w_apr26_andhra_pradesh_-_{i}_kv" for i in range(20)
        )
        text = overlap_utils.format_cluster(items)
        self.assertLess(len(text), 140)
        self.assertLess(text.count(",") + 1, overlap_utils.CLUSTER_PREVIEW_LIMIT)
        self.assertIn("more", text)

    def test_one_very_long_item_is_still_shown_rather_than_only_a_count(self):
        items = frozenset({"x" * 300, "y" * 300})
        text = overlap_utils.format_cluster(items)
        self.assertTrue(text.startswith("x" * 300))
        self.assertTrue(text.endswith("+1 more"))

    def test_an_empty_cluster_uses_the_given_placeholder(self):
        self.assertEqual(overlap_utils.format_cluster(frozenset()), "(blank)")
        self.assertEqual(
            overlap_utils.format_cluster(frozenset(), blank="both blank"), "both blank"
        )

    def test_describe_pair_previews_both_sides_and_the_shared_items(self):
        big = frozenset(f"rto{i:02d}" for i in range(20))
        a = make_group(grid_key=1, new_rto_list=big)
        b = make_group(grid_key=2, new_rto_list=big)
        row = [ax for ax in overlap_utils.describe_pair(a, b) if ax["label"] == "RTO Cluster"][0]
        for value in (row["a"], row["b"], row["overlap"]):
            self.assertTrue(value.endswith("+12 more"))


class ClassifyPairTests(TestCase):
    def test_identical_groups_are_exact_duplicates(self):
        a = make_group(grid_key=1, cc_min=0, cc_max=1500)
        b = make_group(grid_key=2, cc_min=0, cc_max=1500)
        self.assertEqual(overlap_utils.classify_pair(a, b), overlap_utils.CONFLICT_EXACT_DUPLICATE)

    def test_a_range_sitting_inside_another_is_contained(self):
        outer = make_group(grid_key=1, cc_min=0, cc_max=2000)
        inner = make_group(grid_key=2, cc_min=800, cc_max=1200)
        self.assertEqual(overlap_utils.classify_pair(outer, inner), overlap_utils.CONFLICT_CONTAINED)

    def test_a_wildcard_axis_still_counts_as_containing(self):
        # Every policy matching the named-product group also matches the
        # blank-product one, so the narrower group can never win outright.
        outer = make_group(grid_key=1, product="")
        inner = make_group(grid_key=2, product="two wheeler")
        self.assertEqual(overlap_utils.classify_pair(outer, inner), overlap_utils.CONFLICT_CONTAINED)

    def test_a_blank_bound_against_a_pinned_one_is_open_ended(self):
        a = make_group(grid_key=1, cc_min=1000, cc_max=None)
        b = make_group(grid_key=2, cc_min=None, cc_max=1500)
        self.assertEqual(overlap_utils.classify_pair(a, b), overlap_utils.CONFLICT_OPEN_ENDED)

    def test_crossing_ranges_are_a_partial_overlap(self):
        a = make_group(grid_key=1, cc_min=0, cc_max=1500)
        b = make_group(grid_key=2, cc_min=1000, cc_max=2000)
        self.assertEqual(overlap_utils.classify_pair(a, b), overlap_utils.CONFLICT_PARTIAL)


class CandidatePairsScalabilityTests(TestCase):
    """
    _candidate_pairs pre-filters by bucketing on every WILDCARD_BUCKET_AXES
    field (product, sub_product, fuel_type, class, ncb, cpa, zd) instead of
    product alone, so one insurer/product combination with many distinct
    values on the OTHER axes doesn't force an O(n^2) scan over its whole
    group list - this is what a production scan actually hit: 15+ minutes
    inside this exact loop on a Rate Master too large for the untouched
    product-only version to finish on the platform's time budget.

    Getting this wrong in the other direction - dropping a pair it should
    have yielded - would silently hide real conflicts, so every test here
    checks the new implementation against a brute-force O(n^2) reference
    covering every WILDCARD_BUCKET_AXES field, not just a few hand-picked
    examples.
    """

    def _brute_force_pairs(self, members):
        """
        Every pair not already ruled out by WILDCARD_BUCKET_AXES alone - the
        ground truth _candidate_pairs must match exactly, since that's the
        one set of axes it's allowed to pre-filter on before the caller
        re-checks everything (including these same axes) itself.
        """
        pairs = set()
        for i, first in enumerate(members):
            for second in members[i + 1:]:
                if overlap_utils._bucket_keys_compatible(
                    overlap_utils._bucket_key(first), overlap_utils._bucket_key(second)
                ):
                    pairs.add((first["grid_key"], second["grid_key"]))
        return pairs

    def _assert_matches_brute_force(self, members):
        expected = self._brute_force_pairs(members)
        actual = {
            (a["grid_key"], b["grid_key"]) if a["grid_key"] < b["grid_key"]
            else (b["grid_key"], a["grid_key"])
            for a, b in overlap_utils._candidate_pairs(members)
        }
        self.assertEqual(actual, expected)

    def test_matches_brute_force_with_no_wildcards_at_all(self):
        members = [
            make_group(grid_key=1, product="private car", sub_product="comp"),
            make_group(grid_key=2, product="private car", sub_product="comp"),
            make_group(grid_key=3, product="private car", sub_product="stp"),
            make_group(grid_key=4, product="two wheeler", sub_product="comp"),
        ]
        self._assert_matches_brute_force(members)

    def test_matches_brute_force_with_a_wildcard_on_one_axis(self):
        members = [
            make_group(grid_key=1, product="", sub_product="comp"),
            make_group(grid_key=2, product="private car", sub_product="comp"),
            make_group(grid_key=3, product="two wheeler", sub_product="comp"),
            make_group(grid_key=4, product="two wheeler", sub_product="stp"),
        ]
        self._assert_matches_brute_force(members)

    def test_matches_brute_force_with_wildcards_on_several_independent_axes(self):
        # A blank sub_product and a blank fuel_type on DIFFERENT groups must
        # each still be tried against every value of the other axis - this is
        # exactly the case a naive multi-axis bucketing could get wrong.
        members = [
            make_group(grid_key=1, product="gcv 4w", sub_product="", fuel_type="diesel"),
            make_group(grid_key=2, product="gcv 4w", sub_product="1+1", fuel_type=""),
            make_group(grid_key=3, product="gcv 4w", sub_product="1+1", fuel_type="petrol"),
            make_group(grid_key=4, product="gcv 4w", sub_product="std", fuel_type="diesel"),
        ]
        self._assert_matches_brute_force(members)

    def test_matches_brute_force_with_make_model_class_wildcards(self):
        # CLASS_WILDCARDS has two spellings ("" and "na"), unlike every other
        # axis's single wildcard value.
        members = [
            make_group(grid_key=1, make_model_class="na"),
            make_group(grid_key=2, make_model_class=""),
            make_group(grid_key=3, make_model_class="car"),
            make_group(grid_key=4, make_model_class="bike"),
        ]
        self._assert_matches_brute_force(members)

    def test_matches_brute_force_with_ynn_axis_wildcards(self):
        members = [
            make_group(grid_key=1, is_ncb="NA", is_cpa="YES"),
            make_group(grid_key=2, is_ncb="YES", is_cpa="NA"),
            make_group(grid_key=3, is_ncb="YES", is_cpa="YES"),
            make_group(grid_key=4, is_ncb="NO", is_cpa="YES"),
        ]
        self._assert_matches_brute_force(members)

    def test_matches_brute_force_on_a_larger_randomized_population(self):
        # A denser sweep across every axis at once, closer to a real grid's
        # shape than the hand-picked cases above.
        import random
        rng = random.Random(20260829)
        product_values = ["", "private car", "two wheeler", "gcv 4w"]
        sub_product_values = ["", "comp", "stp", "1+1"]
        fuel_values = ["", "petrol", "diesel", "cng"]
        class_values = ["", "na", "car", "bike"]
        ncb_values = ["NA", "YES", "NO"]

        members = []
        for grid_key in range(1, 121):
            members.append(make_group(
                grid_key=grid_key,
                product=rng.choice(product_values),
                sub_product=rng.choice(sub_product_values),
                fuel_type=rng.choice(fuel_values),
                make_model_class=rng.choice(class_values),
                is_ncb=rng.choice(ncb_values),
                is_cpa=rng.choice(ncb_values),
                is_zd=rng.choice(ncb_values),
            ))
        self._assert_matches_brute_force(members)

    def test_every_pair_is_yielded_exactly_once(self):
        members = [
            make_group(grid_key=1, product=""),
            make_group(grid_key=2, product="private car"),
            make_group(grid_key=3, product="private car"),
        ]
        seen = []
        for first, second in overlap_utils._candidate_pairs(members):
            key = tuple(sorted([first["grid_key"], second["grid_key"]]))
            seen.append(key)
        self.assertEqual(len(seen), len(set(seen)))

    def test_end_to_end_pairs_still_match_the_full_predicate(self):
        # The scalability change is only in candidate generation - the actual
        # conflict decision is untouched, so detect_overlap_pairs' real output
        # on a mixed population must be identical to a full brute-force
        # groups_conflict sweep over the same groups.
        members = [
            make_group(grid_key=1, product="", cc_min=0, cc_max=1500),
            make_group(grid_key=2, product="private car", cc_min=1000, cc_max=2000),
            make_group(grid_key=3, product="private car", cc_min=0, cc_max=1500),
            make_group(grid_key=4, product="two wheeler", cc_min=0, cc_max=1500),
        ]
        expected = set()
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if overlap_utils.groups_conflict(a, b):
                    key = tuple(sorted([a["grid_key"], b["grid_key"]]))
                    expected.add(key)

        pairs, _counts, _capped, _skipped = overlap_utils.detect_overlap_pairs(members)
        actual = {tuple(sorted([p["group_key_a"], p["group_key_b"]])) for p in pairs}
        self.assertEqual(actual, expected)


class DetectOverlapPairsTests(TestCase):
    def test_pairs_are_reported_once_and_severity_ordered(self):
        groups = [
            make_group(grid_key=1, cc_min=0, cc_max=1500),
            make_group(grid_key=2, cc_min=1000, cc_max=2000),   # partial with 1
            make_group(grid_key=3, cc_min=0, cc_max=1500),      # duplicate of 1
        ]
        pairs, counts, capped_types, _skipped = overlap_utils.detect_overlap_pairs(groups)
        self.assertEqual(capped_types, [])
        keys = {(p["group_key_a"], p["group_key_b"]) for p in pairs}
        self.assertEqual(keys, {(1, 2), (1, 3), (2, 3)})
        self.assertEqual(counts[overlap_utils.CONFLICT_EXACT_DUPLICATE], 1)
        self.assertEqual(counts[overlap_utils.CONFLICT_PARTIAL], 2)
        # Exact duplicates rank first so a capped scan keeps the clearest fixes.
        self.assertEqual(pairs[0]["conflict_type"], overlap_utils.CONFLICT_EXACT_DUPLICATE)
        self.assertTrue(all(p["group_key_a"] < p["group_key_b"] for p in pairs))

    def test_a_capped_type_still_reports_its_true_count(self):
        # Five identical groups -> 10 exact-duplicate pairs, stored subset of 3.
        groups = [make_group(grid_key=i) for i in range(1, 6)]
        pairs, counts, capped_types, _skipped = overlap_utils.detect_overlap_pairs(
            groups, type_caps={overlap_utils.CONFLICT_EXACT_DUPLICATE: 3}
        )
        self.assertEqual(len(pairs), 3)
        self.assertEqual(capped_types, [overlap_utils.CONFLICT_EXACT_DUPLICATE])
        # The count the dashboard card shows is the real one, not the stored one.
        self.assertEqual(counts[overlap_utils.CONFLICT_EXACT_DUPLICATE], 10)

    def test_an_uncapped_type_is_not_listed_as_capped(self):
        groups = [make_group(grid_key=i) for i in range(1, 4)]
        _pairs, _counts, capped_types, _skipped = overlap_utils.detect_overlap_pairs(
            groups, type_caps={overlap_utils.CONFLICT_EXACT_DUPLICATE: 99}
        )
        self.assertEqual(capped_types, [])

    def test_wildcard_product_groups_are_compared_against_every_bucket(self):
        # _candidate_pairs buckets by product for speed; a blank-product group
        # must still be tried against every other bucket.
        groups = [
            make_group(grid_key=1, product=""),
            make_group(grid_key=2, product="two wheeler"),
            make_group(grid_key=3, product="private car"),
        ]
        pairs, _counts, _capped, _skipped = overlap_utils.detect_overlap_pairs(groups)
        keys = {(p["group_key_a"], p["group_key_b"]) for p in pairs}
        self.assertEqual(keys, {(1, 2), (1, 3)})


class BuildClusterCodeIndexTests(TestCase):
    """
    build_cluster_code_index({name -> set(codes)}) is the raw material the
    Double Rate Risk sweep expands a group's cluster NAMES into cluster CODES
    through - it has to normalize exactly the way the two things it bridges
    already do: names like load_active_groups' split_cluster (lower, stripped)
    so a group's new_rto_list frozenset can key straight into it, and codes
    like mapping_engine.build_master_lookup's resolve() (upper, stripped) so a
    match here is one the engine would actually make.
    """

    def test_indexes_by_lowercased_name_and_uppercased_codes(self):
        RTOMaster.objects.create(rto_name="ZYX", rto_cluster="mh01, mh02")
        index = overlap_utils.build_cluster_code_index("rto_name", "rto_cluster", RTOMaster)
        self.assertEqual(index, {"zyx": {"MH01", "MH02"}})

    def test_a_blank_cluster_is_skipped(self):
        RTOMaster.objects.create(rto_name="zyx", rto_cluster="")
        RTOMaster.objects.create(rto_name="abc", rto_cluster=None)
        index = overlap_utils.build_cluster_code_index("rto_name", "rto_cluster", RTOMaster)
        self.assertEqual(index, {})

    def test_works_for_make_model_master_too(self):
        MakeModelMaster.objects.create(make_model_name="honda_all", make_model_cluster="ACTIVA, DIO")
        index = overlap_utils.build_cluster_code_index(
            "make_model_name", "make_model_cluster", MakeModelMaster
        )
        self.assertEqual(index, {"honda_all": {"ACTIVA", "DIO"}})

    def test_a_literal_zero_placeholder_is_not_a_real_code(self):
        # Several real RTOMaster rows carry a bare "0" as cluster padding
        # (e.g. "AP30,0,0,0,0,0,0,0,0,0,0,0,0,0") - left in, it alone connects
        # every row that happens to have the same padding, none of which
        # share anything real.
        RTOMaster.objects.create(rto_name="ap_zone_a", rto_cluster="AP04,0,0,0")
        RTOMaster.objects.create(rto_name="ap_zone_b", rto_cluster="AP31,0,0")
        index = overlap_utils.build_cluster_code_index("rto_name", "rto_cluster", RTOMaster)
        self.assertEqual(index, {"ap_zone_a": {"AP04"}, "ap_zone_b": {"AP31"}})

    def test_a_cluster_that_is_only_placeholders_is_dropped_entirely(self):
        # No real codes left after stripping padding - the name shouldn't
        # appear in the index at all, or it would falsely register as
        # "compatible with nothing" rather than simply absent.
        RTOMaster.objects.create(rto_name="all_padding", rto_cluster="0,0,0")
        index = overlap_utils.build_cluster_code_index("rto_name", "rto_cluster", RTOMaster)
        self.assertEqual(index, {})


class AxisConflictStatusTests(TestCase):
    """_axis_conflict_status: the per-axis decision the Double Rate Risk sweep
    is built on - compatible-by-name, compatible-only-through-a-hidden-code,
    or genuinely incompatible."""

    def test_shared_name_is_compatible_and_not_hidden(self):
        # Already the primary sweep's territory - nothing new to report here.
        compatible, hidden = overlap_utils._axis_conflict_status(
            frozenset({"zyx"}), frozenset({"zyx"}), {}
        )
        self.assertTrue(compatible)
        self.assertEqual(hidden, frozenset())

    def test_both_blank_is_compatible_and_not_hidden(self):
        compatible, hidden = overlap_utils._axis_conflict_status(
            frozenset(), frozenset(), {}
        )
        self.assertTrue(compatible)
        self.assertEqual(hidden, frozenset())

    def test_different_names_sharing_a_raw_code_are_compatible_and_hidden(self):
        code_index = {"zyx": {"MH01", "MH02"}, "abc": {"MH02", "DL01"}}
        compatible, hidden = overlap_utils._axis_conflict_status(
            frozenset({"zyx"}), frozenset({"abc"}), code_index
        )
        self.assertTrue(compatible)
        self.assertEqual(hidden, {"MH02"})

    def test_different_names_with_no_shared_code_are_incompatible(self):
        code_index = {"zyx": {"MH01"}, "abc": {"DL01"}}
        compatible, hidden = overlap_utils._axis_conflict_status(
            frozenset({"zyx"}), frozenset({"abc"}), code_index
        )
        self.assertFalse(compatible)
        self.assertEqual(hidden, frozenset())

    def test_blank_against_populated_is_incompatible_not_hidden(self):
        # A blank cluster resolves to zero codes, so it can never "hide" a
        # collision with a populated one - RULE 5's own blank-isn't-a-wildcard
        # rule still holds.
        code_index = {"zyx": {"MH01"}}
        compatible, hidden = overlap_utils._axis_conflict_status(
            frozenset(), frozenset({"zyx"}), code_index
        )
        self.assertFalse(compatible)


class DetectDoubleRateRiskPairsTests(TestCase):
    """
    Pure unit tests against make_group() dicts - no DB. These are the pairs
    entirely missed by detect_overlap_pairs: everything else about the two
    groups matches, but their RTO/Make cluster NAMES differ, and only the raw
    codes underneath (via RTOMaster/MakeModelMaster) actually collide.
    """

    def test_a_hidden_rto_collision_is_reported(self):
        a = make_group(grid_key=1, new_rto_list=frozenset({"cluster_a"}))
        b = make_group(grid_key=2, new_rto_list=frozenset({"cluster_b"}))
        rto_index = {"cluster_a": {"MH01"}, "cluster_b": {"MH01", "MH02"}}

        pairs, count, capped = overlap_utils.detect_double_rate_risk_pairs(
            [a, b], rto_index, {}
        )
        self.assertEqual(count, 1)
        self.assertFalse(capped)
        self.assertEqual(pairs[0]["conflict_type"], overlap_utils.CONFLICT_DOUBLE_RATE_RISK)
        self.assertEqual(pairs[0]["group_key_a"], 1)
        self.assertEqual(pairs[0]["group_key_b"], 2)

    def test_a_hidden_make_model_collision_is_reported(self):
        a = make_group(grid_key=1, new_vehicle_makes=frozenset({"honda_a"}))
        b = make_group(grid_key=2, new_vehicle_makes=frozenset({"honda_b"}))
        make_index = {"honda_a": {"ACTIVA"}, "honda_b": {"ACTIVA", "DIO"}}

        pairs, count, _capped = overlap_utils.detect_double_rate_risk_pairs(
            [a, b], {}, make_index
        )
        self.assertEqual(count, 1)
        self.assertEqual(pairs[0]["detail"]["axes"], overlap_utils.describe_pair(
            a, b, hidden_axis_codes={"new_vehicle_makes": frozenset({"ACTIVA"})}
        ))

    def test_a_pair_already_conflicting_by_name_is_not_reported_again(self):
        # Shared cluster name -> already detect_overlap_pairs' territory. This
        # sweep exists for pairs that ONLY collide through the master tables.
        a = make_group(grid_key=1, new_rto_list=frozenset({"zyx"}))
        b = make_group(grid_key=2, new_rto_list=frozenset({"zyx"}))
        rto_index = {"zyx": {"MH01"}}

        pairs, count, _capped = overlap_utils.detect_double_rate_risk_pairs(
            [a, b], rto_index, {}
        )
        self.assertEqual(count, 0)
        self.assertEqual(pairs, [])

    def test_no_shared_code_at_all_is_not_reported(self):
        a = make_group(grid_key=1, new_rto_list=frozenset({"cluster_a"}))
        b = make_group(grid_key=2, new_rto_list=frozenset({"cluster_b"}))
        rto_index = {"cluster_a": {"MH01"}, "cluster_b": {"DL01"}}

        pairs, count, _capped = overlap_utils.detect_double_rate_risk_pairs(
            [a, b], rto_index, {}
        )
        self.assertEqual(count, 0)

    def test_a_hidden_rto_collision_is_not_reported_if_another_axis_discriminates(self):
        # The user's own requirement: the two groups must "match the same
        # policy parameters" for a master-table collision to be a real risk.
        # Disjoint CC ranges mean no policy can ever reach both groups anyway.
        a = make_group(grid_key=1, cc_min=0, cc_max=1000, new_rto_list=frozenset({"cluster_a"}))
        b = make_group(grid_key=2, cc_min=1500, cc_max=2000, new_rto_list=frozenset({"cluster_b"}))
        rto_index = {"cluster_a": {"MH01"}, "cluster_b": {"MH01"}}

        pairs, count, _capped = overlap_utils.detect_double_rate_risk_pairs(
            [a, b], rto_index, {}
        )
        self.assertEqual(count, 0)

    def test_differing_add_tnc_does_not_exclude_a_double_rate_risk_pair(self):
        # Unlike every other conflict type: the fix here is RTOMaster/
        # MakeModelMaster, which the brokerage owns outright, regardless of
        # whether the two RateMaster rows are legitimately different offers.
        a = make_group(
            grid_key=1, new_rto_list=frozenset({"cluster_a"}), add_tnc="Garbage Van"
        )
        b = make_group(
            grid_key=2, new_rto_list=frozenset({"cluster_b"}), add_tnc="Construction Eq"
        )
        rto_index = {"cluster_a": {"MH01"}, "cluster_b": {"MH01"}}

        pairs, count, _capped = overlap_utils.detect_double_rate_risk_pairs(
            [a, b], rto_index, {}
        )
        self.assertEqual(count, 1)

    def test_cap_reports_the_true_count_even_when_truncated(self):
        groups = [
            make_group(grid_key=i, new_rto_list=frozenset({f"cluster_{i}"}))
            for i in range(1, 5)
        ]
        rto_index = {f"cluster_{i}": {"MH01"} for i in range(1, 5)}

        pairs, count, capped = overlap_utils.detect_double_rate_risk_pairs(
            groups, rto_index, {}, cap=2
        )
        self.assertEqual(len(pairs), 2)
        self.assertEqual(count, 6)  # C(4, 2)
        self.assertTrue(capped)


class RunOverlapScanDoubleRateRiskTests(TestCase):
    """Integration: does a real master-table collision reach the stored scan?"""

    def setUp(self):
        self.product = ProductMaster.objects.create(name="Private Car")

    def _rate(self, group, **overrides):
        fields = {
            "insurance_company": "Acme General",
            "product": self.product,
            "status": "ACTIVE",
            "is_deleted": "NO",
            "group": group,
        }
        fields.update(overrides)
        return RateMaster.objects.create(**fields)

    def test_a_real_master_table_collision_is_scanned_and_stored(self):
        RTOMaster.objects.create(rto_name="cluster_a", rto_cluster="MH01, MH02")
        RTOMaster.objects.create(rto_name="cluster_b", rto_cluster="MH02, DL01")
        group_a = RateGroup.objects.create(key_hash="h1")
        group_b = RateGroup.objects.create(key_hash="h2")
        self._rate(group_a, new_rto_list="cluster_a")
        self._rate(group_b, new_rto_list="cluster_b")

        scan = RateOverlapScan.objects.create()
        overlap_utils.run_overlap_scan(scan.id)
        scan.refresh_from_db()

        self.assertEqual(scan.type_counts.get(overlap_utils.CONFLICT_DOUBLE_RATE_RISK), 1)
        pair = RateOverlapPair.objects.get(conflict_type="DOUBLE_RATE_RISK")
        self.assertEqual({pair.group_key_a, pair.group_key_b}, {group_a.id, group_b.id})
        rto_axis = [ax for ax in pair.detail["axes"] if ax["label"] == "RTO Cluster"][0]
        self.assertTrue(rto_axis["is_hidden_risk"])
        self.assertIn("MH02", rto_axis["overlap"])

    def test_a_pair_with_no_master_collision_produces_no_double_rate_risk(self):
        RTOMaster.objects.create(rto_name="cluster_a", rto_cluster="MH01")
        RTOMaster.objects.create(rto_name="cluster_b", rto_cluster="DL01")
        self._rate(RateGroup.objects.create(key_hash="h1"), new_rto_list="cluster_a")
        self._rate(RateGroup.objects.create(key_hash="h2"), new_rto_list="cluster_b")

        scan = RateOverlapScan.objects.create()
        overlap_utils.run_overlap_scan(scan.id)
        scan.refresh_from_db()

        self.assertEqual(scan.type_counts.get(overlap_utils.CONFLICT_DOUBLE_RATE_RISK), 0)
        self.assertFalse(RateOverlapPair.objects.filter(conflict_type="DOUBLE_RATE_RISK").exists())


class LoadActiveGroupsTests(TestCase):
    """The DB half: what gets loaded, and what a 'group' means."""

    def setUp(self):
        self.product = ProductMaster.objects.create(name="Private Car")
        self.other_product = ProductMaster.objects.create(name="Two Wheeler")
        self.na = YesNoNAMaster.objects.create(code="NA")

    def _rate(self, group=None, **overrides):
        fields = {
            "insurance_company": "Acme General",
            "product": self.product,
            "status": "ACTIVE",
            "is_deleted": "NO",
            "cc_min": 0,
            "cc_max": 1500,
            "group": group,
        }
        fields.update(overrides)
        return RateMaster.objects.create(**fields)

    def test_insurer_filter_excludes_other_insurers(self):
        self._rate(group=RateGroup.objects.create(key_hash="h1"), insurance_company="Acme General")
        self._rate(group=RateGroup.objects.create(key_hash="h2"), insurance_company="Zenith Insurance")

        groups = overlap_utils.load_active_groups(insurer="Acme General")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["insurer_display"], "Acme General")

    def test_no_insurer_filter_returns_everyone(self):
        self._rate(group=RateGroup.objects.create(key_hash="h1"), insurance_company="Acme General")
        self._rate(group=RateGroup.objects.create(key_hash="h2"), insurance_company="Zenith Insurance")
        self.assertEqual(len(overlap_utils.load_active_groups()), 2)

    def test_as_of_date_excludes_a_group_not_valid_on_that_date(self):
        from datetime import date
        self._rate(
            group=RateGroup.objects.create(key_hash="h1"),
            from_date=date(2026, 1, 1), to_date=date(2026, 6, 30),
        )
        self._rate(
            group=RateGroup.objects.create(key_hash="h2"),
            from_date=date(2026, 7, 1), to_date=date(2026, 12, 31),
        )

        groups = overlap_utils.load_active_groups(as_of_date=date(2026, 3, 15))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["from_date"], date(2026, 1, 1))

    def test_as_of_date_treats_a_null_bound_as_open_ended(self):
        # Same convention as the payout lookups (motor_payout_rates etc.): a
        # blank from_date/to_date means "always valid", not "never valid".
        from datetime import date
        self._rate(group=RateGroup.objects.create(key_hash="h1"), from_date=None, to_date=None)

        groups = overlap_utils.load_active_groups(as_of_date=date(2026, 3, 15))
        self.assertEqual(len(groups), 1)

    def test_insurer_and_as_of_date_combine(self):
        from datetime import date
        self._rate(
            group=RateGroup.objects.create(key_hash="h1"), insurance_company="Acme General",
            from_date=date(2026, 1, 1), to_date=date(2026, 6, 30),
        )
        self._rate(
            group=RateGroup.objects.create(key_hash="h2"), insurance_company="Acme General",
            from_date=date(2026, 7, 1), to_date=date(2026, 12, 31),
        )
        self._rate(
            group=RateGroup.objects.create(key_hash="h3"), insurance_company="Zenith Insurance",
            from_date=date(2026, 1, 1), to_date=date(2026, 6, 30),
        )

        groups = overlap_utils.load_active_groups(insurer="Acme General", as_of_date=date(2026, 3, 15))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["insurer_display"], "Acme General")

    def test_run_overlap_scan_reads_its_scope_off_the_scan_row(self):
        # start_overlap_scan/the management command record filter_insurer and
        # filter_as_of_date at CREATION time; run_overlap_scan(scan_id) itself
        # takes no extra arguments, so it has to read them back off the scan.
        self._rate(group=RateGroup.objects.create(key_hash="h1"), insurance_company="Acme General")
        self._rate(group=RateGroup.objects.create(key_hash="h2"), insurance_company="Zenith Insurance")

        scan = RateOverlapScan.objects.create(filter_insurer="Acme General")
        overlap_utils.run_overlap_scan(scan.id)
        scan.refresh_from_db()

        self.assertEqual(scan.status, RateOverlapScan.STATUS_COMPLETED)
        self.assertEqual(scan.groups_scanned, 1)

    def test_rows_sharing_a_group_are_one_group_not_a_self_conflict(self):
        # A rate card exploded across per-RTO rows must not be reported as
        # conflicting with itself — the engine counts distinct groups.
        group = RateGroup.objects.create(key_hash="hash-a")
        self._rate(group=group, new_rto_list="mh01")
        self._rate(group=group, new_rto_list="mh02")

        groups = overlap_utils.load_active_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["row_count"], 2)
        # The group's RTO footprint is the union of its rows'.
        self.assertEqual(groups[0]["new_rto_list"], frozenset({"mh01", "mh02"}))

        pairs, _counts, _capped, _skipped = overlap_utils.detect_overlap_pairs(groups)
        self.assertEqual(pairs, [])

    def test_inactive_and_deleted_rows_are_excluded(self):
        self._rate(group=RateGroup.objects.create(key_hash="h1"))
        self._rate(group=RateGroup.objects.create(key_hash="h2"), status="INACTIVE")
        self._rate(group=RateGroup.objects.create(key_hash="h3"), is_deleted="YES")

        groups = overlap_utils.load_active_groups()
        self.assertEqual(len(groups), 1)

    def test_insurer_names_differing_only_in_case_still_collide(self):
        # mapping_engine lower-cases insurance_company before partitioning, so
        # these two land in the same bucket and can both match one policy.
        self._rate(group=RateGroup.objects.create(key_hash="h1"), insurance_company="Acme General")
        self._rate(group=RateGroup.objects.create(key_hash="h2"), insurance_company="ACME GENERAL")

        pairs, _counts, _capped, _skipped = overlap_utils.detect_overlap_pairs(overlap_utils.load_active_groups())
        self.assertEqual(len(pairs), 1)

    def test_policy_type_does_not_separate_two_groups(self):
        # No rule in RULE 1-6 reads policy_type, so two groups differing only
        # there are genuinely ambiguous to the engine.
        first = PolicyTypeMaster.objects.create(name="New")
        second = PolicyTypeMaster.objects.create(name="Rollover")
        self._rate(group=RateGroup.objects.create(key_hash="h1"), policy_type=first)
        self._rate(group=RateGroup.objects.create(key_hash="h2"), policy_type=second)

        pairs, _counts, _capped, _skipped = overlap_utils.detect_overlap_pairs(overlap_utils.load_active_groups())
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["conflict_type"], overlap_utils.CONFLICT_EXACT_DUPLICATE)

    def test_different_product_groups_are_not_reported(self):
        self._rate(group=RateGroup.objects.create(key_hash="h1"), product=self.product)
        self._rate(group=RateGroup.objects.create(key_hash="h2"), product=self.other_product)

        pairs, _counts, _capped, _skipped = overlap_utils.detect_overlap_pairs(overlap_utils.load_active_groups())
        self.assertEqual(pairs, [])

    def test_run_overlap_scan_stores_pairs_and_completes(self):
        self._rate(group=RateGroup.objects.create(key_hash="h1"))
        self._rate(group=RateGroup.objects.create(key_hash="h2"))

        scan = RateOverlapScan.objects.create()
        overlap_utils.run_overlap_scan(scan.id)

        scan.refresh_from_db()
        self.assertEqual(scan.status, RateOverlapScan.STATUS_COMPLETED)
        self.assertEqual(scan.groups_scanned, 2)
        self.assertEqual(scan.pairs_found, 1)
        self.assertEqual(scan.type_counts[overlap_utils.CONFLICT_EXACT_DUPLICATE], 1)
        self.assertEqual(scan.capped_types, [])
        self.assertFalse(scan.was_capped)
        self.assertIsNotNone(scan.finished_at)
        self.assertEqual(RateOverlapPair.objects.filter(scan=scan).count(), 1)

    def test_a_completed_scan_supersedes_the_previous_one(self):
        self._rate(group=RateGroup.objects.create(key_hash="h1"))
        self._rate(group=RateGroup.objects.create(key_hash="h2"))

        first = RateOverlapScan.objects.create()
        overlap_utils.run_overlap_scan(first.id)
        second = RateOverlapScan.objects.create()
        overlap_utils.run_overlap_scan(second.id)

        self.assertFalse(RateOverlapScan.objects.filter(id=first.id).exists())
        self.assertEqual(RateOverlapPair.objects.count(), 1)
        self.assertEqual(RateOverlapPair.objects.first().scan_id, second.id)


@override_settings(
    # These are the only tests in the suite that render a full template, and
    # the project's real staticfiles backend
    # (CompressedManifestStaticFilesStorage) refuses to resolve a {% static %}
    # tag without a manifest from collectstatic. Swap in the plain backend so
    # the assertions exercise the view, not the asset pipeline.
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class OverlapDashboardViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        Group.objects.get_or_create(name="Can_View_Rate_Master_Health")
        self.user = User.objects.create_user(username="ops", password="a-strong-test-password-1")
        self.user.groups.add(Group.objects.get(name="Can_View_Rate_Master_Health"))
        self.client.force_login(self.user)

        self.product = ProductMaster.objects.create(name="Private Car")
        for key_hash in ("h1", "h2"):
            RateMaster.objects.create(
                insurance_company="Acme General",
                product=self.product,
                status="ACTIVE",
                is_deleted="NO",
                cc_min=0,
                cc_max=1500,
                group=RateGroup.objects.create(key_hash=key_hash),
            )
        scan = RateOverlapScan.objects.create()
        overlap_utils.run_overlap_scan(scan.id)
        self.scan = scan

    def test_overlaps_tab_shows_the_latest_scan_counts(self):
        response = self.client.get(reverse("rate_master_health"), {"view": "overlap"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["overlap_total"], 1)
        counts = {rule["key"]: rule["count"] for rule in response.context["overlap_counts"]}
        self.assertEqual(counts["EXACT_DUPLICATE"], 1)
        self.assertEqual(counts["PARTIAL"], 0)

    def test_drill_down_lists_the_pairs_of_the_selected_type(self):
        response = self.client.get(
            reverse("rate_master_health"), {"view": "overlap", "overlap_type": "EXACT_DUPLICATE"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["overlap_page_obj"].paginator.count, 1)

    def test_no_template_comment_leaks_into_the_rendered_page(self):
        # Django's {# #} is single-line only - a multi-line one renders as
        # visible page text instead of being stripped, which previously dumped
        # developer notes into the Action column and blew out the table width.
        for params in (
            {"view": "overlap"},
            {"view": "overlap", "overlap_type": "EXACT_DUPLICATE"},
        ):
            html = self.client.get(reverse("rate_master_health"), params).content.decode()
            self.assertNotIn("{#", html)
            self.assertNotIn("#}", html)

    def test_drill_down_renders_add_tnc_under_a_context_divider(self):
        response = self.client.get(
            reverse("rate_master_health"), {"view": "overlap", "overlap_type": "EXACT_DUPLICATE"}
        )
        html = response.content.decode()
        self.assertIn("Add T&amp;C", html)
        # The divider marks where matching fields stop and context begins.
        self.assertIn("Not part of the matching chain", html)

    def test_unknown_overlap_type_selects_nothing(self):
        response = self.client.get(
            reverse("rate_master_health"), {"view": "overlap", "overlap_type": "NOT_A_TYPE"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_overlap_type"], "")
        self.assertIsNone(response.context["overlap_page_obj"])

    def test_deactivating_a_group_marks_its_rows_inactive_and_audits_it(self):
        pair = RateOverlapPair.objects.get()
        response = self.client.post(
            reverse("deactivate_rate_group", args=[pair.group_key_b]),
            {"overlap_type": "EXACT_DUPLICATE", "pair_id": pair.id},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            RateMaster.objects.filter(group_id=pair.group_key_b, status="INACTIVE").count(), 1
        )
        # The other side is untouched — deactivating both would leave the
        # segment with no rate at all.
        self.assertEqual(
            RateMaster.objects.filter(group_id=pair.group_key_a, status="ACTIVE").count(), 1
        )
        from insurance.models import AuditLog
        self.assertTrue(AuditLog.objects.filter(action="OVERLAP DEACTIVATE").exists())

    def test_deactivate_ignores_a_get(self):
        pair = RateOverlapPair.objects.get()
        self.client.get(reverse("deactivate_rate_group", args=[pair.group_key_b]))
        self.assertEqual(RateMaster.objects.filter(status="ACTIVE").count(), 2)

    def test_deactivate_is_refused_for_a_pair_the_latest_scan_dropped(self):
        # Pairs whose T&Cs differ never get listed, so a stale page or a
        # hand-made POST naming one finds no live pair id and is refused -
        # switching one off would drop a rate that is still sold.
        pair = RateOverlapPair.objects.get()
        stale_pair_id = pair.id
        pair.delete()

        self.client.post(
            reverse("deactivate_rate_group", args=[2]),
            {"overlap_type": "EXACT_DUPLICATE", "pair_id": stale_pair_id},
        )
        self.assertEqual(RateMaster.objects.filter(status="ACTIVE").count(), 2)

    def test_deactivate_is_refused_without_a_matching_pair(self):
        pair = RateOverlapPair.objects.get()
        self.client.post(
            reverse("deactivate_rate_group", args=[pair.group_key_b]),
            {"overlap_type": "EXACT_DUPLICATE", "pair_id": 999999},
        )
        self.assertEqual(RateMaster.objects.filter(status="ACTIVE").count(), 2)

    def test_deactivate_is_refused_when_the_group_is_not_in_the_named_pair(self):
        pair = RateOverlapPair.objects.get()
        unrelated = RateMaster.objects.create(
            insurance_company="Acme General",
            product=self.product,
            status="ACTIVE",
            is_deleted="NO",
            group=RateGroup.objects.create(key_hash="h-unrelated"),
        )
        self.client.post(
            reverse("deactivate_rate_group", args=[unrelated.group_id]),
            {"overlap_type": "EXACT_DUPLICATE", "pair_id": pair.id},
        )
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.status, "ACTIVE")


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class OverlapScanScopeViewTests(TestCase):
    """The Insurer / Valid As Of filter on the Run Scan form itself."""

    def setUp(self):
        self.client = Client()
        Group.objects.get_or_create(name="Can_View_Rate_Master_Health")
        self.user = User.objects.create_user(username="ops", password="a-strong-test-password-1")
        self.user.groups.add(Group.objects.get(name="Can_View_Rate_Master_Health"))
        self.client.force_login(self.user)

        self.product = ProductMaster.objects.create(name="Private Car")
        RateMaster.objects.create(
            insurance_company="Acme General", product=self.product,
            status="ACTIVE", is_deleted="NO",
            group=RateGroup.objects.create(key_hash="h1"),
        )
        RateMaster.objects.create(
            insurance_company="Zenith Insurance", product=self.product,
            status="ACTIVE", is_deleted="NO",
            group=RateGroup.objects.create(key_hash="h2"),
        )

    def test_posting_an_insurer_scopes_the_created_scan(self):
        self.client.post(reverse("start_overlap_scan"), {"insurer": "Acme General"})
        scan = RateOverlapScan.objects.get()
        self.assertEqual(scan.filter_insurer, "Acme General")
        self.assertIsNone(scan.filter_as_of_date)

    def test_posting_an_as_of_date_scopes_the_created_scan(self):
        self.client.post(reverse("start_overlap_scan"), {"as_of_date": "2026-03-15"})
        scan = RateOverlapScan.objects.get()
        self.assertEqual(str(scan.filter_as_of_date), "2026-03-15")

    def test_posting_neither_filter_scans_everything(self):
        self.client.post(reverse("start_overlap_scan"), {})
        scan = RateOverlapScan.objects.get()
        self.assertIsNone(scan.filter_insurer)
        self.assertIsNone(scan.filter_as_of_date)

    def test_an_insurer_with_no_active_rows_is_refused_before_queuing(self):
        response = self.client.post(
            reverse("start_overlap_scan"), {"insurer": "Not A Real Insurer"}, follow=True
        )
        self.assertFalse(RateOverlapScan.objects.exists())
        self.assertContains(response, "has no active Rate Master rows")

    def test_an_invalid_date_is_refused_before_queuing(self):
        response = self.client.post(
            reverse("start_overlap_scan"), {"as_of_date": "not-a-date"}, follow=True
        )
        self.assertFalse(RateOverlapScan.objects.exists())
        self.assertContains(response, "is not a valid date")

    def test_overlap_insurer_list_is_scoped_to_the_active_rate_master(self):
        response = self.client.get(reverse("rate_master_health"), {"view": "overlap"})
        self.assertEqual(
            set(response.context["overlap_insurer_list"]), {"Acme General", "Zenith Insurance"}
        )

    def test_the_form_pre_fills_from_the_latest_scans_own_scope(self):
        RateOverlapScan.objects.create(
            filter_insurer="Acme General", filter_as_of_date=None,
            status=RateOverlapScan.STATUS_COMPLETED,
        )
        response = self.client.get(reverse("rate_master_health"), {"view": "overlap"})
        self.assertContains(response, 'value="Acme General" selected')


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class DoubleRateRiskDashboardViewTests(TestCase):
    """
    The new card end-to-end: real master-table rows -> a real scan -> the
    Overlaps tab's card and drill-down for Double Rate Risks specifically.
    """

    def setUp(self):
        self.client = Client()
        Group.objects.get_or_create(name="Can_View_Rate_Master_Health")
        self.user = User.objects.create_user(username="ops", password="a-strong-test-password-1")
        self.user.groups.add(Group.objects.get(name="Can_View_Rate_Master_Health"))
        self.client.force_login(self.user)

        self.product = ProductMaster.objects.create(name="Private Car")
        RTOMaster.objects.create(rto_name="cluster_a", rto_cluster="MH01, MH02")
        RTOMaster.objects.create(rto_name="cluster_b", rto_cluster="MH02, DL01")
        RateMaster.objects.create(
            insurance_company="Acme General", product=self.product,
            status="ACTIVE", is_deleted="NO",
            group=RateGroup.objects.create(key_hash="h1"), new_rto_list="cluster_a",
        )
        RateMaster.objects.create(
            insurance_company="Acme General", product=self.product,
            status="ACTIVE", is_deleted="NO",
            group=RateGroup.objects.create(key_hash="h2"), new_rto_list="cluster_b",
        )
        scan = RateOverlapScan.objects.create()
        overlap_utils.run_overlap_scan(scan.id)
        self.scan = scan

    def test_double_rate_risk_card_shows_the_count(self):
        response = self.client.get(reverse("rate_master_health"), {"view": "overlap"})
        counts = {rule["key"]: rule["count"] for rule in response.context["overlap_counts"]}
        self.assertEqual(counts["DOUBLE_RATE_RISK"], 1)

    def test_drill_down_names_the_colliding_raw_code(self):
        response = self.client.get(
            reverse("rate_master_health"), {"view": "overlap", "overlap_type": "DOUBLE_RATE_RISK"}
        )
        html = response.content.decode()
        self.assertIn("MH02", html)
        self.assertIn("shares raw code", html)

    def test_deactivate_button_is_not_offered_for_this_card(self):
        # Deactivating a group doesn't fix the actual cause - the master
        # table entry - so the button that IS offered for every other card
        # must not appear here. "ovl-btn-danger" alone isn't a safe check -
        # its CSS rule is always in the page's <style> block - so check for
        # the actual deactivate form/URL instead.
        pair = RateOverlapPair.objects.get(conflict_type="DOUBLE_RATE_RISK")
        response = self.client.get(
            reverse("rate_master_health"), {"view": "overlap", "overlap_type": "DOUBLE_RATE_RISK"}
        )
        html = response.content.decode()
        self.assertNotIn(reverse("deactivate_rate_group", args=[pair.group_key_b]), html)
        self.assertIn("fix the master table entry", html)

    def test_deactivate_is_still_refused_server_side_for_a_double_rate_risk_pair(self):
        # The button is hidden client-side, but the view must not trust that -
        # a hand-made POST naming this pair should still be refused, the same
        # way a stale/no-longer-listed pair already is.
        pair = RateOverlapPair.objects.get(conflict_type="DOUBLE_RATE_RISK")
        self.client.post(
            reverse("deactivate_rate_group", args=[pair.group_key_b]),
            {"overlap_type": "DOUBLE_RATE_RISK", "pair_id": pair.id},
        )
        self.assertEqual(RateMaster.objects.filter(status="ACTIVE").count(), 2)
