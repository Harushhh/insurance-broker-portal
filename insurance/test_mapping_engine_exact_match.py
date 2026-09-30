import io
import tempfile
from unittest import mock

import pandas as pd
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from insurance.mapping_engine import check_categorical_match, check_exact_match, process_mis_mapping
from insurance.models import FuelTypeMaster, MISFile, ProductMaster, RateMaster, SubProductMaster

INSURER = "Acme General Insurance"
MATCH = "✅ MATCH"
NO_MATCH = "❌ NO MATCH"


class CheckExactMatchTests(SimpleTestCase):
    """RULE 2's product / sub product / fuel check: exact and case-insensitive, nothing looser."""

    def test_equal_ignoring_case_and_surrounding_whitespace(self):
        self.assertTrue(check_exact_match("gcv 3w", "GCV 3W"))
        self.assertTrue(check_exact_match("GCV 3W", "gcv 3w"))
        self.assertTrue(check_exact_match("  gcv 3w ", "GCV 3W"))
        self.assertTrue(check_exact_match("private car", "Private Car"))
        self.assertTrue(check_exact_match("cng", "CNG"))
        self.assertTrue(check_exact_match("saod", "SAOD"))

    def test_sibling_products_no_longer_match(self):
        # Every pair the fuzzy scorer used to wave through at 83.3 (> 75).
        for mis, grid in [
            ("gcv 3w", "GCV 4W"), ("gcv 4w", "GCV 3W"),
            ("gcv 3w", "PCV 3W"), ("pcv 4w", "GCV 4W"),
            ("pcv 2w", "PCV 3W"), ("pcv 3w", "PCV 4W"),
        ]:
            with self.subTest(mis=mis, grid=grid):
                self.assertFalse(check_exact_match(mis, grid))

    def test_no_substring_or_abbreviation_matching(self):
        self.assertFalse(check_exact_match("pvt car", "Private Car"))
        self.assertFalse(check_exact_match("two wheeler", "TW"))
        self.assertFalse(check_exact_match("tw", "Two Wheeler"))
        self.assertFalse(check_exact_match("private car", "Private Car Package"))
        # sub product / fuel values that used to match by containment
        self.assertFalse(check_exact_match("petrol", "Petrol/CNG"))
        self.assertFalse(check_exact_match("cng", "Petrol/CNG"))
        self.assertFalse(check_exact_match("saod", "SAOD (Own Damage)"))

    def test_no_typo_tolerance(self):
        self.assertFalse(check_exact_match("privte car", "Private Car"))
        self.assertFalse(check_exact_match("petrl", "Petrol"))

    def test_blank_rate_card_value_is_still_a_wildcard(self):
        for blank in (None, "", "nan", float("nan")):
            with self.subTest(grid=blank):
                self.assertTrue(check_exact_match("gcv 3w", blank))

    def test_blank_mis_value_matches_nothing_but_a_wildcard(self):
        for blank in (None, "", "nan", float("nan")):
            with self.subTest(mis=blank):
                self.assertFalse(check_exact_match(blank, "GCV 3W"))
                self.assertTrue(check_exact_match(blank, ""))

    def test_the_shared_categorical_matcher_is_unchanged(self):
        # Health's product_name / policy_category / business_type still use it.
        self.assertTrue(check_categorical_match("gcv 3w", "gcv 4w"))
        self.assertTrue(check_categorical_match("pvt car", "private car"))


