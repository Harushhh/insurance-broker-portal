"""Motor payout row collection shared by the Motor payout page and its load-more endpoint."""
from ..models import MakeModelClassMaster
from .make_model import get_translated_make_model


MOTOR_PAYOUT_MAX_RESULTS = 300


def _collect_motor_payout_rows(qs, matching_rto_names, matching_make_groups, rto_code, make_names, skip, limit):
    """Streams the ordered queryset and applies the same in-Python RTO/make
    cluster matching + group de-duplication as before, returning the slice
    of unique groups from `skip` up to `skip + limit` (never exceeding
    MOTOR_PAYOUT_MAX_RESULTS overall), plus whether any group exists beyond
    that slice."""
    effective_limit = min(limit, MOTOR_PAYOUT_MAX_RESULTS - skip)
    if effective_limit <= 0:
        return [], False

    results = []
    seen_groups = set()
    group_index = 0

    for row in qs.iterator(chunk_size=2000):
        if rto_code and matching_rto_names:
            if not row.new_rto_list:
                continue
            row_rtos = [x.strip().upper() for x in row.new_rto_list.split(",")]
            if not any(x in matching_rto_names for x in row_rtos):
                continue

        if make_names and matching_make_groups:
            if not row.new_vehicle_makes:
                continue
            row_makes = [x.strip().upper() for x in row.new_vehicle_makes.split(",")]
            if not any(x in matching_make_groups for x in row_makes):
                continue

        gid = row.group_id if row.group_id is not None else row.id
        if gid in seen_groups:
            continue
        seen_groups.add(gid)

        if group_index < skip:
            group_index += 1
            continue
        group_index += 1

        if len(results) >= effective_limit:
            # This row is proof at least one more group exists beyond the
            # slice we're returning — no need to keep scanning further.
            return results, True

        row.display_group_id = gid

        mmc_name = row.make_model_class.name.strip().upper() if row.make_model_class else "NA"

        if mmc_name in ["NA", ""]:
            prod_name = row.product.name if row.product else ""
            translated_name = get_translated_make_model(prod_name)
            if translated_name:
                row.make_model_class = MakeModelClassMaster(name=translated_name)
                row.display_make_model_class = translated_name
            else:
                row.display_make_model_class = "NA"
        else:
            row.display_make_model_class = row.make_model_class.name

        # 'On OD and TP' means the payout is genuinely split across two
        # rates -- picking just one (as the elif chain below does for every
        # other type) silently drops the other half. Same "OD+TP" display
        # convention as Policy Lock Checker's po_rate and the MIS export's
        # format_od_tp_rate.
        if row.pi_type == 'On OD and TP':
            row.pi_rate = f"{row.pi_od_rate or 0:.2f}+{row.pi_tp_rate or 0:.2f}"
        elif row.pi_net_rate and row.pi_net_rate > 0:
            row.pi_rate = row.pi_net_rate
        elif row.pi_od_rate and row.pi_od_rate > 0:
            row.pi_rate = row.pi_od_rate
        elif row.pi_tp_rate and row.pi_tp_rate > 0:
            row.pi_rate = row.pi_tp_rate
        else:
            row.pi_rate = 0.0

        if row.po_type == 'On OD and TP':
            row.po_rate = f"{row.po_od_rate or 0:.2f}+{row.po_tp_rate or 0:.2f}"
        elif row.po_net_rate and row.po_net_rate > 0:
            row.po_rate = row.po_net_rate
        elif row.po_od_rate and row.po_od_rate > 0:
            row.po_rate = row.po_od_rate
        elif row.po_tp_rate and row.po_tp_rate > 0:
            row.po_rate = row.po_tp_rate
        else:
            row.po_rate = 0.0

        results.append(row)

    return results, False
