"""
Soft-deletes exact-duplicate RateMaster rows found by
find_duplicate_rate_master.py -- rows that share a group_id and are
identical on every business field (the signature produced by the
now-fixed api_upload_chunk bug: a source file listing the same rate line
twice, a resent chunk, or a replayed upload session).

Within each duplicate cluster, the lowest id (the original insert) is
kept; every other id in the cluster is soft-deleted (is_deleted="YES").
Nothing else about the row is touched -- status, rates, dates all stay
as they are.

Dry-run by default: prints what would change and writes nothing. Pass
--apply to actually perform the soft-delete.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from insurance.models import AuditLog, RateMaster
from insurance.management.commands.find_duplicate_rate_master import CONTENT_FIELDS

BATCH_SIZE = 2000


class Command(BaseCommand):
    help = "Soft-delete exact-duplicate RateMaster rows (dry-run unless --apply is passed)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually perform the soft-delete. Without this, only reports what would change.",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]

        fields = ["id", "group_id"] + CONTENT_FIELDS
        rows = (
            RateMaster.objects.exclude(group_id__isnull=True)
            .filter(is_deleted="NO")
            .values(*fields)
            .iterator(chunk_size=5000)
        )

        from collections import defaultdict
        by_group = defaultdict(lambda: defaultdict(list))
        for row in rows:
            sig = tuple(row[f] for f in CONTENT_FIELDS)
            by_group[row["group_id"]][sig].append(row["id"])

        drop_ids = []
        affected_group_count = 0
        for group_id, sigs in by_group.items():
            group_has_dup = False
            for sig, ids in sigs.items():
                if len(ids) > 1:
                    ids.sort()
                    drop_ids.extend(ids[1:])
                    group_has_dup = True
            if group_has_dup:
                affected_group_count += 1

        drop_ids.sort()

        self.stdout.write(f"Groups with exact-duplicate rows: {affected_group_count:,}")
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
                    f"dedupe_rate_master management command: soft-deleted {updated_total} "
                    f"exact-duplicate RateMaster rows across {affected_group_count} groups. "
                    f"Lowest id per duplicate cluster was kept; nothing else on any row was modified."
                ),
            )

        self.stdout.write(self.style.SUCCESS(f"Soft-deleted {updated_total:,} rows."))
