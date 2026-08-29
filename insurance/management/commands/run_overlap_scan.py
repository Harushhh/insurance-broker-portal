from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from insurance.models import RateMaster, RateOverlapPair, RateOverlapScan
from insurance.overlap_utils import run_overlap_scan


class Command(BaseCommand):
    help = (
        "Sweep the Rate Master for pairs of ACTIVE rate groups the MIS Payout Engine "
        "cannot tell apart, and store the results for the Rate Master Health > Overlaps "
        "tab. Runs the scan in this process, so unlike the dashboard's Run Scan button "
        "it needs no Redis or Celery worker."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--insurer",
            help="Scope the scan to one insurer's ACTIVE Rate Master rows (exact match, e.g. 'Acme General Insurance Ltd').",
        )
        parser.add_argument(
            "--as-of-date",
            help="Scope the scan to groups valid on this date (YYYY-MM-DD) - same inclusive from_date/to_date check the payout lookups use.",
        )

    def handle(self, *args, **options):
        insurer = (options.get("insurer") or "").strip() or None
        if insurer:
            active_insurers = set(
                RateMaster.objects.filter(status="ACTIVE", is_deleted="NO")
                .exclude(insurance_company="")
                .values_list("insurance_company", flat=True)
            )
            if insurer not in active_insurers:
                raise CommandError(f"'{insurer}' has no active Rate Master rows.")

        as_of_date = None
        as_of_date_raw = (options.get("as_of_date") or "").strip()
        if as_of_date_raw:
            try:
                as_of_date = datetime.strptime(as_of_date_raw, "%Y-%m-%d").date()
            except ValueError:
                raise CommandError(f"'{as_of_date_raw}' is not a valid YYYY-MM-DD date.")

        scan = RateOverlapScan.objects.create(filter_insurer=insurer, filter_as_of_date=as_of_date)
        scope_bits = [b for b in (insurer, as_of_date and f"as of {as_of_date}") if b]
        scope_note = f" ({', '.join(scope_bits)})" if scope_bits else ""
        self.stdout.write(f"Scan #{scan.id}{scope_note} started - this takes ~20s on a full Rate Master.")

        run_overlap_scan(scan.id)
        scan.refresh_from_db()

        self.stdout.write(
            self.style.SUCCESS(
                f"Scan #{scan.id} {scan.status.lower()}: {scan.groups_scanned} active groups "
                f"compared, {scan.pairs_found} conflicting pair(s) found."
            )
        )
        capped = set(scan.capped_types or [])
        for conflict_type, label in RateOverlapPair.CONFLICT_CHOICES:
            count = (scan.type_counts or {}).get(conflict_type, 0)
            note = "  (browsable sample stored)" if conflict_type in capped else ""
            self.stdout.write(f"  {label}: {count}{note}")
