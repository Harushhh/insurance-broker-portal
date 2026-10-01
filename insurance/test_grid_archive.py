import os
import shutil
import tempfile
import time
from datetime import date, datetime, timedelta
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from insurance import grid_archive
from insurance.models import GridArchiveBatch, GridDocument

HDFC = "HDFC ERGO GENERAL INSURANCE CO LTD"
OCT_1 = date(2026, 10, 1)


def local(year, month, day, hour=12):
    return timezone.make_aware(datetime(year, month, day, hour))


class RetentionCutoffTests(SimpleTestCase):
    def test_three_months_on_the_first_of_october_keeps_aug_sep_oct(self):
        self.assertEqual(grid_archive.retention_cutoff(3, OCT_1), local(2026, 8, 1, 0))

    def test_wraps_back_across_the_year(self):
        self.assertEqual(grid_archive.retention_cutoff(3, date(2026, 1, 15)), local(2025, 11, 1, 0))

    def test_one_month_keeps_only_the_current_month(self):
        self.assertEqual(grid_archive.retention_cutoff(1, date(2026, 10, 31)), local(2026, 10, 1, 0))

    def test_rejects_a_window_smaller_than_one_month(self):
        with self.assertRaises(ValueError):
            grid_archive.retention_cutoff(0, OCT_1)


class GuessInsurerTests(SimpleTestCase):
    CHOICES = [
        HDFC,
        "ICICI LOMBARD GENERAL INSURANCE CO LTD",
        "UNITED INDIA INSURANCE CO LTD",
        "THE NEW INDIA ASSURANCE CO LTD",
    ]

    def test_unique_match_ignores_case_separators_and_extra_words(self):
        self.assertEqual(grid_archive.guess_insurer("grid_documents/2026/09/hdfc_ERGO-motor.grid.xlsx", self.CHOICES), HDFC)

    def test_partial_name_is_not_a_match(self):
        # "india" alone fits two insurers and "new india" is missing "assurance":
        # an unknown beats a wrong name that can't be edited afterwards.
        self.assertIsNone(grid_archive.guess_insurer("grid_documents/2026/09/new_india.xlsx", self.CHOICES))

    def test_no_match(self):
        self.assertIsNone(grid_archive.guess_insurer("grid_documents/2026/09/mystery.xlsx", self.CHOICES))

    def test_two_insurers_named_in_one_file_is_ambiguous(self):
        self.assertIsNone(grid_archive.guess_insurer("grid_documents/2026/09/hdfc_ergo_icici_lombard.xlsx", self.CHOICES))


class GridStorageTestCase(TestCase):
    """Live and archive storage both on temp dirs; the archive counts as "remote"."""

    def setUp(self):
        self.live_dir = tempfile.mkdtemp()
        self.archive_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.live_dir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.archive_dir, ignore_errors=True)
        fs = "django.core.files.storage.FileSystemStorage"
        override = override_settings(
            USE_S3_STORAGE=True,
            STORAGES={
                "default": {"BACKEND": fs, "OPTIONS": {"location": self.live_dir}},
                "grid_archive": {"BACKEND": fs, "OPTIONS": {"location": self.archive_dir}},
                "staticfiles": settings.STORAGES["staticfiles"],
            },
        )
        override.enable()
        self.addCleanup(override.disable)
        self.archive = grid_archive.get_archive_storage()

    def make_doc(self, name, uploaded, content=b"grid bytes", insurer=HDFC, **fields):
        doc = GridDocument.objects.create(
            insurer_name=insurer, uploaded_file=ContentFile(content, name=name), **fields
        )
        GridDocument.objects.filter(pk=doc.pk).update(uploaded_date=uploaded)
        doc.refresh_from_db()
        return doc

    def ids(self):
        return set(GridDocument.objects.values_list("id", flat=True))

    def manifests(self, label):
        return [f for f in self.archive.listdir(label)[1] if f.startswith("manifest-")]


