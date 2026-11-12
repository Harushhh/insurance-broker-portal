import datetime
import io
import tempfile
from unittest import mock

import pandas as pd
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from insurance.mapping_engine import (
    VEHICLE_AGE_NEW_VEHICLE_LABEL,
    VEHICLE_AGE_REGISTRATION_LABEL,
    build_coverage_gap_summary,
    calculate_vehicle_age,
    process_mis_mapping,
)
from insurance.models import MISFile, RateMaster

INSURER = "Acme General Insurance"
MATCH = "✅ MATCH"
NO_MATCH = "❌ NO MATCH"
MULTIPLE = "⚠️ MULTIPLE MATCHES"


# The year the end-to-end tests below pretend the mapping run happens in, so
# they keep passing after the real clock rolls over to the next year.
RUN_YEAR = 2026


def _age(registration, inception=None, is_new=None, current_year=RUN_YEAR):
    """calculate_vehicle_age over plain lists; every row is a standard policy unless is_new says otherwise."""
    n = len(registration)
    return calculate_vehicle_age(
        pd.Series(registration),
        pd.Series(inception if inception is not None else [None] * n),
        pd.Series(is_new if is_new is not None else [False] * n),
        current_year,
    ).tolist()


class CalculateVehicleAgeTests(SimpleTestCase):
    """Calculated_Age = (CURRENT_YEAR + 1) - Year; Year is the registration year, or the inception year for a new vehicle."""

    def test_current_year_plus_one_minus_registration_year(self):
        self.assertEqual(_age([2026, 2025, 2024, 2020]), [1, 2, 3, 7])

    def test_registered_in_2025_and_run_in_2026_is_age_2(self):
        # The worked example: (2026 + 1) - 2025 = 2.
        self.assertEqual(_age(["14-06-2025"], current_year=2026), [2])

    def test_current_year_moves_on_with_the_clock(self):
        self.assertEqual(_age([2026, 2025], current_year=2026), [1, 2])
        self.assertEqual(_age([2026, 2025], current_year=2027), [2, 3])

    def test_registration_year_is_read_out_of_a_dd_mm_yyyy_date(self):
        self.assertEqual(_age(["20-12-2025", "01-01-2024", "31-12-2019"]), [2, 3, 8])

    def test_registration_year_is_read_out_of_any_other_common_cell_format(self):
        ages = _age(["15/03/2025", "2025-03-15 00:00:00", "2025", "2025.0", " 15-03-2025 "])
        self.assertEqual(ages, [2, 2, 2, 2, 2])

    def test_a_new_vehicle_takes_its_year_from_the_inception_date(self):
        ages = _age(
            ["15-03-2018", "15-03-2018"],
            inception=["01-08-2026", "01-08-2025"],
            is_new=[True, True],
        )
        self.assertEqual(ages, [1, 2])

    def test_a_standard_policy_ignores_the_inception_date(self):
        self.assertEqual(_age(["15-03-2025"], inception=["01-08-2026"], is_new=[False]), [2])

    def test_each_row_uses_its_own_source_column(self):
        ages = _age(
            ["15-03-2025", "15-03-2020"],
            inception=["01-08-2026", "01-08-2026"],
            is_new=[False, True],
        )
        self.assertEqual(ages, [2, 1])

    def test_blank_or_unparseable_year_gives_nan(self):
        ages = _age([None, "", "n/a", "12345", "999"])
        self.assertTrue(all(pd.isna(a) for a in ages))

    def test_there_is_no_fallback_between_the_two_date_columns(self):
        # A standard policy with no registration date is not aged from its
        # inception date, and a new vehicle with no inception date is not aged
        # from its registration date.
        standard = _age([None], inception=["01-08-2026"], is_new=[False])
        new_vehicle = _age(["15-03-2025"], inception=[None], is_new=[True])
        self.assertTrue(pd.isna(standard[0]))
        self.assertTrue(pd.isna(new_vehicle[0]))

    def test_a_missing_column_gives_nan(self):
        # safe_get_col hands back an all-None series for an absent column.
        self.assertTrue(all(pd.isna(a) for a in _age([None, None])))

    def test_a_year_ahead_of_the_current_year_is_not_clamped(self):
        # (2026 + 1) - 2027 = 0, and anything further ahead goes negative.
        self.assertEqual(_age([2027, 2028]), [0, -1])


