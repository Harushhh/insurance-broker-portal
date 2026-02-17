from django.shortcuts import render
import csv
from datetime import datetime
from django.db.models import Count, Max

from .models import (
    RateMaster, YesNoNAMaster,
    ProductMaster, SubProductMaster, PolicyTypeMaster,
    FuelTypeMaster, MakeModelClassMaster
)

# ---------- DATE PARSER ----------
def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%d/%m/%Y").date()
    except:
        return None

# ---------- YES / NO / NA PARSER ----------
def parse_yes_no_na(value):
    if not value:
        return YesNoNAMaster.objects.get(code="NA")

    v = str(value).strip().lower()

    if v in ["yes", "y", "true", "1"]:
        return YesNoNAMaster.objects.get(code="YES")
    elif v in ["no", "n", "false", "0"]:
        return YesNoNAMaster.objects.get(code="NO")
    else:
        return YesNoNAMaster.objects.get(code="NA")


# helper: works for all master tables (SubProduct / PolicyType / FuelType / MakeModelClass)
def resolve_master(value, ModelClass):
    """
    If value is number -> find by id
    If value is text -> find/create by name
    """
    if value is None:
        return None

    v = str(value).strip()
    if not v:
        return None

    if v.isdigit():
        return ModelClass.objects.filter(id=int(v)).first()

    obj, _ = ModelClass.objects.get_or_create(name=v)
    return obj


# ---------- CSV UPLOAD ----------
def upload_csv(request):
    if request.method == "POST" and request.FILES.get("file"):
        csv_file = request.FILES["file"]
        decoded_file = csv_file.read().decode("utf-8-sig").splitlines()
        reader = csv.DictReader(decoded_file, skipinitialspace=True)

        inserted = 0
        errors = []

        for i, row in enumerate(reader, start=2):
            try:
                # ---------- PRODUCT ----------
                product_val = str(row.get("product", "")).strip()
                product_obj = None
                if product_val:
                    # if your ProductMaster table uses numeric id
                    if product_val.isdigit():
                        product_obj = ProductMaster.objects.filter(id=int(product_val)).first()
                    else:
                        # otherwise create/find by name
                        product_obj, _ = ProductMaster.objects.get_or_create(name=product_val)

                # ---------- OTHER MASTERS ----------
                sub_product_obj = resolve_master(row.get("sub_product"), SubProductMaster)
                policy_type_obj = resolve_master(row.get("policy_type"), PolicyTypeMaster)
                fuel_type_obj = resolve_master(row.get("fuel_type"), FuelTypeMaster)
                mmc_obj = resolve_master(row.get("make_model_class"), MakeModelClassMaster)

                # ---------- YES / NO / NA ----------
                is_ncb_obj = parse_yes_no_na(row.get("is_ncb"))
                is_cpa_obj = parse_yes_no_na(row.get("is_cpa"))
                is_zd_obj = parse_yes_no_na(row.get("is_zd"))

                # ✅ IMPORTANT: NO "group" FIELD PASSED HERE
                RateMaster.objects.create(
                    new_vehicle_makes=row.get("new_vehicle_makes") or None,
                    new_rto_list=row.get("new_rto_list") or None,
                    insurer_vertical=row.get("insurer_vertical") or None,
                    insurance_company=str(row.get("insurance_company", "")).strip(),

                    product=product_obj,
                    sub_product=sub_product_obj,
                    policy_type=policy_type_obj,
                    fuel_type=fuel_type_obj,
                    make_model_class=mmc_obj,

                    vehicle_age_min=int(float(row.get("vehicle_age_min") or 0)),
                    vehicle_age_max=int(float(row.get("vehicle_age_max") or 0)),

                    pi_od_rate=float(row.get("pi_od_rate") or 0),
                    pi_tp_rate=float(row.get("pi_tp_rate") or 0),
                    pi_tp_2=float(row.get("pi_tp_2") or 0),
                    pi_tp_3=float(row.get("pi_tp_3") or 0),
                    pi_tp_4=float(row.get("pi_tp_4") or 0),
                    pi_tp_5=float(row.get("pi_tp_5") or 0),

                    pi_net_rate=float(row.get("pi_net_rate") or 0),
                    pi_flat_amount=float(row.get("pi_flat_amount") or 0),
                    pi_vli=float(row.get("pi_vli") or 0),
                    pi_type=row.get("pi_type") or None,

                    tariff_min=float(row.get("tariff_min") or 0),
                    tariff_max=float(row.get("tariff_max") or 0),

                    is_ncb=is_ncb_obj,
                    is_cpa=is_cpa_obj,
                    is_zd=is_zd_obj,

                    cc_min=int(float(row.get("cc_min") or 0)),
                    cc_max=int(float(row.get("cc_max") or 0)),

                    from_date=parse_date(row.get("from_date")),
                    to_date=parse_date(row.get("to_date")),

                    user_id=int(float(row.get("user_id") or 0)) if row.get("user_id") else None,
                    sc_min=float(row.get("sc_min") or 0),
                    sc_max=float(row.get("sc_max") or 0),

                    veh_use=row.get("veh_use") or None,
                    add_tnc=row.get("add_tnc") or None,
                    remarks=row.get("remarks") or None,

                    po_type=row.get("po_type") or None,
                    po_od_rate=float(row.get("po_od_rate") or 0),
                    po_tp_rate=float(row.get("po_tp_rate") or 0),
                    po_net_rate=float(row.get("po_net_rate") or 0),
                    po_flat_amount=float(row.get("po_flat_amount") or 0),
                )

                inserted += 1

            except Exception as e:
                errors.append(f"Row {i}: {str(e)}")

        return render(request, "upload.html", {
            "summary": {"inserted": inserted, "duplicates": 0, "errors": len(errors)},
            "errors": errors
        })

    return render(request, "upload.html")


