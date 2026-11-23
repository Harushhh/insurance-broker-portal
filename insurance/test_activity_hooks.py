"""The events the pages and jobs put on the Team Activity feed.

One per thing a person did, with the right person, category and wording, and never a vehicle number, a policy-holder
name or the file name of a policy document: those are personal data, and the feed is read by managers.
"""
import json
import tempfile
from datetime import date
from unittest import mock

import pandas as pd
from django.contrib.auth.models import AnonymousUser, Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from insurance.mapping_engine import process_mis_mapping
from insurance.models import (
    ActivityEvent, CsvUploadAttempt, GridDocument, LifeGridUpload, LifeRate, LockedPolicy,
    MissingMakeModelManualResolution, MISFile, PolicyDocumentUpload, RateMaster, RateOverlapScan,
    SpecialRateRequest, SupportTicket,
)
from insurance.overlap_utils import run_overlap_scan
from insurance.services.points_audit import _log_motor_points_search
from insurance.services.policy_documents import process_policy_document
from insurance.test_life_standard_file import LifeStandardViewMixin


class HookCase(TestCase):
    """Signs a user in and finds the events they left. Uploaded files go to a throwaway folder."""

    def setUp(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)

    def sign_in(self, username="rita", *groups):
        user = User.objects.create(username=username, first_name="Rita", last_name="Shah")
        for name in groups:
            user.groups.add(Group.objects.get_or_create(name=name)[0])
        self.client.force_login(user)
        return user

    def post_json(self, url_name, payload):
        return self.client.post(reverse(url_name), data=json.dumps(payload), content_type="application/json")

    def only_event(self, action):
        found = list(ActivityEvent.objects.filter(action=action))
        self.assertEqual(len(found), 1, f"{action}: events are {[e.action for e in ActivityEvent.objects.all()]}")
        return found[0]

    def assert_no_personal_data(self, *secrets):
        everything = json.dumps(list(ActivityEvent.objects.values(
            "summary", "action", "extra", "target_type", "target_id", "actor_username", "actor_name")), default=str)
        for secret in secrets:
            self.assertNotIn(secret, everything)


class EligibilityCheckEventTests(HookCase):
    def test_a_motor_search_is_recorded_without_its_values(self):
        user = self.sign_in("tester", "Can_View_Motor_Payout_Rates")
        response = self.client.get(reverse("motor_payout_rates"),
                                   {"vehicle_no": "MH12AB1234", "policy_holder_name": "Ravi Kumar"})
        self.assertEqual(response.status_code, 200)
        event = self.only_event("motor.eligibility.checked")
        self.assertEqual((event.actor, event.category), (user, "TEST"))
        self.assertIn("Motor Payout Rates", event.summary)
        self.assert_no_personal_data("MH12AB1234", "Ravi")

    def test_opening_the_page_without_searching_records_nothing(self):
        self.sign_in("tester", "Can_View_Motor_Payout_Rates")
        self.client.get(reverse("motor_payout_rates"))
        self.assertEqual(ActivityEvent.objects.count(), 0)

    def test_a_policy_lock_checker_search_is_told_apart_from_a_motor_one(self):
        user = self.sign_in("tester", "Can_View_Policy_Locker")
        response = self.client.get(reverse("policy_lock_checker"), {"vehicle_no": "MH12AB1234"})
        self.assertEqual(response.status_code, 200)
        event = self.only_event("policy_lock.checked")
        self.assertEqual((event.actor, event.category), (user, "TEST"))
        self.assert_no_personal_data("MH12AB1234")

    def test_a_health_search_is_recorded(self):
        user = self.sign_in("tester", "Can_View_Health_Payout_Rates")
        response = self.client.get(reverse("health_payout_rates"), {"policy_term": "1"})
        self.assertEqual(response.status_code, 200)
        event = self.only_event("health.eligibility.checked")
        self.assertEqual((event.actor, event.category), (user, "TEST"))

    def test_a_life_search_is_recorded_with_how_many_rows_it_found_and_nothing_it_searched_for(self):
        user = self.sign_in("tester", "Can_View_Life_Payout_Grid")
        response = self.client.get(reverse("life_payout_rates"), {"q": "Ravi Kumar"})
        self.assertEqual(response.status_code, 200)
        event = self.only_event("life.rates.searched")
        self.assertEqual((event.actor, event.category, event.extra), (user, "TEST", {"results": 0}))
        self.assert_no_personal_data("Ravi")

    def test_the_life_checker_opened_without_a_search_records_nothing(self):
        self.sign_in("tester", "Can_View_Life_Payout_Grid")
        self.client.get(reverse("life_payout_rates"))
        self.assertEqual(ActivityEvent.objects.count(), 0)

    def test_a_request_with_no_signed_in_user_records_no_event(self):
        # The policy-lock and health APIs reach the same search code with an API key and no person.
        request = RequestFactory().get("/api/policy-lock-checker/", {"vehicle_no": "MH12AB1234"})
        request.user = AnonymousUser()
        _log_motor_points_search(request)
        self.assertEqual(ActivityEvent.objects.count(), 0)


