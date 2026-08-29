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
    PolicyTypeMaster, ProductMaster,
    RateGroup, RateMaster, RateOverlapPair, RateOverlapScan,
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