class ExactMatchMappingTests(TestCase):
    """End to end: a policy no longer collides with (or is saved by) a near-miss value."""

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

    def _rate(self, product=None, payout=10, sub_product=None, fuel=None):
        # Wildcard everything else, like a GCV 4W group with class 'NA'.
        return RateMaster.objects.create(
            insurance_company=INSURER, status="ACTIVE", is_deleted="NO",
            product=ProductMaster.objects.get_or_create(name=product)[0] if product else None,
            sub_product=SubProductMaster.objects.get_or_create(name=sub_product)[0] if sub_product else None,
            fuel_type=FuelTypeMaster.objects.get_or_create(name=fuel)[0] if fuel else None,
            po_type="On Net", po_net_rate=payout, pi_type="On Net", pi_net_rate=payout + 10,
            po_od_rate=0, po_tp_rate=0, pi_od_rate=0, pi_tp_rate=0,
        )

    def _run(self, rows):
        base = {"Product": "motor", "Policy: insurance company": INSURER}
        buf = io.StringIO()
        pd.DataFrame([{**base, **r} for r in rows]).to_csv(buf, index=False)
        mis = MISFile.objects.create(
            uploaded_file=SimpleUploadedFile("mis.csv", buf.getvalue().encode("utf-8"))
        )
        process_mis_mapping(mis.id)
        mis.refresh_from_db()
        self.assertEqual(mis.status, "COMPLETED", mis.error_message)
        with mis.processed_file.open("rb") as f:
            return pd.read_csv(f)

    # ---- product ----------------------------------------------------------

    def test_a_gcv_3w_policy_maps_to_the_3w_group_only(self):
        gcv_3w = self._rate("GCV 3W", payout=43)
        self._rate("GCV 4W", payout=53)
        out = self._run([{"Policy: vehproduct": "GCV 3W"}])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "Displaygroupid"], gcv_3w.id)
        self.assertEqual(out.loc[0, "Porate"], 43)

    def test_a_gcv_4w_policy_maps_to_the_4w_group_only(self):
        self._rate("GCV 3W", payout=43)
        gcv_4w = self._rate("GCV 4W", payout=53)
        out = self._run([{"Policy: vehproduct": "GCV 4W"}])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "Displaygroupid"], gcv_4w.id)

    def test_every_sibling_product_is_kept_apart(self):
        rates = {p: self._rate(p, payout=10 + i)
                 for i, p in enumerate(["GCV 3W", "GCV 4W", "PCV 2W", "PCV 3W", "PCV 4W"])}
        out = self._run([{"Policy: vehproduct": p} for p in rates])
        self.assertEqual(list(out["Mapping Status"]), [MATCH] * 5)
        self.assertEqual(list(out["Displaygroupid"]), [r.id for r in rates.values()])

    def test_product_case_does_not_matter(self):
        gcv_3w = self._rate("GCV 3W", payout=43)
        out = self._run([{"Policy: vehproduct": "gcv 3w"}, {"Policy: vehproduct": "  Gcv 3W "}])
        self.assertEqual(list(out["Displaygroupid"]), [gcv_3w.id, gcv_3w.id])

    def test_an_abbreviated_product_no_longer_matches(self):
        self._rate("TW", payout=10)
        out = self._run([{"Policy: vehproduct": "Two Wheeler"}])
        self.assertEqual(out.loc[0, "Mapping Status"], NO_MATCH)
        reason = out.loc[0, "Failure Reason"]
        self.assertTrue(reason.startswith("Failed on: Policy: vehproduct —"))
        self.assertIn("'two wheeler'", reason)

    def test_a_blank_rate_card_product_still_matches_any_product(self):
        wildcard = self._rate(None, payout=10)
        out = self._run([{"Policy: vehproduct": "GCV 3W"}])
        self.assertEqual(out.loc[0, "Displaygroupid"], wildcard.id)

    # ---- sub product (RULE 3) --------------------------------------------

    def test_sub_product_is_exact_and_case_insensitive(self):
        saod = self._rate("TW", payout=10, sub_product="SAOD")
        out = self._run([{"Policy: vehproduct": "TW", "Policy: sub product": "saod"}])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "Displaygroupid"], saod.id)

    def test_a_longer_sub_product_no_longer_matches_by_containment(self):
        self._rate("TW", payout=10, sub_product="SAOD (Own Damage)")
        out = self._run([{"Policy: vehproduct": "TW", "Policy: sub product": "SAOD"}])
        self.assertEqual(out.loc[0, "Mapping Status"], NO_MATCH)
        self.assertTrue(out.loc[0, "Failure Reason"].startswith("Failed on: Policy: sub product —"))

    def test_a_near_miss_sub_product_group_is_not_a_second_match(self):
        exact = self._rate("TW", payout=10, sub_product="SAOD")
        self._rate("TW", payout=20, sub_product="SAOD (Own Damage)")
        out = self._run([{"Policy: vehproduct": "TW", "Policy: sub product": "SAOD"}])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "Displaygroupid"], exact.id)

    # ---- fuel (RULE 4) ----------------------------------------------------

    def test_fuel_is_exact_and_case_insensitive(self):
        cng = self._rate("GCV 3W", payout=10, fuel="CNG")
        out = self._run([{"Policy: vehproduct": "GCV 3W", "Policy: fuel": "cng"}])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "Displaygroupid"], cng.id)

    def test_a_dual_fuel_rate_row_no_longer_matches_a_single_fuel_policy(self):
        self._rate("GCV 3W", payout=10, fuel="Petrol/CNG")
        out = self._run([{"Policy: vehproduct": "GCV 3W", "Policy: fuel": "Petrol"}])
        self.assertEqual(out.loc[0, "Mapping Status"], NO_MATCH)
        self.assertTrue(out.loc[0, "Failure Reason"].startswith("Failed on: Policy: fuel —"))

    def test_a_near_miss_fuel_group_is_not_a_second_match(self):
        petrol = self._rate("GCV 3W", payout=10, fuel="Petrol")
        self._rate("GCV 3W", payout=20, fuel="Petrol/CNG")
        out = self._run([{"Policy: vehproduct": "GCV 3W", "Policy: fuel": "Petrol"}])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "Displaygroupid"], petrol.id)

    def test_a_blank_rate_card_fuel_or_sub_product_is_still_a_wildcard(self):
        wildcard = self._rate("GCV 3W", payout=10)
        out = self._run([{"Policy: vehproduct": "GCV 3W", "Policy: fuel": "Diesel", "Policy: sub product": "1+1"}])
        self.assertEqual(out.loc[0, "Displaygroupid"], wildcard.id)