class UploadEventTests(HookCase):
    def test_uploading_a_mis_file_is_recorded_with_the_file_name(self):
        user = self.sign_in("uploader", "Can_View_MIS_Payout_Engine")
        with mock.patch("insurance.views.mis_payout.process_mis_mapping_task") as task:
            task.delay.return_value = mock.Mock(id="task-1")
            response = self.client.post(reverse("mis_payout_automation"),
                                        {"uploaded_file": SimpleUploadedFile("sept.csv", b"a,b\n1,2\n")})
        self.assertEqual(response.status_code, 302)
        event = self.only_event("mis.file.uploaded")
        self.assertEqual((event.actor, event.category, event.target_type), (user, "UPLOAD", "misfile"))
        self.assertIn("sept", event.summary)

    def test_a_finished_csv_upload_is_recorded_for_the_person_who_started_it(self):
        user = self.sign_in("importer", "Can_Upload_CSV")
        attempt = CsvUploadAttempt.objects.create(
            target_table="rate_master", file_hash="h" * 64, file_name="acko_sept.csv", uploaded_by=user)
        response = self.post_json("api_complete_upload", {"attempt_id": attempt.id, "success": True, "row_count": 120})
        self.assertEqual(response.status_code, 200)
        event = self.only_event("csv.upload.completed")
        self.assertEqual((event.actor, event.category), (user, "UPLOAD"))
        self.assertEqual(event.summary, "Uploaded 'acko_sept.csv' into Rate Master: 120 rows")
        self.assertEqual(event.extra, {"table": "rate_master", "rows": 120})

    def test_a_failed_csv_upload_is_recorded_as_failed(self):
        user = self.sign_in("importer", "Can_Upload_CSV")
        attempt = CsvUploadAttempt.objects.create(
            target_table="rto_master", file_hash="h" * 64, file_name="rto.csv", uploaded_by=user)
        self.post_json("api_complete_upload", {"attempt_id": attempt.id, "success": False})
        self.assertEqual(self.only_event("csv.upload.failed").summary, "Upload of 'rto.csv' into RTO Master failed")

    def test_someone_elses_upload_attempt_is_not_put_on_my_feed(self):
        owner = User.objects.create(username="owner")
        attempt = CsvUploadAttempt.objects.create(
            target_table="rate_master", file_hash="h" * 64, file_name="theirs.csv", uploaded_by=owner)
        self.sign_in("importer", "Can_Upload_CSV")
        self.post_json("api_complete_upload", {"attempt_id": attempt.id, "success": True, "row_count": 5})
        self.assertEqual(ActivityEvent.objects.count(), 0)

    def test_uploading_a_grid_and_moving_its_status_are_recorded(self):
        user = self.sign_in("gridder", "Can_View_Grid_Management")
        insurer = "ACKO GENERAL INSURANCE LIMITED"
        with mock.patch("insurance.views.grid_management.save_grid_document_file_task"), \
                mock.patch("insurance.views.grid_management.send_email_background"):
            self.client.post(reverse("grid_management"), {
                "insurer_name": insurer, "remarks": "", "uploaded_file": SimpleUploadedFile("acko.xlsx", b"x")})
        uploaded = self.only_event("grid.uploaded")
        self.assertEqual((uploaded.actor, uploaded.category), (user, "UPLOAD"))
        self.assertEqual(uploaded.summary, f"Uploaded the grid 'acko.xlsx' for {insurer}")

        doc = GridDocument.objects.get()
        self.client.post(reverse("grid_management"), {"action": "update_status", "doc_id": doc.id, "status": "DONE"})
        changed = self.only_event("grid.status.changed")
        self.assertEqual((changed.actor, changed.category), (user, "STATUS"))
        self.assertEqual(changed.summary, f"Changed the {insurer} grid from Pending to Done")
        self.assertEqual(changed.extra, {"from": "PENDING", "to": "DONE"})

        self.client.post(reverse("grid_management"), {"action": "update_status", "doc_id": doc.id, "status": "DONE"})
        self.assertEqual(ActivityEvent.objects.filter(action="grid.status.changed").count(), 1)   # no change, no event

    def test_a_policy_document_upload_never_carries_its_file_name(self):
        user = self.sign_in("scanner", "Can_View_OCR_Pipeline")
        with mock.patch("insurance.views.ocr_pipeline.process_policy_document_task"):
            self.client.post(reverse("upload_extract_pdf"), {
                "uploaded_file": SimpleUploadedFile("Ravi Kumar MH12AB1234.pdf", b"%PDF-1.4 test",
                                                    content_type="application/pdf")})
        event = self.only_event("ocr.document.uploaded")
        self.assertEqual((event.actor, event.category, event.target_type), (user, "UPLOAD", "policydocumentupload"))
        self.assert_no_personal_data("Ravi", "MH12AB1234", ".pdf")


