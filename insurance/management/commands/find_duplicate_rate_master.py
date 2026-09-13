"""
Read-only scan for exact-duplicate RateMaster rows.

Looks for rows that share the same group_id and are identical on every
business field (everything except id/created_at/updated_at) -- the exact
signature produced by uploading a source file that listed the same rate
line twice (see api_upload_chunk's rate_master branch, which has no
intra-upload duplicate check). Prints a report; writes nothing.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand

from insurance.models import RateMaster

# Every field that defines a row's content for duplicate-detection purposes.
# Deliberately excludes id/created_at/updated_at (those are expected to
# differ even between two otherwise-identical inserts) and the FK id fields
# are used directly (product_id, not product) to avoid extra joins.
CONTENT_FIELDS = [
    "new_vehicle_makes", "new_rto_list", "insurer_vertical", "insurance_company",
    "product_id", "sub_product_id", "policy_type_id", "fuel_type_id", "make_model_class_id",
    "status", "is_deleted",
    "vehicle_age_min", "vehicle_age_max",
    "pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5",
    "pi_net_rate", "pi_flat_amount", "pi_vli", "pi_type",
    "tariff_min", "tariff_max",
    "is_ncb_id", "is_cpa_id", "is_zd_id",
    "cc_min", "cc_max", "from_date", "to_date", "user_id",
    "sc_min", "sc_max", "veh_use", "add_tnc", "remarks",
    "po_type", "po_od_rate", "po_tp_rate", "po_net_rate", "po_flat_amount",
]


class Command(BaseCommand):
    help = "Read-only report of exact-duplicate RateMaster rows sharing a group_id."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sample-groups", type=int, default=15,
            help="How many affected groups to print row-level detail for (default 15).",
        )

    def handle(self, *args, **options):
        sample_groups = options["sample_groups"]

        fields = ["id", "group_id"] + CONTENT_FIELDS
        rows = RateMaster.objects.exclude(group_id__isnull=True).values(*fields).iterator(chunk_size=5000)

        # group_id -> content_signature -> [row ids] (lowest id first)
        by_group = defaultdict(lambda: defaultdict(list))
        total_rows = 0
        for row in rows:
            total_rows += 1
            group_id = row["group_id"]
            sig = tuple(row[f] for f in CONTENT_FIELDS)
            by_group[group_id][sig].append(row["id"])

        affected_groups = []
        total_extra_rows = 0
        for group_id, sigs in by_group.items():
            group_extra = 0
            dup_clusters = []
            for sig, ids in sigs.items():
                if len(ids) > 1:
                    ids.sort()
                    group_extra += len(ids) - 1
                    dup_clusters.append(ids)
            if group_extra:
                affected_groups.append((group_id, group_extra, dup_clusters))
                total_extra_rows += group_extra

        affected_groups.sort(key=lambda t: -t[1])

        self.stdout.write(f"Scanned {total_rows:,} RateMaster rows across {len(by_group):,} groups.")
        self.stdout.write(f"Groups with exact-duplicate rows: {len(affected_groups):,}")
        self.stdout.write(f"Total extra (duplicate) rows found: {total_extra_rows:,}")
        self.stdout.write("")

        if not affected_groups:
            self.stdout.write(self.style.SUCCESS("No exact-duplicate rows found."))
            return

        self.stdout.write(f"Top {min(sample_groups, len(affected_groups))} affected groups (group_id, extra_rows, clusters):")
        for group_id, extra, clusters in affected_groups[:sample_groups]:
            cluster_desc = "; ".join(f"keep {c[0]} drop {c[1:]}" for c in clusters[:5])
            more = "" if len(clusters) <= 5 else f" (+{len(clusters) - 5} more clusters)"
            self.stdout.write(f"  group_id={group_id}  extra_rows={extra}  {cluster_desc}{more}")

        if len(affected_groups) > sample_groups:
            self.stdout.write(f"  ... and {len(affected_groups) - sample_groups} more affected groups")