class ArchiveTests(GridStorageTestCase):
    def test_moves_old_months_to_the_archive_and_keeps_the_active_window(self):
        jul1 = self.make_doc("jul1.xlsx", local(2026, 7, 3), b"july one")
        jul2 = self.make_doc("jul2.xlsx", local(2026, 7, 28), b"july two")
        aug = self.make_doc("aug.xlsx", local(2026, 8, 1, 0))  # first instant of the window
        sep = self.make_doc("sep.xlsx", local(2026, 9, 15))
        keys = {d.id: d.uploaded_file.name for d in (jul1, jul2)}

        results = grid_archive.run_retention(today=OCT_1)

        self.assertEqual([r["month"] for r in results], ["2026-07"])
        self.assertEqual(results[0]["status"], "COMPLETED")
        self.assertEqual(self.ids(), {aug.id, sep.id})
        for doc, content in ((jul1, b"july one"), (jul2, b"july two")):
            key = keys[doc.id]
            self.assertFalse(default_storage.exists(key), "original should be deleted after archiving")
            self.assertEqual(grid_archive._read(self.archive, f"2026-07/files/{key}"), content)
        self.assertTrue(default_storage.exists(aug.uploaded_file.name))
        self.assertEqual(len(self.manifests("2026-07")), 1)

        batch = GridArchiveBatch.objects.get()
        self.assertEqual((batch.month, batch.status), (date(2026, 7, 1), "COMPLETED"))
        self.assertEqual((batch.documents_archived, batch.files_archived), (2, 2))
        self.assertEqual(batch.bytes_archived, len(b"july one") + len(b"july two"))
        self.assertTrue(batch.manifest_key.startswith("2026-07/manifest-"))

    def test_catches_up_every_month_that_fell_out_of_the_window(self):
        self.make_doc("jun.xlsx", local(2026, 6, 10))
        self.make_doc("jul.xlsx", local(2026, 7, 10))
        results = grid_archive.run_retention(today=OCT_1)
        self.assertEqual([r["month"] for r in results], ["2026-06", "2026-07"])
        self.assertEqual(GridDocument.objects.count(), 0)

    def test_dry_run_changes_nothing(self):
        doc = self.make_doc("jul.xlsx", local(2026, 7, 10))
        results = grid_archive.run_retention(dry_run=True, today=OCT_1)
        self.assertEqual((results[0]["status"], results[0]["found"], results[0]["files"]), ("DRY_RUN", 1, 1))
        self.assertEqual(self.ids(), {doc.id})
        self.assertTrue(default_storage.exists(doc.uploaded_file.name))
        self.assertEqual(GridArchiveBatch.objects.count(), 0)
        self.assertEqual(grid_archive.list_archive_months(), [])

    def test_a_file_that_fails_verification_stays_put(self):
        good = self.make_doc("good.xlsx", local(2026, 7, 10))
        bad = self.make_doc("bad.xlsx", local(2026, 7, 11))
        real = grid_archive._put_verified

        def flaky(storage, key, data, overwrite=True):
            if "bad" in key:
                raise RuntimeError("read-back mismatch")
            return real(storage, key, data, overwrite)

        with mock.patch.object(grid_archive, "_put_verified", flaky):
            (result,) = grid_archive.run_retention(today=OCT_1)

        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(self.ids(), {bad.id})
        self.assertTrue(default_storage.exists(bad.uploaded_file.name))
        self.assertFalse(default_storage.exists(good.uploaded_file.name))
        batch = GridArchiveBatch.objects.get()
        self.assertEqual(batch.status, "PARTIAL")
        self.assertIn(f"GridDocument {bad.id}", batch.error_message)

    def test_later_run_adds_a_second_manifest_and_restore_reads_both(self):
        good = self.make_doc("good.xlsx", local(2026, 7, 10), b"good")
        bad = self.make_doc("bad.xlsx", local(2026, 7, 11), b"bad")
        real = grid_archive._put_verified

        def flaky(storage, key, data, overwrite=True):
            if "bad" in key:
                raise RuntimeError("boom")
            return real(storage, key, data, overwrite)

        with mock.patch.object(grid_archive, "_put_verified", flaky):
            grid_archive.run_retention(today=OCT_1)
        grid_archive.run_retention(today=OCT_1)

        self.assertEqual(GridDocument.objects.count(), 0)
        self.assertEqual(len(self.manifests("2026-07")), 2, "the second run must not overwrite the first manifest")

        result = grid_archive.restore_from_archive()
        self.assertEqual(result["restored"], 2)
        self.assertEqual(
            {GridDocument.objects.get(pk=pk).uploaded_file.read() for pk in self.ids()}, {b"good", b"bad"}
        )

    def test_missing_file_is_archived_as_metadata_only(self):
        doc = self.make_doc("lost.xlsx", local(2026, 7, 10), remarks="keep me")
        default_storage.delete(doc.uploaded_file.name)
        (result,) = grid_archive.run_retention(today=OCT_1)
        self.assertEqual((result["status"], result["documents"], result["files"]), ("COMPLETED", 1, 0))
        self.assertEqual(GridDocument.objects.count(), 0)
        entry = grid_archive._load_entries(self.archive, "2026-07")[0]
        self.assertEqual((entry["remarks"], entry["archive_key"]), ("keep me", ""))
        self.assertIn("missing", entry["note"])

    def test_stops_after_consecutive_failures_and_deletes_nothing(self):
        for i in range(5):
            self.make_doc(f"doc{i}.xlsx", local(2026, 7, 10 + i))
        with mock.patch.object(grid_archive, "_put_verified", side_effect=RuntimeError("R2 down")) as put:
            (result,) = grid_archive.run_retention(today=OCT_1)
        self.assertEqual(put.call_count, grid_archive.MAX_CONSECUTIVE_FAILURES)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(GridDocument.objects.count(), 5)
        self.assertEqual(grid_archive.list_archive_months(), [], "no manifest should be written for a failed month")
        self.assertEqual(GridArchiveBatch.objects.get().status, "FAILED")

    def test_refuses_to_purge_when_the_archive_is_not_on_r2(self):
        doc = self.make_doc("jul.xlsx", local(2026, 7, 10))
        with override_settings(USE_S3_STORAGE=False):
            with self.assertRaises(grid_archive.ArchiveNotConfigured):
                grid_archive.run_retention(today=OCT_1)
            grid_archive.run_retention(dry_run=True, today=OCT_1)  # previewing is always fine
            self.assertEqual(self.ids(), {doc.id})
            grid_archive.run_retention(allow_local_archive=True, today=OCT_1)
        self.assertEqual(GridDocument.objects.count(), 0)