# ---------- DASHBOARD (GROUPED) ----------

def dashboard(request):
    # IMPORTANT:
    # We DO NOT include new_rto_list and fuel_type in grouping
    # because we want to MERGE those values inside the group.
    group_fields = [
        "new_vehicle_makes",
        "insurer_vertical",
        "insurance_company",
        "product",
        "sub_product",
        "policy_type",
        "vehicle_age_min",
        "vehicle_age_max",
        "make_model_class",
        "pi_od_rate",
        "pi_tp_rate",
        "pi_tp_2",
        "pi_tp_3",
        "pi_tp_4",
        "pi_tp_5",
        "pi_net_rate",
        "pi_flat_amount",
        "pi_vli",
        "pi_type",
        "tariff_min",
        "tariff_max",
        "is_ncb",
        "is_cpa",
        "cc_min",
        "cc_max",
        "is_zd",
        "from_date",
        "to_date",
        "sc_min",
        "sc_max",
        "add_tnc",
    ]

    grouped = (
        RateMaster.objects
        .values(*group_fields)
        .annotate(
            records_in_group=Count("id"),
            latest_id=Max("id"),
        )
        .order_by("-latest_id")
    )

    latest_ids = [g["latest_id"] for g in grouped]

    data = (
        RateMaster.objects
        .select_related(
            "product", "sub_product", "policy_type", "fuel_type",
            "make_model_class", "is_ncb", "is_cpa", "is_zd"
        )
        .filter(id__in=latest_ids)
        .order_by("-id")
    )

    count_map = {g["latest_id"]: g["records_in_group"] for g in grouped}
    grouped_map = {g["latest_id"]: g for g in grouped}

    # attach counts + build comma lists for NEW_RTO_LIST and FUEL_TYPE
    for row in data:
        row.records_in_group = count_map.get(row.id, 1)

        g = grouped_map.get(row.id)
        if not g:
            continue

        # build filter for all rows in this group
        criteria = {}
        for f in group_fields:
            field_obj = RateMaster._meta.get_field(f)
            if field_obj.is_relation:   # ForeignKey
                criteria[f"{f}_id"] = getattr(row, f"{f}_id")
            else:
                criteria[f] = getattr(row, f)

        all_rows = RateMaster.objects.filter(**criteria).select_related("fuel_type")

        # NEW_RTO_LIST -> unique, comma joined (keeps order)
        rto_seen = set()
        rto_list = []
        for rto in all_rows.values_list("new_rto_list", flat=True):
            if not rto:
                continue
            rto = str(rto).strip()
            if rto and rto not in rto_seen:
                rto_seen.add(rto)
                rto_list.append(rto)

        # FUEL_TYPE -> unique names only, comma joined (keeps order)
        fuel_seen = set()
        fuel_list = []
        for ft in all_rows.values_list("fuel_type__name", flat=True):
            if not ft:
                continue
            ft = str(ft).strip()
            if ft and ft not in fuel_seen:
                fuel_seen.add(ft)
                fuel_list.append(ft)

        # overwrite displayed values on dashboard
        row.new_rto_list_joined = ", ".join(rto_list)
        row.fuel_type_joined = ", ".join(fuel_list)

    field_names = [f.name for f in RateMaster._meta.fields]
    field_names = ["records_in_group"] + field_names

    return render(request, "dashboard.html", {
        "data": data,
        "field_names": field_names,
        "total": data.count()
    })