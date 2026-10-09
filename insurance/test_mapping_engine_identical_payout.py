import io
import tempfile
from unittest import mock

import pandas as pd
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from insurance.mapping_engine import (
    health_matched_rows_share_one_rate,
    matched_rows_share_one_payout,
    payout_signature,
    process_mis_mapping,
)
from insurance.models import HealthRateMaster, MISFile, RateGroup, RateMaster

INSURER = "Acme General Insurance"
HEALTH_INSURER = "Zenith Health Insurance"
MATCH = "✅ MATCH"
MULTIPLE = "⚠️ MULTIPLE MATCHES"


def _row(**overrides):
    base = {
        "po_type": "On Net", "po_od_rate": 0.0, "po_tp_rate": 0.0, "po_net_rate": 10.0,
        "po_flat_amount": None,
        "pi_type": "On Net", "pi_od_rate": 0.0, "pi_tp_rate": 0.0, "pi_net_rate": 20.0,
        "pi_flat_amount": None,
    }
    return {**base, **overrides}


def _share_one_payout(*rows):
    return matched_rows_share_one_payout(pd.DataFrame(list(rows)))


class PayoutSignatureTests(SimpleTestCase):
    """What counts as 'the same payout' when several rate groups match one policy."""

    def test_identical_rows_share_one_payout(self):
        self.assertTrue(_share_one_payout(_row(), _row(), _row()))

    def test_a_different_payout_rate_does_not(self):
        self.assertFalse(_share_one_payout(_row(), _row(po_net_rate=11.0)))

    def test_a_different_payin_rate_does_not(self):
        # Same payout, different payin: the mapped row's Pirate / margin would differ.
        self.assertFalse(_share_one_payout(_row(), _row(pi_net_rate=21.0)))

    def test_only_the_rates_are_compared_not_the_flat_amount(self):
        self.assertTrue(_share_one_payout(_row(), _row(po_flat_amount=500.0, pi_flat_amount=900.0)))

    def test_the_same_number_under_a_different_payout_type_is_a_different_rate(self):
        self.assertFalse(_share_one_payout(
            _row(),
            _row(po_type="On OD", po_od_rate=10.0, po_net_rate=0.0),
        ))

    def test_the_same_number_under_a_different_payin_type_is_a_different_rate(self):
        # Pitype picks the premium base (cp premium#), so On OD 20 != On Net 20.
        self.assertFalse(_share_one_payout(
            _row(),
            _row(pi_type="On OD", pi_od_rate=20.0, pi_net_rate=0.0),
        ))

    def test_a_blank_type_only_equals_another_blank_type(self):
        self.assertTrue(_share_one_payout(_row(po_type=None), _row(po_type=None)))
        self.assertFalse(_share_one_payout(_row(po_type=None), _row()))

    def test_the_rate_is_still_read_from_the_column_its_type_points_at(self):
        # Same type, but the number sits in a different column on each card.
        self.assertFalse(_share_one_payout(
            _row(po_type="On OD", po_od_rate=10.0, po_net_rate=0.0),
            _row(po_type="On OD", po_od_rate=0.0, po_net_rate=10.0),
        ))

    def test_only_the_rate_the_type_reads_matters(self):
        # An 'On Net' card ignores its OD/TP columns, so stray values there don't
        # make two otherwise identical cards different.
        self.assertTrue(_share_one_payout(
            _row(po_od_rate=0.0, po_tp_rate=0.0),
            _row(po_od_rate=None, po_tp_rate=3.0),
        ))

    def test_blank_and_zero_net_rate_are_different(self):
        # A blank rate leaves Pay-out Amt blank, 0 makes it 0.
        self.assertFalse(_share_one_payout(_row(po_net_rate=None), _row(po_net_rate=0.0)))

    def test_od_plus_tp_compares_the_pair_not_the_sum(self):
        od_tp = {"po_type": "On OD and TP", "po_net_rate": None}
        # 10+5 and 5+10 consolidate to the same 15 but pay out differently
        # (OD% of Total OD + TP% of TP Premium).
        self.assertFalse(_share_one_payout(
            _row(**od_tp, po_od_rate=10.0, po_tp_rate=5.0),
            _row(**od_tp, po_od_rate=5.0, po_tp_rate=10.0),
        ))
        self.assertTrue(_share_one_payout(
            _row(**od_tp, po_od_rate=10.0, po_tp_rate=5.0),
            _row(**od_tp, po_od_rate=10.0, po_tp_rate=5.0),
        ))

    def test_od_plus_tp_treats_a_missing_leg_as_zero(self):
        od_tp = {"po_type": "On OD and TP", "po_net_rate": None}
        self.assertTrue(_share_one_payout(
            _row(**od_tp, po_od_rate=10.0, po_tp_rate=None),
            _row(**od_tp, po_od_rate=10.0, po_tp_rate=0.0),
        ))

    def test_signature_is_hashable_and_stable(self):
        self.assertEqual(payout_signature(pd.Series(_row())), payout_signature(pd.Series(_row())))


