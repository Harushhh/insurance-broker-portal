from django.core.management.base import BaseCommand

from insurance.models import RateOverlapPair, RateOverlapScan
from insurance.overlap_utils import run_overlap_scan


class Command(BaseCommand):
    help = (
        "Sweep the Rate Master for pairs of ACTIVE rate groups the MIS Payout Engine "
        "cannot tell apart, and store the results for the Rate Master Health > Overlaps "
        "tab. Runs the scan in this process, so unlike the dashboard's Run Scan button "
        "it needs no Redis or Celery worker."
    )

    def handle(self, *args, **options):
        scan = RateOverlapScan.objects.create()
        self.stdout.write(f"Scan #{scan.id} started - this takes ~20s on a full Rate Master.")

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
