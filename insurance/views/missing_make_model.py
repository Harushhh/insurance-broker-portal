"""Missing Make/Model page: MIS rows whose make/model matched no master, with add-to-master and resolve actions."""
from django.contrib import messages
from django.shortcuts import render, redirect
from django.urls import reverse
from django.http import HttpResponse
from django.core.paginator import Paginator
from django.core.cache import cache
from django.db import transaction
import json
from urllib.parse import urlencode
from datetime import datetime
from openpyxl import Workbook
from ..models import MakeModelMaster, AuditLog, MissingMakeModelManualResolution
from ..services.masters import RTO_MAKE_CHOICES_CACHE_KEY
from ..services.missing_make_model import _annotate_make_model_resolution, _append_make_model_cluster_item, _filter_missing_make_model, _missing_make_model_groups, _missing_make_model_manual_key_from_post
from ..services.rate_master_checks import _sync_unsynced_mis_files


# -------------------------
# MISSING MAKE/MODEL
# -------------------------
# The single most actionable slice of Rate Master Health: MIS policy rows that
# failed RULE 5a because their "make + model" string matched NO MakeModelMaster
# cluster entry at all. Every one of those is fixed by adding that value to a
# master cluster -- unlike the sibling Rule 5a failure ("resolved to master
# group(s) [...] but no candidate rate row lists that group"), which is a gap in
# the rate grid, or the blank-make/model variant, which is a source-data
# problem. Only the first is in scope here; see the reason prefix constant.

MISSING_MAKE_MODEL_BATCH_SIZE = 50


# Longest value this page will write into a cluster. make_model_cluster is a
# TextField so there's no hard limit -- this is a sanity bound against a pasted
# essay reaching the master table through the Add to Master form.
MISSING_MM_MAX_VALUE_LEN = 500


MISSING_MM_FILTER_KEYS = ("insurer", "product", "status", "q", "date_from", "date_to", "page")


def _missing_make_model_selection(request):
    """Parses the page's filter params off a GET or POST, tolerating bad dates."""
    src = request.POST if request.method == "POST" else request.GET
    raw = {k: (src.get(k) or "").strip() for k in MISSING_MM_FILTER_KEYS}
    if raw["status"] not in ("missing", "resolved", "all"):
        raw["status"] = "missing"

    dates = {}
    for key in ("date_from", "date_to"):
        dates[key] = None
        if raw[key]:
            try:
                dates[key] = datetime.strptime(raw[key], "%Y-%m-%d").date()
            except ValueError:
                raw[key] = ""     # same "ignore an unparseable date" tolerance
    return raw, dates                 # rate_master_health's own filters apply


def _missing_make_model_redirect(request):
    """
    Back to the page with its filters intact. Rebuilt from the KNOWN filter
    params rather than a posted `next=` or HTTP_REFERER, same as
    _overlap_redirect -- there's no open-redirect surface this way.
    """
    raw, _ = _missing_make_model_selection(request)
    params = {k: v for k, v in raw.items() if v}
    url = reverse("missing_make_model")
    return redirect(f"{url}?{urlencode(params)}" if params else url)


def missing_make_model(request):
    _sync_unsynced_mis_files()          # same backfill rate_master_health does

    raw, dates = _missing_make_model_selection(request)

    groups = _missing_make_model_groups(date_from=dates["date_from"], date_to=dates["date_to"])
    _annotate_make_model_resolution(groups)

    # Dropdown options come from the unfiltered aggregate, so applying a filter
    # never removes its own option from the list.
    insurer_list = sorted({g["insurer"] for g in groups if g["insurer"]})
    product_list = sorted({g["product"] for g in groups if g["product"]})

    total_all = len(groups)
    total_missing = sum(1 for g in groups if not g["resolved"])

    rows = _filter_missing_make_model(groups, raw["insurer"], raw["product"], raw["status"], raw["q"])

    paginator = Paginator(rows, MISSING_MAKE_MODEL_BATCH_SIZE)
    try:
        page_number = int(raw["page"] or 1)
    except ValueError:
        page_number = 1
    page_obj = paginator.get_page(page_number)
    elided_page_range = list(paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1))

    from ..mapping_engine import MAKE_MODEL_MIN_WORD_MATCH

    return render(request, "missing_make_model.html", {
        "page_obj": page_obj,
        "elided_page_range": elided_page_range,
        "total": paginator.count,
        "total_all": total_all,
        "total_missing": total_missing,
        "total_resolved": total_all - total_missing,
        "total_policies": sum(g["policy_count"] for g in groups),
        "policies_shown": sum(g["policy_count"] for g in rows),
        "insurer_list": insurer_list,
        "product_list": product_list,
        "master_rows": list(
            MakeModelMaster.objects.order_by("make_model_name").values("id", "make_model_name")
        ),
        "min_word_match": MAKE_MODEL_MIN_WORD_MATCH,
        "selected": raw,
    })