class LifeGridEventTests(LifeStandardViewMixin, TestCase):
    """The Life Rate Master's upload actions (self.manager, who may open it, is signed in by the mixin)."""

    def upload(self, label, **fields):
        upload = LifeGridUpload.objects.create(label=label, file_name="grid.xlsx", uploaded_by=self.manager, **fields)
        LifeRate.objects.create(upload=upload, position=0, insurer="Acme Life", product="Plan", payout_yr1=10.0)
        upload.row_count, upload.insurer_count = 1, 1
        upload.save()
        return upload

    def test_a_standard_file_upload_is_recorded_as_not_live(self):
        self.post_standard(name="Life grid Nov 2026.xlsx")
        event = ActivityEvent.objects.get(action="life.grid.uploaded")
        self.assertEqual((event.actor, event.category), (self.manager, "UPLOAD"))
        self.assertIn("'Nov'26' for review", event.summary)
        self.assertEqual(event.extra["live"], False)

    def test_making_an_upload_live_discarding_and_deleting_are_recorded(self):
        live = self.live("Oct'26")
        pending = self.upload("Nov'26", is_pending=True)
        earlier = self.upload("Sep'26")

        self.client.post(reverse("life_rate_master_activate", args=[earlier.pk]))
        self.assertEqual(ActivityEvent.objects.get(action="life.grid.live").summary,
                         "Made the Life grid 'Sep'26' live")

        self.client.post(reverse("life_rate_master_discard", args=[pending.pk]))
        self.assertEqual(ActivityEvent.objects.get(action="life.grid.discarded").summary,
                         "Discarded the Life grid upload 'Nov'26' that was waiting for review")

        self.client.post(reverse("life_rate_master_delete_upload", args=[live.pk]))
        deleted = ActivityEvent.objects.get(action="life.grid.deleted")
        self.assertIn("Deleted an earlier upload 'Oct'26'", deleted.summary)
        self.assertEqual(deleted.category, "STATUS")

    def test_a_refused_delete_records_nothing(self):
        live = self.live("Oct'26")
        self.client.post(reverse("life_rate_master_delete_upload", args=[live.pk]))   # the live grid cannot be deleted
        self.assertFalse(ActivityEvent.objects.filter(action="life.grid.deleted").exists())


