"""Policy lock checker, locking / unlocking a policy, the locked policy dashboard and its Excel export."""
from django.utils import timezone
from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse, JsonResponse
import json
import re
from datetime import datetime
from urllib.parse import urlencode
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from ..models import RateMaster, ProductMaster, SubProductMaster, FuelTypeMaster, MakeModelClassMaster, LockedPolicy
from ..services.activity import STATUS, record_activity
from ..services.common import safe_cell
from ..services.make_model import NA_MAKE_MODEL_MAP, get_dynamic_make_model_class_list, get_make_mapping_context, get_translated_make_model
from ..services.policy_lock import _run_policy_lock_checker_search, locked_policies_for


def policy_lock_checker(request):
    results, has_searched, selected = _run_policy_lock_checker_search(request)

    # Reuses the same product->makes mapping motor_payout_rates uses for its
    # Product -> Make Name cascade, rather than recomputing an equivalent
    # unfiltered make list here from scratch.
    _, _, product_makes_mapping_json, make_name_list = get_make_mapping_context()

    insurer_list = RateMaster.objects.exclude(is_deleted="YES").exclude(insurance_company="").values_list(
        "insurance_company", flat=True
    ).distinct().order_by("insurance_company")

    return render(request, "policy_lock_checker.html", {
        "data": results,
        "total_found": len(results),
        "has_searched": has_searched,
        "product_list": ProductMaster.objects.all().order_by("name"),
        "sub_product_list": SubProductMaster.objects.all().order_by("name"),
        "fuel_list": FuelTypeMaster.objects.all().order_by("name"),
        "make_model_class_list": get_dynamic_make_model_class_list(selected["product"]),
        "make_name_list": make_name_list,
        "product_makes_mapping_json": product_makes_mapping_json,
        "insurer_list": insurer_list,

        "make_class_mapping_json": json.dumps(NA_MAKE_MODEL_MAP),
        "all_make_classes_json": json.dumps(list(MakeModelClassMaster.objects.exclude(name__iexact="NA").values('id', 'name'))),

        "selected": selected,
    })


# -------------------------
# LOCK POLICY
# -------------------------
def lock_unlock_policy(request, rate_id):
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "Invalid request."})

    action_type = (request.POST.get("action_type") or "").strip().upper()
    vehicle_no = (request.POST.get("vehicle_no") or "").strip()
    policy_holder_name = (request.POST.get("policy_holder_name") or "").strip()
    target_date = (request.POST.get("target_date") or "").strip()

    if not vehicle_no:
        return JsonResponse({"success": False, "message": "Vehicle No. is required."})

    if action_type != "LOCK":
        return JsonResponse({"success": False, "message": "Invalid action."})
        
    today_str = datetime.today().strftime("%Y-%m-%d")
    if target_date != today_str:
        return JsonResponse({
            "success": False, 
            "message": "Policies can only be locked for today's date. Past or future dates are restricted."
        })

    rate_obj = get_object_or_404(
        RateMaster.objects.select_related("product", "sub_product", "fuel_type").filter(is_deleted="NO"),
        id=rate_id
    )

    po_rate = 0.0
    if rate_obj.po_net_rate and rate_obj.po_net_rate > 0:
        po_rate = rate_obj.po_net_rate
    elif rate_obj.po_od_rate and rate_obj.po_od_rate > 0:
        po_rate = rate_obj.po_od_rate
    elif rate_obj.po_tp_rate and rate_obj.po_tp_rate > 0:
        po_rate = rate_obj.po_tp_rate

    obj, created = LockedPolicy.objects.get_or_create(
        source_rate=rate_obj,
        vehicle_no=vehicle_no,
        policy_holder_name=policy_holder_name,
        defaults={
            "product_name": rate_obj.product.name if rate_obj.product else "",
            "sub_product_name": rate_obj.sub_product.name if rate_obj.sub_product else "",
            "insurance_company": rate_obj.insurance_company,
            "po_type": rate_obj.po_type,
            "po_rate": po_rate,
            "po_flat_amount": rate_obj.po_flat_amount,
            "add_tnc": rate_obj.add_tnc,
            "locked_by": request.user,
            "rto_code": request.POST.get("rto_code", ""),
            "make_name": request.POST.get("make_names", ""),
            "fuel": request.POST.get("fuel", ""),
            "cc": request.POST.get("cc", ""),
            "sc": request.POST.get("sc", ""),
            "mfg_year": request.POST.get("mfg_year", ""),
            "status": "LOCKED",
        }
    )

    # Object-level check: the get_or_create lookup above is keyed on
    # (source_rate, vehicle_no, policy_holder_name) only — not locked_by — so
    # without this, a second user submitting the same vehicle/holder combo
    # would find and silently take over someone else's existing lock
    # (every field below, including locked_by, gets unconditionally
    # overwritten). ADMIN keeps the ability to reassign/fix any lock;
    # everyone else can only touch a lock that's already theirs, or brand new.
    is_admin = request.user.groups.filter(name="ADMIN").exists()
    if not created and obj.locked_by_id and obj.locked_by_id != request.user.id and not is_admin:
        return JsonResponse({
            "success": False,
            "message": "This vehicle/policy holder is already locked by another user.",
        }, status=403)

    obj.product_name = rate_obj.product.name if rate_obj.product else ""
    obj.sub_product_name = rate_obj.sub_product.name if rate_obj.sub_product else ""
    obj.insurance_company = rate_obj.insurance_company
    obj.po_type = rate_obj.po_type
    obj.po_rate = po_rate
    obj.po_flat_amount = rate_obj.po_flat_amount
    obj.add_tnc = rate_obj.add_tnc
    obj.rto_code = request.POST.get("rto_code", "")
    obj.make_name = request.POST.get("make_names", "")
    obj.fuel = request.POST.get("fuel", "")
    obj.cc = request.POST.get("cc", "")
    obj.sc = request.POST.get("sc", "")
    obj.mfg_year = request.POST.get("mfg_year", "")
    obj.status = "LOCKED"
    obj.locked_by = request.user
    obj.locked_at = timezone.now()
    obj.save()
    # Product and insurer only: the vehicle number and policy holder are personal data.
    kind = f"a {obj.product_name} policy" if obj.product_name else "a policy"
    record_activity(
        request.user, STATUS, "policy.locked",
        f"Locked {kind} with {obj.insurance_company or 'an insurer'}", target=obj,
        details={
            "locked_policy_id": obj.id, "rate_id": rate_obj.id, "newly_locked": created,
            "vehicle_no": vehicle_no, "policy_holder_name": policy_holder_name, "target_date": target_date,
            "product": obj.product_name, "sub_product": obj.sub_product_name, "insurer": obj.insurance_company,
            "rto_code": obj.rto_code, "make": obj.make_name, "fuel": obj.fuel, "cc": obj.cc, "sc": obj.sc,
            "mfg_year": obj.mfg_year, "po_type": obj.po_type, "po_rate": obj.po_rate,
            "po_flat_amount": obj.po_flat_amount, "add_tnc": obj.add_tnc,
        },
    )

    return JsonResponse({"success": True, "message": f"Successfully locked policy for {vehicle_no}."})


