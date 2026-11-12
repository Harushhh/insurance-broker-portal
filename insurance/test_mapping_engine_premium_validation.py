"""
Premium split / NCB validation on Motor MIS rows (mapping_engine.premium_validation_errors).

Keyed on 'Policy: sub product':

    STP              premium in 'Policy: tp premium' ONLY      (total od zero / blank)
    SAOD             premium in 'Policy: total od' ONLY        (tp premium zero / blank)
    1+1 / 1+3 / 1+5  premium in BOTH total od and tp premium — except Private Car
                     ('Policy: vehproduct'), where total od alone is enough
    1+3 / 1+5        'Policy: no claim bonus' zero / blank, on top of the above

A row that breaks one is rejected as FAILED - BAD DATA before any rate lookup.
"""
import io
import tempfile
from unittest import mock

import pandas as pd
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from insurance.mapping_engine import (
    VALIDATION_ERROR_PREFIX,
    premium_validation_errors,
    process_mis_mapping,
)
from insurance.models import HealthRateMaster, MISFailedRow, MISFile, RateMaster

INSURER = "Acme General Insurance"             # exists ONLY in RateMaster
HEALTH_INSURER = "Zenith Health Insurance"     # exists ONLY in HealthRateMaster

MATCH = "✅ MATCH"
NO_MATCH = "❌ NO MATCH"
BAD_DATA = "FAILED - BAD DATA"

PACKAGE_SUB_PRODUCTS = ("1+1", "1+3", "1+5")
BLANK_VARIANTS = (None, "", " ", 0, 0.0, "0", "0.00", "null", "NULL", "NA", "nill", "nul")


# ── unit: premium_validation_errors over plain values ───────────────────────

def _policy(sub, od=None, tp=None, ncb=None, vehicle="TW"):
    return {"sub": sub, "vehicle": vehicle, "od": od, "tp": tp, "ncb": ncb}


def _errors(*policies):
    """One reason per policy: '' = passes, anything else is the whole failure reason."""
    df = pd.DataFrame(list(policies), columns=["sub", "vehicle", "od", "tp", "ncb"])
    return premium_validation_errors(df["sub"], df["vehicle"], df["od"], df["tp"], df["ncb"]).tolist()


def _error(policy):
    [reason] = _errors(policy)
    return reason


class StpRuleTests(SimpleTestCase):
    """STP: premium in tp premium ONLY."""

    def test_premium_in_tp_premium_only_passes(self):
        for od in BLANK_VARIANTS:
            with self.subTest(od=od):
                self.assertEqual(_error(_policy("stp", od=od, tp=5883)), "")

    def test_premium_in_both_columns_fails(self):
        # The example in the request: an STP policy containing premiums in both OD and TP.
        reason = _error(_policy("stp", od=2829, tp=2829))
        self.assertTrue(reason.startswith(VALIDATION_ERROR_PREFIX))
        self.assertIn("Policy: total od is 2,829, but STP policies must have it zero/blank", reason)
        self.assertNotIn("Policy: tp premium is", reason)     # tp premium is where it belongs

    def test_no_tp_premium_fails(self):
        for tp in BLANK_VARIANTS:
            with self.subTest(tp=tp):
                reason = _error(_policy("stp", od=None, tp=tp))
                self.assertIn(
                    "Policy: tp premium is zero/blank, but STP policies must carry their premium there.", reason
                )

    def test_premium_in_total_od_only_breaks_both_halves_of_the_rule(self):
        reason = _error(_policy("stp", od=2829, tp=0))
        self.assertIn("Policy: tp premium is zero/blank", reason)
        self.assertIn("Policy: total od is 2,829", reason)

    def test_the_private_car_exception_does_not_apply_to_stp(self):
        reason = _error(_policy("stp", od=2829, tp=2829, vehicle="Private Car"))
        self.assertIn("Policy: total od is 2,829, but STP policies", reason)


