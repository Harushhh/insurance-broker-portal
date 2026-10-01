"""
Bring Grid Management uploads back into the active database from Cloudflare R2.
Never deletes anything, and skips any file a GridDocument row already points
at, so it is safe to run twice.

--source live (default)  For when the GridDocument table was emptied but the
    files are still in the bucket under grid_documents/. Re-creates a row per
    unreferenced file. Only the file survives such a wipe, so insurer (guessed
    from the file name), remarks, work-effected date, uploader and original
    status are NOT recovered - upload time comes from the object's
    last-modified time. If the rows were never lost, this reports 0 to restore.

--source archive  Restores months archived by archive_grid_documents, with
    every field intact, from the manifests in the archive. Archived months are
    older than the retention window, so the next scheduled run moves them out
    again.

    python manage.py restore_grid_documents --dry-run
    python manage.py restore_grid_documents
    python manage.py restore_grid_documents --source archive --only 2026-07
"""
from django.core.management.base import BaseCommand, CommandError

from insurance.grid_archive import (
    list_archive_months, restore_from_archive, restore_orphans,
)
from insurance.models import GridDocument


class Command(BaseCommand):
    help = "Restore Grid Management records and files from Cloudflare R2 into the active database."

    def add_arguments(self, parser):
        parser.add_argument("--source", choices=["live", "archive"], default="live")
        parser.add_argument("--dry-run", action="store_true", help="Report what would be restored; change nothing.")
        parser.add_argument(
            "--months", type=int, default=None,
            help="[live] Months of files to bring back, counting the current one (default: settings.GRID_RETENTION_MONTHS).",
        )
        parser.add_argument("--all", action="store_true", help="[live] Ignore the window and restore files of any age.")
        parser.add_argument(
            "--status", choices=[code for code, _ in GridDocument.STATUS_CHOICES], default="PENDING",
            help="[live] Status given to restored rows (the original one is unknown). Default PENDING.",
        )
        parser.add_argument(
            "--only", action="append", metavar="YYYY-MM",
            help="[archive] Restore just this archived month; repeat for several. Default: every archived month.",
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]
        prefix = "[DRY RUN] would restore" if dry else "Restored"

        if options["source"] == "live":
            try:
                result = restore_orphans(
                    months=options["months"], include_all=options["all"],
                    status=options["status"], dry_run=dry,
                )
            except ValueError as exc:
                raise CommandError(str(exc))
            for key, insurer, modified in result["rows"]:
                self.stdout.write(f"  {key}  ->  {insurer}  ({modified:%d %b %Y %H:%M})")
            self.stdout.write(
                f"{prefix} {result['restored']}; already in the database {result['already_present']}; "
                f"older than the window {result['outside_window']}; too recent {result['too_recent']}."
            )
        else:
            available = list_archive_months()
            months = options["only"] or None
            for m in months or []:
                if m not in available:
                    raise CommandError(f"No archive for {m}. Archived months: {', '.join(available) or 'none'}.")
            result = restore_from_archive(months=months, dry_run=dry)
            self.stdout.write(
                f"{prefix} {result['restored']}; already in the database {result['already_present']}; "
                f"metadata-only entries skipped {result['without_file']}."
            )

        for error in result["errors"]:
            self.stderr.write(self.style.ERROR(f"  {error}"))
        if result["errors"]:
            raise CommandError(f"{len(result['errors'])} item(s) failed; see above.")
        if dry:
            self.stdout.write(self.style.WARNING("[DRY RUN] nothing was changed."))
