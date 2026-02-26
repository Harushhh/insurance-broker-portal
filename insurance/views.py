from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User, Group
from django.shortcuts import render, redirect
from django.http import HttpResponse
from django.db.models import Q
from .models import RTOMaster, MakeModelMaster
from django import forms
import csv
import json
from datetime import datetime
from collections import defaultdict
import hashlib

from openpyxl import Workbook

from .models import (
    RateMaster, YesNoNAMaster,
    ProductMaster, SubProductMaster, PolicyTypeMaster,
    FuelTypeMaster, MakeModelClassMaster,
    RateGroup,
)

# =========================================================
# ✅ NA MEANS THESE MAKE_MODEL_CLASS VALUES (PER PRODUCT)
# =========================================================
NA_MAKE_MODEL_MAP = {
    "GCV 4W": [
        "Truck",
        "Pick Up Van",
        "Dumper Tipper",
        "Trailer",
        "Tanker",
        "Goods Carrying Tractor",
    ],
    "TW": [
        "Scooter",
        "Bike",
    ],
}

# -------------------------
# Helpers
# -------------------------
def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%d/%m/%Y").date()
    except:
        return None

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

GROUP_FIELDS = [
    "new_vehicle_makes", "insurer_vertical", "insurance_company", "product", "sub_product",
    "policy_type", "vehicle_age_min", "vehicle_age_max", "make_model_class",
    "pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5",
    "pi_net_rate", "pi_flat_amount", "pi_vli", "pi_type",
    "tariff_min", "tariff_max",
    "is_ncb", "is_cpa",
    "cc_min", "cc_max",
    "is_zd",
    "from_date", "to_date",
    "sc_min", "sc_max",
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
        if hasattr(v, "pk"):
            v = v.pk
        if hasattr(v, "strftime"):
            v = v.strftime("%Y-%m-%d")
        parts.append(normalize(v))

    key_text = "|".join(parts)
    key_hash = hashlib.sha256(key_text.encode("utf-8")).hexdigest()
    return key_hash, key_text

def unique_join(values):
    seen = set()
    out = []
    for v in values:
        if not v: continue
        s = str(v).strip()
        if not s: continue
        k = s.lower()
        if k in seen: continue
        seen.add(k)
        out.append(s)
    return ", ".join(out)

def split_csv_values(values):
    out = []
    for v in values:
        if not v: continue
        for part in str(v).split(","):
            part = part.strip()
            if part: out.append(part)
    return out

def apply_range_filter(qs, field_min, field_max, range_val):
    if not range_val:
        return qs
    if '-' in range_val:
        parts = range_val.split('-')
        val_min = parts[0].strip()
        val_max = parts[1].strip()
        if val_min:
            qs = qs.filter(**{field_min: val_min})
        if val_max:
            qs = qs.filter(**{field_max: val_max})
    else:
        qs = qs.filter(**{field_min: range_val})
    return qs

def get_na_class_list_for_product(product_id: str):
    if product_id and str(product_id).isdigit():
        p = ProductMaster.objects.filter(id=int(product_id)).first()
        if p: return NA_MAKE_MODEL_MAP.get(p.name, [])
        return []
    combined = []
    for vals in NA_MAKE_MODEL_MAP.values():
        combined.extend(vals)
    return combined

def apply_make_model_filter(qs, product_id: str, make_model_class: str):
    if not make_model_class: return qs
    if str(make_model_class).isdigit():
        return qs.filter(make_model_class_id=make_model_class)
    if make_model_class == "NA":
        na_list = get_na_class_list_for_product(product_id)
        if not na_list: return qs.filter(make_model_class__name__iexact="NA")
        return qs.filter(
            Q(make_model_class__name__in=na_list) |
            Q(make_model_class__name__iexact="NA")
        )
    return qs

def should_display_na(product_name: str, make_model_name: str):
    if not make_model_name: return False
    if make_model_name.strip().upper() == "NA": return True
    mapped = NA_MAKE_MODEL_MAP.get(product_name, [])
    return make_model_name in mapped


# -------------------------
# Helper to split Clusters and map actual names to Classes (For Checker Tool ONLY)
# -------------------------
def get_make_mapping_context():
    all_makes_objs = MakeModelMaster.objects.all()
    all_individual_makes = set()
    for obj in all_makes_objs:
        if obj.make_model_cluster:
            for item in str(obj.make_model_cluster).split(','):
                item = item.strip()
                if item:
                    all_individual_makes.add(item)
    all_individual_makes = sorted(list(all_individual_makes))
    
    class_to_makes = defaultdict(set)
    rate_makes = RateMaster.objects.exclude(make_model_class__isnull=True)\
                                   .exclude(new_vehicle_makes__isnull=True)\
                                   .exclude(new_vehicle_makes='')\
                                   .values_list('make_model_class_id', 'new_vehicle_makes')
                                   
    for mmc_id, makes_str in rate_makes:
        rate_groups = [m.strip() for m in makes_str.split(',')]
        for rg in rate_groups:
            if rg:
                for obj in all_makes_objs:
                    if obj.make_model_name and obj.make_model_name.strip() == rg:
                        if obj.make_model_cluster:
                            for item in str(obj.make_model_cluster).split(','):
                                item = item.strip()
                                if item:
                                    class_to_makes[str(mmc_id)].add(item)
                                    
    class_makes_mapping = {k: sorted(list(v)) for k, v in class_to_makes.items()}
    return json.dumps(all_individual_makes), json.dumps(class_makes_mapping), all_individual_makes


# -------------------------
# Access control
# -------------------------
def is_admin(user):
    return user.is_superuser or user.groups.filter(name="ADMIN").exists()


# -------------------------
# Upload CSV
# -------------------------
@login_required
def upload_csv(request):
    if request.method == "POST" and request.FILES.get("file"):
        csv_file = request.FILES["file"]
        upload_type = (request.POST.get("upload_type") or "").strip()

        decoded_file = csv_file.read().decode("utf-8-sig").splitlines()
        reader = csv.DictReader(decoded_file, skipinitialspace=True)

        inserted = 0
        errors = []

        if upload_type == "rto_master":
            for i, row in enumerate(reader, start=2):
                try:
                    rto_name = (row.get("rto_name") or "").strip()
                    rto_cluster = (row.get("rto_cluster") or "").strip()
                    if not rto_name: raise ValueError("rto_name is blank")
                    RTOMaster.objects.update_or_create(
                        rto_name=rto_name,
                        defaults={"rto_cluster": rto_cluster or None}
                    )
                    inserted += 1
                except Exception as e:
                    errors.append(f"Row {i}: {str(e)}")
            return render(request, "upload.html", {
                "summary": {"inserted": inserted, "duplicates": 0, "errors": len(errors)},
                "errors": errors,
            })

        if upload_type == "make_model_master":
            for i, row in enumerate(reader, start=2):
                try:
                    make_model_name = (row.get("make_model_name") or "").strip()
                    make_model_cluster = (row.get("make_model_cluster") or "").strip()
                    if not make_model_name: raise ValueError("make_model_name is blank")
                    MakeModelMaster.objects.update_or_create(
                        make_model_name=make_model_name,
                        defaults={"make_model_cluster": make_model_cluster or None}
                    )
                    inserted += 1
                except Exception as e:
                    errors.append(f"Row {i}: {str(e)}")
            return render(request, "upload.html", {
                "summary": {"inserted": inserted, "duplicates": 0, "errors": len(errors)},
                "errors": errors,
            })

        inserted = 0
        errors = []
        valid_rtos = set(RTOMaster.objects.values_list('rto_name', flat=True))
        valid_makes = set(MakeModelMaster.objects.values_list('make_model_name', flat=True))

        def resolve_master(value, ModelClass):
            if value is None: return None
            v = str(value).strip()
            if not v: return None
            if v.isdigit(): return ModelClass.objects.filter(id=int(v)).first()
            obj, _ = ModelClass.objects.get_or_create(name=v)
            return obj

        for i, row in enumerate(reader, start=2):
            try:
                raw_rtos = row.get("new_rto_list") or ""
                rto_items = [x.strip() for x in raw_rtos.split(",") if x.strip()]
                for rto in rto_items:
                    if rto not in valid_rtos: raise ValueError(f"RTO '{rto}' does not exist in RTOMaster. Please add it first.")

                raw_makes = row.get("new_vehicle_makes") or ""
                make_items = [x.strip() for x in raw_makes.split(",") if x.strip()]
                for make in make_items:
                    if make not in valid_makes: raise ValueError(f"Vehicle Make '{make}' does not exist in MakeModelMaster. Please add it first.")

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
                is_zd_obj = parse_yes_no_na(row.get("is_zd"))

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

                key_hash, key_text = build_key_hash(cleaned)
                group_obj, _ = RateGroup.objects.get_or_create(
                    key_hash=key_hash,
                    defaults={"key_text": key_text}
                )

                RateMaster.objects.create(
                    group=group_obj,
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


# -------------------------
# Dashboard (GROUPED view)
# -------------------------
@login_required
def dashboard(request):
    qs = RateMaster.objects.select_related(
        "group",
        "product", "sub_product", "policy_type", "fuel_type", "make_model_class",
        "is_ncb", "is_cpa", "is_zd"
    ).all()

    q = (request.GET.get("q") or "").strip()
    insurance_company = (request.GET.get("insurance_company") or "").strip()
    product = (request.GET.get("product") or "").strip()
    fuel = (request.GET.get("fuel") or "").strip()
    sub_product = (request.GET.get("sub_product") or "").strip()
    make_model_class = (request.GET.get("make_model_class") or "").strip()
    from_date = (request.GET.get("from_date") or "").strip()
    to_date = (request.GET.get("to_date") or "").strip()
    
    rto_code = (request.GET.get("rto_code") or "").strip()
    make_model_code = (request.GET.get("make_model_code") or "").strip() 
    
    age_range = (request.GET.get("age_range") or "").strip()
    cc_range = (request.GET.get("cc_range") or "").strip()
    sc_range = (request.GET.get("sc_range") or "").strip()
    is_zd = (request.GET.get("is_zd") or "").strip()
    is_ncb = (request.GET.get("is_ncb") or "").strip()
    is_cpa = (request.GET.get("is_cpa") or "").strip()

    if q:
        qs = qs.filter(
            Q(insurance_company__icontains=q) |
            Q(new_rto_list__icontains=q) |
            Q(new_vehicle_makes__icontains=q)
        )
    if insurance_company: qs = qs.filter(insurance_company=insurance_company)
    if product: qs = qs.filter(product_id=product)
    if fuel: qs = qs.filter(fuel_type_id=fuel)
    if sub_product: qs = qs.filter(sub_product_id=sub_product)

    if rto_code:
        matching_rto_names = RTOMaster.objects.filter(rto_cluster__icontains=rto_code).values_list("rto_name", flat=True)
        if matching_rto_names:
            q_rto = Q()
            for rto_name in matching_rto_names: q_rto |= Q(new_rto_list__icontains=rto_name)
            qs = qs.filter(q_rto)
        else: qs = qs.none()

    # ✅ REVERTED: Original Dashboard logic uses standard text search for Make/Model Code
    if make_model_code:
        matching_make_names = MakeModelMaster.objects.filter(make_model_cluster__icontains=make_model_code).values_list("make_model_name", flat=True)
        if matching_make_names:
            q_make = Q()
            for make_name in matching_make_names: q_make |= Q(new_vehicle_makes__icontains=make_name)
            qs = qs.filter(q_make)
        else: qs = qs.none()

    qs = apply_make_model_filter(qs, product, make_model_class)
    if from_date: qs = qs.filter(from_date__gte=from_date)
    if to_date: qs = qs.filter(from_date__lte=to_date)
    
    qs = apply_range_filter(qs, "vehicle_age_min", "vehicle_age_max", age_range)
    qs = apply_range_filter(qs, "cc_min", "cc_max", cc_range)
    qs = apply_range_filter(qs, "sc_min", "sc_max", sc_range)

    if is_zd: qs = qs.filter(is_zd__code=is_zd)
    if is_ncb: qs = qs.filter(is_ncb__code=is_ncb)
    if is_cpa: qs = qs.filter(is_cpa__code=is_cpa)

    qs = qs.order_by("-id")

    buckets = defaultdict(list)
    for row in qs:
        gid = row.group_id if row.group_id is not None else row.id
        buckets[gid].append(row)

    grouped_rows = []
    for gid, rows in buckets.items():
        first = rows[0]
        all_rtos = split_csv_values([r.new_rto_list for r in rows])
        all_fuels = [r.fuel_type.name for r in rows if r.fuel_type]

        first.records_in_group = len(rows)
        first.display_group_id = gid
        first.display_rto_list = unique_join(all_rtos)
        first.display_fuel_types = unique_join(all_fuels)

        product_name = first.product.name if first.product else ""
        mmc_name = first.make_model_class.name if first.make_model_class else ""

        if should_display_na(product_name, mmc_name):
            first.display_make_model_class = "NA"
        else:
            first.display_make_model_class = mmc_name

        age_min_str = str(first.vehicle_age_min) if first.vehicle_age_min is not None else ""
        age_max_str = str(first.vehicle_age_max) if first.vehicle_age_max is not None else ""
        first.age_range = f"{age_min_str} - {age_max_str}" if (age_min_str or age_max_str) else ""

        cc_min_str = str(first.cc_min) if first.cc_min is not None else ""
        cc_max_str = str(first.cc_max) if first.cc_max is not None else ""
        first.cc_range = f"{cc_min_str} - {cc_max_str}" if (cc_min_str or cc_max_str) else ""

        sc_min_str = str(first.sc_min) if first.sc_min is not None else ""
        sc_max_str = str(first.sc_max) if first.sc_max is not None else ""
        first.sc_range = f"{sc_min_str} - {sc_max_str}" if (sc_min_str or sc_max_str) else ""

        grouped_rows.append(first)

    field_names = [
        "display_group_id", "records_in_group", "new_vehicle_makes", "display_rto_list",
        "insurer_vertical", "insurance_company", "product", "sub_product", "policy_type",
        "display_fuel_types", "display_make_model_class", 
        "age_range", 
        "pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5",
        "pi_net_rate", "pi_flat_amount", "pi_vli", "pi_type", "tariff_min", "tariff_max",
        "is_ncb", "is_cpa", "is_zd", 
        "cc_range",  
        "from_date", "to_date",
        "sc_range",  
        "user_id", "veh_use", "remarks", "add_tnc",
        "po_type", "po_od_rate", "po_tp_rate", "po_net_rate", "po_flat_amount",
    ]

    insurance_company_list = RateMaster.objects.values_list("insurance_company", flat=True).distinct().order_by("insurance_company")
    product_list = ProductMaster.objects.all().order_by("name")
    fuel_list = FuelTypeMaster.objects.all().order_by("name")
    sub_product_list = SubProductMaster.objects.all().order_by("name")
    make_model_class_list = list(MakeModelClassMaster.objects.all().order_by("name"))
    MakeModelClassMaster.objects.get_or_create(name="NA")
    yes_no_na_list = YesNoNAMaster.objects.all().order_by("code")

    return render(request, "dashboard.html", {
        "data": grouped_rows,
        "field_names": field_names,
        "total": len(grouped_rows),

        "insurance_company_list": insurance_company_list,
        "product_list": product_list,
        "fuel_list": fuel_list,
        "sub_product_list": sub_product_list,
        "make_model_class_list": make_model_class_list,
        "yes_no_na_list": yes_no_na_list,

        "selected": {
            "q": q,
            "insurance_company": insurance_company,
            "product": product,
            "fuel": fuel,
            "sub_product": sub_product,
            "make_model_class": make_model_class,
            "from_date": from_date,
            "to_date": to_date,
            "rto_code": rto_code,
            "make_model_code": make_model_code, 
            "age_range": age_range,
            "cc_range": cc_range,
            "sc_range": sc_range,
            "is_zd": is_zd,
            "is_ncb": is_ncb,
            "is_cpa": is_cpa,
        }
    })

# -------------------------
# Export UNGROUPED to Excel (FILTERED)
# -------------------------
@login_required
def export_rates_xlsx(request):
    qs = RateMaster.objects.select_related(
        "group",
        "product", "sub_product", "policy_type", "fuel_type", "make_model_class",
        "is_ncb", "is_cpa", "is_zd"
    ).all()

    q = (request.GET.get("q") or "").strip()
    insurance_company = (request.GET.get("insurance_company") or "").strip()
    product = (request.GET.get("product") or "").strip()
    fuel = (request.GET.get("fuel") or "").strip()
    sub_product = (request.GET.get("sub_product") or "").strip()
    make_model_class = (request.GET.get("make_model_class") or "").strip()
    from_date = (request.GET.get("from_date") or "").strip()
    to_date = (request.GET.get("to_date") or "").strip()
    rto_code = (request.GET.get("rto_code") or "").strip()
    make_model_code = (request.GET.get("make_model_code") or "").strip()
    age_range = (request.GET.get("age_range") or "").strip()
    cc_range = (request.GET.get("cc_range") or "").strip()
    sc_range = (request.GET.get("sc_range") or "").strip()
    is_zd = (request.GET.get("is_zd") or "").strip()
    is_ncb = (request.GET.get("is_ncb") or "").strip()
    is_cpa = (request.GET.get("is_cpa") or "").strip()

    if q:
        qs = qs.filter(Q(insurance_company__icontains=q) | Q(new_rto_list__icontains=q) | Q(new_vehicle_makes__icontains=q))
    if insurance_company: qs = qs.filter(insurance_company=insurance_company)
    if product: qs = qs.filter(product_id=product)
    if fuel: qs = qs.filter(fuel_type_id=fuel)
    if sub_product: qs = qs.filter(sub_product_id=sub_product)

    if rto_code:
        matching_rto_names = RTOMaster.objects.filter(rto_cluster__icontains=rto_code).values_list("rto_name", flat=True)
        if matching_rto_names:
            q_rto = Q()
            for rto_name in matching_rto_names: q_rto |= Q(new_rto_list__icontains=rto_name)
            qs = qs.filter(q_rto)
        else:
            qs = qs.none()

    # ✅ REVERTED: Original Dashboard logic uses standard text search for Make/Model Code
    if make_model_code:
        matching_make_names = MakeModelMaster.objects.filter(make_model_cluster__icontains=make_model_code).values_list("make_model_name", flat=True)
        if matching_make_names:
            q_make = Q()
            for make_name in matching_make_names: q_make |= Q(new_vehicle_makes__icontains=make_name)
            qs = qs.filter(q_make)
        else: qs = qs.none()

    qs = apply_make_model_filter(qs, product, make_model_class)
    if from_date: qs = qs.filter(from_date__gte=from_date)
    if to_date: qs = qs.filter(from_date__lte=to_date)

    qs = apply_range_filter(qs, "vehicle_age_min", "vehicle_age_max", age_range)
    qs = apply_range_filter(qs, "cc_min", "cc_max", cc_range)
    qs = apply_range_filter(qs, "sc_min", "sc_max", sc_range)

    if is_zd: qs = qs.filter(is_zd__code=is_zd)
    if is_ncb: qs = qs.filter(is_ncb__code=is_ncb)
    if is_cpa: qs = qs.filter(is_cpa__code=is_cpa)

    qs = qs.order_by("-id")  

    wb = Workbook()
    ws = wb.active
    ws.title = "Rates (Ungrouped)"

    headers = [
        "id", "group_id", "new_vehicle_makes", "new_rto_list", "insurer_vertical",
        "insurance_company", "product", "sub_product", "policy_type", "fuel_type",
        "make_model_class", 
        "age_range", 
        "pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5",
        "pi_net_rate", "pi_flat_amount", "pi_vli", "pi_type",
        "tariff_min", "tariff_max", 
        "cc_range", 
        "is_ncb", "is_cpa", "is_zd", "from_date", "to_date",
        "sc_range", 
        "user_id", "veh_use", "remarks", "add_tnc",
        "po_type", "po_od_rate", "po_tp_rate", "po_net_rate", "po_flat_amount",
    ]
    ws.append(headers)

    for r in qs:
        age_min_val = str(r.vehicle_age_min) if r.vehicle_age_min is not None else ""
        age_max_val = str(r.vehicle_age_max) if r.vehicle_age_max is not None else ""
        age_range_val = f"{age_min_val} - {age_max_val}" if (age_min_val or age_max_val) else ""

        cc_min_val = str(r.cc_min) if r.cc_min is not None else ""
        cc_max_val = str(r.cc_max) if r.cc_max is not None else ""
        cc_range_val = f"{cc_min_val} - {cc_max_val}" if (cc_min_val or cc_max_val) else ""

        sc_min_val = str(r.sc_min) if r.sc_min is not None else ""
        sc_max_val = str(r.sc_max) if r.sc_max is not None else ""
        sc_range_val = f"{sc_min_val} - {sc_max_val}" if (sc_min_val or sc_max_val) else ""

        ws.append([
            r.id, r.group_id, r.new_vehicle_makes or "", r.new_rto_list or "", r.insurer_vertical or "",
            r.insurance_company or "", r.product.name if r.product else "",
            r.sub_product.name if r.sub_product else "", r.policy_type.name if r.policy_type else "",
            r.fuel_type.name if r.fuel_type else "", r.make_model_class.name if r.make_model_class else "",
            age_range_val,
            r.pi_od_rate if r.pi_od_rate is not None else "", r.pi_tp_rate if r.pi_tp_rate is not None else "",
            r.pi_tp_2 if r.pi_tp_2 is not None else "", r.pi_tp_3 if r.pi_tp_3 is not None else "",
            r.pi_tp_4 if r.pi_tp_4 is not None else "", r.pi_tp_5 if r.pi_tp_5 is not None else "",
            r.pi_net_rate if r.pi_net_rate is not None else "", r.pi_flat_amount if r.pi_flat_amount is not None else "",
            r.pi_vli if r.pi_vli is not None else "", r.pi_type or "",
            r.tariff_min if r.tariff_min is not None else "", r.tariff_max if r.tariff_max is not None else "",
            cc_range_val,
            r.is_ncb.code if r.is_ncb else "", r.is_cpa.code if r.is_cpa else "", r.is_zd.code if r.is_zd else "",
            r.from_date.strftime("%Y-%m-%d") if r.from_date else "", r.to_date.strftime("%Y-%m-%d") if r.to_date else "",
            sc_range_val,
            r.user_id if r.user_id is not None else "", r.veh_use or "", r.remarks or "", r.add_tnc or "",
            r.po_type or "", r.po_od_rate if r.po_od_rate is not None else "", r.po_tp_rate if r.po_tp_rate is not None else "",
            r.po_net_rate if r.po_net_rate is not None else "", r.po_flat_amount if r.po_flat_amount is not None else "",
        ])

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="rates_ungrouped.xlsx"'
    wb.save(response)
    return response

# -------------------------
# User Management (ADMIN only)
# -------------------------
@login_required
@user_passes_test(is_admin)
def user_management(request):
    Group.objects.get_or_create(name="ADMIN")
    Group.objects.get_or_create(name="USER")

    users = User.objects.all().order_by("username")
    groups = Group.objects.filter(name__in=["ADMIN", "USER"]).order_by("name")

    if request.method == "POST":
        user_id = request.POST.get("user_id")
        group_name = request.POST.get("group_name")
        u = User.objects.get(id=user_id)
        for g in groups:
            u.groups.remove(g)
        if group_name in ["ADMIN", "USER"]:
            g = Group.objects.get(name=group_name)
            u.groups.add(g)
        return redirect("user_management")

    def get_role(u):
        if u.groups.filter(name="ADMIN").exists(): return "ADMIN"
        if u.groups.filter(name="USER").exists(): return "USER"
        return ""

    user_rows = []
    for u in users:
        user_rows.append({"id": u.id, "username": u.username, "role": get_role(u), "is_superuser": u.is_superuser})

    return render(request, "user_management.html", {"users": user_rows, "groups": groups})

@login_required
def rto_dashboard(request):
    qs = RTOMaster.objects.all().order_by("rto_name")
    rto_name = (request.GET.get("rto_name") or "").strip()
    cluster_q = (request.GET.get("cluster_q") or "").strip()

    if rto_name: qs = qs.filter(rto_name=rto_name)
    if cluster_q: qs = qs.filter(rto_cluster__icontains=cluster_q)

    rto_name_list = RTOMaster.objects.values_list("rto_name", flat=True).distinct().order_by("rto_name")
    return render(request, "rto_dashboard.html", {
        "data": qs, "total": qs.count(), "rto_name_list": rto_name_list,
        "selected": {"rto_name": rto_name, "cluster_q": cluster_q}
    })

@login_required
def make_model_dashboard(request):
    qs = MakeModelMaster.objects.all().order_by("make_model_name")
    make_model_name = (request.GET.get("make_model_name") or "").strip()
    cluster_q = (request.GET.get("cluster_q") or "").strip()

    if make_model_name: qs = qs.filter(make_model_name=make_model_name)
    if cluster_q: qs = qs.filter(make_model_cluster__icontains=cluster_q)

    make_model_name_list = MakeModelMaster.objects.values_list("make_model_name", flat=True).distinct().order_by("make_model_name")
    return render(request, "make_model_dashboard.html", {
        "data": qs, "total": qs.count(), "make_model_name_list": make_model_name_list,
        "selected": {"make_model_name": make_model_name, "cluster_q": cluster_q}
    })

# -------------------------
# Edit Rate (Grouped)
# -------------------------
class RateForm(forms.ModelForm):
    class Meta:
        model = RateMaster
        exclude = ['group', 'created_at']

    def __init__(self, *args, **kwargs):
        super(RateForm, self).__init__(*args, **kwargs)
        
        companies = list(RateMaster.objects.exclude(insurance_company__isnull=True).exclude(insurance_company__exact='').values_list('insurance_company', flat=True).distinct().order_by('insurance_company'))
        if self.instance and self.instance.insurance_company and self.instance.insurance_company not in companies: companies.append(self.instance.insurance_company)
        company_choices = [(c, c) for c in companies]
        self.fields['insurance_company'] = forms.ChoiceField(choices=company_choices, required=False)

        makes = list(MakeModelMaster.objects.values_list('make_model_name', flat=True).order_by('make_model_name'))
        if self.instance and self.instance.new_vehicle_makes and self.instance.new_vehicle_makes not in makes: makes.append(self.instance.new_vehicle_makes)
        make_choices = [("", "---------")] + [(m, m) for m in makes]
        self.fields['new_vehicle_makes'] = forms.ChoiceField(choices=make_choices, required=False)

        verticals = list(RateMaster.objects.exclude(insurer_vertical__isnull=True).exclude(insurer_vertical__exact='').values_list('insurer_vertical', flat=True).distinct().order_by('insurer_vertical'))
        if self.instance and self.instance.insurer_vertical and self.instance.insurer_vertical not in verticals: verticals.append(self.instance.insurer_vertical)
        vertical_choices = [("", "---------")] + [(v, v) for v in verticals]
        self.fields['insurer_vertical'] = forms.ChoiceField(choices=vertical_choices, required=False)

        rtos = list(RTOMaster.objects.values_list('rto_name', flat=True).order_by('rto_name'))
        if self.instance and self.instance.new_rto_list and self.instance.new_rto_list not in rtos: rtos.append(self.instance.new_rto_list)
        rto_choices = [("", "---------")] + [(r, r) for r in rtos]
        self.fields['new_rto_list'] = forms.ChoiceField(choices=rto_choices, required=False)

@login_required
def edit_rate(request, group_id):
    records = RateMaster.objects.filter(group_id=group_id)
    if not records.exists(): records = RateMaster.objects.filter(id=group_id)
    if not records.exists(): return HttpResponse("Record not found.", status=404)

    first_record = records.first()
    all_rtos = split_csv_values([r.new_rto_list for r in records])
    rto_display = unique_join(all_rtos)

    if request.method == "POST":
        form = RateForm(request.POST, instance=first_record)
        if form.is_valid():
            update_data = {}
            for field, value in form.cleaned_data.items():
                update_data[field] = value
            records.update(**update_data)
            return redirect('dashboard')
    else:
        form = RateForm(instance=first_record)

    return render(request, "edit_rate.html", {
        "form": form,
        "count": records.count(),
        "group_id": group_id,
        "rto_display": rto_display 
    })

# -------------------------
# Bulk Update Rates (Single Column)
# -------------------------
@login_required
def bulk_update_rates(request):
    if request.method == "POST":
        group_ids = request.POST.getlist("selected_groups")
        field_name = request.POST.get("update_field")
        new_value = request.POST.get("update_value", "").strip()

        if not group_ids or not field_name: return redirect('dashboard')
        records = RateMaster.objects.filter(Q(group_id__in=group_ids) | Q(id__in=group_ids))

        parsed_value = new_value
        if field_name == "product": parsed_value, _ = ProductMaster.objects.get_or_create(name=new_value) if new_value else (None, False)
        elif field_name == "sub_product": parsed_value, _ = SubProductMaster.objects.get_or_create(name=new_value) if new_value else (None, False)
        elif field_name == "policy_type": parsed_value, _ = PolicyTypeMaster.objects.get_or_create(name=new_value) if new_value else (None, False)
        elif field_name == "fuel_type": parsed_value, _ = FuelTypeMaster.objects.get_or_create(name=new_value) if new_value else (None, False)
        elif field_name == "make_model_class": parsed_value, _ = MakeModelClassMaster.objects.get_or_create(name=new_value) if new_value else (None, False)
        elif field_name in ["is_ncb", "is_cpa", "is_zd"]: parsed_value = parse_yes_no_na(new_value)
        elif field_name in ["vehicle_age_min", "vehicle_age_max", "cc_min", "cc_max", "user_id"]: parsed_value = int(float(new_value)) if new_value else None
        elif field_name in ["pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5", "pi_net_rate", "pi_flat_amount", "pi_vli", "tariff_min", "tariff_max", "sc_min", "sc_max", "po_od_rate", "po_tp_rate", "po_net_rate", "po_flat_amount"]: parsed_value = float(new_value) if new_value else None

        update_dict = {field_name: parsed_value}
        records.update(**update_dict)

    return redirect('dashboard')

# -------------------------
# Motor Payout Rates (Checker Tool)
# -------------------------
@login_required
def motor_payout_rates(request):
    qs = RateMaster.objects.select_related(
        "product", "sub_product", "policy_type", "fuel_type", "make_model_class",
        "is_ncb", "is_cpa", "is_zd"
    ).all()

    today_str = datetime.today().strftime('%Y-%m-%d')
    target_date = request.GET.get("target_date", today_str).strip()
    make_model_class = (request.GET.get("make_model_class") or "").strip()
    sub_product = (request.GET.get("sub_product") or "").strip()
    make_names = (request.GET.get("make_names") or "").strip()
    rto_code = (request.GET.get("rto_code") or "").strip()
    cc = (request.GET.get("cc") or "").strip()
    fuel = (request.GET.get("fuel") or "").strip()
    sc = (request.GET.get("sc") or "").strip()
    mfg_year = (request.GET.get("mfg_year") or "").strip()
    
    # These will now capture "YES" or "NO" from the new buttons
    is_zd = (request.GET.get("is_zd") or "").strip().upper()
    is_cpa = (request.GET.get("is_cpa") or "").strip().upper()
    is_ncb = (request.GET.get("is_ncb") or "").strip().upper()

    if target_date:
        qs = qs.filter(
            (Q(from_date__lte=target_date) | Q(from_date__isnull=True)) &
            (Q(to_date__gte=target_date) | Q(to_date__isnull=True))
        )

    if make_model_class:
        if str(make_model_class).isdigit():
            qs = qs.filter(make_model_class_id=make_model_class)
        elif make_model_class == "NA":
            qs = qs.filter(make_model_class__name__iexact="NA")
            
    if sub_product: qs = qs.filter(sub_product_id=sub_product)
    if fuel: qs = qs.filter(fuel_type_id=fuel)

    if rto_code:
        matching_rto_names = RTOMaster.objects.filter(rto_cluster__icontains=rto_code).values_list("rto_name", flat=True)
        if matching_rto_names:
            q_rto = Q()
            for rto_name in matching_rto_names: q_rto |= Q(new_rto_list__icontains=rto_name)
            qs = qs.filter(q_rto)
        else: qs = qs.none()

    if make_names:
        matching_groups = MakeModelMaster.objects.filter(make_model_cluster__icontains=make_names).values_list("make_model_name", flat=True)
        if matching_groups:
            q_make = Q()
            for group_name in matching_groups:
                if group_name:
                    q_make |= Q(new_vehicle_makes__icontains=group_name.strip())
            qs = qs.filter(q_make)
        else: 
            qs = qs.none()

    if cc and cc.isdigit():
        val = int(cc)
        qs = qs.filter(
            (Q(cc_min__lte=val) | Q(cc_min__isnull=True)) &
            (Q(cc_max__gte=val) | Q(cc_max__isnull=True))
        )

    if sc:
        try:
            val = float(sc)
            qs = qs.filter(
                (Q(sc_min__lte=val) | Q(sc_min__isnull=True)) &
                (Q(sc_max__gte=val) | Q(sc_max__isnull=True))
            )
        except ValueError:
            pass

    if mfg_year and mfg_year.isdigit() and target_date:
        try:
            target_year = datetime.strptime(target_date, '%Y-%m-%d').year
            age = target_year - int(mfg_year)
            qs = qs.filter(
                (Q(vehicle_age_min__lte=age) | Q(vehicle_age_min__isnull=True)) &
                (Q(vehicle_age_max__gte=age) | Q(vehicle_age_max__isnull=True))
            )
        except ValueError:
            pass

    # ✅ NEW WILDCARD LOGIC: If YES or NO is clicked, check for that exact value OR "NA"
    if is_zd: qs = qs.filter(Q(is_zd__code__iexact=is_zd) | Q(is_zd__code__iexact="NA"))
    if is_cpa: qs = qs.filter(Q(is_cpa__code__iexact=is_cpa) | Q(is_cpa__code__iexact="NA"))
    if is_ncb: qs = qs.filter(Q(is_ncb__code__iexact=is_ncb) | Q(is_ncb__code__iexact="NA"))

    qs = qs.order_by("-id")

    results = []
    for row in qs[:300]:
        row.display_group_id = row.group_id if row.group_id is not None else row.id
        results.append(row)

    field_names = [
        "display_group_id", "insurance_company", "po_type", "po_od_rate", 
        "po_tp_rate", "po_net_rate", "po_flat_amount", "add_tnc"
    ]

    all_makes_json, class_makes_mapping_json, all_makes = get_make_mapping_context()

    return render(request, "motor_payout_rates.html", {
        "data": results,
        "total_found": qs.count(),
        "field_names": field_names,
        "sub_product_list": SubProductMaster.objects.all().order_by("name"),
        "fuel_list": FuelTypeMaster.objects.all().order_by("name"),
        "make_model_class_list": list(MakeModelClassMaster.objects.all().order_by("name")),
        
        "all_makes_json": all_makes_json,
        "class_makes_mapping_json": class_makes_mapping_json,

        "selected": {
            "target_date": target_date,
            "make_model_class": make_model_class,
            "sub_product": sub_product,
            "make_names": make_names,
            "rto_code": rto_code,
            "cc": cc,
            "fuel": fuel,
            "sc": sc,
            "mfg_year": mfg_year,
            "is_zd": is_zd,
            "is_cpa": is_cpa,
            "is_ncb": is_ncb,
        }
    })