import io
import tempfile
from unittest import mock

import pandas as pd
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from insurance.mapping_engine import process_mis_mapping
from insurance.models import HealthRateMaster, MISFile, RateMaster

MOTOR_INSURER = "Acme General Insurance"       # exists ONLY in RateMaster
HEALTH_INSURER = "Zenith Health Insurance"     # exists ONLY in HealthRateMaster

MATCH = "✅ MATCH"
NO_MATCH = "❌ NO MATCH"
BAD_DATA = "FAILED - BAD DATA"


def _motor(insurer=MOTOR_INSURER, product="motor", **extra):
    row = {"Product": product, "Policy: insurance company": insurer}
    row.update(extra)
    return row


def _health(insurer=HEALTH_INSURER, product="health", **extra):
    row = {
        "Product": product,
        "Policy: insurance company": insurer,
        # dd/mm/yyyy: the engine parses dates day-first, so ISO strings would swap day/month.
        "Policy: inception date": "01/08/2026",
        "Policy: expiry date": "31/07/2027",
    }
    row.update(extra)
    return row


class ProductRoutingTests(TestCase):
    """
    The Product column is the first thing process_mis_mapping checks on every
    row, and it alone picks the table: 'motor' -> RateMaster only, 'health' ->
    HealthRateMaster only, anything else -> neither.
    """

    def setUp(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)

        # process_mis_mapping closes the DB connection on entry/exit (it's built
        # for a Celery worker thread); inside TestCase's transaction that would
        # poison every later query, so neutralise it here.
        patcher = mock.patch("insurance.mapping_engine.connection")
        patcher.start()
        self.addCleanup(patcher.stop)

        # A wildcard row per table: every rule blank/NA, so a row that reaches
        # the right table and shares its insurer MATCHES.
        self.rate = RateMaster.objects.create(
            insurance_company=MOTOR_INSURER, status="ACTIVE", is_deleted="NO",
            po_type="On Net", po_net_rate=12.5, pi_type="On Net", pi_net_rate=20,
            po_od_rate=0, po_tp_rate=0, pi_od_rate=0, pi_tp_rate=0,
        )
        self.health_rate = HealthRateMaster.objects.create(
            insurance_company=HEALTH_INSURER, status="ACTIVE", is_deleted="NO",
            one_year_rate=5, payin_rate=8,
        )

    def _run(self, rows):
        buf = io.StringIO()
        pd.DataFrame(rows).to_csv(buf, index=False)
        mis = MISFile.objects.create(
            uploaded_file=SimpleUploadedFile("mis.csv", buf.getvalue().encode("utf-8"))
        )
        process_mis_mapping(mis.id)
        mis.refresh_from_db()
        self.assertEqual(mis.status, "COMPLETED", mis.error_message)
        with mis.processed_file.open("rb") as f:
            return pd.read_csv(f)

    # ---- each product reaches its own table -------------------------------

    def test_motor_row_matches_against_rate_master(self):
        out = self._run([_motor()])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[0, "Displaygroupid"], self.rate.id)
        self.assertEqual(out.loc[0, "Porate"], 12.5)

    # NOTE: the health-match tests below put a matched Motor row ahead of the
    # Health row on purpose. A file whose ONLY matches are Health rows currently
    # crashes in process_mis_mapping's Pay-out/Pay-in split step under the pinned
    # pandas 3.0.0 (an empty object-dtype assignment into a float64 column), which
    # is unrelated to routing - so the routing behaviour is exercised in a mixed file.
    def test_health_row_matches_against_health_rate_master(self):
        out = self._run([_motor(), _health()])
        self.assertEqual(out.loc[1, "Mapping Status"], MATCH)
        self.assertEqual(out.loc[1, "Displaygroupid"], self.health_rate.id)
        self.assertEqual(out.loc[1, "Porate"], 5)

    def test_mixed_file_routes_every_row_by_its_own_product(self):
        out = self._run([_motor(), _health(), _motor(product="life")])
        self.assertEqual(list(out["Mapping Status"]), [MATCH, MATCH, NO_MATCH])

    # ---- the other table is never consulted -------------------------------

    def test_health_row_is_never_compared_to_rate_master(self):
        # The insurer exists in RateMaster (with a wildcard row) but not in
        # HealthRateMaster: routed correctly it must fail on the HEALTH table.
        out = self._run([_health(insurer=MOTOR_INSURER)])
        self.assertEqual(out.loc[0, "Mapping Status"], NO_MATCH)
        self.assertIn("active Health Rate Master insurer", out.loc[0, "Failure Reason"])

    def test_motor_row_is_never_compared_to_health_rate_master(self):
        out = self._run([_motor(insurer=HEALTH_INSURER)])
        self.assertEqual(out.loc[0, "Mapping Status"], NO_MATCH)
        reason = out.loc[0, "Failure Reason"]
        self.assertIn("active Rate Master insurer", reason)
        self.assertNotIn("Health Rate Master", reason)

    def test_product_is_case_and_whitespace_insensitive(self):
        out = self._run([_motor(product="  Motor "), _health(product=" HEALTH  ")])
        self.assertEqual(list(out["Mapping Status"]), [MATCH, MATCH])

    # ---- neither table for an unrecognised product ------------------------

    def test_unrecognised_products_are_rejected_before_any_matching(self):
        # Every one of these carries an insurer that WOULD match the motor
        # wildcard row, so the old fall-through-to-motor behaviour matched them.
        rows = [_motor(product="life"), _motor(product="non_motor"), _motor(product="")]
        out = self._run(rows)
        for i in range(len(rows)):
            self.assertEqual(out.loc[i, "Mapping Status"], NO_MATCH)
            self.assertTrue(out.loc[i, "Failure Reason"].startswith("Failed on: Product —"))
            self.assertTrue(pd.isna(out.loc[i, "Displaygroupid"]))
        self.assertIn("'life'", out.loc[0, "Failure Reason"])
        self.assertIn("'non_motor'", out.loc[1, "Failure Reason"])
        self.assertIn("'(blank)'", out.loc[2, "Failure Reason"])

    def test_missing_product_column_is_rejected(self):
        out = self._run([{"Policy: insurance company": MOTOR_INSURER}])
        self.assertEqual(out.loc[0, "Mapping Status"], NO_MATCH)
        self.assertTrue(out.loc[0, "Failure Reason"].startswith("Failed on: Product —"))

    # ---- the BAD DATA guard is motor-only, and runs after routing ---------

    def test_combined_cc_gvw_is_still_flagged_on_motor_rows(self):
        out = self._run([_motor(**{"Policy: cc cubic capacity": "6702/47500"})])
        self.assertEqual(out.loc[0, "Mapping Status"], BAD_DATA)

    def test_combined_cc_gvw_does_not_block_a_health_row(self):
        out = self._run([_motor(), _health(**{"Policy: cc cubic capacity": "6702/47500"})])
        self.assertEqual(out.loc[1, "Mapping Status"], MATCH)

    def test_combined_cc_gvw_does_not_mask_an_unrecognised_product(self):
        out = self._run([_motor(product="life", **{"Policy: cc cubic capacity": "6702/47500"})])
        self.assertEqual(out.loc[0, "Mapping Status"], NO_MATCH)
        self.assertTrue(out.loc[0, "Failure Reason"].startswith("Failed on: Product —"))
