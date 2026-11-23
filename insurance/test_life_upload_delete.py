"""Deleting an upload from the Uploads tab of the Life Rate Master, so test uploads can be cleaned up: an earlier upload
or one waiting for review goes, with its rows and notes; the live grid is refused (the Rate Checker and the MIS engine
read it), so another upload has to be made live first."""
from django.test import TestCase
from django.urls import reverse

from insurance.models import AuditLog, LifeGridUpload, LifeInsurerNote, LifeRate
from insurance.services.life_rates import active_life_upload, delete_life_upload
from insurance.test_life_standard_file import LifeStandardViewMixin


def _upload(label, *, live=False, pending=False, rows=2, user=None, file_name="grid.xlsx"):
    """An upload with `rows` payout rows and one insurer note."""
    upload = LifeGridUpload.objects.create(label=label, file_name=file_name, is_active=live, is_pending=pending,
                                           uploaded_by=user)
    for position in range(rows):
        LifeRate.objects.create(upload=upload, position=position, insurer="Acme Life", product=f"Plan {position}",
                                payout_yr1=10.0 + position)
    LifeInsurerNote.objects.create(upload=upload, insurer="Acme Life", text=f"Terms of {label}")
    upload.row_count, upload.insurer_count = rows, 1
    upload.save()
    return upload


def _last_log():
    return AuditLog.objects.filter(action="LIFE GRID UPLOAD").latest("id")


class DeleteUploadServiceTests(TestCase):
    def test_an_earlier_upload_goes_with_its_rows_and_notes_and_the_live_grid_stays(self):
        live = _upload("Nov'26", live=True)
        old = _upload("Oct'26")
        delete_life_upload(old)
        self.assertFalse(LifeGridUpload.objects.filter(pk=old.pk).exists())
        self.assertEqual(LifeRate.objects.filter(upload=old.pk).count(), 0)
        self.assertEqual(LifeInsurerNote.objects.filter(upload=old.pk).count(), 0)
        self.assertEqual(active_life_upload(), live)
        self.assertEqual((live.rates.count(), live.notes.count()), (2, 1))

    def test_an_upload_waiting_for_review_can_be_deleted_too(self):
        pending = _upload("Dec'26", pending=True)
        delete_life_upload(pending)
        self.assertFalse(LifeGridUpload.objects.filter(pk=pending.pk).exists())

    def test_the_live_grid_is_refused_and_nothing_is_deleted(self):
        live = _upload("Nov'26", live=True)
        with self.assertRaisesMessage(ValueError, "live grid"):
            delete_life_upload(live)
        self.assertTrue(LifeGridUpload.objects.filter(pk=live.pk).exists())
        self.assertEqual((live.rates.count(), live.notes.count()), (2, 1))

    def test_the_database_decides_not_the_instance_the_page_was_loaded_with(self):
        """The page saw the upload as old; meanwhile another tab made it live."""
        old = _upload("Oct'26")
        stale = LifeGridUpload.objects.get(pk=old.pk)
        LifeGridUpload.objects.filter(pk=old.pk).update(is_active=True)
        with self.assertRaisesMessage(ValueError, "live grid"):
            delete_life_upload(stale)
        self.assertTrue(LifeGridUpload.objects.filter(pk=old.pk).exists())

    def test_an_upload_that_is_already_gone_is_reported_not_crashed_on(self):
        gone = LifeGridUpload.objects.get(pk=_upload("Oct'26").pk)
        gone.delete()
        with self.assertRaisesMessage(ValueError, "already been deleted"):
            delete_life_upload(gone)


