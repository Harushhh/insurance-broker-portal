"""
Batch error collection: no check stops at the first failure.

A row that fails several checks reports ALL of them in its one Failure Reason —
the rate-matching rules (Motor RULE 1-6, Health H1-H6) and the data checks (a
combined CC/GVW cell, the premium split / NCB rules) — so fixing one error never
just uncovers the next one on the next run. The first failure keeps exactly the
text it always had (the Missing Make/Model page and the coverage-gap panel read
it); the rest follow on the same line after ' | Also failed on: '.

What does NOT change: which rows MATCH, which are MULTIPLE MATCHES, which are NO
MATCH or BAD DATA, and every payout.
"""
import datetime
import io
import tempfile
from unittest import mock

import pandas as pd
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from insurance.mapping_engine import (
    ALSO_FAILED_ON_PREFIX,
    FAILURE_SEGMENT_SEPARATOR,
    MAKE_MODEL_RULE_LABEL,
    MAKE_MODEL_UNRESOLVED_REASON_PREFIX,
    MAKE_MODEL_UNRESOLVED_VALUE_PATTERN,
    MAKE_MODEL_UNRESOLVED_VALUE_SUFFIX,
    VALIDATION_ERROR_PREFIX,
    VEHICLE_AGE_REGISTRATION_LABEL,
    build_coverage_gap_summary,
    build_make_model_lookup,
    format_failed_on,
    fuzzy_match_make_model,
    make_model_unresolved_detail,
    narrow_candidates,
    process_mis_mapping,
    split_failed_on,
)
from insurance.models import (
    FuelTypeMaster, HealthRateMaster, MakeModelClassMaster, MakeModelMaster, MISFailedRow, MISFile,
    PincodeMaster, ProductMaster, RateMaster, RTOMaster, SubProductMaster, YesNoNAMaster,
)

INSURER = "Acme General Insurance"             # exists ONLY in RateMaster
HEALTH_INSURER = "Zenith Health Insurance"     # exists ONLY in HealthRateMaster

MATCH = "✅ MATCH"
NO_MATCH = "❌ NO MATCH"
BAD_DATA = "FAILED - BAD DATA"

# The year the end-to-end tests pretend the mapping run happens in.
RUN_YEAR = 2026


def _labels(reason):
    return [label for label, _ in split_failed_on(reason)]


# ── unit: the failure text ──────────────────────────────────────────────────

class FailureTextTests(SimpleTestCase):
    def test_one_failure_reads_exactly_as_it_always_did(self):
        self.assertEqual(
            format_failed_on([("Policy: fuel", "Value 'diesel' did not match.")]),
            "Failed on: Policy: fuel — Value 'diesel' did not match.",
        )

    def test_further_failures_follow_on_the_same_line(self):
        text = format_failed_on([("A", "a."), ("B", "b."), ("C", "c.")])
        self.assertEqual(text, "Failed on: A — a. | Also failed on: B — b. | Also failed on: C — c.")
        self.assertNotIn("\n", text)       # the coverage-gap pattern does not cross a newline

    def test_split_is_format_in_reverse(self):
        failures = [
            ("Policy: fuel", "Value 'diesel' did not match any remaining candidate."),
            ("Policy: rto no", "RTO 'GJ01' was not found in any RTOMaster cluster — add it to RTOMaster."),
            ("Policy: vehicle class", "Vehicle class 'auto' has no exact match."),
            (MAKE_MODEL_RULE_LABEL, make_model_unresolved_detail("bmw x5")),
        ]
        for count in range(1, len(failures) + 1):
            with self.subTest(count=count):
                self.assertEqual(split_failed_on(format_failed_on(failures[:count])), failures[:count])

    def test_a_reason_written_before_failures_were_collected_has_the_one_pair(self):
        old = "Failed on: Policy: fuel — Value 'diesel' did not match any remaining candidate."
        self.assertEqual(split_failed_on(old), [("Policy: fuel", "Value 'diesel' did not match any remaining candidate.")])

    def test_reasons_that_are_not_failed_on_reasons_have_no_pairs(self):
        for reason in ("No rates found for criteria", "Matched Successfully", "", "Validation error — x"):
            with self.subTest(reason=reason):
                self.assertEqual(split_failed_on(reason), [])


