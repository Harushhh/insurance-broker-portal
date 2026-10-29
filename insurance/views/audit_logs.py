"""Security Audit & History Log and the Motor/Health points-search log pages, with their Excel exports."""
from django.contrib.auth.models import User
from django.utils import timezone
from django.shortcuts import render
from django.http import HttpResponse
import ast
from datetime import timedelta
from openpyxl import Workbook
from ..constants import POINTS_SEARCH_ACTIONS, SECURITY_AUDIT_LOG_ACTIONS
from ..models import ProductMaster, SubProductMaster, FuelTypeMaster, MakeModelClassMaster, AuditLog
from ..services.audit_logs import _resolve_points_log_param
import logging


logger = logging.getLogger(__name__)


# -------------------------
# AUDIT TRAIL LOGS
# -------------------------
def _filtered_audit_logs(request):
    # Bounded to the last 7 days to match the retention window enforced by
    # cleanup_security_audit_logs (insurance/tasks.py).
    retention_cutoff = timezone.now() - timedelta(days=7)
    qs = AuditLog.objects.filter(
        action__in=SECURITY_AUDIT_LOG_ACTIONS, timestamp__gte=retention_cutoff
    ).select_related("user").order_by("-timestamp")

    user_id = (request.GET.get("user") or "").strip()
    action = (request.GET.get("action") or "").strip()
    date_range = (request.GET.get("date_range") or "").strip()

    if user_id:
        qs = qs.filter(user_id=user_id)
    if action:
        qs = qs.filter(action=action)
    if date_range:
        dates = date_range.split(" - ")
        if len(dates) == 2:
            d_from, d_to = dates[0].strip(), dates[1].strip()
            if d_from and d_to:
                qs = qs.filter(timestamp__date__gte=d_from, timestamp__date__lte=d_to)

    return qs, {"user": user_id, "action": action, "date_range": date_range}


def audit_logs(request):
    qs, selected = _filtered_audit_logs(request)

    # AuditLog's default ordering (-timestamp) makes .distinct() on a single
    # column ineffective at the SQL level (timestamp isn't in the SELECT, so
    # "DISTINCT action" ends up including every differently-timestamped
    # row) - dedupe in Python instead of relying on DB-level DISTINCT here.
    # Scoped to SECURITY_AUDIT_LOG_ACTIONS to match _filtered_audit_logs's base filter.
    tracked_qs = AuditLog.objects.filter(action__in=SECURITY_AUDIT_LOG_ACTIONS)
    user_ids = set(tracked_qs.exclude(user__isnull=True).values_list("user_id", flat=True))
    user_choices = User.objects.filter(id__in=user_ids).order_by("username")
    action_choices = sorted(set(tracked_qs.values_list("action", flat=True)))

    return render(request, "audit_log.html", {
        "logs": qs[:200],
        "user_choices": user_choices,
        "action_choices": action_choices,
        "selected": selected,
    })


def export_audit_log_xlsx(request):
    qs, _ = _filtered_audit_logs(request)

    wb = Workbook()
    ws = wb.active
    ws.title = "Audit Log"
    ws.append(["ID", "Date & Time", "User", "Action Type", "Details"])

    for log in qs[:2000]:
        ws.append([
            log.id,
            log.timestamp.strftime("%Y-%m-%d %H:%M:%S") if log.timestamp else "",
            log.user.username if log.user else "System",
            log.action,
            log.details,
        ])

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="audit_log.xlsx"'
    wb.save(response)
    return response