class DeleteUploadViewTests(LifeStandardViewMixin, TestCase):
    def post(self, upload, **extra):
        return self.client.post(reverse("life_rate_master_delete_upload", args=[upload.pk]), **extra)

    def test_deleting_an_old_upload_from_the_uploads_page(self):
        live = _upload("Nov'26", live=True, user=self.manager)
        old = _upload("Oct'26", user=self.manager, file_name="Life_Grid_working-Oct'26.xlsx", rows=3)
        response = self.post(old, follow=True)
        self.assertRedirects(response, reverse("life_rate_master_uploads"))
        self.assertContains(response, "was deleted with its 3 rows")
        self.assertFalse(LifeGridUpload.objects.filter(pk=old.pk).exists())
        self.assertEqual(LifeRate.objects.filter(upload=old.pk).count(), 0)
        self.assertEqual(active_life_upload(), live)
        self.assertEqual(live.rates.count(), 2)
        log = _last_log()
        self.assertEqual(log.user, self.manager)
        self.assertIn("Deleted an earlier upload 'Oct'26'", log.details)
        self.assertIn("Life_Grid_working-Oct'26.xlsx", log.details)
        self.assertIn("3 rows", log.details)

    def test_an_upload_waiting_for_review_can_be_deleted_and_is_logged_as_such(self):
        live = _upload("Nov'26", live=True)
        pending = _upload("Dec'26", pending=True)
        self.post(pending)
        self.assertFalse(LifeGridUpload.objects.filter(pk=pending.pk).exists())
        self.assertIn("an upload that was waiting for review", _last_log().details)
        self.assertEqual(active_life_upload(), live)

    def test_the_live_grid_cannot_be_deleted(self):
        live = _upload("Nov'26", live=True)
        response = self.post(live, follow=True)
        self.assertRedirects(response, reverse("life_rate_master_uploads"))
        self.assertContains(response, "This is the live grid, so it cannot be deleted")
        self.assertEqual((live.rates.count(), live.notes.count()), (2, 1))
        self.assertFalse(AuditLog.objects.filter(action="LIFE GRID UPLOAD").exists())

    def test_uploads_with_the_same_name_are_told_apart_by_which_one_was_clicked(self):
        """Three uploads of the same file are all called Oct'26."""
        live = _upload("Oct'26", live=True)
        first = _upload("Oct'26")
        second = _upload("Oct'26")
        self.post(first)
        self.assertEqual(set(LifeGridUpload.objects.values_list("pk", flat=True)), {live.pk, second.pk})

    def test_get_is_refused_and_an_unknown_upload_is_not_found(self):
        old = _upload("Oct'26")
        self.assertEqual(self.client.get(reverse("life_rate_master_delete_upload", args=[old.pk])).status_code, 405)
        self.assertTrue(LifeGridUpload.objects.filter(pk=old.pk).exists())
        self.assertEqual(self.client.post(reverse("life_rate_master_delete_upload", args=[99999])).status_code, 404)

    def test_only_the_rate_master_group_may_delete_an_upload(self):
        old = _upload("Oct'26")
        self.client.force_login(self.viewer)                      # the Rate Checker group only
        self.assertEqual(self.post(old).status_code, 403)
        self.client.logout()
        self.assertEqual(self.post(old).status_code, 302)         # sent to sign in
        self.assertTrue(LifeGridUpload.objects.filter(pk=old.pk).exists())

    def test_the_uploads_page_offers_delete_on_an_old_upload_and_not_on_the_live_one(self):
        live = _upload("Nov'26", live=True)
        old = _upload("Oct'26", file_name="old.xlsx")
        pending = _upload("Dec'26", pending=True)
        response = self.client.get(reverse("life_rate_master_uploads"))
        self.assertContains(response, reverse("life_rate_master_delete_upload", args=[old.pk]))
        self.assertContains(response, "Its rows and notes are deleted for good")        # the confirm
        self.assertNotContains(response, reverse("life_rate_master_delete_upload", args=[live.pk]))
        self.assertNotContains(response, reverse("life_rate_master_delete_upload", args=[pending.pk]))  # it has Discard
        self.assertContains(response, reverse("life_rate_master_discard", args=[pending.pk]))
        self.assertContains(response, "life-btn-off")           # the live upload's Delete is shown, greyed out
        self.assertContains(response, "make another upload live first")

    def test_a_page_that_was_showing_the_deleted_upload_falls_back_to_the_live_one(self):
        live = _upload("Nov'26", live=True)
        old = _upload("Oct'26")
        url = reverse("life_rate_master")
        self.assertEqual(self.client.get(url, {"upload": old.pk}).context["upload"], old)
        self.post(old)
        self.assertEqual(self.client.get(url, {"upload": old.pk}).context["upload"], live)

    def test_the_rate_checker_still_reads_the_live_grid_after_a_delete(self):
        live = _upload("Nov'26", live=True)
        old = _upload("Oct'26")
        self.post(old)
        response = self.client.get(reverse("life_payout_rates"), {"q": "Plan"})
        self.assertEqual(response.context["upload"], live)
        self.assertEqual(response.context["total_found"], 2)