# -------------------------
# LOCKED POLICY DASHBOARD
# -------------------------
def locked_policy_dashboard(request):
    # Who sees which rows (everyone except ADMIN only their own locked policies) and the four filters live in
    # locked_policies_for(), shared with the Excel export below so the file can never hold a row the page would not show.
    qs, selected, is_admin = locked_policies_for(request)
    qs = qs.select_related("source_rate__product", "source_rate__make_model_class")

    # Filter dropdown option lists must carry the same scoping as qs — for a
    # non-admin, listing e.g. "root" as a possible Locked By value would leak
    # other users' activity even though they can't open those records.
    all_locked = LockedPolicy.objects.select_related("locked_by").all()
    if not is_admin:
        all_locked = all_locked.filter(locked_by=request.user)

    unique_vehicles = sorted(list(set(all_locked.exclude(vehicle_no__isnull=True).exclude(vehicle_no="").values_list("vehicle_no", flat=True))))
    unique_holders = sorted(list(set(all_locked.exclude(policy_holder_name__isnull=True).exclude(policy_holder_name="").values_list("policy_holder_name", flat=True))))
    unique_companies = sorted(list(set(all_locked.exclude(insurance_company__isnull=True).exclude(insurance_company="").values_list("insurance_company", flat=True))))
    unique_users = sorted(list(set(all_locked.exclude(locked_by__isnull=True).values_list("locked_by__username", flat=True))))

    records = list(qs[:300])          # the page shows the newest 300; the Excel export has every matching record

    fuel_name_by_id = {str(f.id): f.name for f in FuelTypeMaster.objects.all()}

    for row in records:
        row.display_fuel = fuel_name_by_id.get((row.fuel or "").strip(), row.fuel)
        if row.source_rate:
            mmc_name = row.source_rate.make_model_class.name.strip().upper() if row.source_rate.make_model_class else "NA"
            
            if mmc_name in ["NA", ""]:
                prod_name = row.source_rate.product.name if row.source_rate.product else ""
                translated_name = get_translated_make_model(prod_name)
                if translated_name:
                    row.source_rate.make_model_class = MakeModelClassMaster(name=translated_name)
                    row.display_make_model_class = translated_name
                else:
                    row.display_make_model_class = "NA"
            else:
                row.display_make_model_class = row.source_rate.make_model_class.name if row.source_rate.make_model_class else ""

    return render(request, "locked_policy_dashboard.html", {
        "records": records,
        "total_records": qs.count(),
        "unique_vehicles": unique_vehicles,
        "unique_holders": unique_holders,
        "unique_companies": unique_companies,
        "unique_users": unique_users,
        "selected": selected,
        # The Export Excel link carries the filters in force, so the file is what the table above is filtered to.
        "export_query": urlencode({name: value for name, value in selected.items() if value}),
    })