class RestoreFromArchiveTests(GridStorageTestCase):
    def test_round_trip_restores_every_field_and_is_idempotent(self):
        user = get_user_model().objects.create_user("uploader", password="x")
        original = self.make_doc(
            "grid.xlsx", local(2026, 7, 3, 9), b"original bytes",
            remarks="special terms", work_effected_date=date(2026, 7, 5),
            status="DONE", uploaded_by=user,
        )
        key = original.uploaded_file.name
        grid_archive.run_retention(today=OCT_1)
        self.assertEqual(GridDocument.objects.count(), 0)

        # The database - including the batch log - is lost; only R2 remains.
        GridArchiveBatch.objects.all().delete()

        result = grid_archive.restore_from_archive()
        self.assertEqual((result["restored"], result["errors"]), (1, []))
        restored = GridDocument.objects.get()
        self.assertEqual(
            (restored.insurer_name, restored.remarks, restored.work_effected_date, restored.status,
             restored.uploaded_by, restored.uploaded_date, restored.uploaded_file.name),
            (HDFC, "special terms", date(2026, 7, 5), "DONE", user, local(2026, 7, 3, 9), key),
        )
        self.assertEqual(restored.uploaded_file.read(), b"original bytes")

        again = grid_archive.restore_from_archive()
        self.assertEqual((again["restored"], again["already_present"]), (0, 1))
        self.assertEqual(GridDocument.objects.count(), 1)

    def test_only_restores_the_requested_month(self):
        self.make_doc("jun.xlsx", local(2026, 6, 10))
        self.make_doc("jul.xlsx", local(2026, 7, 10))
        grid_archive.run_retention(today=OCT_1)
        result = grid_archive.restore_from_archive(months=["2026-07"])
        self.assertEqual(result["restored"], 1)
        self.assertIn("jul", GridDocument.objects.get().uploaded_file.name)

    def test_dry_run_restores_nothing(self):
        self.make_doc("jul.xlsx", local(2026, 7, 10))
        grid_archive.run_retention(today=OCT_1)
        result = grid_archive.restore_from_archive(dry_run=True)
        self.assertEqual(result["restored"], 1)
        self.assertEqual(GridDocument.objects.count(), 0)

    def test_a_corrupted_archive_copy_is_refused(self):
        self.make_doc("jul.xlsx", local(2026, 7, 10), b"good")
        grid_archive.run_retention(today=OCT_1)
        (entry,) = grid_archive._load_entries(self.archive, "2026-07")
        self.archive.delete(entry["archive_key"])
        self.archive.save(entry["archive_key"], ContentFile(b"tampered"))

        result = grid_archive.restore_from_archive()
        self.assertEqual(result["restored"], 0)
        self.assertIn("checksum", result["errors"][0])
        self.assertEqual(GridDocument.objects.count(), 0)
        self.assertFalse(default_storage.exists(entry["original_key"]))

    def test_never_overwrites_a_different_file_already_in_storage(self):
        doc = self.make_doc("jul.xlsx", local(2026, 7, 10), b"archived")
        key = doc.uploaded_file.name
        grid_archive.run_retention(today=OCT_1)
        default_storage.save(key, ContentFile(b"someone else's file"))

        result = grid_archive.restore_from_archive()
        self.assertEqual(result["restored"], 0)
        self.assertIn("different content", result["errors"][0])
        self.assertEqual(grid_archive._read(default_storage, key), b"someone else's file")