class SaodRuleTests(SimpleTestCase):
    """SAOD: premium in total od ONLY."""

    def test_premium_in_total_od_only_passes(self):
        for tp in BLANK_VARIANTS:
            with self.subTest(tp=tp):
                self.assertEqual(_error(_policy("saod", od=2400, tp=tp)), "")

    def test_premium_in_both_columns_fails(self):
        reason = _error(_policy("saod", od=1690, tp=1690))
        self.assertIn("Policy: tp premium is 1,690, but SAOD policies must have it zero/blank", reason)
        self.assertNotIn("Policy: total od is", reason)

    def test_no_total_od_fails(self):
        for od in BLANK_VARIANTS:
            with self.subTest(od=od):
                reason = _error(_policy("saod", od=od, tp=None))
                self.assertIn(
                    "Policy: total od is zero/blank, but SAOD policies must carry their premium there.", reason
                )

    def test_premium_in_tp_premium_only_breaks_both_halves_of_the_rule(self):
        reason = _error(_policy("saod", od=0, tp=3466))
        self.assertIn("Policy: total od is zero/blank", reason)
        self.assertIn("Policy: tp premium is 3,466", reason)

    def test_the_private_car_exception_does_not_apply_to_saod(self):
        reason = _error(_policy("saod", od=1690, tp=1690, vehicle="Private Car"))
        self.assertIn("Policy: tp premium is 1,690, but SAOD policies", reason)


class PackageRuleTests(SimpleTestCase):
    """1+1 / 1+3 / 1+5: premium in BOTH columns, unless the vehicle product is Private Car."""

    def test_premium_in_both_columns_passes(self):
        for sub in PACKAGE_SUB_PRODUCTS:
            with self.subTest(sub=sub):
                self.assertEqual(_error(_policy(sub, od=2400, tp=5883)), "")

    def test_premium_in_total_od_only_fails(self):
        for sub in PACKAGE_SUB_PRODUCTS:
            for tp in BLANK_VARIANTS:
                with self.subTest(sub=sub, tp=tp):
                    reason = _error(_policy(sub, od=2400, tp=tp))
                    self.assertIn(
                        f"Policy: tp premium is zero/blank, but {sub} policies must carry premium in both "
                        f"Policy: total od and Policy: tp premium.",
                        reason,
                    )
                    self.assertNotIn("Policy: total od is", reason)

    def test_premium_in_tp_premium_only_fails(self):
        for sub in PACKAGE_SUB_PRODUCTS:
            for od in BLANK_VARIANTS:
                with self.subTest(sub=sub, od=od):
                    reason = _error(_policy(sub, od=od, tp=5883))
                    self.assertIn(
                        f"Policy: total od is zero/blank, but {sub} policies must carry premium in both "
                        f"Policy: total od and Policy: tp premium.",
                        reason,
                    )
                    self.assertNotIn("Policy: tp premium is", reason)

    def test_no_premium_at_all_fails_with_one_sentence(self):
        for sub in PACKAGE_SUB_PRODUCTS:
            with self.subTest(sub=sub):
                reason = _error(_policy(sub, od=0, tp=None))
                self.assertIn(
                    f"Policy: total od and Policy: tp premium are both zero/blank, but {sub} policies must "
                    f"carry premium in both Policy: total od and Policy: tp premium.",
                    reason,
                )
                self.assertEqual(reason.count("zero/blank, but"), 1)


class PrivateCarExceptionTests(SimpleTestCase):
    """Private Car: total od alone, or both columns — but never without total od."""

    def test_premium_in_total_od_only_passes(self):
        for sub in PACKAGE_SUB_PRODUCTS:
            for tp in BLANK_VARIANTS:
                with self.subTest(sub=sub, tp=tp):
                    self.assertEqual(_error(_policy(sub, od=2400, tp=tp, vehicle="Private Car")), "")

    def test_premium_in_both_columns_passes(self):
        for sub in PACKAGE_SUB_PRODUCTS:
            with self.subTest(sub=sub):
                self.assertEqual(_error(_policy(sub, od=2400, tp=5883, vehicle="Private Car")), "")

    def test_premium_in_tp_premium_only_fails(self):
        # The one Private Car row in the real file that broke it: 1+1, total od 0, tp premium 3,466.
        for sub in PACKAGE_SUB_PRODUCTS:
            with self.subTest(sub=sub):
                reason = _error(_policy(sub, od=0, tp=3466, vehicle="Private Car"))
                self.assertIn(
                    f"Policy: total od is zero/blank, but Private Car {sub} policies must carry premium in "
                    f"Policy: total od (Policy: tp premium is optional).",
                    reason,
                )

    def test_no_premium_at_all_fails(self):
        reason = _error(_policy("1+1", od=None, tp=None, vehicle="Private Car"))
        self.assertIn("Policy: total od is zero/blank, but Private Car 1+1 policies", reason)

    def test_the_vehicle_product_is_matched_ignoring_case_and_spaces(self):
        for vehicle in ("private car", "PRIVATE CAR", "  Private Car  "):
            with self.subTest(vehicle=vehicle):
                self.assertEqual(_error(_policy("1+1", od=2400, tp=None, vehicle=vehicle)), "")

    def test_any_other_vehicle_product_still_needs_both_columns(self):
        for vehicle in ("TW", "PCV 3W", "GCV 4W", "MISCD", "Two Wheeler", "Private Car Package", None, ""):
            with self.subTest(vehicle=vehicle):
                self.assertNotEqual(_error(_policy("1+1", od=2400, tp=None, vehicle=vehicle)), "")