class NarrowCandidatesTests(SimpleTestCase):
    def setUp(self):
        self.grid = pd.DataFrame({"id": [1, 2, 3], "fuel": ["petrol", "cng", "petrol"], "cc": [100, 100, 500]})

    def test_surviving_rows_become_the_candidates(self):
        failed = []
        narrowed = narrow_candidates(self.grid, self.grid["fuel"] == "petrol", failed, "Policy: fuel", "detail")
        self.assertEqual(list(narrowed["id"]), [1, 3])
        self.assertEqual(failed, [])

    def test_a_rule_nothing_passes_is_recorded_and_the_candidates_are_kept(self):
        failed = []
        kept = narrow_candidates(self.grid, self.grid["fuel"] == "diesel", failed, "Policy: fuel", "no diesel")
        self.assertIs(kept, self.grid)
        self.assertEqual(failed, [("Policy: fuel", "no diesel")])

    def test_later_rules_still_narrow_the_kept_candidates(self):
        failed = []
        grid = narrow_candidates(self.grid, self.grid["fuel"] == "diesel", failed, "Policy: fuel", "no diesel")
        grid = narrow_candidates(grid, grid["cc"] <= 100, failed, "Policy: cc", "too big")
        self.assertEqual(list(grid["id"]), [1, 2])
        self.assertEqual(failed, [("Policy: fuel", "no diesel")])      # the second rule passed


# ── unit: what reads the text ───────────────────────────────────────────────

class CoverageGapTests(SimpleTestCase):
    """The coverage-gap panel counts every failed check, so closing the top gaps closes them all."""

    def _gaps(self, *reasons):
        df = pd.DataFrame({"Mapping Status": [NO_MATCH] * len(reasons), "Failure Reason": list(reasons)})
        return build_coverage_gap_summary(df)["no_match"]

    def test_single_failure_rows_are_counted_as_before(self):
        reason = format_failed_on([("Policy: fuel", "Value 'diesel' did not match.")])
        self.assertEqual(self._gaps(reason, reason), [{"rule": "Policy: fuel", "value": "diesel", "count": 2}])

    def test_a_row_that_failed_several_checks_counts_under_each(self):
        row = format_failed_on([
            ("Policy: fuel", "Value 'diesel' did not match."),
            ("Policy: rto no", "RTO 'GJ01' was not found."),
        ])
        gaps = self._gaps(row, row, format_failed_on([("Policy: rto no", "RTO 'GJ01' was not found.")]))
        self.assertEqual(
            gaps,
            [
                {"rule": "Policy: rto no", "value": "GJ01", "count": 3},
                {"rule": "Policy: fuel", "value": "diesel", "count": 2},
            ],
        )

    def test_a_failure_with_no_quoted_value_is_keyed_on_its_own_detail_not_a_later_ones(self):
        # The first detail has no quote, a later one does: the first must not borrow the later one's value.
        first_detail = "Policy: seating capacity value 9 falls outside the range configured on every remaining candidate rate row."
        row = format_failed_on([("Policy: seating capacity", first_detail), ("Policy: rto no", "RTO 'GJ01' was not found.")])
        gaps = {(g["rule"], g["value"]): g["count"] for g in self._gaps(row)}
        self.assertEqual(
            gaps,
            {("Policy: seating capacity", first_detail[:60].strip()): 1, ("Policy: rto no", "GJ01"): 1},
        )

    def test_reasons_that_are_not_failed_on_reasons_still_land_in_other(self):
        self.assertEqual(
            self._gaps("No rates found for criteria"),
            [{"rule": "Other", "value": "No rates found for criteria", "count": 1}],
        )


