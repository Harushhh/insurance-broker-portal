"""
PERMANENTLY deletes every RateMaster row where is_deleted="YES" -- not a
soft-delete, an actual DELETE FROM. This is irreversible; there is no
--undo. Take a database backup before running this with --apply.

Unlike dedupe_rate_master (which only ever flips is_deleted), this
removes rows outright -- including any that were soft-deleted long ago
for reasons unrelated to the duplicate-upload bug this thread fixed, not
just the ones that cleanup pass touched.

LockedPolicy.source_rate points at RateMaster with on_delete=SET_NULL, so
deleting a row that's still referenced by a locked policy just nulls that
reference rather than cascading or failing.

Dry-run by default: prints the count and writes nothing. Pass --apply to
actually delete.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from insurance.models import AuditLog, RateMaster

BATCH_SIZE = 2000


class Command(BaseCommand):
    help = "PERMANENTLY delete every RateMaster row with is_deleted=YES (dry-run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually perform the permanent delete. Without this, only reports the count.",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]

        qs = RateMaster.objects.filter(is_deleted="YES")
        total = qs.count()

        self.stdout.write(f"RateMaster rows with is_deleted=YES: {total:,}")

        if not total:
            self.stdout.write(self.style.SUCCESS("Nothing to do."))
            return

        if not apply_changes:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Dry run only -- nothing was written. Re-run with --apply to PERMANENTLY "
                "delete these rows. Make sure a database backup exists first -- this cannot "
                "be undone."
            ))
            return

        AuditLog.objects.create(
            user=None,
            action="BULK PERMANENT DELETE",
            details=(
                f"purge_deleted_rate_master management command: about to permanently delete "
                f"{total} RateMaster rows with is_deleted=YES. Logged before deletion since "
                f"the rows won't exist to reference afterward."
            ),
        )

        deleted_total = 0
        while True:
            batch_ids = list(
                RateMaster.objects.filter(is_deleted="YES").values_list("id", flat=True)[:BATCH_SIZE]
            )
            if not batch_ids:
                break
            with transaction.atomic():
                deleted_count, _ = RateMaster.objects.filter(id__in=batch_ids).delete()
            deleted_total += len(batch_ids)
            self.stdout.write(f"  deleted {deleted_total:,} / {total:,}...")

        self.stdout.write(self.style.SUCCESS(f"Permanently deleted {deleted_total:,} rows."))