class IdenticalPayoutMappingTests(TestCase):
    """End to end: a policy matching several groups with the same payout is mapped, not skipped."""

    def setUp(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)
        # See test_mapping_engine_routing: process_mis_mapping closes the DB
        # connection, which would poison TestCase's wrapping transaction.
        patcher = mock.patch("insurance.mapping_engine.connection")
        patcher.start()
        self.addCleanup(patcher.stop)
        self._group_seq = 0

    def _rate(self, grouped=True, **overrides):
        # Wildcard everything else, so every card matches the same policy.
        fields = dict(
            insurance_company=INSURER, status="ACTIVE", is_deleted="NO",
            po_type="On Net", po_net_rate=10, pi_type="On Net", pi_net_rate=20,
            po_od_rate=0, po_tp_rate=0, pi_od_rate=0, pi_tp_rate=0,
        )
        fields.update(overrides)
        if grouped:
            self._group_seq += 1
            fields["group"] = RateGroup.objects.create(key_hash=f"hash-{self._group_seq}")
        return RateMaster.objects.create(**fields)

    def _run(self, rows=None):
        base = {"Product": "motor", "Policy: insurance company": INSURER}
        buf = io.StringIO()
        pd.DataFrame([{**base, **r} for r in (rows or [{}])]).to_csv(buf, index=False)
        mis = MISFile.objects.create(
            uploaded_file=SimpleUploadedFile("mis.csv", buf.getvalue().encode("utf-8"))
        )
        process_mis_mapping(mis.id)
        mis.refresh_from_db()
        self.assertEqual(mis.status, "COMPLETED", mis.error_message)
        with mis.processed_file.open("rb") as f:
            self.output = pd.read_csv(f)
        self.mis = mis
        return self.output

    def test_groups_with_identical_payout_map_to_the_lowest_group_id(self):
        first = self._rate()
        second = self._rate()
        third = self._rate()
        out = self._run()
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "Displaygroupid"], first.group_id)
        self.assertLess(first.group_id, second.group_id)
        self.assertLess(second.group_id, third.group_id)
        self.assertEqual(out.loc[0, "Porate"], 10)
        self.assertEqual(out.loc[0, "Pirate"], 20)

    def test_the_standard_payout_calculation_runs_on_the_resolved_row(self):
        self._rate()
        self._rate()
        out = self._run([{"Policy: gwp net premium": 1000}])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "cp premium#"], 1000)
        self.assertEqual(out.loc[0, "Pay-out Amt"], 100)
        self.assertEqual(out.loc[0, "Pay-in Amt"], 200)
        self.assertEqual(out.loc[0, "Margin Amt"], 100)

    def test_the_pick_does_not_depend_on_row_creation_order(self):
        # Save the higher group id's row first (so it has the lower row id and
        # sorts first in the DB); the lower group id still wins.
        lower_group = RateGroup.objects.create(key_hash="hash-lower")
        higher_group = RateGroup.objects.create(key_hash="hash-higher")
        self.assertLess(lower_group.id, higher_group.id)
        self._rate(grouped=False, group=higher_group)
        self._rate(grouped=False, group=lower_group)
        out = self._run()
        self.assertEqual(out.loc[0, "Displaygroupid"], lower_group.id)

    def test_groups_that_differ_only_in_non_rate_fields_still_collapse(self):
        # Add T&C, flat amount and remarks are not rates; RULE 1-6 already picked these.
        first = self._rate(add_tnc="Excludes bolero", po_flat_amount=100, remarks="a")
        self._rate(add_tnc="Excludes scorpio", po_flat_amount=250, remarks="b")
        out = self._run()
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "Displaygroupid"], first.group_id)
        self.assertEqual(out.loc[0, "Porate"], 10)

    def test_groups_with_the_same_number_but_a_different_rate_type_are_still_skipped(self):
        self._rate(pi_type="On Net", pi_net_rate=20, pi_od_rate=0)
        self._rate(pi_type="On OD", pi_net_rate=0, pi_od_rate=20)
        self.assertEqual(self._run().loc[0, "Mapping Status"], MULTIPLE)

    def test_the_failure_reason_records_that_groups_were_collapsed(self):
        self._rate()
        self._rate()
        reason = self._run().loc[0, "Failure Reason"]
        self.assertTrue(reason.startswith("Matched Successfully"))
        self.assertIn("2 Rate Master groups matched with identical payout and payin rates", reason)

    def test_an_ordinary_single_match_keeps_the_plain_reason(self):
        self._rate()
        self.assertEqual(self._run().loc[0, "Failure Reason"], "Matched Successfully")

    def test_the_run_summary_counts_the_collapsed_rows(self):
        self._rate()
        self._rate()
        self._run([{}, {}])
        self.assertIn("Mapped 2 rates.", self.mis.error_message)
        self.assertIn("2 of them matched several Rate Master groups/rows with identical payout and payin rates",
                      self.mis.error_message)
        self.assertIn("0 rows skipped", self.mis.error_message)

    def test_the_summary_stays_quiet_when_nothing_was_collapsed(self):
        self._rate()
        self._run()
        self.assertNotIn("identical payout", self.mis.error_message)

    def test_groups_with_a_different_payout_rate_are_still_skipped(self):
        self._rate(po_net_rate=10)
        self._rate(po_net_rate=12)
        out = self._run()
        self.assertEqual(out.loc[0, "Mapping Status"], MULTIPLE)
        self.assertIn("Multiple Rate Master groups matched (2 groups", out.loc[0, "Failure Reason"])

    def test_groups_with_the_same_payout_but_a_different_payin_are_still_skipped(self):
        self._rate(pi_net_rate=20)
        self._rate(pi_net_rate=25)
        self.assertEqual(self._run().loc[0, "Mapping Status"], MULTIPLE)

    def test_one_odd_group_out_keeps_the_whole_set_ambiguous(self):
        self._rate()
        self._rate()
        self._rate(po_net_rate=15)
        self.assertEqual(self._run().loc[0, "Mapping Status"], MULTIPLE)

    def test_od_plus_tp_groups_with_swapped_legs_are_still_skipped(self):
        od_tp = dict(po_type="On OD and TP", po_net_rate=None, pi_type="On Net")
        self._rate(**od_tp, po_od_rate=10, po_tp_rate=5)
        self._rate(**od_tp, po_od_rate=5, po_tp_rate=10)
        self.assertEqual(self._run().loc[0, "Mapping Status"], MULTIPLE)

    def test_od_plus_tp_groups_with_equal_legs_collapse_and_split_the_amount(self):
        od_tp = dict(po_type="On OD and TP", po_net_rate=None, po_od_rate=10, po_tp_rate=5)
        self._rate(**od_tp)
        self._rate(**od_tp)
        out = self._run([{"Policy: total od": 1000, "Policy: tp premium": 2000}])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "Pay-out Amt"], 1000 * 0.10 + 2000 * 0.05)

    def test_standalone_rows_without_a_group_collapse_to_the_lowest_row_id(self):
        first = self._rate(grouped=False)
        self._rate(grouped=False)
        out = self._run()
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "Displaygroupid"], first.id)

    def test_a_group_spanning_several_rows_is_still_one_group(self):
        group = RateGroup.objects.create(key_hash="hash-span")
        self._rate(grouped=False, group=group)
        self._rate(grouped=False, group=group)
        out = self._run()
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        # Not a collapse of distinct groups: plain reason, no summary note.
        self.assertEqual(out.loc[0, "Failure Reason"], "Matched Successfully")
        self.assertNotIn("identical payout", self.mis.error_message)