class RestoreOrphansTests(GridStorageTestCase):
    def put(self, name, age):
        key = default_storage.save(f"grid_documents/2026/09/{name}", ContentFile(b"x"))
        ts = time.time() - age.total_seconds()
        os.utime(os.path.join(self.live_dir, key), (ts, ts))
        return key

    def test_restores_unreferenced_files_inside_the_window_only(self):
        wanted = self.put("HDFC_Ergo_Grid.xlsx", timedelta(days=5))
        unknown = self.put("mystery.xlsx", timedelta(days=6))
        self.put("ancient.xlsx", timedelta(days=200))
        self.put("just_uploaded.xlsx", timedelta(seconds=30))
        tracked = self.make_doc("tracked.xlsx", local(2026, 9, 1))

        result = grid_archive.restore_orphans()

        self.assertEqual(
            (result["restored"], result["already_present"], result["outside_window"], result["too_recent"]),
            (2, 1, 1, 1),
        )
        rows = {d.uploaded_file.name: d for d in GridDocument.objects.exclude(pk=tracked.pk)}
        self.assertEqual(set(rows), {wanted, unknown})
        self.assertEqual(rows[wanted].insurer_name, HDFC)
        self.assertIn("guessed", rows[wanted].remarks)
        self.assertEqual(rows[unknown].insurer_name, grid_archive.UNKNOWN_INSURER)
        self.assertEqual((rows[wanted].status, rows[wanted].uploaded_by), ("PENDING", None))
        age = timezone.now() - rows[wanted].uploaded_date
        self.assertAlmostEqual(age.total_seconds(), timedelta(days=5).total_seconds(), delta=60)

        again = grid_archive.restore_orphans()
        self.assertEqual((again["restored"], again["already_present"]), (0, 3))

    def test_dry_run_creates_no_rows(self):
        self.put("HDFC_Ergo_Grid.xlsx", timedelta(days=5))
        result = grid_archive.restore_orphans(dry_run=True)
        self.assertEqual((result["restored"], len(result["rows"])), (1, 1))
        self.assertEqual(GridDocument.objects.count(), 0)

    def test_all_ignores_the_window_and_status_is_configurable(self):
        self.put("ancient.xlsx", timedelta(days=200))
        grid_archive.restore_orphans(include_all=True, status="DONE")
        self.assertEqual(GridDocument.objects.get().status, "DONE")

    def test_nothing_in_storage_is_not_an_error(self):
        result = grid_archive.restore_orphans()
        self.assertEqual(result["restored"], 0)


class WiringTests(GridStorageTestCase):
    def test_beat_schedule_points_at_a_real_task(self):
        from insurance import tasks
        entry = settings.CELERY_BEAT_SCHEDULE["archive-old-grid-documents"]
        self.assertEqual(entry["task"], tasks.archive_old_grid_documents.name)
        self.assertEqual(entry["schedule"].day_of_month, {1})

    def test_task_runs_the_retention_job(self):
        from insurance import tasks
        self.make_doc("ancient.xlsx", local(2020, 1, 10))
        result = tasks.archive_old_grid_documents()
        self.assertEqual([r["month"] for r in result], ["2020-01"])
        self.assertEqual(GridDocument.objects.count(), 0)

    def test_archive_command_dry_run(self):
        self.make_doc("ancient.xlsx", local(2020, 1, 10))
        out = StringIO()
        call_command("archive_grid_documents", "--dry-run", stdout=out)
        self.assertIn("2020-01: DRY_RUN", out.getvalue())
        self.assertEqual(GridDocument.objects.count(), 1)

    def test_archive_command_exits_non_zero_when_something_was_left_behind(self):
        self.make_doc("ancient.xlsx", local(2020, 1, 10))
        with mock.patch.object(grid_archive, "_put_verified", side_effect=RuntimeError("R2 down")):
            with self.assertRaises(CommandError):
                call_command("archive_grid_documents", stdout=StringIO(), stderr=StringIO())
        self.assertEqual(GridDocument.objects.count(), 1)

    def test_restore_command_reports_when_nothing_was_lost(self):
        self.make_doc("recent.xlsx", timezone.now())
        out = StringIO()
        call_command("restore_grid_documents", "--dry-run", stdout=out)
        self.assertIn("would restore 0", out.getvalue())

    def test_restore_command_rejects_an_unknown_archive_month(self):
        with self.assertRaises(CommandError):
            call_command("restore_grid_documents", "--source", "archive", "--only", "2026-07", stdout=StringIO())