class NoClaimBonusRuleTests(SimpleTestCase):
    """1+3 / 1+5 must carry no NCB — and that rule stacks with the premium rules instead of replacing them."""

    def test_a_non_zero_ncb_fails(self):
        for sub in ("1+3", "1+5"):
            with self.subTest(sub=sub):
                reason = _error(_policy(sub, od=2400, tp=5883, ncb=20))
                self.assertEqual(
                    reason,
                    f"{VALIDATION_ERROR_PREFIX}Policy: no claim bonus is 20, but {sub} policies must have it "
                    f"zero/blank. Correct the source MIS data and re-run mapping for this row.",
                )

    def test_zero_blank_or_null_ncb_passes(self):
        for sub in ("1+3", "1+5"):
            for ncb in BLANK_VARIANTS:
                with self.subTest(sub=sub, ncb=ncb):
                    self.assertEqual(_error(_policy(sub, od=2400, tp=5883, ncb=ncb)), "")

    def test_other_sub_products_may_carry_an_ncb(self):
        self.assertEqual(_error(_policy("1+1", od=2400, tp=5883, ncb=50)), "")
        self.assertEqual(_error(_policy("stp", tp=5883, ncb=50)), "")
        self.assertEqual(_error(_policy("saod", od=2400, ncb=35)), "")

    def test_the_private_car_exception_does_not_excuse_an_ncb(self):
        # Private Car 1+3 with premium in total od only is fine on the premium side; the NCB is still a violation.
        reason = _error(_policy("1+3", od=2400, tp=None, ncb=25, vehicle="Private Car"))
        self.assertIn("Policy: no claim bonus is 25", reason)
        self.assertNotIn("Policy: tp premium", reason)
        self.assertNotIn("Policy: total od", reason)

    def test_both_rules_are_reported_together_in_rule_order(self):
        reason = _error(_policy("1+5", od=2829, tp=None, ncb=20))
        self.assertLess(reason.index("Policy: tp premium is zero/blank"), reason.index("Policy: no claim bonus is 20"))

    def test_an_unreadable_ncb_is_reported_not_ignored(self):
        reason = _error(_policy("1+5", od=2400, tp=5883, ncb="twenty"))
        self.assertIn("Policy: no claim bonus value 'twenty' is not a valid number", reason)