class HealthSharedRateTests(SimpleTestCase):
    """Health: the matched rows' payout rate (the tenure column in use) and payin_rate decide it."""

    def _share(self, *rows, rate_col="one_year_rate"):
        base = {"one_year_rate": 5.0, "multi_year_2_rate": 6.0, "payin_rate": 8.0}
        return health_matched_rows_share_one_rate(
            pd.DataFrame([{**base, **r} for r in rows]), rate_col
        )

    def test_identical_rates_share_one_rate(self):
        self.assertTrue(self._share({}, {}, {}))

    def test_a_different_payout_rate_does_not(self):
        self.assertFalse(self._share({}, {"one_year_rate": 5.5}))

    def test_a_different_payin_rate_does_not(self):
        self.assertFalse(self._share({}, {"payin_rate": 9.0}))

    def test_only_the_tenure_column_in_use_is_compared(self):
        # 1-year policy: the 2-year column is irrelevant...
        self.assertTrue(self._share({}, {"multi_year_2_rate": 99.0}))
        # ...but is the payout rate for a 2-year policy.
        self.assertFalse(self._share({}, {"multi_year_2_rate": 99.0}, rate_col="multi_year_2_rate"))

    def test_blank_and_zero_are_different(self):
        self.assertFalse(self._share({"one_year_rate": None}, {"one_year_rate": 0.0}))
        self.assertTrue(self._share({"one_year_rate": None}, {"one_year_rate": None}))