class MissingMakeModelReasonTests(SimpleTestCase):
    """The Missing Make/Model page reads the START of the reason: extra failures after it change nothing."""

    def test_the_failing_value_is_still_read_when_more_failures_follow(self):
        reason = format_failed_on([
            (MAKE_MODEL_RULE_LABEL, make_model_unresolved_detail("bmw 3's series")),
            ("Policy: rto no", "RTO 'GJ01' was not found in any RTOMaster cluster — add it to RTOMaster."),
        ])
        self.assertTrue(reason.startswith(MAKE_MODEL_UNRESOLVED_REASON_PREFIX))
        self.assertIn(MAKE_MODEL_UNRESOLVED_VALUE_SUFFIX, reason)
        self.assertEqual(MAKE_MODEL_UNRESOLVED_VALUE_PATTERN.match(reason).group(1), "bmw 3's series")

    def test_a_make_model_failure_that_is_not_the_first_is_not_picked_up(self):
        # Exactly as before: a row only shows up there when make/model was the rule it first failed on.
        reason = format_failed_on([
            ("Policy: fuel", "Value 'diesel' did not match."),
            (MAKE_MODEL_RULE_LABEL, make_model_unresolved_detail("bmw x5")),
        ])
        self.assertFalse(reason.startswith(MAKE_MODEL_UNRESOLVED_REASON_PREFIX))
        self.assertIsNone(MAKE_MODEL_UNRESOLVED_VALUE_PATTERN.match(reason))


class MakeModelLookupTests(TestCase):
    """
    RULE 5a's lookup is backed by the inverted cluster index (every row now reaches it, so a scan of the
    whole master per distinct vehicle is too slow). It must decide exactly as fuzzy_match_make_model does.
    """

    TERMS = [
        "honda activa", "HONDA  ACTIVA ", "tvs jupiter", "honda", "honda jupiter", "tata nexon ev",
        "honda city 1.5", "honda city-1.5", "honda activa6g", "activa 6g honda", "yamaha alpha",
        "x", "", None,
    ]

    def setUp(self):
        MakeModelMaster.objects.create(make_model_name="Honda_All", make_model_cluster="HONDA ACTIVA,HONDA DIO, TVS JUPITER")
        MakeModelMaster.objects.create(make_model_name="tata_all", make_model_cluster="TATA NEXON EV PRIME,HONDA CITY-1.5,HONDA ACTIVA6G")
        MakeModelMaster.objects.create(make_model_name="empty", make_model_cluster="")
        MakeModelMaster.objects.create(make_model_name="blank", make_model_cluster=None)
        self.masters = list(MakeModelMaster.objects.values_list("make_model_name", "make_model_cluster"))

    def _reference(self, term):
        if not term:
            return set()
        key = str(term).strip().upper()
        return {
            str(name).strip().lower()
            for name, cluster in self.masters
            if cluster and fuzzy_match_make_model(key, cluster)
        }

    def test_the_lookup_agrees_with_fuzzy_match_make_model(self):
        resolve = build_make_model_lookup(MakeModelMaster.objects.all(), "make_model_name", "make_model_cluster")
        for term in self.TERMS:
            with self.subTest(term=term):
                self.assertEqual(resolve(term), self._reference(term))

    def test_names_come_back_lower_cased_and_a_repeat_is_the_same_answer(self):
        resolve = build_make_model_lookup(MakeModelMaster.objects.all(), "make_model_name", "make_model_cluster")
        self.assertEqual(resolve("honda dio"), {"honda_all"})
        self.assertEqual(resolve("HONDA DIO"), resolve("honda dio"))
        # 'HONDA ACTIVA' shares both its words with the 'HONDA ACTIVA6G' item too (ACTIVA6G -> ACTIVA, 6G).
        self.assertEqual(resolve("honda activa"), {"honda_all", "tata_all"})

    def test_a_term_with_fewer_words_than_the_threshold_never_matches(self):
        resolve = build_make_model_lookup(MakeModelMaster.objects.all(), "make_model_name", "make_model_cluster")
        self.assertEqual(resolve("honda"), set())


# ── end to end: Motor ───────────────────────────────────────────────────────

GOOD_MOTOR = {
    "Product": "motor", "Policy: insurance company": INSURER,
    "Policy: vehproduct": "TW", "Policy: sub product": "SAOD", "Policy: fuel": "Petrol",
    "Policy: vehicle class": "Scooter", "Policy: cc cubic capacity": 110, "Policy: seating capacity": 2,
    "Policy: date of registration": "15-06-2026", "Policy: inception date": "15/06/2026",
    "Policy: vehicle make": "HONDA", "Policy: model": "ACTIVA", "Policy: rto no": "MH12",
    "Policy: no claim bonus": 0,
    "Policy: total od": 2400,                 # SAOD: premium in total od only
}


