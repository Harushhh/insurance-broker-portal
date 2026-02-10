from django.shortcuts import render, redirect
import csv
from datetime import datetime
from .models import RateMaster


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date()
    except:
        return None


def clean_value(field, value):
    if value in ["", None]:
        return None

    value = value.strip()
    field_type = field.get_internal_type()

    try:
        if field_type in ["IntegerField", "BigIntegerField"]:
            return int(float(value))  # SAFE FIX → handles 1000.01

        if field_type in ["FloatField", "DecimalField"]:
            return float(value)

        if field_type == "BooleanField":
            return value.lower() in ["1", "true", "yes", "y"]

        if field_type == "DateField":
            return parse_date(value)

    except:
        return None

    return value


def upload_csv(request):
    if request.method == "POST" and request.FILES.get("file"):

        csv_file = request.FILES["file"]
        decoded_file = csv_file.read().decode("utf-8-sig").splitlines()

        reader = csv.DictReader(decoded_file, skipinitialspace=True)

        model_fields = {
            f.name: f for f in RateMaster._meta.fields if f.name != "id"
        }

        for row in reader:
            data = {}

            for key, value in row.items():
                key = key.strip()

                # SKIP PRIMARY KEY
                if key == "id":
                    continue

                if key in model_fields:
                    data[key] = clean_value(model_fields[key], value)

            RateMaster.objects.create(**data)

        return redirect("/dashboard/")

    return render(request, "upload.html")


def dashboard(request):
    data = RateMaster.objects.all().values()
    return render(request, "dashboard.html", {"data": data})