class AmountReadingTests(SimpleTestCase):
    """Amounts are read the way cp premium# reads them (clean_cp_numeric)."""

    def test_thousands_separators_are_part_of_the_number(self):
        self.assertEqual(_error(_policy("stp", od="", tp="5,883.00")), "")
        self.assertEqual(_error(_policy("1+1", od="4,151.00", tp="16,149.00")), "")
        self.assertIn("Policy: total od is 4,151", _error(_policy("stp", od="4,151.00", tp="5,883.00")))

    def test_an_unreadable_amount_is_reported_instead_of_guessed_at(self):
        # The real file's '1870..00': a typo, not a missing premium.
        reason = _error(_policy("1+5", od="1870..00", tp="3,023.00"))
        self.assertEqual(
            reason,
            f"{VALIDATION_ERROR_PREFIX}Policy: total od value '1870..00' is not a valid number, so the premium "
            f"split of this 1+5 policy could not be checked. Correct the source MIS data and re-run mapping for this row.",
        )
        self.assertNotIn("zero/blank", reason)

    def test_two_unreadable_amounts_are_both_reported(self):
        reason = _error(_policy("1+1", od="abc", tp="n/a"))
        self.assertIn("Policy: total od value 'abc'", reason)
        self.assertIn("Policy: tp premium value 'n/a'", reason)

    def test_stray_letters_around_a_number_are_stripped_like_cp_premium_does(self):
        # clean_cp_numeric drops everything but digits, '.' and '-', so 'Rs 5883' is 5883.
        self.assertEqual(_error(_policy("stp", od="", tp="Rs 5883")), "")

    def test_a_negative_amount_is_a_premium(self):
        # An endorsement can refund premium: present, not absent.
        self.assertIn("Policy: total od is -100", _error(_policy("stp", od=-100, tp=5883)))
        self.assertEqual(_error(_policy("1+1", od=100, tp=-250)), "")

    def test_amounts_read_naturally_in_the_reason(self):
        self.assertIn("Policy: tp premium is 1,690,", _error(_policy("saod", od=2400, tp=1690)))
        self.assertIn("Policy: total od is 6,725.88,", _error(_policy("stp", od=6725.88, tp=2469)))
        self.assertIn("Policy: no claim bonus is 20.5,", _error(_policy("1+5", od=1, tp=1, ncb=20.5)))

    def test_the_full_reason_text(self):
        self.assertEqual(
            _errors(_policy("stp", od=2829, tp=2829)),
            [
                "Validation error — Policy: total od is 2,829, but STP policies must have it zero/blank "
                "(the premium belongs in Policy: tp premium only). "
                "Correct the source MIS data and re-run mapping for this row."
            ],
        )


class ScopeTests(SimpleTestCase):
    """Which rows the rules look at at all."""

    def test_sub_products_outside_the_rules_are_not_validated(self):
        for sub in ("3+3", "5+5", "1+2", "", None, "nan", "stp (own damage)"):
            with self.subTest(sub=sub):
                self.assertEqual(_error(_policy(sub, od=0, tp=0, ncb=20)), "")
                self.assertEqual(_error(_policy(sub, od=2829, tp=2829)), "")

    def test_the_sub_product_is_matched_ignoring_case_and_spaces(self):
        self.assertNotEqual(_error(_policy(" StP ", od=2829, tp=2829)), "")
        self.assertNotEqual(_error(_policy("SAOD", od=1690, tp=1690)), "")
        self.assertNotEqual(_error(_policy(" 1+3 ", od=2400, tp=5883, ncb=20)), "")

    def test_a_missing_amount_column_reads_as_blank(self):
        # safe_get_col hands back an all-None series for an absent column.
        none = pd.Series([None, None])
        reasons = premium_validation_errors(pd.Series(["stp", "1+1"]), pd.Series(["tw", "tw"]), none, none, none)
        self.assertIn("Policy: tp premium is zero/blank, but STP policies", reasons[0])
        self.assertIn("Policy: total od and Policy: tp premium are both zero/blank", reasons[1])

    def test_the_result_keeps_the_index_of_its_input(self):
        idx = [10, 20, 30]
        reasons = premium_validation_errors(
            pd.Series(["stp", "stp", "3+3"], index=idx), pd.Series(["tw"] * 3, index=idx),
            pd.Series([None, 2829, None], index=idx), pd.Series([5883, 2829, None], index=idx),
            pd.Series([None] * 3, index=idx),
        )
        self.assertEqual(list(reasons.index), idx)
        self.assertEqual(reasons[10], "")
        self.assertNotEqual(reasons[20], "")
        self.assertEqual(reasons[30], "")

    def test_an_empty_column_set_gives_an_empty_result(self):
        empty = pd.Series([], dtype=object)
        self.assertEqual(len(premium_validation_errors(empty, empty, empty, empty, empty)), 0)


# ── end to end: process_mis_mapping ─────────────────────────────────────────

def _motor(sub, od=None, tp=None, ncb=None, vehicle="TW", **extra):
    """A Motor MIS row whose insurer a wildcard Rate Master row would match, so only validation can stop it."""
    row = {"Product": "motor", "Policy: insurance company": INSURER,
           "Policy: sub product": sub, "Policy: vehproduct": vehicle}
    for column, value in (("Policy: total od", od), ("Policy: tp premium", tp), ("Policy: no claim bonus", ncb)):
        if value is not None:
            row[column] = value
    row.update(extra)
    return row