def export_missing_make_model_xlsx(request):
    """
    Same aggregate as the Missing Make/Model page. Honours every content filter
    (insurer / product / status / search / dates) but ignores `page`, since that
    only controls the on-screen table's pagination -- same convention
    export_grid_summary_xlsx documents.
    """
    raw, dates = _missing_make_model_selection(request)
    groups = _missing_make_model_groups(date_from=dates["date_from"], date_to=dates["date_to"])
    _annotate_make_model_resolution(groups)
    rows = _filter_missing_make_model(groups, raw["insurer"], raw["product"], raw["status"], raw["q"])

    wb = Workbook()
    ws = wb.active
    ws.title = "Missing Make Model"
    ws.append([
        "STATUS", "MAKE", "MODEL", "FAILING VALUE", "VEHICLE CLASS", "PRODUCT",
        "SUB PRODUCT", "INSURER", "POLICIES", "MIS FILES", "FIRST SEEN", "LAST SEEN",
        "RESOLVED BY",
    ])

    for g in rows:
        if g["resolved_names"]:
            resolved_by = ", ".join(g["resolved_names"])
        elif g["resolved_manual"]:
            resolved_by = "(marked resolved manually)"
        else:
            resolved_by = ""
        ws.append([
            "Matched" if g["resolved"] else "Failed",
            g["make"], g["model"], g["value"], g["vehicle_class"], g["product"],
            g["sub_product"], g["insurer"],
            g["policy_count"], g["file_count"],
            g["first_seen"].strftime("%d %b %Y") if g["first_seen"] else "",
            g["last_seen"].strftime("%d %b %Y") if g["last_seen"] else "",
            resolved_by,
        ])

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="missing_make_model.xlsx"'
    wb.save(response)
    return response


