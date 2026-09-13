"""
Read-only scan for exact-duplicate RateMaster rows.

Default mode looks for rows that share the same group_id and are identical
on every business field (everything except id/created_at/updated_at) --
the signature produced by uploading a source file that listed the same
rate line twice (see api_upload_chunk's rate_master branch).

--cross-group mode instead ignores group_id entirely and looks for
identical-content rows anywhere in the table, restricted to status=ACTIVE
-- the signature produced by re-uploading content that's already live
under a different group (e.g. exporting a grid and re-importing it
unchanged), which the default mode can't see since it never compares
across groups.

Prints a report; writes nothing.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand

from insurance.models import RateMaster

# Every field that defines a row's content for duplicate-detection purposes.
# Deliberately excludes id/created_at/updated_at (those are expected to
# differ even between two otherwise-identical inserts) and the FK id fields
# are used directly (product_id, not product) to avoid extra joins. Also
# excludes is_deleted: both modes below only ever look at is_deleted="NO"
# rows, so including it here would make every already-soft-deleted row look
# like a fresh duplicate of every other already-soft-deleted row (they're
# all identical except id) on the very next scan after a cleanup pass.
CONTENT_FIELDS = [
    "new_vehicle_makes", "new_rto_list", "insurer_vertical", "insurance_company",
    "product_id", "sub_product_id", "policy_type_id", "fuel_type_id", "make_model_class_id",
    "status",
    "vehicle_age_min", "vehicle_age_max",
    "pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5",
    "pi_net_rate", "pi_flat_amount", "pi_vli", "pi_type",
    "tariff_min", "tariff_max",
    "is_ncb_id", "is_cpa_id", "is_zd_id",
    "cc_min", "cc_max", "from_date", "to_date", "user_id",
    "sc_min", "sc_max", "veh_use", "add_tnc", "remarks",
    "po_type", "po_od_rate", "po_tp_rate", "po_net_rate", "po_flat_amount",
]


def find_duplicate_clusters(cross_group):
    """
    Returns (total_rows_scanned, clusters) where clusters is a list of
    (extra_row_count, sorted_ids, group_ids_involved) tuples, one per
    duplicate cluster found -- sorted by extra_row_count descending.

    Within-group mode (cross_group=False): a cluster is rows sharing one
    group_id AND identical content. Cross-group mode: a cluster is rows
    sharing identical content anywhere (status=ACTIVE, is_deleted=NO),
    regardless of group_id.
    """
    fields = ["id", "group_id"] + CONTENT_FIELDS
    qs = RateMaster.objects.exclude(group_id__isnull=True).filter(is_deleted="NO")
    if cross_group:
        qs = qs.filter(status="ACTIVE")
    rows = qs.values(*fields).iterator(chunk_size=5000)

    total_rows = 0
    if cross_group:
        # content_signature -> {"ids": [...], "group_ids": set(...)}
        by_sig = defaultdict(lambda: {"ids": [], "group_ids": set()})
        for row in rows:
            total_rows += 1
            sig = tuple(row[f] for f in CONTENT_FIELDS)
            by_sig[sig]["ids"].append(row["id"])
            by_sig[sig]["group_ids"].add(row["group_id"])
        clusters = []
        for sig, data in by_sig.items():
            if len(data["ids"]) > 1:
                ids = sorted(data["ids"])
                clusters.append((len(ids) - 1, ids, sorted(data["group_ids"])))
    else:
        # group_id -> content_signature -> [ids]
        by_group = defaultdict(lambda: defaultdict(list))
        for row in rows:
            total_rows += 1
            by_group[row["group_id"]][tuple(row[f] for f in CONTENT_FIELDS)].append(row["id"])
        clusters = []
        for group_id, sigs in by_group.items():
            for sig, ids in sigs.items():
                if len(ids) > 1:
                    ids.sort()
                    clusters.append((len(ids) - 1, ids, [group_id]))

    clusters.sort(key=lambda c: -c[0])
    return total_rows, clusters


class Command(BaseCommand):
    help = "Read-only report of exact-duplicate RateMaster rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sample-groups", type=int, default=15,
            help="How many duplicate clusters to print row-level detail for (default 15).",
        )
        parser.add_argument(
            "--cross-group", action="store_true",
            help=(
                "Also/instead look for identical-content ACTIVE rows across different "
                "group_ids, not just within the same group."
            ),
        )

    def handle(self, *args, **options):
        sample_groups = options["sample_groups"]
        cross_group = options["cross_group"]

        total_rows, clusters = find_duplicate_clusters(cross_group)
        total_extra_rows = sum(c[0] for c in clusters)

        mode_desc = "cross-group (ACTIVE rows, any group_id)" if cross_group else "within-group"
        self.stdout.write(f"Scanned {total_rows:,} RateMaster rows [{mode_desc} mode].")
        self.stdout.write(f"Duplicate clusters found: {len(clusters):,}")
        self.stdout.write(f"Total extra (duplicate) rows found: {total_extra_rows:,}")
        self.stdout.write("")

        if not clusters:
            self.stdout.write(self.style.SUCCESS("No exact-duplicate rows found."))
            return

        self.stdout.write(f"Top {min(sample_groups, len(clusters))} clusters (extra_rows, keep, drop, group_ids):")
        for extra, ids, group_ids in clusters[:sample_groups]:
            self.stdout.write(f"  extra={extra}  keep {ids[0]} drop {ids[1:]}  group_ids={group_ids}")

        if len(clusters) > sample_groups:
            self.stdout.write(f"  ... and {len(clusters) - sample_groups} more clusters")