# -------------------------
# LOCKED POLICY EXCEL EXPORT
# -------------------------
# The table's columns, in the table's order. PO Rate is a percent number and PO Flat Amount a rupee amount: the unit is
# in the header, the cell is a plain number so Excel can sort and add it up.
LOCKED_EXPORT_HEADERS = [
    "Group ID", "Vehicle No.", "Policy Holder Name", "Insurance Company", "Product", "Sub Product", "PO Type",
    "PO Rate (%)", "PO Flat Amount (₹)", "RTO Code", "Make Name", "Fuel", "CC", "SC", "MFG Year", "Status",
    "Locked By", "Locked At", "Created At",
]
LOCKED_EXPORT_WIDTHS = [10, 16, 26, 38, 14, 12, 10, 12, 18, 10, 28, 10, 8, 6, 10, 11, 16, 22, 22]
LOCKED_EXPORT_DATE_FORMAT = "DD MMM YYYY, hh:mm AM/PM"
_PLAIN_NUMBER = re.compile(r"^(0|[1-9]\d*)(\.\d+)?$")


def _number_if_plain(value):
    """CC, SC and MFG Year are kept as the text the lock form sent. A plain number is written as a number (so Excel can
    sort and filter it); anything else (a range, a dash, leading zeros) stays text, and an empty value stays empty."""
    text = (value or "").strip()
    if not text:
        return None
    if _PLAIN_NUMBER.match(text):
        return float(text) if "." in text else int(text)
    return safe_cell(text)


def _local_naive(moment):
    """An aware datetime as the IST wall-clock time the page shows (Excel has no time zones)."""
    return timezone.localtime(moment).replace(tzinfo=None) if moment else None


def export_locked_policies_xlsx(request):
    """Every locked policy the Locked Policy Dashboard's current filters match, as an Excel file with the table's columns.

    Same rows as the page (locked_policies_for: ADMIN sees all, everyone else only their own), but not cut at 300.
    """
    qs, _selected, _is_admin = locked_policies_for(request)
    fuel_name_by_id = {str(fuel.id): fuel.name for fuel in FuelTypeMaster.objects.all()}

    wb = Workbook()
    ws = wb.active
    ws.title = "Locked Policies"
    ws.append(LOCKED_EXPORT_HEADERS)
    for cell in ws[1]:
        cell.fill = PatternFill("solid", fgColor="0F172A")
        cell.font = Font(bold=True, color="FFFFFF")

    date_columns = (LOCKED_EXPORT_HEADERS.index("Locked At") + 1, LOCKED_EXPORT_HEADERS.index("Created At") + 1)
    for number, row in enumerate(qs.iterator(chunk_size=2000), start=2):
        rate = row.source_rate
        fuel = (row.fuel or "").strip()
        ws.append([
            (rate.group_id or rate.id) if rate else None,
            safe_cell(row.vehicle_no),
            safe_cell(row.policy_holder_name),
            safe_cell(row.insurance_company),
            safe_cell(row.product_name),
            safe_cell(row.sub_product_name),
            safe_cell(row.po_type),
            row.po_rate,
            row.po_flat_amount,
            safe_cell(row.rto_code),
            safe_cell(row.make_name),
            safe_cell(fuel_name_by_id.get(fuel, fuel)),
            _number_if_plain(row.cc),
            _number_if_plain(row.sc),
            _number_if_plain(row.mfg_year),
            row.status,
            safe_cell(row.locked_by.username if row.locked_by else ""),
            _local_naive(row.locked_at),
            _local_naive(row.created_at),
        ])
        for column in date_columns:
            ws.cell(row=number, column=column).number_format = LOCKED_EXPORT_DATE_FORMAT

    for column, width in enumerate(LOCKED_EXPORT_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(column)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(LOCKED_EXPORT_HEADERS))}{ws.max_row}"

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="locked_policies_{timezone.localdate():%Y-%m-%d}.xlsx"'
    wb.save(response)
    return response
