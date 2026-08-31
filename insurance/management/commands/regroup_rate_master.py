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

The reverse case also happens: two DIFFERENT existing groups can converge onto
the same new hash (they were always identical under the fields that now
matter, just recorded under two legacy key_hash values). That's a merge, not
a split — the group with the most current rows keeps its id (ties go to the
lowest/oldest group id); every row from the other group(s) is reassigned onto
the winner, and the now-empty losing RateGroup row(s) are deleted so they
don't collide with the winner's key_hash under the unique constraint.

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
    "status": "status",
    "is_deleted": "is_deleted",
    # api_upload_chunk hashes a browser-generated id that isn't stored on the
    # row itself, so it can't be reproduced exactly here -- created_at is the
    # closest available stand-in. It still does the job group membership
    # actually needs: rows from the same bulk_create share one created_at
    # (same upload), rows from a different upload get a distinctly different
    # one, so existing groups still only split where they genuinely should.
    "upload_batch": "created_at",
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
        rows_by_old_gid = defaultdict(int)          # old group_id -> current row count
        row_old_gid = {}                            # row id -> old group_id (for merge reporting)

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
            row_old_gid[row["id"]] = old_gid
            if old_gid is not None:
                new_hashes_of_old_group[old_gid].add(key_hash)
                rows_by_old_gid[old_gid] += 1
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

        # Merge detection: multiple old groups can independently pick the same
        # winning hash (they were always duplicates of each other under the
        # fields that now matter). Group old_gids by the hash they claim, and
        # for any hash claimed by more than one, pick a merge winner: most
        # current rows, ties broken by the lowest/oldest group id.
        old_gids_claiming_hash = defaultdict(list)
        for old_gid, h in winning_hash_of_old_group.items():
            old_gids_claiming_hash[h].append(old_gid)

        hash_owner = {}            # new_hash -> existing old_gid that will hold it
        merge_losers_of_hash = {}  # new_hash -> [losing old_gid, ...]
        for h, gids in old_gids_claiming_hash.items():
            if len(gids) == 1:
                hash_owner[h] = gids[0]
            else:
                winner = min(gids, key=lambda g: (-rows_by_old_gid[g], g))
                hash_owner[h] = winner
                merge_losers_of_hash[h] = [g for g in gids if g != winner]

        merged_away_gids = {g for losers in merge_losers_of_hash.values() for g in losers}
        rows_moved_via_merge = sum(
            1 for h, losers in merge_losers_of_hash.items()
            for rid in row_ids_by_new_hash[h] if row_old_gid.get(rid) in set(losers)
        )

        # Hashes with no existing owner at all need a brand new RateGroup —
        # this is the split-off case (a losing piece of a splitting group), or
        # a hash that never had an old group to begin with.
        new_group_hashes = [h for h in row_ids_by_new_hash if h not in hash_owner]

        rows_to_reassign = sum(len(row_ids_by_new_hash[h]) for h in new_group_hashes)
        groups_updated_in_place = len(hash_owner)
        groups_created = len(new_group_hashes)

        if not dry_run:
            with transaction.atomic():
                # 1. Refresh/claim every hash that has an existing owner group —
                #    covers plain refreshes, split winners, and merge winners
                #    alike. This pulls every row for that hash (including ones
                #    currently sitting under a merge loser) onto the owner.
                for h, owner_gid in hash_owner.items():
                    RateGroup.objects.filter(pk=owner_gid).update(
                        key_hash=h, key_text=key_text_by_hash[h]
                    )
                    RateMaster.objects.filter(
                        id__in=row_ids_by_new_hash[h]
                    ).update(group_id=owner_gid)

                # 2. Now that no row still points at a merge loser, delete the
                #    redundant RateGroup rows so their key_hash frees up.
                if merged_away_gids:
                    RateGroup.objects.filter(pk__in=merged_away_gids).delete()

                # 3. Create brand new groups for hashes nobody currently owns.
                for new_hash in new_group_hashes:
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
            f"{label}{len(merge_losers_of_hash)} duplicate-group merges found, "
            f"retiring {len(merged_away_gids)} redundant group id(s) "
            f"and moving {rows_moved_via_merge} row(s) onto the surviving group.\n"
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

        if merge_losers_of_hash:
            self.stdout.write(f"\n{len(merge_losers_of_hash)} merge(s) (duplicate groups collapsing into one):")
            self.stdout.write(f"{'winner_gid':>10}  {'loser_gid(s)':>20}  {'rows_moved':>10}")
            for h, losers in merge_losers_of_hash.items():
                winner_gid = hash_owner[h]
                loser_set = set(losers)
                moved = sum(1 for rid in row_ids_by_new_hash[h] if row_old_gid.get(rid) in loser_set)
                self.stdout.write(f"{winner_gid:>10}  {str(losers):>20}  {moved:>10}")