@transaction.atomic
def add_missing_make_model_to_master(request):
    """
    The one write path into MakeModelMaster.make_model_cluster outside the bulk
    importer (api_upload_chunk). POST-only; a GET just bounces back to the page,
    same shape as deactivate_rate_group.
    """
    if request.method != "POST":
        return _missing_make_model_redirect(request)

    from ..mapping_engine import build_make_model_cluster_index, resolve_make_model_with_index

    value = " ".join((request.POST.get("make_model_value") or "").split())
    if not value:
        messages.error(request, "Nothing was added: no make/model value was submitted.")
        return _missing_make_model_redirect(request)

    # A comma would split the appended text into two cluster items, and
    # fuzzy_match_make_model matches per item -- a 1-word item can never reach
    # MAKE_MODEL_MIN_WORD_MATCH, so the append would be silently useless.
    had_comma = "," in value
    if had_comma:
        value = " ".join(value.replace(",", " ").split())

    if len(value) > MISSING_MM_MAX_VALUE_LEN:
        messages.error(
            request,
            f"Nothing was added: that make/model value is longer than "
            f"{MISSING_MM_MAX_VALUE_LEN} characters.",
        )
        return _missing_make_model_redirect(request)

    # The real hazard of this page: a second cluster containing the same words
    # makes these policies resolve to TWO master groups, turning them into
    # MULTIPLE MATCHES instead of fixing them -- the same Double Rate Risk the
    # Overlaps tab flags and deactivate_rate_group refuses to auto-fix. The
    # default status=missing filter already hides resolved rows, but the server
    # must not rely on the UI for that.
    already = resolve_make_model_with_index(
        build_make_model_cluster_index(
            MakeModelMaster.objects.all(), "make_model_name", "make_model_cluster"
        ),
        value,
    )
    if already and request.POST.get("force") != "1":
        messages.warning(
            request,
            f"'{value}' was not added - it already matches master group(s) "
            f"[{', '.join(sorted(already))}]. Adding it again would make these policies "
            f"resolve to two groups, which turns them into MULTIPLE MATCHES instead of "
            f"fixing them. Re-open the dialog and tick \"Add anyway\" if that is intended.",
        )
        return _missing_make_model_redirect(request)

    target = (request.POST.get("target") or "").strip()
    created = False

    if target == "existing":
        # select_for_update: the cluster append is a read-modify-write on a
        # TextField, so two concurrent adds would otherwise clobber each
        # other. Multi-select: the same value can go into several clusters
        # in one request (e.g. it genuinely belongs to more than one
        # vehicle group), ordered so the audit log/success message read
        # predictably rather than in whatever order the DB returns them.
        ids = [i.strip() for i in request.POST.getlist("master_id") if i.strip()]
        masters = list(
            MakeModelMaster.objects.select_for_update().filter(id__in=ids).order_by("make_model_name")
        )
        if not masters:
            messages.error(request, "Nothing was added: pick at least one Make/Model Master row.")
            return _missing_make_model_redirect(request)
    elif target == "new":
        name = (request.POST.get("new_name") or "").strip()
        if not name:
            messages.error(request, "Nothing was added: give the new Make/Model Master row a name.")
            return _missing_make_model_redirect(request)
        if len(name) > 150:
            messages.error(request, "Nothing was added: that name is longer than 150 characters.")
            return _missing_make_model_redirect(request)
        # make_model_name is unique=True, which is exact-case -- pre-check with
        # iexact so a case variant gets this message instead of an IntegrityError.
        if MakeModelMaster.objects.filter(make_model_name__iexact=name).exists():
            messages.error(
                request,
                f"Nothing was added: '{name}' already exists - pick it from the dropdown instead.",
            )
            return _missing_make_model_redirect(request)
        masters = [MakeModelMaster.objects.create(make_model_name=name)]
        created = True
    else:
        messages.error(request, "Nothing was added: pick an existing master row or name a new one.")
        return _missing_make_model_redirect(request)

    # Stored upper-cased: fuzzy_match_make_model upper-cases BOTH sides, so
    # casing provably can't change matching -- which makes this purely a
    # legibility choice, and importer-sourced clusters all read as upper case.
    stored_value = value.upper()
    changed_masters, unchanged_masters = [], []
    for master in masters:
        # created is only ever True for the single-row "new" target, so this
        # is never ambiguous about which master it refers to.
        if _append_make_model_cluster_item(master, stored_value):
            master.save(update_fields=None if created else ["make_model_cluster", "updated_at"])
            changed_masters.append(master)
        else:
            unchanged_masters.append(master)

    if not changed_masters:
        where = (
            "every selected row" if len(unchanged_masters) > 1
            else f"'{unchanged_masters[0].make_model_name}'"
        )
        messages.info(request, f"'{stored_value}' is already in {where} - nothing to change.")
        return _missing_make_model_redirect(request)

    try:
        policy_count = int(request.POST.get("policy_count") or 0)
    except ValueError:
        policy_count = 0

    names = ", ".join(f"'{m.make_model_name}'" for m in changed_masters)
    AuditLog.objects.create(
        user=request.user,
        action="MAKE MODEL CLUSTER ADD",
        details=(
            f"Added '{stored_value}' to MakeModelMaster {names} "
            f"(id(s) {', '.join(str(m.id) for m in changed_masters)}) from the Missing Make/Model page."
            + (" Created this master row." if created else "")
            + (f" {policy_count} affected MIS policy row(s) reported by the page." if policy_count else "")
            + (" Comma(s) in the submitted value were replaced with spaces." if had_comma else "")
            + (" Added despite already matching another cluster (forced)." if already else "")
            + (f" Already present in {len(unchanged_masters)} other selected row(s)." if unchanged_masters else "")
        ),
    )
    # Mirrors api_upload_chunk: every write to these master tables busts the key
    # get_rto_and_make_choices caches the rate forms' dropdowns under.
    cache.delete(RTO_MAKE_CHOICES_CACHE_KEY)

    messages.success(
        request,
        f"'{stored_value}' added to {names}. It now resolves"
        + (f" - {policy_count} historical failure(s) are marked Resolved" if policy_count else "")
        + ", and new MIS uploads will map these policies. To undo, edit the cluster(s) "
        "on the Make/Model Master page.",
    )
    return _missing_make_model_redirect(request)


