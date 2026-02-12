from django.shortcuts import render
import csv
from datetime import datetime
from .models import RateMaster, YesNoNAMaster


# ---------- DATE PARSER ----------
def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date()
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

    # blank checks
    for f in required_fields:
        if not row.get(f) or str(row.get(f)).strip() == "":
            errors.append(f"{f} cannot be blank")

    # vehicle age validation
    try:
        if float(row["vehicle_age_min"]) > float(row["vehicle_age_max"]):
            errors.append("vehicle_age_min > vehicle_age_max")
    except:
        errors.append("Invalid vehicle age values")

    # cc validation
    try:
        if float(row["cc_min"]) > float(row["cc_max"]):
            errors.append("cc_min > cc_max")
    except:
        errors.append("Invalid CC values")

    # sc validation
    try:
        if float(row["sc_min"]) > float(row["sc_max"]):
            errors.append("sc_min > sc_max")
    except:
        errors.append("Invalid SC values")

    # date validation (from_date should be <= to_date)
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
            for key, value in row.items():
                key = key.strip()

                if key in ["is_ncb", "is_cpa", "is_zd"]:
                    data[key] = parse_yes_no_na(value)
                elif key in model_fields:
                    data[key] = clean_value(model_fields[key], value)

            # 3) insert (NO DUPLICATE CHECK)
            try:
                RateMaster.objects.create(**data)
                inserted += 1
            except Exception as e:
                errors.append(f"Row {i}: {str(e)}")

        return render(request, "upload.html", {
            "summary": {
                "inserted": inserted,
                "duplicates": 0,          # kept only so your upload.html doesn't break
                "errors": len(errors)
            },
            "errors": errors
        })

    return render(request, "upload.html")


# ---------- DASHBOARD ----------
def dashboard(request):
    data = RateMaster.objects.select_related("is_ncb", "is_cpa", "is_zd").all()
    field_names = [f.name for f in RateMaster._meta.fields]

    return render(request, "dashboard.html", {
        "data": data,
        "field_names": field_names,
        "total": data.count()
    })
