"""
Loads (or refreshes) HealthRateMaster rows from a Health commission-grid
Excel file, e.g. media/grid_documents/health/Health_commission_grid.xlsx.

Re-running against an updated grid is safe: each row is matched to an
existing HealthRateMaster by a hash of its identity fields (insurer, product,
category, business type, deductible/sum-insured/age bands, zone, plan names,
validity window) and its rates/dates are refreshed in place. status/is_deleted
/remarks are only set when a row is first created, so manual overrides made
afterwards in the UI survive a re-import. Genuinely new rows are inserted;
rows removed from the new file are left untouched (not deleted) — use the
UI's status/is_deleted fields to retire them.
"""
from pathlib import Path

import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from insurance.models import HealthRateMaster
from insurance.health_grid_utils import (
    build_row_hash, clean_text, parse_grid_date, parse_number, parse_percent, upsert_health_rate_row,
)

DEFAULT_FILE = Path(settings.MEDIA_ROOT) / "grid_documents" / "health" / "Health_commission_grid.xlsx"
DEFAULT_SHEET = "Commission Grid"

# Source column -> model field for everything that isn't a rate/date.
IDENTITY_COLUMNS = {
    "insurance_company": "insurance_company",
    "ProductName": "product_name",
    "PolicyCategory": "policy_category",
    "plan_name": "plan_names",
    "business_type": "business_type",
    "min_deductible": "min_deductible",
    "max_deductible": "max_deductible",
    "min_insurance_cover": "min_sum_insured",
    "max_insurance_cover": "max_sum_insured",
    "min_age": "min_age",
    "max_age": "max_age",
    "pincode": "pincode_zone",
}
RATE_COLUMNS = {
    "payin_rate": "payin_rate",
    "One Year Policy": "one_year_rate",
    "Multi Year Policy(2Y)": "multi_year_2_rate",
    "Multi Year Policy(3Y)": "multi_year_3_rate",
    "Multi Year Policy(4Y)": "multi_year_4_rate",
    "Multi Year Policy(5Y)": "multi_year_5_rate",
}
DATE_COLUMNS = {
    "From Date": "from_date",
    "to_date": "to_date",
}
REQUIRED_COLUMNS = list(IDENTITY_COLUMNS) + list(RATE_COLUMNS) + list(DATE_COLUMNS)


class Command(BaseCommand):
    help = "Import/refresh HealthRateMaster rows from a Health commission-grid Excel file."

    def add_arguments(self, parser):
        parser.add_argument(
            "file", nargs="?", default=str(DEFAULT_FILE),
            help=f"Path to the commission-grid .xlsx (default: {DEFAULT_FILE})",
        )
        parser.add_argument("--sheet", default=DEFAULT_SHEET, help=f"Sheet name (default: '{DEFAULT_SHEET}')")
        parser.add_argument("--dry-run", action="store_true", help="Report what would happen without writing anything.")

    def handle(self, *args, **options):
        file_path = Path(options["file"])
        if not file_path.exists():
            raise CommandError(f"File not found: {file_path}")

        df = pd.read_excel(file_path, sheet_name=options["sheet"], dtype=str)

        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise CommandError(f"Missing expected column(s) in '{options['sheet']}': {missing}")

        created, updated, skipped = 0, 0, 0
        seen_hashes = set()

        with transaction.atomic():
            for _, raw_row in df.iterrows():
                cleaned = {}
                for src_col, field in IDENTITY_COLUMNS.items():
                    val = clean_text(raw_row[src_col])
                    if field in ("min_deductible", "max_deductible", "min_sum_insured",
                                  "max_sum_insured", "min_age", "max_age"):
                        cleaned[field] = parse_number(val)
                    else:
                        cleaned[field] = val or None

                if not cleaned.get("insurance_company"):
                    skipped += 1
                    continue

                for src_col, field in DATE_COLUMNS.items():
                    cleaned[field] = parse_grid_date(raw_row[src_col])

                row_hash = build_row_hash(cleaned)
                if row_hash in seen_hashes:
                    # Exact duplicate row within this same file (identity
                    # fields, including plan list, all match) — collapse to
                    # one DB row rather than fighting update_or_create over
                    # the same key twice in one pass.
                    skipped += 1
                    continue
                seen_hashes.add(row_hash)

                for src_col, field in RATE_COLUMNS.items():
                    cleaned[field] = parse_percent(raw_row[src_col])

                if options["dry_run"]:
                    if HealthRateMaster.objects.filter(source_row_hash=row_hash).exists():
                        updated += 1
                    else:
                        created += 1
                    continue

                obj, was_created = upsert_health_rate_row(cleaned)
                if was_created:
                    created += 1
                else:
                    updated += 1

            if options["dry_run"]:
                transaction.set_rollback(True)

        label = "[DRY RUN] " if options["dry_run"] else ""
        self.stdout.write(self.style.SUCCESS(
            f"{label}Processed {len(df)} rows from {file_path.name}: "
            f"{created} created, {updated} updated, {skipped} skipped (blank insurer or duplicate row)."
        ))