@transaction.atomic
def bulk_add_missing_make_model_to_master(request):
    """
    Bulk version of add_missing_make_model_to_master: appends every selected
    row's failing value into ONE master row (existing or new) in a single
    request -- for when several distinct Missing Make/Model rows genuinely
    belong to the same vehicle group (e.g. many Activa variants all really
    are 'two_wheeler_all'). POST-only; a GET just bounces back to the page.
    """
    if request.method != "POST":
        return _missing_make_model_redirect(request)

    from ..mapping_engine import build_make_model_cluster_index, resolve_make_model_with_index

    values = []
    seen = set()
    for raw in request.POST.getlist("group_keys"):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        value = " ".join(str(parsed.get("value") or "").split())
        if not value or value.lower() in seen:
            continue
        seen.add(value.lower())
        values.append(value)

    if not values:
        messages.error(request, "Nothing was added: no rows were selected.")
        return _missing_make_model_redirect(request)

    target = (request.POST.get("target") or "").strip()
    created = False

    if target == "existing":
        # select_for_update: the cluster append is a read-modify-write on a
        # TextField, so two concurrent bulk adds would otherwise clobber
        # each other -- locked once and appended to in a loop below.
        # Multi-select: every selected value goes into every selected master.
        ids = [i.strip() for i in request.POST.getlist("master_id") if i.strip()]
        masters = list(
            MakeModelMaster.objects.select_for_update().filter(id__in=ids).order_by("make_model_name")
        )
        if not masters:
            messages.error(request, "Nothing was added: pick at least one Make/Model Master row.")
            return _missing_make_model_redirect(request)
    elif target == "new":
        name = (request.POST.get("new_name") or "").strip()
        if not name:
            messages.error(request, "Nothing was added: give the new Make/Model Master row a name.")
            return _missing_make_model_redirect(request)
        if len(name) > 150:
            messages.error(request, "Nothing was added: that name is longer than 150 characters.")
            return _missing_make_model_redirect(request)
        if MakeModelMaster.objects.filter(make_model_name__iexact=name).exists():
            messages.error(
                request,
                f"Nothing was added: '{name}' already exists - pick it from the dropdown instead.",
            )
            return _missing_make_model_redirect(request)
        masters = [MakeModelMaster.objects.create(make_model_name=name)]
        created = True
    else:
        messages.error(request, "Nothing was added: pick an existing master row or name a new one.")
        return _missing_make_model_redirect(request)

    force = request.POST.get("force") == "1"
    # Built once up front from the master table's state before this request's
    # own appends below -- same "already resolves elsewhere" Double Rate Risk
    # guard add_missing_make_model_to_master applies per value, just batched.
    index = build_make_model_cluster_index(
        MakeModelMaster.objects.all(), "make_model_name", "make_model_cluster"
    )

    added, skipped, no_op_count, had_comma = [], [], 0, False
    touched_masters = set()
    for raw_value in values:
        value = raw_value[:MISSING_MM_MAX_VALUE_LEN]
        if "," in value:
            had_comma = True
            value = " ".join(value.replace(",", " ").split())
        stored_value = value.upper()

        already = resolve_make_model_with_index(index, value)
        if already and not force:
            skipped.append(stored_value)
            continue
        value_added = False
        for master in masters:
            if _append_make_model_cluster_item(master, stored_value):
                value_added = True
                touched_masters.add(master.id)
        if value_added:
            added.append(stored_value)
        else:
            no_op_count += 1

    for master in masters:
        if master.id in touched_masters:
            master.save(update_fields=None if created else ["make_model_cluster", "updated_at"])

    master_names = ", ".join(f"'{m.make_model_name}'" for m in masters)
    AuditLog.objects.create(
        user=request.user,
        action="MAKE MODEL CLUSTER BULK ADD",
        details=(
            f"Added {len(added)} value(s) to MakeModelMaster {master_names} "
            f"from the Missing Make/Model page: "
            f"{', '.join(added[:10])}" + (f" (+{len(added) - 10} more)" if len(added) > 10 else "") + "."
            + (" Created this master row." if created else "")
            + (f" {no_op_count} value(s) were already present in every selected row." if no_op_count else "")
            + (f" {len(skipped)} value(s) skipped (already match another cluster): "
               f"{', '.join(skipped[:10])}." if skipped else "")
            + (" Comma(s) in submitted value(s) were replaced with spaces." if had_comma else "")
        ),
    )
    cache.delete(RTO_MAKE_CHOICES_CACHE_KEY)

    if added:
        messages.success(
            request,
            f"{len(added)} value(s) added to {master_names}. They now resolve, and new "
            f"MIS uploads will map these policies."
            + (f" {no_op_count} were already there." if no_op_count else "")
            + (f" {len(skipped)} skipped - already match another cluster; re-select them and tick "
               f"\"Add anyway\" to include them." if skipped else ""),
        )
    elif skipped:
        messages.warning(
            request,
            f"Nothing was added - all {len(skipped)} selected value(s) already match another cluster. "
            f"Re-select them and tick \"Add anyway\" if that is intended.",
        )
    else:
        messages.info(request, f"Nothing to add - all selected values are already in {master_names}.")
    return _missing_make_model_redirect(request)


