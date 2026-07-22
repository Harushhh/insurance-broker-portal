"""
One-time re-grouping pass, needed after fuel_type was added to GROUP_FIELDS.

group_id is a static FK assigned once per row at upload time — changing
GROUP_FIELDS only affects new uploads going forward. This command recomputes
what each EXISTING row's group should be under the current GROUP_FIELDS.

Adding a field to GROUP_FIELDS changes the computed hash for every row,
mathematically, regardless of whether that row's original group ever had a
fuel-type mix — so "does this row's hash match its current group" is the
wrong question to ask row-by-row. What actually matters is group *membership*:
- A group where every row still lands on the same new hash isn't splitting —
  its RateGroup keeps its existing id, just with key_hash/key_text updated
  in place. No RateMaster row needs to move.
- A group whose rows land on multiple different new hashes is genuinely
  splitting. The largest resulting piece keeps the original group id (updated
  in place); only the smaller split-off pieces get new RateGroup rows and
  have their rows' `group` FK reassigned.

This keeps existing group ids stable for the ~90%+ of groups that were never
actually mixed, and only touches rows that need to move for a real, semantic
reason. No rate/pricing field on any row is ever read for writing or modified
— only the `group` FK column, and only where membership genuinely changes.

Safe to re-run: a second run should report zero changes.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from insurance.models import RateMaster, RateGroup
from insurance.views import GROUP_FIELDS, build_key_hash

# Maps each GROUP_FIELDS entry to the column to read it from. FK fields read
# the raw *_id column rather than fetching the related object — build_key_hash()
# treats anything with a .pk the same as a raw id via normalize(), so this
# produces an identical hash to what the original upload path computes from
# full model instances, without an extra query per row per FK.
FIELD_TO_COLUMN = {
    "new_vehicle_makes": "new_vehicle_makes",
    "insurer_vertical": "insurer_vertical",
    "insurance_company": "insurance_company",
    "product": "product_id",
    "sub_product": "sub_product_id",
    "policy_type": "policy_type_id",
    "vehicle_age_min": "vehicle_age_min",
    "vehicle_age_max": "vehicle_age_max",
    "make_model_class": "make_model_class_id",
    "fuel_type": "fuel_type_id",
    "pi_od_rate": "pi_od_rate",
    "pi_tp_rate": "pi_tp_rate",
    "pi_tp_2": "pi_tp_2",
    "pi_tp_3": "pi_tp_3",
    "pi_tp_4": "pi_tp_4",
    "pi_tp_5": "pi_tp_5",
    "pi_net_rate": "pi_net_rate",
    "pi_flat_amount": "pi_flat_amount",
    "pi_vli": "pi_vli",
    "pi_type": "pi_type",
    "tariff_min": "tariff_min",
    "tariff_max": "tariff_max",
    "is_ncb": "is_ncb_id",
    "is_cpa": "is_cpa_id",
    "cc_min": "cc_min",
    "cc_max": "cc_max",
    "is_zd": "is_zd_id",
    "from_date": "from_date",
    "to_date": "to_date",
    "sc_min": "sc_min",
    "sc_max": "sc_max",
    "add_tnc": "add_tnc",
}


class Command(BaseCommand):
    help = (
        "Recompute RateMaster grouping under the current GROUP_FIELDS "
        "(now includes fuel_type). Groups that aren't actually splitting keep "
        "their id; only genuinely-splitting groups get new ids for the "
        "smaller pieces, and only those rows are reassigned."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing anything.")
        parser.add_argument("--batch-size", type=int, default=2000, help="Rows fetched per DB round-trip.")
        parser.add_argument("--top", type=int, default=10, help="How many most-affected original groups to list.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        batch_size = options["batch_size"]
        top_n = options["top"]

        missing = [f for f in GROUP_FIELDS if f not in FIELD_TO_COLUMN]
        if missing:
            self.stderr.write(self.style.ERROR(f"No column mapping for GROUP_FIELDS entries: {missing}"))
            return

        select_columns = ["id", "group_id"] + [FIELD_TO_COLUMN[f] for f in GROUP_FIELDS]

        row_ids_by_new_hash = defaultdict(list)     # new_hash -> [row ids]
        key_text_by_hash = {}                       # new_hash -> key_text
        old_group_of_new_hash = {}                  # new_hash -> old group_id (or None)
        new_hashes_of_old_group = defaultdict(set)  # old group_id -> {new_hash, ...}

        total_rows = 0
        ungrouped_rows = 0

        queryset = (
            RateMaster.objects.all()
            .order_by("id")
            .values(*select_columns)
            .iterator(chunk_size=batch_size)
        )

        for row in queryset:
            total_rows += 1
            cleaned = {f: row[FIELD_TO_COLUMN[f]] for f in GROUP_FIELDS}
            key_hash, key_text = build_key_hash(cleaned)
            key_text_by_hash[key_hash] = key_text
            row_ids_by_new_hash[key_hash].append(row["id"])

            old_gid = row["group_id"]
            old_group_of_new_hash[key_hash] = old_gid
            if old_gid is not None:
                new_hashes_of_old_group[old_gid].add(key_hash)
            else:
                ungrouped_rows += 1

        # Decide, per old group, which new hash "wins" (keeps the id) and
        # which are genuine splits.
        winning_hash_of_old_group = {}
        splitting_groups = {}  # old group_id -> {losing new_hash: row_count}
        for old_gid, hashes in new_hashes_of_old_group.items():
            if len(hashes) == 1:
                winning_hash_of_old_group[old_gid] = next(iter(hashes))
            else:
                winner = max(hashes, key=lambda h: len(row_ids_by_new_hash[h]))
                winning_hash_of_old_group[old_gid] = winner
                splitting_groups[old_gid] = {
                    h: len(row_ids_by_new_hash[h]) for h in hashes if h != winner
                }

        winning_hashes = set(winning_hash_of_old_group.values())
        losing_hashes = [h for h in row_ids_by_new_hash if h not in winning_hashes]
        # (losing_hashes includes both split-off pieces and hashes with no old group at all)

        rows_to_reassign = sum(len(row_ids_by_new_hash[h]) for h in losing_hashes)
        groups_updated_in_place = len(winning_hash_of_old_group)
        groups_created = len(losing_hashes)

        if not dry_run:
            with transaction.atomic():
                for old_gid, winning_hash in winning_hash_of_old_group.items():
                    RateGroup.objects.filter(pk=old_gid).update(
                        key_hash=winning_hash, key_text=key_text_by_hash[winning_hash]
                    )
                for new_hash in losing_hashes:
                    new_group = RateGroup.objects.create(
                        key_hash=new_hash, key_text=key_text_by_hash[new_hash]
                    )
                    RateMaster.objects.filter(
                        id__in=row_ids_by_new_hash[new_hash]
                    ).update(group_id=new_group.pk)

        label = "[DRY RUN] " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(
            f"{label}Scanned {total_rows} rows across {len(new_hashes_of_old_group)} existing groups "
            f"({ungrouped_rows} rows currently ungrouped).\n"
            f"{label}{groups_updated_in_place} existing groups keep their id (hash refreshed in place).\n"
            f"{label}{len(splitting_groups)} existing groups are genuinely splitting.\n"
            f"{label}{groups_created} new groups would be created "
            f"(the split-off pieces, plus any previously-ungrouped rows).\n"
            f"{label}{rows_to_reassign} rows would actually be reassigned to a different group "
            f"(out of {total_rows} scanned)."
        ))

        if splitting_groups:
            top = sorted(
                splitting_groups.items(),
                key=lambda item: sum(item[1].values()),
                reverse=True,
            )[:top_n]
            self.stdout.write(f"\nTop {len(top)} splitting groups (by rows actually moving):")
            self.stdout.write(f"{'group_id':>10}  {'moving_rows':>12}  splits_into")
            for gid, losers in top:
                total_moving = sum(losers.values())
                self.stdout.write(f"{gid:>10}  {total_moving:>12}  {len(losers) + 1} groups total")