class HealthIdenticalRateMappingTests(TestCase):
    """End to end: a health policy matching several Health Rate Master rows with the same rates is mapped."""

    def setUp(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)
        patcher = mock.patch("insurance.mapping_engine.connection")
        patcher.start()
        self.addCleanup(patcher.stop)
        # A file whose only matches are Health rows trips an unrelated pandas
        # bug in the Pay-out/Pay-in split step (see test_mapping_engine_routing),
        # so every run leads with one plain Motor match.
        RateMaster.objects.create(
            insurance_company=INSURER, status="ACTIVE", is_deleted="NO",
            po_type="On Net", po_net_rate=10, pi_type="On Net", pi_net_rate=20,
            po_od_rate=0, po_tp_rate=0, pi_od_rate=0, pi_tp_rate=0,
        )

    def _health_rate(self, **overrides):
        # Wildcard everything else, so every row matches the same policy.
        fields = dict(
            insurance_company=HEALTH_INSURER, status="ACTIVE", is_deleted="NO",
            one_year_rate=5, payin_rate=8,
        )
        fields.update(overrides)
        return HealthRateMaster.objects.create(**fields)

    def _run(self, health_rows=1):
        motor = {"Product": "motor", "Policy: insurance company": INSURER}
        health = {
            "Product": "health", "Policy: insurance company": HEALTH_INSURER,
            # dd/mm/yyyy: the engine parses dates day-first.
            "Policy: inception date": "01/08/2026", "Policy: expiry date": "31/07/2027",
        }
        buf = io.StringIO()
        pd.DataFrame([motor] + [health] * health_rows).to_csv(buf, index=False)
        mis = MISFile.objects.create(
            uploaded_file=SimpleUploadedFile("mis.csv", buf.getvalue().encode("utf-8"))
        )
        process_mis_mapping(mis.id)
        mis.refresh_from_db()
        self.assertEqual(mis.status, "COMPLETED", mis.error_message)
        self.mis = mis
        with mis.processed_file.open("rb") as f:
            return pd.read_csv(f)

    def test_rows_with_identical_rates_map_to_the_lowest_id(self):
        first = self._health_rate()
        self._health_rate()
        self._health_rate()
        out = self._run()
        self.assertEqual(out.loc[1, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[1, "Displaygroupid"], first.id)
        self.assertEqual(out.loc[1, "Porate"], 5)
        self.assertEqual(out.loc[1, "Pirate"], 8)

    def test_the_failure_reason_records_that_rows_were_collapsed(self):
        self._health_rate()
        self._health_rate()
        reason = self._run().loc[1, "Failure Reason"]
        self.assertTrue(reason.startswith("Matched Successfully"))
        self.assertIn("2 Health Rate Master rows matched with identical payout and payin rates", reason)

    def test_an_ordinary_single_health_match_keeps_the_plain_reason(self):
        self._health_rate()
        self.assertEqual(self._run().loc[1, "Failure Reason"], "Matched Successfully")
        self.assertNotIn("identical payout", self.mis.error_message)

    def test_rows_with_a_different_payout_rate_are_still_skipped(self):
        self._health_rate(one_year_rate=5)
        self._health_rate(one_year_rate=6)
        out = self._run()
        self.assertEqual(out.loc[1, "Mapping Status"], MULTIPLE)
        self.assertIn("Multiple Health Rate Master rows matched (2 rows)", out.loc[1, "Failure Reason"])

    def test_rows_with_a_different_payin_rate_are_still_skipped(self):
        self._health_rate(payin_rate=8)
        self._health_rate(payin_rate=9)
        self.assertEqual(self._run().loc[1, "Mapping Status"], MULTIPLE)

    def test_one_odd_row_out_keeps_the_whole_set_ambiguous(self):
        self._health_rate()
        self._health_rate()
        self._health_rate(one_year_rate=7)
        self.assertEqual(self._run().loc[1, "Mapping Status"], MULTIPLE)

    def test_rows_that_differ_only_in_fields_other_than_the_rates_still_collapse(self):
        first = self._health_rate(remarks="a", multi_year_2_rate=6)
        self._health_rate(remarks="b", multi_year_2_rate=60)
        out = self._run()
        self.assertEqual(out.loc[1, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[1, "Displaygroupid"], first.id)

    def test_the_run_summary_counts_health_rows_alongside_motor(self):
        self._health_rate()
        self._health_rate()
        self._run(health_rows=3)
        self.assertIn("Mapped 4 rates.", self.mis.error_message)
        self.assertIn("3 of them matched several Rate Master groups/rows with identical payout and payin rates",
                      self.mis.error_message)
        self.assertIn("0 rows skipped", self.mis.error_message)
