"""
Archive Grid Management uploads older than the retention window to Cloudflare
R2 and remove them from the active database. This is what the monthly Celery
task runs; use the command to preview (--dry-run), to re-run after a failure,
or to try a different window.

    python manage.py archive_grid_documents --dry-run
    python manage.py archive_grid_documents
    python manage.py archive_grid_documents --months 2
"""
from django.core.management.base import BaseCommand, CommandError

from insurance.grid_archive import ArchiveNotConfigured, retention_cutoff, run_retention


class Command(BaseCommand):
    help = (
        "Move Grid Management uploads older than the retention window to the "
        "archive, verify them there, then delete them from the active database."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would be archived; change nothing.")
        parser.add_argument(
            "--months", type=int, default=None,
            help="Months kept active, counting the current one (default: settings.GRID_RETENTION_MONTHS).",
        )
        parser.add_argument(
            "--allow-local-archive", action="store_true",
            help="Local testing only: allow purging when the archive is on this machine's disk, not R2.",
        )

    def handle(self, *args, **options):
        try:
            cutoff = retention_cutoff(options["months"])
            results = run_retention(
                months=options["months"],
                dry_run=options["dry_run"],
                allow_local_archive=options["allow_local_archive"],
            )
        except (ArchiveNotConfigured, ValueError) as exc:
            raise CommandError(str(exc))

        self.stdout.write(f"Archiving uploads before {cutoff:%d %b %Y}.")
        if not results:
            self.stdout.write(self.style.SUCCESS("Nothing to archive."))
            return

        failed = False
        for r in results:
            line = (
                f"{r['month']}: {r['status']} - {r['documents'] or r['found']} document(s), "
                f"{r['files']} file(s), {r['bytes']:,} bytes"
            )
            self.stdout.write(line)
            for error in r["errors"]:
                failed = True
                self.stderr.write(self.style.ERROR(f"  {error}"))
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("[DRY RUN] nothing was archived or deleted."))
        elif failed:
            raise CommandError("Some documents could not be archived and were left in place; see above.")
        else:
            self.stdout.write(self.style.SUCCESS("Done."))
