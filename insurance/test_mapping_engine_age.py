import datetime
import io
import tempfile
from unittest import mock

import pandas as pd
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from insurance.mapping_engine import (
    VEHICLE_AGE_RULE_LABEL,
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


def _age(mfg_values, current_year=RUN_YEAR):
    return calculate_vehicle_age(pd.Series(mfg_values), current_year).tolist()


class CalculateVehicleAgeTests(SimpleTestCase):
    """Calculated_Age = CURRENT_YEAR - Manufacturing_Year."""

    def test_current_year_minus_manufacturing_year(self):
        self.assertEqual(_age([2026, 2025, 2024, 2020]), [0, 1, 2, 6])

    def test_current_year_moves_on_with_the_clock(self):
        self.assertEqual(_age([2026, 2025], current_year=2026), [0, 1])
        self.assertEqual(_age([2026, 2025], current_year=2027), [1, 2])

    def test_manufacturing_year_is_read_out_of_any_common_cell_format(self):
        ages = _age(["2025", "2025.0", "15/03/2024", "2023-03-15 00:00:00", " 2022 "])
        self.assertEqual(ages, [1, 1, 2, 3, 4])

    def test_blank_or_unparseable_manufacturing_year_gives_nan(self):
        ages = _age([None, "", "n/a", "12345", "999"])
        self.assertTrue(all(pd.isna(a) for a in ages))

    def test_a_missing_manufacturing_year_column_gives_nan(self):
        # safe_get_col hands back an all-None series for an absent column.
        self.assertTrue(all(pd.isna(a) for a in _age([None, None])))

    def test_a_future_manufacturing_year_is_not_clamped(self):
        self.assertEqual(_age([2027]), [-1])


class AgeBandMatchingTests(TestCase):
    """RULE 3 reads the calculated age against [min, max): upper bound exclusive."""

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
        base = {"Product": "motor", "Policy: insurance company": INSURER,
                "Policy: inception date": "01/08/2026"}
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

    def test_consecutive_whole_number_bands_no_longer_overlap(self):
        zero_to_one = self._rate(0, 1, payout=10)
        one_to_two = self._rate(1, 2, payout=20)
        two_to_three = self._rate(2, 3, payout=30)
        out = self._run([
            {"Policy: manufacturing year": 2026},   # age 0
            {"Policy: manufacturing year": 2025},   # age 1 - the old double match
            {"Policy: manufacturing year": 2024},   # age 2
        ])
        self.assertEqual(list(out["Mapping Status"]), [MATCH, MATCH, MATCH])
        self.assertEqual(list(out["Displaygroupid"]), [zero_to_one.id, one_to_two.id, two_to_three.id])
        self.assertEqual(list(out["Porate"]), [10, 20, 30])

    def test_x01_bands_keep_covering_the_whole_number_below_their_upper_bound(self):
        up_to_1 = self._rate(0, 1.01, payout=10)      # ages 0 and 1
        from_2 = self._rate(1.01, 3.01, payout=20)     # ages 2 and 3
        out = self._run([
            {"Policy: manufacturing year": 2026},
            {"Policy: manufacturing year": 2025},
            {"Policy: manufacturing year": 2024},
            {"Policy: manufacturing year": 2023},
        ])
        self.assertEqual(list(out["Mapping Status"]), [MATCH] * 4)
        self.assertEqual(
            list(out["Displaygroupid"]), [up_to_1.id, up_to_1.id, from_2.id, from_2.id]
        )

    def test_an_age_on_the_upper_bound_of_the_last_band_is_not_covered(self):
        self._rate(0, 1, payout=10)
        out = self._run([{"Policy: manufacturing year": 2025}])   # age 1
        self.assertEqual(out.loc[0, "Mapping Status"], NO_MATCH)
        reason = out.loc[0, "Failure Reason"]
        self.assertTrue(reason.startswith(f"Failed on: {VEHICLE_AGE_RULE_LABEL} —"))
        self.assertIn("Calculated vehicle age 1", reason)

    def test_bands_that_genuinely_overlap_are_still_ambiguous(self):
        # Both bands cover age 1 (x.01 upper bound vs a whole-number lower bound),
        # so half-open bounds do not - and must not - hide a real conflict.
        self._rate(0, 1.01, payout=10)
        self._rate(1, 5.01, payout=20)
        out = self._run([{"Policy: manufacturing year": 2025}])   # age 1
        self.assertEqual(out.loc[0, "Mapping Status"], MULTIPLE)

    def test_the_mis_vehage_column_is_ignored(self):
        zero_to_one = self._rate(0, 1, payout=10)
        out = self._run([{"Policy: manufacturing year": 2026, "Policy: vehage": 7}])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "Displaygroupid"], zero_to_one.id)

    def test_current_year_is_the_year_of_the_run_and_rolls_over(self):
        zero_to_one = self._rate(0, 1, payout=10)
        one_to_two = self._rate(1, 2, payout=20)
        rows = [{"Policy: manufacturing year": 2026}]
        # Mapped in 2026 a 2026 vehicle is age 0; mapped in 2027 the same row is age 1.
        self.assertEqual(self._run(rows, run_year=2026).loc[0, "Displaygroupid"], zero_to_one.id)
        self.assertEqual(self._run(rows, run_year=2027).loc[0, "Displaygroupid"], one_to_two.id)

    def test_inception_date_does_not_change_the_age(self):
        self._rate(0, 1, payout=10)
        one_to_two = self._rate(1, 2, payout=20)
        # A 2025 vehicle is age 1 in 2026 whatever the policy's own inception year.
        out = self._run([{"Policy: manufacturing year": 2025, "Policy: inception date": "20/12/2025"}])
        self.assertEqual(out.loc[0, "Displaygroupid"], one_to_two.id)

    def test_blank_manufacturing_year_only_matches_an_open_age_range(self):
        self._rate(0, 1, payout=10)
        out = self._run([{"Policy: manufacturing year": None}])
        self.assertEqual(out.loc[0, "Mapping Status"], NO_MATCH)
        self.assertIn(f"{VEHICLE_AGE_RULE_LABEL} is blank/unparseable", out.loc[0, "Failure Reason"])

        open_rate = self._rate(None, None, payout=99)
        out = self._run([{"Policy: manufacturing year": None}])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "Displaygroupid"], open_rate.id)

    def test_other_ranges_stay_inclusive_on_both_ends(self):
        # Seating capacity 4 sits exactly on the upper bound of a 0-4 band and
        # must still match: only the AGE rule became half-open.
        rate = RateMaster.objects.create(
            insurance_company=INSURER, status="ACTIVE", is_deleted="NO",
            sc_min=0, sc_max=4, po_type="On Net", po_net_rate=10, pi_type="On Net", pi_net_rate=20,
            po_od_rate=0, po_tp_rate=0, pi_od_rate=0, pi_tp_rate=0,
        )
        out = self._run([{"Policy: seating capacity": 4}])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "Displaygroupid"], rate.id)