# -------------------------
# POINTS AUDIT LOGS (Motor + Health "Check Eligibility" search trail)
# -------------------------
def points_audit_logs(request):
    retention_cutoff = timezone.now() - timedelta(days=7)

    type_filter = (request.GET.get("type") or "").strip().lower()
    action_filter = {
        "motor": ["MOTOR_POINTS_SEARCH"],
        "health": ["HEALTH_POINTS_SEARCH"],
    }.get(type_filter, POINTS_SEARCH_ACTIONS)

    # Other active filters (vehicle_no, username, ...), preserved when a
    # Motor/Health/All toggle link is clicked so switching type doesn't
    # silently drop them. Trailing "&" so the template can just append
    # "type=...", even when this is empty.
    _qs_without_type = request.GET.copy()
    _qs_without_type.pop("type", None)
    filter_base_qs = _qs_without_type.urlencode() + "&" if _qs_without_type else ""

    qs = AuditLog.objects.filter(
        action__in=action_filter, timestamp__gte=retention_cutoff
    ).select_related("user").order_by("-timestamp")

    vehicle_no_filter = (request.GET.get("vehicle_no") or "").strip()
    policy_holder_name_filter = (request.GET.get("policy_holder_name") or "").strip()
    insurance_company_filter = (request.GET.get("insurance_company") or "").strip()
    username_filter = (request.GET.get("username") or "").strip()
    # Health-tab equivalents of the three Motor-only fields above (Vehicle
    # No./Policy Holder/Make-Model Name don't apply to Health searches) --
    # same field names the Health Points Checker itself submits, so they
    # match what's actually stored in a HEALTH_POINTS_SEARCH log's details.
    product_name_filter = (request.GET.get("product_name") or "").strip()
    business_type_filter = (request.GET.get("business_type") or "").strip()
    policy_category_filter = (request.GET.get("policy_category") or "").strip()

    if vehicle_no_filter:
        qs = qs.filter(details__icontains=vehicle_no_filter)
    if policy_holder_name_filter:
        qs = qs.filter(details__icontains=policy_holder_name_filter)
    if insurance_company_filter:
        qs = qs.filter(details__icontains=insurance_company_filter)
    if username_filter:
        qs = qs.filter(user__username__icontains=username_filter)
    if product_name_filter:
        qs = qs.filter(details__icontains=product_name_filter)
    if business_type_filter:
        qs = qs.filter(details__icontains=business_type_filter)
    if policy_category_filter:
        qs = qs.filter(details__icontains=policy_category_filter)

    logs = qs[:500]

    all_logs_for_dropdowns = AuditLog.objects.filter(
        action__in=action_filter, timestamp__gte=retention_cutoff
    ).select_related("user").order_by("-timestamp")[:1000]
    unique_vehicles = set()
    unique_holders = set()
    unique_companies = set()
    unique_users = set()
    unique_health_products = set()
    unique_health_sub_products = set()
    unique_health_plan_types = set()

    for log in all_logs_for_dropdowns:
        if log.user and log.user.username:
            unique_users.add(log.user.username)

        try:
            clean_str = log.details.replace("Eligibility Check Parameters: ", "")
            params_dict = ast.literal_eval(clean_str)

            flat_params = {}
            if isinstance(params_dict, dict):
                for k, v in params_dict.items():
                    if isinstance(v, list) and len(v) > 0:
                        flat_params[k] = str(v[0]).strip()
                    else:
                        flat_params[k] = str(v).strip()

            if flat_params.get("vehicle_no"):
                unique_vehicles.add(flat_params["vehicle_no"])
            if flat_params.get("policy_holder_name"):
                unique_holders.add(flat_params["policy_holder_name"])
            if flat_params.get("make_names"):
                unique_companies.add(flat_params["make_names"])
            if flat_params.get("product_name"):
                unique_health_products.add(flat_params["product_name"])
            if flat_params.get("business_type"):
                unique_health_sub_products.add(flat_params["business_type"])
            if flat_params.get("policy_category"):
                unique_health_plan_types.add(flat_params["policy_category"])
        except Exception:
            logger.debug("Skipping unreadable search parameters in a points-search log entry", exc_info=True)

    # Loaded once and reused for every row below, instead of running up to 4
    # separate ProductMaster/SubProductMaster/MakeModelClassMaster/FuelTypeMaster
    # queries per log row (which, at 500 rows, meant up to ~2000 queries on
    # this one page load).
    product_names = dict(ProductMaster.objects.values_list("id", "name"))
    sub_product_names = dict(SubProductMaster.objects.values_list("id", "name"))
    make_model_class_names = dict(MakeModelClassMaster.objects.values_list("id", "name"))
    fuel_type_names = dict(FuelTypeMaster.objects.values_list("id", "name"))

    for log in logs:
        try:
            clean_str = log.details.replace("Eligibility Check Parameters: ", "")
            params_dict = ast.literal_eval(clean_str)

            flat_params = {}
            if isinstance(params_dict, dict):
                for k, v in params_dict.items():
                    if isinstance(v, list) and len(v) > 0:
                        flat_params[k] = v[0]
                    else:
                        flat_params[k] = v

            if flat_params.get("product") and str(flat_params["product"]).isdigit():
                name = product_names.get(int(flat_params["product"]))
                if name:
                    flat_params["product"] = name

            if flat_params.get("sub_product") and str(flat_params["sub_product"]).isdigit():
                name = sub_product_names.get(int(flat_params["sub_product"]))
                if name:
                    flat_params["sub_product"] = name

            if flat_params.get("make_model_class") and str(flat_params["make_model_class"]).isdigit():
                name = make_model_class_names.get(int(flat_params["make_model_class"]))
                if name:
                    flat_params["make_model_class"] = name

            if flat_params.get("fuel") and str(flat_params["fuel"]).isdigit():
                name = fuel_type_names.get(int(flat_params["fuel"]))
                if name:
                    flat_params["fuel"] = name

            log.params = flat_params
        except Exception:
            logger.debug("Could not parse points-search parameters for audit log %s", log.pk, exc_info=True)
            log.params = {}

    return render(request, "points_audit_logs.html", {
        "logs": logs,
        "unique_vehicles": sorted(list(unique_vehicles)),
        "unique_holders": sorted(list(unique_holders)),
        "unique_companies": sorted(list(unique_companies)),
        "unique_users": sorted(list(unique_users)),
        "unique_health_products": sorted(list(unique_health_products)),
        "unique_health_sub_products": sorted(list(unique_health_sub_products)),
        "unique_health_plan_types": sorted(list(unique_health_plan_types)),
        "filter_base_qs": filter_base_qs,
        "selected": {
            "vehicle_no": vehicle_no_filter,
            "policy_holder_name": policy_holder_name_filter,
            "insurance_company": insurance_company_filter,
            "username": username_filter,
            "product_name": product_name_filter,
            "business_type": business_type_filter,
            "policy_category": policy_category_filter,
            "type": type_filter,
        }
    })


