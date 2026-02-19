from django.shortcuts import render
from django.http import HttpResponse
from django.db.models import Q
import csv
from datetime import datetime
from collections import defaultdict
import hashlib

from openpyxl import Workbook

from .models import (
    RateMaster, YesNoNAMaster,
    ProductMaster, SubProductMaster, PolicyTypeMaster,
    FuelTypeMaster, MakeModelClassMaster,
    RateGroup,   # ✅ group table
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

    value = str(value).strip().lower()

    if value in ["yes", "y", "true", "1"]:
        return YesNoNAMaster.objects.get(code="YES")
    elif value in ["no", "n", "false", "0"]:
        return YesNoNAMaster.objects.get(code="NO")
    else:
        return YesNoNAMaster.objects.get(code="NA")

# ---------- GROUPING FIELDS ----------
GROUP_FIELDS = [
    "new_vehicle_makes","insurer_vertical","insurance_company","product","sub_product",
    "policy_type","vehicle_age_min","vehicle_age_max","make_model_class",
    "pi_od_rate","pi_tp_rate","pi_tp_2","pi_tp_3","pi_tp_4","pi_tp_5",
    "pi_net_rate","pi_flat_amount","pi_vli","pi_type",
    "tariff_min","tariff_max",
    "is_ncb","is_cpa",
    "cc_min","cc_max",
    "is_zd",
    "from_date","to_date",
    "sc_min","sc_max",
    "add_tnc"
]

def normalize(v):
    if v is None:
        return ""
    return str(v).strip().lower()

def build_key_hash(cleaned_dict):
    parts = []
    for f in GROUP_FIELDS:
        v = cleaned_dict.get(f)

        # FK objects -> pk
        if hasattr(v, "pk"):
            v = v.pk

        # Date -> stable format
        if hasattr(v, "strftime"):
            v = v.strftime("%Y-%m-%d")

        parts.append(normalize(v))

    key_text = "|".join(parts)
    key_hash = hashlib.sha256(key_text.encode("utf-8")).hexdigest()
    return key_hash, key_text

def unique_join(values):
    """Remove duplicates + blanks (case-insensitive), keep order"""
    seen = set()
    out = []
    for v in values:
        if not v:
            continue
        s = str(v).strip()
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return ", ".join(out)

def split_csv_values(values):
    """
    If any value contains commas (like 'MH01, MH02'),
    split them and return a flat list.
    """
    out = []
    for v in values:
        if not v:
            continue
        for part in str(v).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


# ---------- CSV UPLOAD ----------
def upload_csv(request):
    if request.method == "POST" and request.FILES.get("file"):
        csv_file = request.FILES["file"]
        decoded_file = csv_file.read().decode("utf-8-sig").splitlines()
        reader = csv.DictReader(decoded_file, skipinitialspace=True)

        inserted = 0
        errors = []

        def resolve_master(value, ModelClass):
            if value is None:
                return None
            v = str(value).strip()
            if not v:
                return None
            if v.isdigit():
                return ModelClass.objects.filter(id=int(v)).first()
            obj, _ = ModelClass.objects.get_or_create(name=v)
            return obj

        for i, row in enumerate(reader, start=2):
            try:
                # ---------- PRODUCT ----------
                product_val = str(row.get("product", "")).strip()
                product_obj = None
                if product_val:
                    if product_val.isdigit():
                        product_obj = ProductMaster.objects.filter(id=int(product_val)).first()
                    else:
                        product_obj = ProductMaster.objects.filter(name=product_val).first()
                        if not product_obj:
                            product_obj = ProductMaster.objects.create(name=product_val)

                sub_product_obj = resolve_master(row.get("sub_product"), SubProductMaster)
                policy_type_obj = resolve_master(row.get("policy_type"), PolicyTypeMaster)
                fuel_type_obj = resolve_master(row.get("fuel_type"), FuelTypeMaster)
                mmc_obj = resolve_master(row.get("make_model_class"), MakeModelClassMaster)

                is_ncb_obj = parse_yes_no_na(row.get("is_ncb"))
                is_cpa_obj = parse_yes_no_na(row.get("is_cpa"))
                is_zd_obj  = parse_yes_no_na(row.get("is_zd"))

                cleaned = {
                    "new_vehicle_makes": row.get("new_vehicle_makes") or None,
                    "insurer_vertical": row.get("insurer_vertical") or None,
                    "insurance_company": str(row.get("insurance_company", "")).strip(),
                    "product": product_obj,
                    "sub_product": sub_product_obj,
                    "policy_type": policy_type_obj,
                    "vehicle_age_min": float(row.get("vehicle_age_min") or 0),
                    "vehicle_age_max": float(row.get("vehicle_age_max") or 0),
                    "make_model_class": mmc_obj,

                    "pi_od_rate": float(row.get("pi_od_rate") or 0),
                    "pi_tp_rate": float(row.get("pi_tp_rate") or 0),
                    "pi_tp_2": float(row.get("pi_tp_2") or 0),
                    "pi_tp_3": float(row.get("pi_tp_3") or 0),
                    "pi_tp_4": float(row.get("pi_tp_4") or 0),
                    "pi_tp_5": float(row.get("pi_tp_5") or 0),

                    "pi_net_rate": float(row.get("pi_net_rate") or 0),
                    "pi_flat_amount": float(row.get("pi_flat_amount") or 0),
                    "pi_vli": float(row.get("pi_vli") or 0),
                    "pi_type": row.get("pi_type") or None,

                    "tariff_min": float(row.get("tariff_min") or 0),
                    "tariff_max": float(row.get("tariff_max") or 0),

                    "is_ncb": is_ncb_obj,
                    "is_cpa": is_cpa_obj,

                    "cc_min": float(row.get("cc_min") or 0),
                    "cc_max": float(row.get("cc_max") or 0),

                    "is_zd": is_zd_obj,

                    "from_date": parse_date(row.get("from_date")),
                    "to_date": parse_date(row.get("to_date")),

                    "sc_min": float(row.get("sc_min") or 0),
                    "sc_max": float(row.get("sc_max") or 0),

                    "add_tnc": row.get("add_tnc") or None,
                }

                # ✅ Create/Get group row -> gives group_id
                key_hash, key_text = build_key_hash(cleaned)
                group_obj, _ = RateGroup.objects.get_or_create(
                    key_hash=key_hash,
                    defaults={"key_text": key_text}
                )

                RateMaster.objects.create(
                    group=group_obj,  # ✅ writes group_id

                    new_vehicle_makes=cleaned["new_vehicle_makes"],
                    new_rto_list=row.get("new_rto_list") or None,
                    insurer_vertical=cleaned["insurer_vertical"],
                    insurance_company=cleaned["insurance_company"],

                    product=product_obj,
                    sub_product=sub_product_obj,
                    policy_type=policy_type_obj,
                    fuel_type=fuel_type_obj,
                    make_model_class=mmc_obj,

                    vehicle_age_min=cleaned["vehicle_age_min"],
                    vehicle_age_max=cleaned["vehicle_age_max"],

                    pi_od_rate=cleaned["pi_od_rate"],
                    pi_tp_rate=cleaned["pi_tp_rate"],
                    pi_tp_2=cleaned["pi_tp_2"],
                    pi_tp_3=cleaned["pi_tp_3"],
                    pi_tp_4=cleaned["pi_tp_4"],
                    pi_tp_5=cleaned["pi_tp_5"],

                    pi_net_rate=cleaned["pi_net_rate"],
                    pi_flat_amount=cleaned["pi_flat_amount"],
                    pi_vli=cleaned["pi_vli"],
                    pi_type=cleaned["pi_type"],

                    tariff_min=cleaned["tariff_min"],
                    tariff_max=cleaned["tariff_max"],

                    is_ncb=is_ncb_obj,
                    is_cpa=is_cpa_obj,
                    is_zd=is_zd_obj,

                    cc_min=cleaned["cc_min"],
                    cc_max=cleaned["cc_max"],

                    from_date=cleaned["from_date"],
                    to_date=cleaned["to_date"],

                    user_id=int(float(row.get("user_id") or 0)) if row.get("user_id") else None,
                    sc_min=cleaned["sc_min"],
                    sc_max=cleaned["sc_max"],

                    veh_use=row.get("veh_use") or None,
                    add_tnc=cleaned["add_tnc"],
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


# ---------- DASHBOARD (ONE ROW PER group_id) ----------
def dashboard(request):
    qs = RateMaster.objects.select_related(
        "group",
        "product", "sub_product", "policy_type", "fuel_type", "make_model_class",
        "is_ncb", "is_cpa", "is_zd"
    ).all()

    # --- filters ---
    q = (request.GET.get("q") or "").strip()
    insurance_company = (request.GET.get("insurance_company") or "").strip()
    product = (request.GET.get("product") or "").strip()
    fuel = (request.GET.get("fuel") or "").strip()

    sub_product = (request.GET.get("sub_product") or "").strip()
    make_model_class = (request.GET.get("make_model_class") or "").strip()

    from_date = (request.GET.get("from_date") or "").strip()   # YYYY-MM-DD
    to_date = (request.GET.get("to_date") or "").strip()       # YYYY-MM-DD

    if q:
        qs = qs.filter(
            Q(insurance_company__icontains=q) |
            Q(new_rto_list__icontains=q) |
            Q(new_vehicle_makes__icontains=q)
        )

    if insurance_company:
        qs = qs.filter(insurance_company=insurance_company)

    if product:
        qs = qs.filter(product_id=product)

    if fuel:
        qs = qs.filter(fuel_type_id=fuel)

    if sub_product:
        qs = qs.filter(sub_product_id=sub_product)

    if make_model_class:
        qs = qs.filter(make_model_class_id=make_model_class)

    if from_date:
        qs = qs.filter(from_date__gte=from_date)
    if to_date:
        qs = qs.filter(from_date__lte=to_date)

    qs = qs.order_by("-id")

    # ✅ GROUP BY group_id (NO group_key needed)
    buckets = defaultdict(list)
    for row in qs:
        gid = row.group_id or f"NO_GROUP_{row.id}"
        buckets[gid].append(row)

    grouped_rows = []
    for gid, rows in buckets.items():
        first = rows[0]

        # RTOs -> merge + split commas + unique
        all_rtos = split_csv_values([r.new_rto_list for r in rows])
        first.display_rto_list = unique_join(all_rtos)

        # Fuels -> unique names
        all_fuels = [r.fuel_type.name for r in rows if r.fuel_type]
        first.display_fuel_types = unique_join(all_fuels)

        first.records_in_group = len(rows)
        first.display_group_id = gid  # safe display

        grouped_rows.append(first)

    # ✅ Columns shown on dashboard (include everything you asked)
    field_names = [
        "display_group_id",
        "records_in_group",
        "new_vehicle_makes",
        "display_rto_list",
        "insurer_vertical",
        "insurance_company",
        "product",
        "sub_product",
        "policy_type",
        "display_fuel_types",
        "make_model_class",

        "vehicle_age_min",
        "vehicle_age_max",

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
        "is_zd",

        "cc_min",
        "cc_max",

        "from_date",
        "to_date",

        "sc_min",
        "sc_max",

        "user_id",
        "veh_use",
        "remarks",

        "add_tnc",

        "po_type",
        "po_od_rate",
        "po_tp_rate",
        "po_net_rate",
        "po_flat_amount",
    ]

    # dropdown lists
    insurance_company_list = (
        RateMaster.objects.values_list("insurance_company", flat=True)
        .distinct().order_by("insurance_company")
    )
    product_list = ProductMaster.objects.all().order_by("name")
    fuel_list = FuelTypeMaster.objects.all().order_by("name")
    sub_product_list = SubProductMaster.objects.all().order_by("name")
    make_model_class_list = MakeModelClassMaster.objects.all().order_by("name")

    return render(request, "dashboard.html", {
        "data": grouped_rows,
        "field_names": field_names,
        "total": len(grouped_rows),

        "insurance_company_list": insurance_company_list,
        "product_list": product_list,
        "fuel_list": fuel_list,
        "sub_product_list": sub_product_list,
        "make_model_class_list": make_model_class_list,

        "selected": {
            "q": q,
            "insurance_company": insurance_company,
            "product": product,
            "fuel": fuel,
            "sub_product": sub_product,
            "make_model_class": make_model_class,
            "from_date": from_date,
            "to_date": to_date,
        }
    })


# ---------- EXPORT (GROUPED) TO EXCEL ----------
def export_groups_xlsx(request):
    qs = RateMaster.objects.select_related(
        "group", "product", "sub_product", "policy_type", "fuel_type", "make_model_class",
        "is_ncb", "is_cpa", "is_zd"
    ).all()

    q = (request.GET.get("q") or "").strip()
    insurance_company = (request.GET.get("insurance_company") or "").strip()
    product = (request.GET.get("product") or "").strip()
    fuel = (request.GET.get("fuel") or "").strip()

    if q:
        qs = qs.filter(
            Q(insurance_company__icontains=q) |
            Q(new_rto_list__icontains=q) |
            Q(new_vehicle_makes__icontains=q)
        )

    if insurance_company:
        qs = qs.filter(insurance_company=insurance_company)
    if product:
        qs = qs.filter(product_id=product)
    if fuel:
        qs = qs.filter(fuel_type_id=fuel)

    qs = qs.order_by("-id")

    buckets = defaultdict(list)
    for row in qs:
        gid = row.group_id or f"NO_GROUP_{row.id}"
        buckets[gid].append(row)

    wb = Workbook()
    ws = wb.active
    ws.title = "Grouped Rates"

    headers = [
        "group_id", "records_in_group",
        "rto_list", "fuel_types",
        "insurance_company", "product", "sub_product", "policy_type", "make_model_class",
        "vehicle_age_min", "vehicle_age_max",
        "pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5",
        "pi_net_rate", "pi_flat_amount", "pi_vli", "pi_type",
        "tariff_min", "tariff_max",
        "cc_min", "cc_max",
        "is_ncb", "is_cpa", "is_zd",
        "from_date", "to_date",
        "sc_min", "sc_max",
        "add_tnc",
    ]
    ws.append(headers)

    for gid, rows in buckets.items():
        first = rows[0]
        rtos = unique_join(split_csv_values([r.new_rto_list for r in rows]))
        fuels = unique_join([r.fuel_type.name for r in rows if r.fuel_type])

        ws.append([
            gid,
            len(rows),
            rtos,
            fuels,

            first.insurance_company or "",
            str(first.product) if first.product else "",
            str(first.sub_product) if first.sub_product else "",
            str(first.policy_type) if first.policy_type else "",
            str(first.make_model_class) if first.make_model_class else "",

            first.vehicle_age_min or "",
            first.vehicle_age_max or "",

            first.pi_od_rate or "",
            first.pi_tp_rate or "",
            first.pi_tp_2 or "",
            first.pi_tp_3 or "",
            first.pi_tp_4 or "",
            first.pi_tp_5 or "",

            first.pi_net_rate or "",
            first.pi_flat_amount or "",
            first.pi_vli or "",
            first.pi_type or "",

            first.tariff_min or "",
            first.tariff_max or "",

            first.cc_min or "",
            first.cc_max or "",

            first.is_ncb.code if first.is_ncb else "",
            first.is_cpa.code if first.is_cpa else "",
            first.is_zd.code if first.is_zd else "",

            first.from_date.strftime("%Y-%m-%d") if first.from_date else "",
            first.to_date.strftime("%Y-%m-%d") if first.to_date else "",

            first.sc_min or "",
            first.sc_max or "",

            first.add_tnc or "",
        ])

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="grouped_rates.xlsx"'
    wb.save(response)
    return response