class RunEventTests(HookCase):
    def setUp(self):
        super().setUp()
        # process_mis_mapping closes the DB connection on entry and exit (it is built for a Celery worker);
        # inside TestCase's transaction that would poison every later query.
        patcher = mock.patch("insurance.mapping_engine.connection")
        patcher.start()
        self.addCleanup(patcher.stop)

    def mis_file(self, content, user):
        return MISFile.objects.create(uploaded_file=SimpleUploadedFile("jan.csv", content), uploaded_by=user)

    def test_a_finished_mis_run_is_recorded_with_its_counts_and_no_policy_data(self):
        user = User.objects.create(username="runner")
        RateMaster.objects.create(
            insurance_company="Acme General Insurance", status="ACTIVE", is_deleted="NO", po_type="On Net",
            po_net_rate=12.5, pi_type="On Net", pi_net_rate=20, po_od_rate=0, po_tp_rate=0, pi_od_rate=0, pi_tp_rate=0)
        csv = pd.DataFrame([
            {"Product": "motor", "Policy: insurance company": "Acme General Insurance",
             "Policy: policy no": "POLICY-9999", "Policy: vehicle registration number": "MH12AB1234"},
        ]).to_csv(index=False).encode("utf-8")
        mis = self.mis_file(csv, user)
        process_mis_mapping(mis.id)
        mis.refresh_from_db()
        self.assertEqual(mis.status, "COMPLETED", mis.error_message)

        event = self.only_event("mis.run.finished")
        self.assertEqual((event.actor, event.category, event.target_id), (user, "RUN", str(mis.pk)))
        self.assertIn("1 rows, 1 matched", event.summary)
        self.assertEqual(event.extra["rows"], 1)
        self.assertEqual(event.extra["matched"], 1)
        self.assert_no_personal_data("POLICY-9999", "MH12AB1234")

    def test_a_failed_mis_run_is_recorded_without_the_error_text(self):
        user = User.objects.create(username="runner")
        mis = self.mis_file(b"", user)                  # an empty file cannot be read as a table
        with self.assertLogs("insurance.mapping_engine", level="ERROR"):    # the expected traceback, kept out of the output
            process_mis_mapping(mis.id)
        mis.refresh_from_db()
        self.assertEqual(mis.status, "FAILED")
        event = self.only_event("mis.run.failed")
        self.assertEqual((event.actor, event.category), (user, "RUN"))
        self.assertNotIn(mis.error_message, event.summary)

    def test_a_run_with_no_uploader_records_nothing(self):
        mis = self.mis_file(b"", None)
        with self.assertLogs("insurance.mapping_engine", level="ERROR"):
            process_mis_mapping(mis.id)
        self.assertEqual(ActivityEvent.objects.count(), 0)

    def test_cancelling_a_run_is_recorded_under_whoever_cancelled_it(self):
        uploader = User.objects.create(username="uploader")
        canceller = self.sign_in("canceller", "Can_View_MIS_Payout_Engine")
        mis = MISFile.objects.create(
            uploaded_file=SimpleUploadedFile("jan.csv", b"a\n1\n"), uploaded_by=uploader, status="PROCESSING")
        response = self.client.post(reverse("cancel_mis_processing", args=[mis.id]))
        self.assertTrue(response.json()["success"])
        event = self.only_event("mis.run.cancelled")
        self.assertEqual((event.actor, event.category), (canceller, "RUN"))

    def test_cancelling_a_run_that_already_finished_records_nothing(self):
        self.sign_in("canceller", "Can_View_MIS_Payout_Engine")
        mis = MISFile.objects.create(uploaded_file=SimpleUploadedFile("jan.csv", b"a\n1\n"), status="COMPLETED")
        response = self.client.post(reverse("cancel_mis_processing", args=[mis.id]))
        self.assertFalse(response.json()["success"])
        self.assertEqual(ActivityEvent.objects.count(), 0)

    def test_starting_an_overlap_scan_is_recorded(self):
        user = self.sign_in("scanner", "Can_View_Rate_Master_Health")
        with mock.patch("insurance.views.rate_master_health.run_rate_overlap_scan_task"):
            self.client.post(reverse("start_overlap_scan"))
        event = self.only_event("overlap.scan.started")
        self.assertEqual((event.actor, event.category, event.target_type), (user, "RUN", "rateoverlapscan"))

    def test_a_scan_that_could_not_be_queued_is_recorded_as_failed(self):
        self.sign_in("scanner", "Can_View_Rate_Master_Health")
        with mock.patch("insurance.views.rate_master_health.run_rate_overlap_scan_task") as task:
            task.delay.side_effect = ConnectionError("redis is down")
            with self.assertLogs("security", level="ERROR"):
                self.client.post(reverse("start_overlap_scan"))
        self.assertEqual(self.only_event("overlap.scan.failed").category, "RUN")
        self.assertFalse(ActivityEvent.objects.filter(action="overlap.scan.started").exists())

    def test_a_finished_overlap_scan_is_recorded_for_the_person_who_started_it(self):
        user = User.objects.create(username="scanner")
        scan = RateOverlapScan.objects.create(triggered_by=user)
        run_overlap_scan(scan.id)
        event = self.only_event("overlap.scan.finished")
        self.assertEqual((event.actor, event.category), (user, "RUN"))
        self.assertEqual(event.summary, "Overlap scan finished: 0 groups, 0 conflicts")

    def test_a_scan_that_blows_up_is_recorded_as_failed_and_still_raises(self):
        user = User.objects.create(username="scanner")
        scan = RateOverlapScan.objects.create(triggered_by=user)
        with mock.patch("insurance.overlap_utils.load_active_groups", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                run_overlap_scan(scan.id)
        scan.refresh_from_db()
        self.assertEqual(scan.status, RateOverlapScan.STATUS_FAILED)
        self.assertEqual(self.only_event("overlap.scan.failed").actor, user)

    def test_a_scan_run_from_the_command_line_has_no_person_and_records_nothing(self):
        scan = RateOverlapScan.objects.create()
        run_overlap_scan(scan.id)
        self.assertEqual(ActivityEvent.objects.count(), 0)

    def test_ocr_extraction_is_recorded_for_the_uploader_without_naming_the_document(self):
        user = User.objects.create(username="scanner")
        doc = PolicyDocumentUpload.objects.create(
            uploaded_file=SimpleUploadedFile("Ravi Kumar.pdf", b"%PDF-1.4"), uploaded_by=user,
            original_filename="Ravi Kumar.pdf")
        with mock.patch("insurance.services.policy_documents.extract_data_with_gemini",
                        return_value={"policy_number": "P-1", "insured_name": "Ravi Kumar"}):
            process_policy_document(doc)
        event = self.only_event("ocr.document.extracted")
        self.assertEqual((event.actor, event.category), (user, "RUN"))
        self.assert_no_personal_data("Ravi", "P-1")

    def test_a_failed_ocr_extraction_is_recorded_without_the_error_text(self):
        user = User.objects.create(username="scanner")
        doc = PolicyDocumentUpload.objects.create(
            uploaded_file=SimpleUploadedFile("a.pdf", b"%PDF-1.4"), uploaded_by=user)
        with mock.patch("insurance.services.policy_documents.extract_data_with_gemini",
                        side_effect=RuntimeError("could not read Ravi Kumar")):
            with self.assertLogs("insurance.services.policy_documents", level="ERROR"):
                process_policy_document(doc)
        self.assertEqual(self.only_event("ocr.document.failed").actor, user)
        self.assert_no_personal_data("Ravi")


class StatusEventTests(HookCase):
    def test_opening_and_moving_a_ticket_are_recorded_without_its_text(self):
        user = self.sign_in("raiser", "Can_View_Tickets")
        self.post_json("create_ticket_api", {
            "remarks": "Wrong rate for MH12AB1234, owner Ravi", "category": "MOTOR",
            "form_payload": {"vehicle_no": "MH12AB1234"}})
        ticket = SupportTicket.objects.get()
        opened = self.only_event("ticket.opened")
        self.assertEqual((opened.actor, opened.category), (user, "STATUS"))
        self.assertEqual(opened.summary, f"Opened Motor support ticket #{ticket.id}")
        self.assert_no_personal_data("MH12AB1234", "Ravi")

        self.post_json("update_ticket_status", {"ticket_id": ticket.id, "status": "closed"})
        changed = self.only_event("ticket.status.changed")
        self.assertEqual(changed.summary, f"Changed support ticket #{ticket.id} from OPEN to CLOSED")
        self.post_json("update_ticket_status", {"ticket_id": ticket.id, "status": "CLOSED"})
        self.assertEqual(ActivityEvent.objects.filter(action="ticket.status.changed").count(), 1)

    def test_submitting_and_reviewing_a_special_rate_request_are_recorded_without_the_entry_number(self):
        user = self.sign_in("requester", "Can_View_Special_Rates", "ADMIN")
        self.client.post(reverse("special_rate_requests"), {
            "submit_bg": "1", "bg-product": "MOTOR", "bg-entry_no": "ENT-SECRET-77", "bg-remarks": "please"})
        request = SpecialRateRequest.objects.get()
        submitted = self.only_event("special_rate.requested")
        self.assertEqual((submitted.actor, submitted.category, submitted.summary),
                         (user, "STATUS", "Submitted a Special BG rate request"))
        self.assert_no_personal_data("ENT-SECRET-77")

        self.post_json("update_special_rate_request_status", {"request_id": request.id, "status": "approved"})
        reviewed = self.only_event("special_rate.reviewed")
        self.assertEqual(reviewed.summary, "Set a Special BG rate request to Approved")
        self.assertEqual(reviewed.extra, {"from": "PENDING", "to": "APPROVED", "rate_type": "BG"})

    def test_locking_a_policy_records_the_product_and_insurer_but_not_the_vehicle_or_holder(self):
        user = self.sign_in("locker", "Can_View_Policy_Locker")
        rate = RateMaster.objects.create(
            insurance_company="Acme General", status="ACTIVE", is_deleted="NO", po_type="On Net", po_net_rate=10)
        response = self.client.post(reverse("lock_unlock_policy", args=[rate.id]), {
            "action_type": "LOCK", "vehicle_no": "MH12AB1234", "policy_holder_name": "Ravi Kumar",
            "target_date": date.today().strftime("%Y-%m-%d")})
        self.assertTrue(response.json()["success"], response.json())
        event = self.only_event("policy.locked")
        self.assertEqual((event.actor, event.category), (user, "STATUS"))
        self.assertEqual(event.summary, "Locked a policy with Acme General")
        self.assertTrue(LockedPolicy.objects.filter(vehicle_no="MH12AB1234").exists())
        self.assert_no_personal_data("MH12AB1234", "Ravi")

    def test_marking_and_unmarking_a_missing_make_model_resolved_are_recorded(self):
        user = self.sign_in("resolver", "Can_View_Missing_Make_Model")
        fields = {"mm_mark_value": "Hero Splendor", "mm_mark_product": "Two Wheeler"}
        self.client.post(reverse("mark_missing_make_model_resolved"), fields)
        marked = self.only_event("missing_make_model.marked_resolved")
        self.assertEqual((marked.actor, marked.category), (user, "STATUS"))
        self.assertEqual(marked.summary, "Marked missing make/model 'hero splendor' as resolved")

        self.client.post(reverse("mark_missing_make_model_resolved"), fields)         # already marked: nothing new
        self.assertEqual(ActivityEvent.objects.filter(action="missing_make_model.marked_resolved").count(), 1)

        self.client.post(reverse("unmark_missing_make_model_resolved"), fields)
        self.assertEqual(self.only_event("missing_make_model.reopened").actor, user)
        self.assertFalse(MissingMakeModelManualResolution.objects.exists())

    def test_marking_several_missing_make_models_resolved_at_once_is_one_event(self):
        self.sign_in("resolver", "Can_View_Missing_Make_Model")
        keys = [json.dumps({"value": value, "product": "Two Wheeler"}) for value in ("Hero Splendor", "Honda Activa")]
        self.client.post(reverse("bulk_mark_missing_make_model_resolved"), {"group_keys": keys})
        event = self.only_event("missing_make_model.marked_resolved")
        self.assertEqual(event.summary, "Marked 2 missing make/model row(s) as resolved")

    def test_adding_a_missing_make_model_to_the_master_is_recorded_as_resolving_it(self):
        user = self.sign_in("resolver", "Can_View_Missing_Make_Model")
        self.client.post(reverse("add_missing_make_model_to_master"), {
            "make_model_value": "Hero Splendor Plus", "target": "new", "new_name": "HERO SPLENDOR", "policy_count": "4"})
        event = self.only_event("missing_make_model.resolved")
        self.assertEqual((event.actor, event.category), (user, "STATUS"))
        self.assertEqual(event.summary, "Resolved missing make/model 'HERO SPLENDOR PLUS' by adding it to 'HERO SPLENDOR'")
        self.assertEqual(event.extra, {"policy_rows": 4})
