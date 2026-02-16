from django.shortcuts import render
import csv
from datetime import datetime

from .models import (
    RateMaster, YesNoNAMaster, ProductMaster,
    SubProductMaster, PolicyTypeMaster, FuelTypeMaster, MakeModelClassMaster
)


# ---------- ALLOWED INSURANCE COMPANIES ----------
ALLOWED_INSURANCE_COMPANIES = {
    "ACKO GENERAL INSURANCE LIMITED",
    "BAJAJ ALLIANZ GENERAL INSURANCE CO LTD",
    "CHOLAMANDALAM MS GENERAL INSURANCE COMPANY LTD",
    "FUTURE GENERALI INDIA INSURANCE COMPANY LTD",
    "GO DIGIT GENERAL INSURANCE LTD",
    "HDFC ERGO GENERAL INSURANCE CO LTD",
    "ICICI LOMBARD GENERAL INSURANCE CO LTD",
    "IFFCO-TOKIO GENERAL INSURANCE CO LTD",
    "ZURICH KOTAK GENERAL INSURANCE COMPANY (I) LIMITED",
    "LIBERTY GENERAL INSURANCE LTD",
    "MAGMA HDI GENERAL INSURANCE CO LTD",
    "NATIONAL INSURANCE CO LTD",
    "RAHEJA QBE GENERAL INSURANCE CO LTD",
    "RELIANCE GENERAL INSURANCE CO LTD",
    "ROYAL SUNDARAM GENERAL INSURANCE COMPANY LIMITED",
    "SBI GENERAL INSURANCE COMPANY LIMITED",
    "SHRIRAM GENERAL INSURANCE COMPANY LTD",
    "TATA AIG GENERAL INSURANCE CO LTD",
    "THE NEW INDIA ASSURANCE CO LTD",
    "THE ORIENTAL INSURANCE CO LTD",
    "UNITED INDIA INSURANCE CO LTD",
    "UNIVERSAL SOMPO GENERAL INSURANCE CO LTD",
    "ZUNO GENERAL INSURANCE LTD",
}

# ---------- ALLOWED PRODUCTS ----------
ALLOWED_PRODUCTS = {
    "PCV 4W",
    "PCV 2W",
    "PCV 3W",
    "GCV 3W",
    "GCV 4W",
    "MISCD",
    "PRIVATE CAR",
    "TW",
}

# ---------- ALLOWED SUB PRODUCTS (STRICT) ----------
ALLOWED_SUB_PRODUCTS = {"1+5", "STP", "1+1", "1+3", "SAOD"}