def export_points_audit_logs_xlsx(request):
    """XLSX twin of points_audit_logs -- same filters, same per-row field
    resolution, but without the [:500] display cap (7-day retention already
    bounds the result set) so it always reflects the full filtered table."""
    retention_cutoff = timezone.now() - timedelta(days=7)

    type_filter = (request.GET.get("type") or "").strip().lower()
    action_filter = {
        "motor": ["MOTOR_POINTS_SEARCH"],
        "health": ["HEALTH_POINTS_SEARCH"],
    }.get(type_filter, POINTS_SEARCH_ACTIONS)

    qs = AuditLog.objects.filter(
        action__in=action_filter, timestamp__gte=retention_cutoff
    ).select_related("user").order_by("-timestamp")

    vehicle_no_filter = (request.GET.get("vehicle_no") or "").strip()
    policy_holder_name_filter = (request.GET.get("policy_holder_name") or "").strip()
    insurance_company_filter = (request.GET.get("insurance_company") or "").strip()
    username_filter = (request.GET.get("username") or "").strip()
    product_name_filter = (request.GET.get("product_name") or "").strip()
    business_type_filter = (request.GET.get("business_type") or "").strip()
    policy_category_filter = (request.GET.get("policy_category") or "").strip()

    if vehicle_no_filter:
        qs = qs.filter(details__icontains=vehicle_no_filter)
    if policy_holder_name_filter:
        qs = qs.filter(details__icontains=policy_holder_name_filter)
    if insurance_company_filter:
        qs = qs.filter(details__icontains=insurance_company_filter)
    if username_filter:
        qs = qs.filter(user__username__icontains=username_filter)
    if product_name_filter:
        qs = qs.filter(details__icontains=product_name_filter)
    if business_type_filter:
        qs = qs.filter(details__icontains=business_type_filter)
    if policy_category_filter:
        qs = qs.filter(details__icontains=policy_category_filter)

    # Loaded once and reused per row, same reasoning as points_audit_logs
    # itself (avoids up to 4 extra queries per row).
    product_names = dict(ProductMaster.objects.values_list("id", "name"))
    sub_product_names = dict(SubProductMaster.objects.values_list("id", "name"))
    make_model_class_names = dict(MakeModelClassMaster.objects.values_list("id", "name"))
    fuel_type_names = dict(FuelTypeMaster.objects.values_list("id", "name"))

    wb = Workbook()
    ws = wb.active
    ws.title = "Points Search Logs"
    ws.append([
        "ID", "User", "Search Type", "Search Time", "Target Date",
        "Vehicle No.", "Policy Holder Name",
        "Product", "Sub Product",
        "Make", "Class", "RTO Code", "Mfg Year", "Fuel", "CC", "SC",
        "Plan Type", "Sum Assured", "Age", "Pincode", "Policy Term",
        "ZD", "CPA", "NCB",
    ])

    for log in qs.iterator(chunk_size=2000):
        is_health = log.action == "HEALTH_POINTS_SEARCH"
        flat_params = {}
        try:
            clean_str = log.details.replace("Eligibility Check Parameters: ", "")
            params_dict = ast.literal_eval(clean_str)
            if isinstance(params_dict, dict):
                for k, v in params_dict.items():
                    flat_params[k] = v[0] if isinstance(v, list) and len(v) > 0 else v
        except Exception:
            logger.debug("Could not parse points-search parameters for audit log %s", log.pk, exc_info=True)

        product = _resolve_points_log_param(flat_params, "product", product_names)
        sub_product = _resolve_points_log_param(flat_params, "sub_product", sub_product_names)
        make_model_class = _resolve_points_log_param(flat_params, "make_model_class", make_model_class_names)
        fuel = _resolve_points_log_param(flat_params, "fuel", fuel_type_names)

        ws.append([
            log.id,
            log.user.username if log.user else "System",
            "Health" if is_health else "Motor",
            log.timestamp.strftime("%Y-%m-%d %H:%M:%S") if log.timestamp else "",
            flat_params.get("target_date") or "",
            flat_params.get("vehicle_no") or "",
            flat_params.get("policy_holder_name") or "",
            (flat_params.get("product_name") if is_health else product) or "",
            (flat_params.get("business_type") if is_health else sub_product) or "",
            flat_params.get("make_names") or "",
            make_model_class or "",
            flat_params.get("rto_code") or "",
            flat_params.get("mfg_year") or "",
            fuel or "",
            flat_params.get("cc") or "",
            flat_params.get("sc") or "",
            flat_params.get("policy_category") or "",
            flat_params.get("sum_insured") or "",
            flat_params.get("age") or "",
            flat_params.get("pincode") or "",
            flat_params.get("policy_term") or "",
            "YES" if flat_params.get("is_zd") == "YES" else "NO",
            "YES" if flat_params.get("is_cpa") == "YES" else "NO",
            "YES" if flat_params.get("is_ncb") == "YES" else "NO",
        ])

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="points_search_logs.xlsx"'
    wb.save(response)
    return response
