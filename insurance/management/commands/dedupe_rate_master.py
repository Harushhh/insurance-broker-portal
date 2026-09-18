"""
Soft-deletes exact-duplicate RateMaster rows found by
find_duplicate_rate_master.py.

Default mode: rows that share a group_id and are identical on every
business field (the signature produced by the now-fixed api_upload_chunk
bug: a source file listing the same rate line twice, a resent chunk, or a
replayed upload session).

--cross-group mode: identical-content ACTIVE rows anywhere in the table,
regardless of group_id (the signature produced by re-uploading content
that's already live under a different group). Run the default mode first
-- cross-group is a separate, broader sweep meant to catch what that pass
can't see, not a replacement for it.

Within each duplicate cluster (whichever mode found it), the lowest id
(the original insert) is kept; every other id is soft-deleted
(is_deleted="YES"). Nothing else about the row is touched -- status,
rates, dates all stay as they are.

Dry-run by default: prints what would change and writes nothing. Pass
--apply to actually perform the soft-delete.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from insurance.models import AuditLog, RateMaster
from insurance.management.commands.find_duplicate_rate_master import find_duplicate_clusters

BATCH_SIZE = 2000


class Command(BaseCommand):
    help = "Soft-delete exact-duplicate RateMaster rows (dry-run unless --apply is passed)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually perform the soft-delete. Without this, only reports what would change.",
        )
        parser.add_argument(
            "--cross-group", action="store_true",
            help="Also/instead soft-delete identical-content rows (of the given --status) across different group_ids.",
        )
        parser.add_argument(
            "--status", choices=["ACTIVE", "INACTIVE"], default="ACTIVE",
            help="Cross-group mode only: which status to scan for duplicates (default ACTIVE).",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        cross_group = options["cross_group"]
        status = options["status"]

        _, clusters = find_duplicate_clusters(cross_group, status)

        drop_ids = []
        for extra, ids, group_ids in clusters:
            drop_ids.extend(ids[1:])
        drop_ids.sort()

        mode_desc = f"cross-group ({status} rows, any group_id)" if cross_group else "within-group"
        self.stdout.write(f"Mode: {mode_desc}")
        self.stdout.write(f"Duplicate clusters found: {len(clusters):,}")
        self.stdout.write(f"Rows that would be soft-deleted (is_deleted=YES): {len(drop_ids):,}")

        if not drop_ids:
            self.stdout.write(self.style.SUCCESS("Nothing to do."))
            return

        if not apply_changes:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Dry run only -- nothing was written. Re-run with --apply to soft-delete these rows."
            ))
            self.stdout.write(f"Sample ids (first 20): {drop_ids[:20]}")
            return

        updated_total = 0
        now = timezone.now()
        with transaction.atomic():
            for i in range(0, len(drop_ids), BATCH_SIZE):
                batch = drop_ids[i:i + BATCH_SIZE]
                updated_total += RateMaster.objects.filter(id__in=batch).update(
                    is_deleted="YES", updated_at=now,
                )
            AuditLog.objects.create(
                user=None,
                action="BULK DEDUPE",
                details=(
                    f"dedupe_rate_master management command ({mode_desc}): soft-deleted "
                    f"{updated_total} exact-duplicate RateMaster rows across {len(clusters)} clusters. "
                    f"Lowest id per duplicate cluster was kept; nothing else on any row was modified."
                ),
            )

        self.stdout.write(self.style.SUCCESS(f"Soft-deleted {updated_total:,} rows."))
