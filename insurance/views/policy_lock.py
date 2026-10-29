"""Policy lock checker, locking / unlocking a policy, and the locked policy dashboard."""
from django.utils import timezone
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
import json
from datetime import datetime
from ..models import RateMaster, ProductMaster, SubProductMaster, FuelTypeMaster, MakeModelClassMaster, LockedPolicy
from ..services.make_model import NA_MAKE_MODEL_MAP, get_dynamic_make_model_class_list, get_make_mapping_context, get_translated_make_model
from ..services.policy_lock import _run_policy_lock_checker_search


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

    return JsonResponse({"success": True, "message": f"Successfully locked policy for {vehicle_no}."})


# -------------------------
# LOCKED POLICY DASHBOARD
# -------------------------
def locked_policy_dashboard(request):
    # Data-privacy scoping: everyone except ADMIN only ever sees their own
    # locked policies — matches page_access_required's existing "ADMIN sees
    # everything, per-page groups see only their own scope" convention.
    is_admin = request.user.groups.filter(name="ADMIN").exists()

    qs = LockedPolicy.objects.select_related(
        "source_rate",
        "source_rate__product",
        "source_rate__make_model_class",
        "locked_by"
    ).all()
    if not is_admin:
        qs = qs.filter(locked_by=request.user)

    vehicle_no = (request.GET.get("vehicle_no") or "").strip()
    policy_holder_name = (request.GET.get("policy_holder_name") or "").strip()
    insurance_company = (request.GET.get("insurance_company") or "").strip()
    locked_by_user = (request.GET.get("locked_by_user") or "").strip()

    if vehicle_no:
        qs = qs.filter(vehicle_no__iexact=vehicle_no)
    if policy_holder_name:
        qs = qs.filter(policy_holder_name__iexact=policy_holder_name)
    if insurance_company:
        qs = qs.filter(insurance_company__iexact=insurance_company)
    if locked_by_user:
        qs = qs.filter(locked_by__username__iexact=locked_by_user)

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

    records = list(qs.order_by("-created_at")[:300])

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
        "selected": {
            "vehicle_no": vehicle_no,
            "policy_holder_name": policy_holder_name,
            "insurance_company": insurance_company,
            "locked_by_user": locked_by_user,
        }
    })