class AgeBandMatchingTests(TestCase):
    """RULE 3 reads the calculated age against (min, max]: lower bound exclusive, upper bound inclusive."""

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

    def _rate(self, age_min, age_max, payout):
        # Explicit zero OD/TP rates, as real Rate Master rows carry.
        return RateMaster.objects.create(
            insurance_company=INSURER, status="ACTIVE", is_deleted="NO",
            vehicle_age_min=age_min, vehicle_age_max=age_max,
            po_type="On Net", po_net_rate=payout, pi_type="On Net", pi_net_rate=payout + 10,
            po_od_rate=0, po_tp_rate=0, pi_od_rate=0, pi_tp_rate=0,
        )

    def _run(self, rows, run_year=RUN_YEAR):
        # Premium in both columns: the valid split for the 1+1 / 1+3 / 1+5 rows
        # below (see test_mapping_engine_premium_validation), so only the age
        # rule decides what these rows do.
        base = {"Product": "motor", "Policy: insurance company": INSURER,
                "Policy: inception date": "01/08/2026",
                "Policy: total od": 1000, "Policy: tp premium": 500}
        buf = io.StringIO()
        pd.DataFrame([{**base, **r} for r in rows]).to_csv(buf, index=False)
        mis = MISFile.objects.create(
            uploaded_file=SimpleUploadedFile("mis.csv", buf.getvalue().encode("utf-8"))
        )
        # Pin "now" so CURRENT_YEAR is run_year whatever the real date is.
        with mock.patch(
            "insurance.mapping_engine.timezone.localdate",
            return_value=datetime.date(run_year, 6, 1),
        ):
            process_mis_mapping(mis.id)
        mis.refresh_from_db()
        self.assertEqual(mis.status, "COMPLETED", mis.error_message)
        with mis.processed_file.open("rb") as f:
            return pd.read_csv(f)

    def test_the_current_year_always_falls_under_the_0_to_1_slab(self):
        zero_to_one = self._rate(0, 1, payout=10)
        one_to_five = self._rate(1, 5.01, payout=20)
        # Registered in the year of the run is age 1, whichever year that is...
        for run_year in (2026, 2027):
            out = self._run([{"Policy: date of registration": f"15-06-{run_year}"}], run_year=run_year)
            self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
            self.assertEqual(out.loc[0, "Displaygroupid"], zero_to_one.id)
        # ...and the year before that is age 2, the next slab up.
        out = self._run([{"Policy: date of registration": "15-06-2025"}], run_year=2026)
        self.assertEqual(out.loc[0, "Displaygroupid"], one_to_five.id)

    def test_consecutive_whole_number_bands_each_own_one_age(self):
        zero_to_one = self._rate(0, 1, payout=10)
        one_to_two = self._rate(1, 2, payout=20)
        two_to_three = self._rate(2, 3, payout=30)
        out = self._run([
            {"Policy: date of registration": "15-06-2026"},   # age 1
            {"Policy: date of registration": "15-06-2025"},   # age 2
            {"Policy: date of registration": "15-06-2024"},   # age 3
        ])
        self.assertEqual(list(out["Mapping Status"]), [MATCH, MATCH, MATCH])
        self.assertEqual(list(out["Displaygroupid"]), [zero_to_one.id, one_to_two.id, two_to_three.id])
        self.assertEqual(list(out["Porate"]), [10, 20, 30])

    def test_an_age_on_a_seam_belongs_to_the_band_below(self):
        zero_to_one = self._rate(0, 1, payout=10)
        one_to_five = self._rate(1, 5.01, payout=20)
        five_up = self._rate(5.01, 25.01, payout=30)
        out = self._run([
            {"Policy: date of registration": "15-06-2026"},   # age 1: on the 0-1 | 1-5.01 seam
            {"Policy: date of registration": "15-06-2025"},   # age 2
            {"Policy: date of registration": "15-06-2022"},   # age 5: top of 1-5.01
            {"Policy: date of registration": "15-06-2021"},   # age 6: first age of 5.01-25.01
        ])
        self.assertEqual(list(out["Mapping Status"]), [MATCH] * 4)
        self.assertEqual(
            list(out["Displaygroupid"]),
            [zero_to_one.id, one_to_five.id, one_to_five.id, five_up.id],
        )

    def test_x01_bands_keep_covering_the_whole_number_below_their_upper_bound(self):
        up_to_1 = self._rate(0, 1.01, payout=10)       # age 1
        from_2 = self._rate(1.01, 3.01, payout=20)      # ages 2 and 3
        out = self._run([
            {"Policy: date of registration": "15-06-2026"},   # age 1
            {"Policy: date of registration": "15-06-2025"},   # age 2
            {"Policy: date of registration": "15-06-2024"},   # age 3
        ])
        self.assertEqual(list(out["Mapping Status"]), [MATCH] * 3)
        self.assertEqual(list(out["Displaygroupid"]), [up_to_1.id, from_2.id, from_2.id])

    def test_an_age_above_the_last_band_is_not_covered(self):
        self._rate(0, 1, payout=10)
        out = self._run([{"Policy: date of registration": "15-06-2025"}])   # age 2
        self.assertEqual(out.loc[0, "Mapping Status"], NO_MATCH)
        reason = out.loc[0, "Failure Reason"]
        self.assertTrue(reason.startswith(f"Failed on: {VEHICLE_AGE_REGISTRATION_LABEL} —"))
        self.assertIn("Calculated vehicle age '2'", reason)
        self.assertIn("lower bound exclusive, upper bound inclusive", reason)

    def test_an_uncovered_age_shows_up_as_its_own_row_in_the_coverage_gaps(self):
        # The coverage-gap panel keys a failure on the first quoted value in its
        # detail, so the age has to be the first quoted thing in the message.
        self._rate(0, 1, payout=10)
        out = self._run([
            {"Policy: date of registration": "15-06-2025"},   # age 2
            {"Policy: date of registration": "15-06-2025"},   # age 2
            {"Policy: date of registration": "15-06-2024"},   # age 3
        ])
        gaps = build_coverage_gap_summary(out)["no_match"]
        self.assertEqual(
            gaps,
            [
                {"rule": VEHICLE_AGE_REGISTRATION_LABEL, "value": "2", "count": 2},
                {"rule": VEHICLE_AGE_REGISTRATION_LABEL, "value": "3", "count": 1},
            ],
        )

    def test_a_year_ahead_of_the_current_year_matches_no_band(self):
        # (2026 + 1) - 2027 = age 0, below the exclusive lower bound of 0.
        self._rate(0, 1, payout=10)
        self._rate(0, 99, payout=20)
        out = self._run([{"Policy: date of registration": "15-06-2027"}])
        self.assertEqual(out.loc[0, "Mapping Status"], NO_MATCH)
        self.assertIn("Calculated vehicle age '0'", out.loc[0, "Failure Reason"])

    def test_bands_that_genuinely_overlap_are_still_ambiguous(self):
        # Both bands cover age 2, so the seam rule does not - and must not - hide a real conflict.
        self._rate(0, 2.01, payout=10)
        self._rate(1, 5.01, payout=20)
        out = self._run([{"Policy: date of registration": "15-06-2025"}])   # age 2
        self.assertEqual(out.loc[0, "Mapping Status"], MULTIPLE)

    def test_the_mis_vehage_column_is_ignored(self):
        zero_to_one = self._rate(0, 1, payout=10)
        self._rate(6, 7, payout=20)
        out = self._run([{"Policy: date of registration": "15-06-2026", "Policy: vehage": 7}])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "Displaygroupid"], zero_to_one.id)

    def test_the_manufacturing_year_column_is_no_longer_read(self):
        self._rate(0, 1, payout=10)
        six_to_seven = self._rate(6, 7, payout=20)
        # Registered 2020 -> age 7, whatever the manufacturing year says.
        out = self._run([{"Policy: date of registration": "15-06-2020", "Policy: manufacturing year": 2026}])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "Displaygroupid"], six_to_seven.id)

    def test_current_year_is_the_year_of_the_run_and_rolls_over(self):
        zero_to_one = self._rate(0, 1, payout=10)
        one_to_two = self._rate(1, 2, payout=20)
        rows = [{"Policy: date of registration": "15-06-2026"}]
        # Mapped in 2026 a 2026 registration is age 1 ('0 to 1'); mapped in 2027 the same row is age 2 ('1 to 2').
        self.assertEqual(self._run(rows, run_year=2026).loc[0, "Displaygroupid"], zero_to_one.id)
        self.assertEqual(self._run(rows, run_year=2027).loc[0, "Displaygroupid"], one_to_two.id)

    def test_a_standard_policy_is_aged_from_registration_not_inception(self):
        zero_to_one = self._rate(0, 1, payout=10)
        one_to_two = self._rate(1, 2, payout=20)
        # Registered 2025, incepted 2026 (the base row's 01/08/2026): age 2 ('1 to 2'), not age 1.
        out = self._run([{"Policy: sub product": "1+1", "Policy: date of registration": "15-06-2025"}])
        self.assertEqual(out.loc[0, "Displaygroupid"], one_to_two.id)
        self.assertNotEqual(out.loc[0, "Displaygroupid"], zero_to_one.id)

    def test_new_vehicle_sub_products_are_aged_from_the_inception_date(self):
        zero_to_one = self._rate(0, 1, payout=10)
        one_to_two = self._rate(1, 2, payout=20)
        seven_to_eight = self._rate(7, 8, payout=30)
        # The 2018 registration date would be age 9 and is ignored for 1+3 / 1+5.
        out = self._run([
            {"Policy: sub product": "1+3", "Policy: date of registration": "15-06-2018",
             "Policy: inception date": "01/08/2026"},   # (2026 + 1) - 2026 = 1
            {"Policy: sub product": "1+5", "Policy: date of registration": "15-06-2018",
             "Policy: inception date": "01/08/2025"},   # (2026 + 1) - 2025 = 2
            {"Policy: sub product": "1+1", "Policy: date of registration": "15-06-2019",
             "Policy: inception date": "01/08/2026"},   # standard: (2026 + 1) - 2019 = 8
        ])
        self.assertEqual(list(out["Mapping Status"]), [MATCH, MATCH, MATCH])
        self.assertEqual(
            list(out["Displaygroupid"]), [zero_to_one.id, one_to_two.id, seven_to_eight.id]
        )

    def test_new_vehicle_sub_product_is_recognised_ignoring_case_and_spaces(self):
        zero_to_one = self._rate(0, 1, payout=10)
        out = self._run([{"Policy: sub product": " 1+3 ", "Policy: date of registration": "15-06-2018"}])
        self.assertEqual(out.loc[0, "Displaygroupid"], zero_to_one.id)

    def test_a_new_vehicle_with_no_inception_date_is_flagged_not_guessed(self):
        self._rate(0, 1, payout=10)
        # The registration date is present, but a new vehicle is never aged from it.
        out = self._run([{"Policy: sub product": "1+3", "Policy: date of registration": "15-06-2026",
                          "Policy: inception date": None}])
        self.assertEqual(out.loc[0, "Mapping Status"], NO_MATCH)
        self.assertIn(
            f"Failed on: {VEHICLE_AGE_NEW_VEHICLE_LABEL} —", out.loc[0, "Failure Reason"]
        )
        self.assertIn(f"{VEHICLE_AGE_NEW_VEHICLE_LABEL} is blank/unparseable", out.loc[0, "Failure Reason"])

    def test_a_standard_policy_with_no_registration_date_only_matches_an_open_age_range(self):
        self._rate(0, 1, payout=10)
        # The inception date is present, but a standard policy is never aged from it.
        out = self._run([{"Policy: sub product": "1+1", "Policy: date of registration": None}])
        self.assertEqual(out.loc[0, "Mapping Status"], NO_MATCH)
        self.assertIn(
            f"{VEHICLE_AGE_REGISTRATION_LABEL} is blank/unparseable", out.loc[0, "Failure Reason"]
        )

        open_rate = self._rate(None, None, payout=99)
        out = self._run([{"Policy: sub product": "1+1", "Policy: date of registration": None}])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "Displaygroupid"], open_rate.id)

    def test_other_ranges_stay_inclusive_on_both_ends(self):
        # Seating capacity on either bound of a 4-8 band must still match: only
        # the AGE lower bound is exclusive.
        rate = RateMaster.objects.create(
            insurance_company=INSURER, status="ACTIVE", is_deleted="NO",
            sc_min=4, sc_max=8, po_type="On Net", po_net_rate=10, pi_type="On Net", pi_net_rate=20,
            po_od_rate=0, po_tp_rate=0, pi_od_rate=0, pi_tp_rate=0,
        )
        out = self._run([{"Policy: seating capacity": 4}, {"Policy: seating capacity": 8}])
        self.assertEqual(list(out["Mapping Status"]), [MATCH, MATCH])
        self.assertEqual(list(out["Displaygroupid"]), [rate.id, rate.id])