class _EngineCase(TestCase):
    """Temp media, a neutralised DB connection (see test_mapping_engine_routing) and a pinned run year."""

    def setUp(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)
        for patcher in (
            mock.patch("insurance.mapping_engine.connection"),
            mock.patch("insurance.mapping_engine.timezone.localdate", return_value=datetime.date(RUN_YEAR, 6, 1)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

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


class MotorBatchErrorTests(_EngineCase):
    def setUp(self):
        super().setUp()
        MakeModelMaster.objects.create(make_model_name="honda_all", make_model_cluster="HONDA ACTIVA,HONDA DIO")
        RTOMaster.objects.create(rto_name="mh_all", rto_cluster="MH12,MH14")
        # ONE rate card that GOOD_MOTOR satisfies on every rule, so a MIS row that differs
        # from it fails exactly the rules it differs on, however many that is.
        self.card = RateMaster.objects.create(
            insurance_company=INSURER, status="ACTIVE", is_deleted="NO",
            product=ProductMaster.objects.get_or_create(name="TW")[0],
            sub_product=SubProductMaster.objects.get_or_create(name="SAOD")[0],
            fuel_type=FuelTypeMaster.objects.get_or_create(name="Petrol")[0],
            make_model_class=MakeModelClassMaster.objects.get_or_create(name="Scooter")[0],
            cc_min=0, cc_max=150, sc_min=1, sc_max=2,
            vehicle_age_min=0, vehicle_age_max=1,
            from_date=datetime.date(2026, 1, 1), to_date=datetime.date(2026, 12, 31),
            new_vehicle_makes="honda_all", new_rto_list="mh_all",
            is_ncb=YesNoNAMaster.objects.get_or_create(code="NO", defaults={"meaning": "No"})[0],
            po_type="On Net", po_net_rate=10, pi_type="On Net", pi_net_rate=20,
            po_od_rate=0, po_tp_rate=0, pi_od_rate=0, pi_tp_rate=0,
        )

    def test_a_row_that_passes_every_check_still_matches(self):
        out = self._run([GOOD_MOTOR])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH, out.loc[0, "Failure Reason"])
        self.assertEqual(out.loc[0, "Displaygroupid"], self.card.id)
        self.assertEqual(out.loc[0, "Porate"], 10)

    def test_one_failed_check_reads_exactly_as_before_and_is_never_a_match(self):
        # The rest of the rate card still fits this row, which is what must NOT turn it into a match.
        out = self._run([{**GOOD_MOTOR, "Policy: fuel": "Diesel"}])
        self.assertEqual(out.loc[0, "Mapping Status"], NO_MATCH)
        self.assertEqual(
            out.loc[0, "Failure Reason"],
            "Failed on: Policy: fuel — Value 'diesel' did not match any remaining candidate Rate Master "
            "row's fuel type (after the previous rules narrowed the field).",
        )
        self.assertTrue(pd.isna(out.loc[0, "Displaygroupid"]))
        self.assertTrue(pd.isna(out.loc[0, "Porate"]))

    def test_every_failed_check_is_listed_in_rule_order(self):
        out = self._run([{
            **GOOD_MOTOR,
            "Policy: vehproduct": "PCV 3W",                                    # RULE 2  product
            "Policy: sub product": "1+1", "Policy: tp premium": 5883,         # RULE 2  sub product (a valid 1+1 split)
            "Policy: fuel": "Diesel",                                          # RULE 2  fuel
            "Policy: vehicle class": "Auto",                                   # RULE 2b vehicle class
            "Policy: cc cubic capacity": 500,                                  # RULE 3  cc
            "Policy: seating capacity": 9,                                     # RULE 3  seats
            "Policy: date of registration": "15-06-2018",                      # RULE 3  age 9
            "Policy: inception date": "15/06/2027",                            # RULE 4  date window
            "Policy: vehicle make": "BMW", "Policy: model": "X5",              # RULE 5a make/model
            "Policy: rto no": "GJ01",                                          # RULE 5b RTO
            "Policy: no claim bonus": 20,                                      # RULE 6  NCB
        }])
        reason = out.loc[0, "Failure Reason"]
        self.assertEqual(out.loc[0, "Mapping Status"], NO_MATCH)
        self.assertEqual(
            _labels(reason),
            [
                "Policy: vehproduct", "Policy: sub product", "Policy: fuel", "Policy: vehicle class",
                "Policy: cc cubic capacity", "Policy: seating capacity", VEHICLE_AGE_REGISTRATION_LABEL,
                "Policy: inception date", MAKE_MODEL_RULE_LABEL, "Policy: rto no", "Policy: no claim bonus",
            ],
        )
        # The first one is what the row would have said on its own, and the rest follow on the one line.
        self.assertTrue(reason.startswith("Failed on: Policy: vehproduct — Value 'pcv 3w' did not match"))
        self.assertEqual(reason.count(ALSO_FAILED_ON_PREFIX), 10)
        self.assertNotIn("\n", reason)
        self.assertTrue(pd.isna(out.loc[0, "Displaygroupid"]))

    def test_rules_that_pass_are_not_reported(self):
        # Only the RTO and the NCB are wrong: the other rules pass and say nothing.
        out = self._run([{**GOOD_MOTOR, "Policy: rto no": "GJ01", "Policy: no claim bonus": 20}])
        self.assertEqual(_labels(out.loc[0, "Failure Reason"]), ["Policy: rto no", "Policy: no claim bonus"])

    def test_the_coverage_gaps_count_every_failed_check(self):
        out = self._run([{**GOOD_MOTOR, "Policy: fuel": "Diesel", "Policy: rto no": "GJ01"}] * 2)
        gaps = {(g["rule"], g["value"]): g["count"] for g in self.mis.coverage_gaps["no_match"]}
        self.assertEqual(gaps, {("Policy: fuel", "diesel"): 2, ("Policy: rto no", "GJ01"): 2})
        self.assertEqual(list(out["Mapping Status"]), [NO_MATCH, NO_MATCH])

    def test_the_stored_failed_row_carries_the_whole_reason(self):
        self._run([GOOD_MOTOR, {**GOOD_MOTOR, "Policy: fuel": "Diesel", "Policy: rto no": "GJ01"}])
        stored = MISFailedRow.objects.get(mis_file=self.mis)
        self.assertEqual(stored.status_key, "NO_MATCH")
        self.assertEqual(_labels(stored.failure_reason), ["Policy: fuel", "Policy: rto no"])

    def test_an_unknown_insurer_leaves_nothing_else_to_check(self):
        out = self._run([{**GOOD_MOTOR, "Policy: insurance company": "Totally Unknown Underwriters",
                          "Policy: fuel": "Diesel"}])
        self.assertEqual(out.loc[0, "Mapping Status"], NO_MATCH)
        self.assertEqual(_labels(out.loc[0, "Failure Reason"]), ["Policy: insurance company"])

    def test_the_other_rows_of_the_file_are_untouched(self):
        out = self._run([GOOD_MOTOR, {**GOOD_MOTOR, "Policy: fuel": "Diesel", "Policy: rto no": "GJ01"}, GOOD_MOTOR])
        self.assertEqual(list(out["Mapping Status"]), [MATCH, NO_MATCH, MATCH])
        self.assertEqual(out.loc[0, "Failure Reason"], "Matched Successfully")
        self.assertEqual(list(out["Displaygroupid"].iloc[[0, 2]]), [self.card.id, self.card.id])

    # ---- data errors are collected too -------------------------------------

    def test_a_row_with_only_a_data_error_reads_exactly_as_before(self):
        out = self._run([{**GOOD_MOTOR, "Policy: tp premium": 1690}])           # SAOD with a tp premium
        self.assertEqual(out.loc[0, "Mapping Status"], BAD_DATA)
        self.assertEqual(
            out.loc[0, "Failure Reason"],
            "Validation error — Policy: tp premium is 1,690, but SAOD policies must have it zero/blank "
            "(the premium belongs in Policy: total od only). "
            "Correct the source MIS data and re-run mapping for this row.",
        )

    def test_a_row_with_a_data_error_also_lists_its_rate_matching_failures(self):
        out = self._run([{**GOOD_MOTOR, "Policy: tp premium": 1690, "Policy: fuel": "Diesel", "Policy: rto no": "GJ01"}])
        reason = out.loc[0, "Failure Reason"]
        self.assertEqual(out.loc[0, "Mapping Status"], BAD_DATA)             # still no payout from a rate card
        self.assertTrue(reason.startswith(VALIDATION_ERROR_PREFIX))
        self.assertIn(f"{FAILURE_SEGMENT_SEPARATOR}{ALSO_FAILED_ON_PREFIX}Policy: fuel —", reason)
        self.assertIn(f"{FAILURE_SEGMENT_SEPARATOR}{ALSO_FAILED_ON_PREFIX}Policy: rto no —", reason)
        self.assertTrue(pd.isna(out.loc[0, "Displaygroupid"]))
        self.assertTrue(pd.isna(out.loc[0, "Porate"]))

    def test_a_data_error_wins_even_when_the_rate_chain_would_have_matched(self):
        out = self._run([GOOD_MOTOR, {**GOOD_MOTOR, "Policy: tp premium": 1690}])
        self.assertEqual(list(out["Mapping Status"]), [MATCH, BAD_DATA])
        self.assertNotIn(ALSO_FAILED_ON_PREFIX, out.loc[1, "Failure Reason"])

    def test_a_cc_gvw_cell_and_a_premium_error_are_both_reported(self):
        out = self._run([{**GOOD_MOTOR, "Policy: cc cubic capacity": "6702/47500", "Policy: tp premium": 1690}])
        reason = out.loc[0, "Failure Reason"]
        self.assertEqual(out.loc[0, "Mapping Status"], BAD_DATA)
        self.assertTrue(reason.startswith("Policy: cc cubic capacity value '6702/47500' looks like a combined CC/GVW figure"))
        self.assertIn(f"{FAILURE_SEGMENT_SEPARATOR}{VALIDATION_ERROR_PREFIX}", reason)
        self.assertLess(reason.index("combined CC/GVW"), reason.index(VALIDATION_ERROR_PREFIX))

    def test_a_combined_cc_gvw_cell_is_not_also_range_checked(self):
        # The cell has no usable number: it is reported as a data error, not as a CC outside every band.
        out = self._run([{**GOOD_MOTOR, "Policy: cc cubic capacity": "6702/47500"}])
        reason = out.loc[0, "Failure Reason"]
        self.assertEqual(out.loc[0, "Mapping Status"], BAD_DATA)
        self.assertIn("combined CC/GVW", reason)
        self.assertNotIn("falls outside the range", reason)


# ── end to end: Health ──────────────────────────────────────────────────────

GOOD_HEALTH = {
    "Product": "health", "Policy: insurance company": HEALTH_INSURER,
    # dd/mm/yyyy: the engine parses dates day-first.
    "Policy: inception date": "01/08/2026", "Policy: expiry date": "31/07/2027",
    "Policy: product name": "alpha shield", "Policy: policy category": "individual",
    "Policy Type": "New", "Policy: plan name": "Plan Alpha",
    "Policy: deductible amount": 0, "Policy: sum assured": 200000,
    "Customer: age": 30, "Customer: pin code": "400001",
}


class HealthBatchErrorTests(_EngineCase):
    def setUp(self):
        super().setUp()
        # A file whose only matches are Health rows trips an unrelated pandas bug (see
        # test_mapping_engine_routing), so every run leads with one plain Motor match.
        RateMaster.objects.create(
            insurance_company=INSURER, status="ACTIVE", is_deleted="NO",
            po_type="On Net", po_net_rate=10, pi_type="On Net", pi_net_rate=20,
            po_od_rate=0, po_tp_rate=0, pi_od_rate=0, pi_tp_rate=0,
        )
        PincodeMaster.objects.create(pincode_zone="zone a", pincode_cluster="400001,400002")
        self.card = HealthRateMaster.objects.create(
            insurance_company=HEALTH_INSURER, status="ACTIVE", is_deleted="NO",
            product_name="alpha shield", policy_category="individual", business_type="new",
            plan_names="Plan Alpha",
            min_deductible=0, max_deductible=0, min_sum_insured=100000, max_sum_insured=500000,
            min_age=18, max_age=45, pincode_zone="zone a",
            from_date=datetime.date(2026, 1, 1), to_date=datetime.date(2026, 12, 31),
            one_year_rate=5, payin_rate=8,
        )
        self.lead = {"Product": "motor", "Policy: insurance company": INSURER}

    def _run_health(self, *health_rows):
        out = self._run([self.lead, *health_rows])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)
        return out

    def test_a_row_that_passes_every_check_still_matches(self):
        out = self._run_health(GOOD_HEALTH)
        self.assertEqual(out.loc[1, "Mapping Status"], MATCH, out.loc[1, "Failure Reason"])
        self.assertEqual(out.loc[1, "Displaygroupid"], self.card.id)

    def test_one_failed_check_reads_exactly_as_before_and_is_never_a_match(self):
        out = self._run_health({**GOOD_HEALTH, "Customer: age": 70})
        self.assertEqual(out.loc[1, "Mapping Status"], NO_MATCH)
        self.assertEqual(
            out.loc[1, "Failure Reason"],
            "Failed on: Customer: age — Customer: age value 70.0 falls outside the range configured on "
            "every remaining candidate Health Grid row.",
        )

    def test_every_failed_check_is_listed_in_rule_order(self):
        out = self._run_health({
            **GOOD_HEALTH,
            "Policy: inception date": "01/08/2027", "Policy: expiry date": "31/07/2028",   # H2 date window
            "Policy: product name": "zzz qqq",                                              # H3 product name
            "Policy: policy category": "corporate",                                         # H3 policy category
            "Policy Type": "Port",                                                          # H3 business type
            "Policy: plan name": "Totally Different",                                       # H4 plan name
            "Policy: deductible amount": 50000,                                             # H5 deductible
            "Policy: sum assured": 9999999,                                                 # H5 sum assured
            "Customer: age": 70,                                                            # H5 age
            "Customer: pin code": "999999",                                                 # H6 pincode
        })
        reason = out.loc[1, "Failure Reason"]
        self.assertEqual(out.loc[1, "Mapping Status"], NO_MATCH)
        self.assertEqual(
            _labels(reason),
            [
                "Policy: inception date", "Policy: product name", "Policy: policy category", "Policy Type",
                "Policy: plan name", "Policy: deductible amount", "Policy: sum assured", "Customer: age",
                "Customer: pin code",
            ],
        )
        self.assertEqual(reason.count(ALSO_FAILED_ON_PREFIX), 8)

    def test_an_unsupported_tenure_alone_reads_exactly_as_before(self):
        out = self._run_health({**GOOD_HEALTH, "Policy: expiry date": "01/07/2026"})    # expires before it starts
        self.assertEqual(out.loc[1, "Mapping Status"], NO_MATCH)
        self.assertEqual(
            out.loc[1, "Failure Reason"],
            "Failed on: Policy tenure — computed an unparseable tenure from Policy: inception date / "
            "Policy: expiry date, but the Health Grid only defines payout rates for 1-5 year tenures.",
        )

    def test_the_tenure_is_listed_first_and_no_longer_hides_the_other_failures(self):
        out = self._run_health({**GOOD_HEALTH, "Policy: expiry date": "01/07/2026", "Customer: age": 70,
                                "Customer: pin code": "999999"})
        self.assertEqual(
            _labels(out.loc[1, "Failure Reason"]),
            ["Policy tenure", "Customer: age", "Customer: pin code"],
        )

    def test_the_coverage_gaps_count_every_failed_health_check(self):
        self._run_health({**GOOD_HEALTH, "Customer: age": 70, "Customer: pin code": "999999"})
        gaps = {(g["rule"], g["value"]) for g in self.mis.coverage_gaps["no_match"]}
        self.assertIn(("Customer: pin code", "999999"), gaps)
        self.assertTrue(any(rule == "Customer: age" for rule, _ in gaps))