class PremiumValidationMappingTests(TestCase):
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

        # A wildcard rate row per table: any row that gets past validation and
        # shares its insurer MATCHES, so a BAD DATA result can only come from
        # the validation.
        self.rate = RateMaster.objects.create(
            insurance_company=INSURER, status="ACTIVE", is_deleted="NO",
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
        self.mis = MISFile.objects.create(
            uploaded_file=SimpleUploadedFile("mis.csv", buf.getvalue().encode("utf-8"))
        )
        process_mis_mapping(self.mis.id)
        self.mis.refresh_from_db()
        self.assertEqual(self.mis.status, "COMPLETED", self.mis.error_message)
        with self.mis.processed_file.open("rb") as f:
            return pd.read_csv(f)

    def test_a_valid_split_for_every_covered_sub_product_still_matches(self):
        out = self._run([
            _motor("STP", tp=5883),
            _motor("SAOD", od=2400),
            _motor("1+1", od=2400, tp=5883),
            _motor("1+3", od=2400, tp=5883, vehicle="Private Car"),
            _motor("1+5", od=2400, tp=5883),
            _motor("1+1", od=2400, vehicle="Private Car"),        # Private Car: total od alone is fine
        ])
        self.assertEqual(list(out["Mapping Status"]), [MATCH] * 6)
        self.assertEqual(list(out["Displaygroupid"]), [self.rate.id] * 6)

    def test_a_row_that_breaks_a_rule_is_rejected_before_any_rate_lookup(self):
        # The request's example: an STP policy with premium in both OD and TP.
        # The wildcard rate row would have matched it.
        out = self._run([_motor("STP", od=2829, tp=2829)])
        self.assertEqual(out.loc[0, "Mapping Status"], BAD_DATA)
        self.assertTrue(out.loc[0, "Failure Reason"].startswith(VALIDATION_ERROR_PREFIX))
        self.assertIn("Policy: total od is 2,829, but STP policies", out.loc[0, "Failure Reason"])
        self.assertTrue(pd.isna(out.loc[0, "Displaygroupid"]))
        self.assertTrue(pd.isna(out.loc[0, "Porate"]))
        self.assertTrue(pd.isna(out.loc[0, "Pay-out Amt"]))

    def test_each_rule_rejects_its_own_violation(self):
        out = self._run([
            _motor("STP", od=2829, tp=2829),                      # STP: premium in both
            _motor("SAOD", od=1690, tp=1690),                     # SAOD: premium in both
            _motor("1+1", od=2400),                               # 1+1: tp premium missing
            _motor("1+1", tp=3466, vehicle="Private Car"),        # Private Car: total od missing
            _motor("1+5", od=2400, tp=5883, ncb=20),              # 1+5: NCB set
        ])
        self.assertEqual(list(out["Mapping Status"]), [BAD_DATA] * 5)
        for reason in out["Failure Reason"]:
            self.assertTrue(reason.startswith(VALIDATION_ERROR_PREFIX), reason)

    def test_every_broken_rule_on_a_row_is_reported_together(self):
        out = self._run([_motor("1+5", od=2829, ncb=20)])        # tp premium missing AND an NCB
        reason = out.loc[0, "Failure Reason"]
        self.assertIn("Policy: tp premium is zero/blank", reason)
        self.assertIn("Policy: no claim bonus is 20", reason)

    def test_the_rest_of_the_file_is_not_held_up(self):
        out = self._run([_motor("STP", tp=5883), _motor("STP", od=2829, tp=2829), _motor("SAOD", od=2400)])
        self.assertEqual(list(out["Mapping Status"]), [MATCH, BAD_DATA, MATCH])
        self.assertEqual(out.loc[0, "Porate"], 12.5)
        self.assertEqual(out.loc[2, "Porate"], 12.5)

    def test_sub_products_outside_the_rules_are_left_alone(self):
        out = self._run([
            _motor("3+3", od=0, tp=0, ncb=20),
            _motor("5+5", od=2829, tp=2829),
            _motor(None),
        ])
        self.assertEqual(list(out["Mapping Status"]), [MATCH] * 3)

    def test_private_car_is_read_from_vehproduct_not_policy_category(self):
        out = self._run([
            _motor("1+1", od=2400, vehicle="TW", **{"Policy: policy category": "Private Car"}),
            _motor("1+1", od=2400, vehicle="Private Car", **{"Policy: policy category": "Two Wheeler"}),
        ])
        self.assertEqual(list(out["Mapping Status"]), [BAD_DATA, MATCH])

    def test_health_rows_and_unrecognised_products_are_never_validated(self):
        # Product routing comes first: a Health row (or an unrecognised product) carrying
        # premium columns that would fail the Motor rules must not be touched by them.
        out = self._run([
            _motor("1+1", od=2400, tp=5883),
            {
                "Product": "health", "Policy: insurance company": HEALTH_INSURER,
                "Policy: inception date": "01/08/2026", "Policy: expiry date": "31/07/2027",
                "Policy: sub product": "STP", "Policy: total od": 2829, "Policy: tp premium": 2829,
            },
            _motor("STP", od=2829, tp=2829, Product="life"),
        ])
        self.assertEqual(list(out["Mapping Status"]), [MATCH, MATCH, NO_MATCH])
        self.assertEqual(out.loc[1, "Displaygroupid"], self.health_rate.id)
        self.assertTrue(out.loc[2, "Failure Reason"].startswith("Failed on: Product —"))

    def test_a_row_failing_the_cc_gvw_guard_and_the_premium_rules_reports_both(self):
        # Neither stops the other (see test_mapping_engine_batch_errors); the CC/GVW text keeps its wording and comes first.
        out = self._run([_motor("STP", od=2829, tp=2829, **{"Policy: cc cubic capacity": "6702/47500"})])
        reason = out.loc[0, "Failure Reason"]
        self.assertEqual(out.loc[0, "Mapping Status"], BAD_DATA)
        self.assertTrue(reason.startswith("Policy: cc cubic capacity value '6702/47500' looks like a combined CC/GVW figure"))
        self.assertIn(f" | {VALIDATION_ERROR_PREFIX}", reason)

    def test_comma_formatted_amounts_are_read_as_numbers(self):
        # Excel exports hold these as text: '5,883.00'.
        out = self._run([
            _motor("1+1", od="4,151.00", tp="5,883.00"),
            _motor("STP", od="", tp="5,883.00"),
        ])
        self.assertEqual(list(out["Mapping Status"]), [MATCH, MATCH])

    def test_a_typo_in_an_amount_is_reported_not_treated_as_missing(self):
        out = self._run([_motor("1+5", od="1870..00", tp="3,023.00")])
        reason = out.loc[0, "Failure Reason"]
        self.assertEqual(out.loc[0, "Mapping Status"], BAD_DATA)
        self.assertIn("Policy: total od value '1870..00' is not a valid number", reason)
        self.assertNotIn("zero/blank", reason)

    def test_a_file_without_the_premium_columns_reads_them_as_blank(self):
        out = self._run([
            {"Product": "motor", "Policy: insurance company": INSURER, "Policy: sub product": "STP"},
            {"Product": "motor", "Policy: insurance company": INSURER},
        ])
        self.assertEqual(list(out["Mapping Status"]), [BAD_DATA, MATCH])
        self.assertIn("Policy: tp premium is zero/blank, but STP policies", out.loc[0, "Failure Reason"])

    def test_a_file_where_every_row_fails_still_completes(self):
        out = self._run([_motor("STP", od=1, tp=1), _motor("SAOD", od=0, tp=0)])
        self.assertEqual(list(out["Mapping Status"]), [BAD_DATA, BAD_DATA])

    def test_the_failed_row_is_stored_for_rate_master_health_with_its_premium_columns(self):
        self._run([_motor("STP", tp=5883), _motor("STP", od=2829, tp=2829)])
        failed = MISFailedRow.objects.get(mis_file=self.mis)
        self.assertEqual(failed.row_id, 1)
        self.assertEqual(failed.status_key, "BAD_DATA")
        self.assertEqual(failed.mapping_status, BAD_DATA)
        self.assertTrue(failed.failure_reason.startswith(VALIDATION_ERROR_PREFIX))
        # What a human needs to see to fix it is in the details, not only in the reason text.
        self.assertEqual(failed.payload["Policy: total od"], 2829)
        self.assertEqual(failed.payload["Policy: tp premium"], 2829)
        self.assertEqual(failed.payload["Policy: sub product"], "STP")

    def test_the_run_summary_counts_validation_failures_as_bad_data(self):
        self._run([_motor("STP", tp=5883), _motor("STP", od=2829, tp=2829), _motor("SAOD", tp=5883)])
        self.assertIn("Mapped 1 rates", self.mis.error_message)
        self.assertIn("2 rows flagged FAILED - BAD DATA", self.mis.error_message)