def normalize_spaces(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def normalize_company_name(value: str) -> str:
    return normalize_spaces(value).upper()


def normalize_product_name(value: str) -> str:
    # keep original spacing fixed, but do comparisons in upper
    return normalize_spaces(value)


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


# ---------- PRODUCT PARSER (MASTER LOOKUP) ----------
def parse_product(value):
    if not value:
        raise ValueError("product cannot be blank")

    raw = normalize_product_name(value)
    if raw.upper() not in ALLOWED_PRODUCTS:
        raise ValueError(f"product invalid: '{value}'")

    try:
        return ProductMaster.objects.get(name__iexact=raw)
    except ProductMaster.DoesNotExist:
        raise ValueError(f"product not found in master table: '{value}'")


# ---------- SUB PRODUCT PARSER (STRICT LIST + MASTER LOOKUP) ----------
def parse_sub_product(value):
    if not value:
        raise ValueError("sub_product cannot be blank")

    name = normalize_spaces(value)  # IMPORTANT: keep 1+1 exactly
    if name not in ALLOWED_SUB_PRODUCTS:
        raise ValueError(f"sub_product invalid: '{name}'")

    obj, _ = SubProductMaster.objects.get_or_create(name=name)
    return obj


# ---------- OTHER MASTER PARSERS (AUTO CREATE) ----------
def parse_policy_type(value):
    name = normalize_spaces(value)
    if not name:
        raise ValueError("policy_type cannot be blank")
    obj, _ = PolicyTypeMaster.objects.get_or_create(name=name)
    return obj


def parse_fuel_type(value):
    name = normalize_spaces(value)
    if not name:
        raise ValueError("fuel_type cannot be blank")
    obj, _ = FuelTypeMaster.objects.get_or_create(name=name)
    return obj


def parse_make_model_class(value):
    name = normalize_spaces(value)
    if not name:
        raise ValueError("make_model_class cannot be blank")
    obj, _ = MakeModelClassMaster.objects.get_or_create(name=name)
    return obj


# ---------- CLEAN FIELD VALUE ----------
def clean_value(field, value):
    if value in ["", None]:
        return None

    value = str(value).strip()
    field_type = field.get_internal_type()

    try:
        if field_type in ["IntegerField", "BigIntegerField"]:
            return int(float(value))

        if field_type in ["FloatField", "DecimalField"]:
            return float(value)

        if field_type == "DateField":
            return parse_date(value)

    except:
        return None

    return value


# ---------- VALIDATION ----------
def validate_row(row):
    errors = []

    required_fields = [
        "new_vehicle_makes", "new_rto_list", "insurer_vertical",
        "insurance_company", "product", "sub_product", "policy_type",
        "fuel_type", "vehicle_age_min", "vehicle_age_max", "make_model_class",
        "pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5",
        "pi_net_rate", "pi_flat_amount", "pi_vli", "pi_type",
        "tariff_min", "tariff_max",
        "cc_min", "cc_max",
        "from_date", "to_date", "user_id", "sc_min", "sc_max"
    ]

    # 1) blank checks
    for f in required_fields:
        if not row.get(f) or str(row.get(f)).strip() == "":
            errors.append(f"{f} cannot be blank")

    # 2) insurance_company whitelist
    company = normalize_company_name(row.get("insurance_company"))
    if company and company not in ALLOWED_INSURANCE_COMPANIES:
        errors.append(f"insurance_company invalid: '{row.get('insurance_company')}'")

    # 3) product whitelist
    prod_raw = normalize_product_name(row.get("product"))
    if prod_raw and prod_raw.upper() not in ALLOWED_PRODUCTS:
        errors.append(f"product invalid: '{row.get('product')}'")

    # 4) sub_product whitelist (STRICT)
    sp = normalize_spaces(row.get("sub_product"))
    if sp and sp not in ALLOWED_SUB_PRODUCTS:
        errors.append(f"sub_product invalid: '{row.get('sub_product')}'")

    # 5) vehicle age validation
    try:
        if float(row["vehicle_age_min"]) > float(row["vehicle_age_max"]):
            errors.append("vehicle_age_min > vehicle_age_max")
    except:
        errors.append("Invalid vehicle age values")

    # 6) cc validation
    try:
        if float(row["cc_min"]) > float(row["cc_max"]):
            errors.append("cc_min > cc_max")
    except:
        errors.append("Invalid CC values")

    # 7) sc validation
    try:
        if float(row["sc_min"]) > float(row["sc_max"]):
            errors.append("sc_min > sc_max")
    except:
        errors.append("Invalid SC values")

    # 8) date validation
    try:
        from_d = datetime.strptime(str(row["from_date"]).strip(), "%d/%m/%Y").date()
        to_d = datetime.strptime(str(row["to_date"]).strip(), "%d/%m/%Y").date()
        if from_d > to_d:
            errors.append("from_date cannot be after to_date")
    except:
        errors.append("Invalid date format (use DD/MM/YYYY)")

    return errors


# ---------- CSV UPLOAD ----------
def upload_csv(request):
    if request.method == "POST" and request.FILES.get("file"):

        csv_file = request.FILES["file"]
        decoded_file = csv_file.read().decode("utf-8-sig").splitlines()
        reader = csv.DictReader(decoded_file, skipinitialspace=True)

        model_fields = {f.name: f for f in RateMaster._meta.fields if f.name != "id"}

        inserted = 0
        errors = []

        for i, row in enumerate(reader, start=2):

            # 1) validations
            row_errors = validate_row(row)
            if row_errors:
                errors.append(f"Row {i}: " + ", ".join(row_errors))
                continue

            # 2) build clean data
            data = {}

            try:
                for key, value in row.items():
                    key = key.strip()

                    if key == "insurance_company":
                        data[key] = normalize_company_name(value)

                    elif key == "product":
                        data[key] = parse_product(value)

                    elif key == "sub_product":
                        data[key] = parse_sub_product(value)

                    elif key == "policy_type":
                        data[key] = parse_policy_type(value)

                    elif key == "fuel_type":
                        data[key] = parse_fuel_type(value)

                    elif key == "make_model_class":
                        data[key] = parse_make_model_class(value)

                    elif key in ["is_ncb", "is_cpa", "is_zd"]:
                        data[key] = parse_yes_no_na(value)

                    elif key in model_fields:
                        data[key] = clean_value(model_fields[key], value)

                RateMaster.objects.create(**data)
                inserted += 1

            except Exception as e:
                errors.append(f"Row {i}: {str(e)}")

        return render(request, "upload.html", {
            "summary": {
                "inserted": inserted,
                "duplicates": 0,
                "errors": len(errors)
            },
            "errors": errors
        })

    return render(request, "upload.html")


# ---------- DASHBOARD ----------
def dashboard(request):
    data = RateMaster.objects.select_related(
        "is_ncb", "is_cpa", "is_zd",
        "product", "sub_product", "policy_type", "fuel_type", "make_model_class"
    ).all()

    field_names = [f.name for f in RateMaster._meta.fields]

    return render(request, "dashboard.html", {
        "data": data,
        "field_names": field_names,
        "total": data.count()
    })