def mark_missing_make_model_resolved(request):
    """
    Manual override: mark one Missing Make/Model row Resolved without writing
    to MakeModelMaster through this page -- for a value already fixed by
    editing the Make/Model Master dashboard directly, or through the bulk
    Rate Master importer, that the live re-check hasn't (yet) picked up.
    POST-only; a GET just bounces back to the page.
    """
    if request.method != "POST":
        return _missing_make_model_redirect(request)

    value, product, sub_product, insurer, vehicle_class = _missing_make_model_manual_key_from_post(request.POST, prefix="mm_mark_")
    if not value:
        messages.error(request, "Nothing was marked resolved: no value was submitted.")
        return _missing_make_model_redirect(request)

    _, created = MissingMakeModelManualResolution.objects.get_or_create(
        value=value, product=product, sub_product=sub_product,
        insurer=insurer, vehicle_class=vehicle_class,
        defaults={"resolved_by": request.user},
    )

    if created:
        try:
            policy_count = int(request.POST.get("policy_count") or 0)
        except ValueError:
            policy_count = 0
        AuditLog.objects.create(
            user=request.user,
            action="MAKE MODEL MANUALLY RESOLVED",
            details=(
                f"Marked '{value}' (product={product or '-'}, sub_product={sub_product or '-'}, "
                f"insurer={insurer or '-'}, vehicle_class={vehicle_class or '-'}) Resolved on the "
                f"Missing Make/Model page without a cluster write."
                + (f" {policy_count} affected MIS policy row(s) reported by the page." if policy_count else "")
            ),
        )
        messages.success(request, f"'{value}' marked Resolved.")
    else:
        messages.info(request, f"'{value}' was already marked Resolved.")
    return _missing_make_model_redirect(request)


def unmark_missing_make_model_resolved(request):
    """Undo a manual Resolved mark. POST-only; a GET just bounces back to the page."""
    if request.method != "POST":
        return _missing_make_model_redirect(request)

    value, product, sub_product, insurer, vehicle_class = _missing_make_model_manual_key_from_post(request.POST, prefix="mm_mark_")
    deleted, _ = MissingMakeModelManualResolution.objects.filter(
        value=value, product=product, sub_product=sub_product,
        insurer=insurer, vehicle_class=vehicle_class,
    ).delete()

    if deleted:
        AuditLog.objects.create(
            user=request.user,
            action="MAKE MODEL MANUAL RESOLUTION UNDONE",
            details=f"Un-marked '{value}' as Resolved on the Missing Make/Model page.",
        )
        messages.success(request, f"'{value}' is no longer marked Resolved.")
    else:
        messages.info(request, f"'{value}' was not manually marked Resolved.")
    return _missing_make_model_redirect(request)


@transaction.atomic
def bulk_mark_missing_make_model_resolved(request):
    """Bulk version of mark_missing_make_model_resolved for several selected rows at once."""
    if request.method != "POST":
        return _missing_make_model_redirect(request)

    keys = []
    seen = set()
    for raw in request.POST.getlist("group_keys"):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        key = _missing_make_model_manual_key_from_post(parsed)
        if key[0] and key not in seen:
            seen.add(key)
            keys.append(key)

    if not keys:
        messages.error(request, "Nothing was marked resolved: no rows were selected.")
        return _missing_make_model_redirect(request)

    created_count = 0
    for value, product, sub_product, insurer, vehicle_class in keys:
        _, created = MissingMakeModelManualResolution.objects.get_or_create(
            value=value, product=product, sub_product=sub_product,
            insurer=insurer, vehicle_class=vehicle_class,
            defaults={"resolved_by": request.user},
        )
        if created:
            created_count += 1

    if created_count:
        AuditLog.objects.create(
            user=request.user,
            action="MAKE MODEL MANUALLY RESOLVED",
            details=(
                f"Marked {created_count} Missing Make/Model row(s) Resolved in bulk without a cluster "
                f"write: {', '.join(k[0] for k in keys[:10])}"
                + (f" (+{len(keys) - 10} more)" if len(keys) > 10 else "") + "."
            ),
        )
        messages.success(
            request,
            f"{created_count} row(s) marked Resolved."
            + (f" {len(keys) - created_count} were already marked." if len(keys) > created_count else ""),
        )
    else:
        messages.info(request, "All selected rows were already marked Resolved.")
    return _missing_make_model_redirect(request)
