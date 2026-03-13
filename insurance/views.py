from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User, Group
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.utils import timezone
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.db.models import Q, F, Count, Avg
from django.core.mail import send_mail
from django.conf import settings
from django import forms
import csv
import json
import re
import hashlib
import threading
import ast  # ADDED FOR AUDIT LOG PARSING

from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from collections import defaultdict

from openpyxl import Workbook

# --- DRF & SWAGGER IMPORTS ---
from rest_framework import generics
from rest_framework_api_key.permissions import HasAPIKey
from .serializers import RateMasterSerializer
# -----------------------------

# Optional extraction libs
try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

try:
    from PIL import Image
except Exception:
    Image = None

try:
    import pytesseract
except Exception:
    pytesseract = None

from .models import (
    RTOMaster, MakeModelMaster, RateMaster, YesNoNAMaster,
    ProductMaster, SubProductMaster, PolicyTypeMaster,
    FuelTypeMaster, MakeModelClassMaster,
    RateGroup, AuditLog, GridDocument, UserProfile,
    MISFieldMaster, MISFieldAlias, PolicyDocumentUpload, PolicyMISRecord,
    LockedPolicy
)

# =========================================================
# PAGE-LEVEL ACCESS GROUPS
# =========================================================
PAGE_GROUPS = [
    "Can_View_Dashboard",
    "Can_View_Analysis",
    "Can_View_Motor_Payout_Rates",
    "Can_Upload_CSV",
    "Can_View_RTO_Dashboard",
    "Can_View_Make_Model_Dashboard",
    "Can_View_Audit_Log",
    "Can_View_Grid_Management",
    "Can_View_Alias_Management",
]


def is_admin(user):
    return user.is_superuser or user.groups.filter(name="ADMIN").exists()


def can_view_dashboard(user):
    return user.is_superuser or user.groups.filter(
        name__in=["ADMIN", "Can_View_Dashboard"]
    ).exists()


def can_view_analysis(user):
    return user.is_superuser or user.groups.filter(
        name__in=["ADMIN", "Can_View_Analysis"]
    ).exists()


def can_view_motor_payout(user):
    return user.is_superuser or user.groups.filter(
        name__in=["ADMIN", "Can_View_Motor_Payout_Rates"]
    ).exists()


def can_upload(user):
    return user.is_superuser or user.groups.filter(
        name__in=["ADMIN", "Can_Upload_CSV"]
    ).exists()


def can_view_rto(user):
    return user.is_superuser or user.groups.filter(
        name__in=["ADMIN", "Can_View_RTO_Dashboard"]
    ).exists()


def can_view_make_model(user):
    return user.is_superuser or user.groups.filter(
        name__in=["ADMIN", "Can_View_Make_Model_Dashboard"]
    ).exists()


def can_view_audit_log(user):
    return user.is_superuser or user.groups.filter(
        name__in=["ADMIN", "Can_View_Audit_Log"]
    ).exists()


def can_view_grid_management(user):
    return user.is_superuser or user.groups.filter(
        name__in=["ADMIN", "Can_View_Grid_Management"]
    ).exists()


def can_view_alias_management(user):
    return user.is_superuser or user.groups.filter(
        name__in=["ADMIN", "Can_View_Alias_Management"]
    ).exists()


# =========================================================
# NA CONFIGURATION
# =========================================================
NA_MAKE_MODEL_MAP = {
    "TW": ["Scooter", "Bike"],
    "Private Car": ["Car"],
    "GCV 4W": ["Truck", "Pick Up Van", "Dumper Tipper", "Trailer", "Tanker", "Goods Carrying Tractor"],
    "GCV 3W": ["Goods Carrying Rickshaw"],
    "PCV 3W": ["Passenger Carrying Rickshaw"],
    "PCV 4W": ["Taxi", "School Bus", "Other Bus"],
    "MISCD": ["MISCD-Tractor", "MISCD-Others"],
}


# =========================================================
# UTILITY FUNCTIONS
# =========================================================
def strict_match_in_cluster(search_term, cluster_string):
    if not cluster_string:
        return False
    term = str(search_term).strip().upper()
    items = [x.strip().upper() for x in str(cluster_string).split(",")]
    if term in items:
        return True
    pattern = rf"(?<![A-Z0-9]){re.escape(term)}(?![A-Z0-9])"
    for item in items:
        if re.search(pattern, item):
            return True
    return False


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%d/%m/%Y").date()
    except Exception:
        return None


def parse_yes_no_na(value):
    if not value:
        return YesNoNAMaster.objects.get(code="NA")
    v_clean = str(value).strip().lower()
    if v_clean in ["yes", "y", "true", "1"]:
        return YesNoNAMaster.objects.get(code="YES")
    elif v_clean in ["no", "n", "false", "0"]:
        return YesNoNAMaster.objects.get(code="NO")
    else:
        return YesNoNAMaster.objects.get(code="NA")


GROUP_FIELDS = [
    "new_vehicle_makes", "insurer_vertical", "insurance_company", "product", "sub_product",
    "policy_type", "vehicle_age_min", "vehicle_age_max", "make_model_class",
    "pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5",
    "pi_net_rate", "pi_flat_amount", "pi_vli", "pi_type",
    "tariff_min", "tariff_max", "is_ncb", "is_cpa", "cc_min", "cc_max",
    "is_zd", "from_date", "to_date", "sc_min", "sc_max", "add_tnc",
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
    out = []
    for v in values:
        if not v:
            continue
        for part in str(v).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def apply_range_filter(qs, field_min, field_max, range_val):
    if not range_val:
        return qs
    if "-" in range_val:
        parts = range_val.split("-")
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
        if p:
            return NA_MAKE_MODEL_MAP.get(p.name, [])
        return []
    combined = []
    for vals in NA_MAKE_MODEL_MAP.values():
        combined.extend(vals)
    return combined


def apply_make_model_filter(qs, product_id: str, make_model_class: str):
    if not make_model_class:
        return qs

    if make_model_class == "NA":
        na_list = get_na_class_list_for_product(product_id)
        if not na_list:
            return qs.filter(make_model_class__name__iexact="NA")
        return qs.filter(
            Q(make_model_class__name__in=na_list) |
            Q(make_model_class__name__iexact="NA")
        )

    if str(make_model_class).isdigit():
        selected_class_obj = MakeModelClassMaster.objects.filter(id=int(make_model_class)).first()
        if selected_class_obj:
            selected_class_name = selected_class_obj.name
            is_na_equivalent = False

            if product_id and str(product_id).isdigit():
                p = ProductMaster.objects.filter(id=int(product_id)).first()
                if p and p.name in NA_MAKE_MODEL_MAP and selected_class_name in NA_MAKE_MODEL_MAP[p.name]:
                    is_na_equivalent = True
            else:
                for mapped_classes in NA_MAKE_MODEL_MAP.values():
                    if selected_class_name in mapped_classes:
                        is_na_equivalent = True
                        break

            if is_na_equivalent:
                return qs.filter(
                    Q(make_model_class_id=make_model_class) |
                    Q(make_model_class__name__iexact="NA")
                )

        return qs.filter(make_model_class_id=make_model_class)

    return qs


def get_make_mapping_context():
    all_makes_objs = MakeModelMaster.objects.all()
    all_individual_makes = set()

    for obj in all_makes_objs:
        if obj.make_model_cluster:
            for item in str(obj.make_model_cluster).split(","):
                item = item.strip()
                if item:
                    all_individual_makes.add(item)

    all_individual_makes = sorted(list(all_individual_makes))
    class_to_makes = defaultdict(set)

    rate_makes = RateMaster.objects.exclude(make_model_class__isnull=True).exclude(
        new_vehicle_makes__isnull=True
    ).exclude(new_vehicle_makes="").values_list("make_model_class_id", "new_vehicle_makes")

    for mmc_id, makes_str in rate_makes:
        rate_groups = [m.strip() for m in makes_str.split(",")]
        for rg in rate_groups:
            if rg:
                for obj in all_makes_objs:
                    if obj.make_model_name and obj.make_model_name.strip() == rg:
                        if obj.make_model_cluster:
                            for item in str(obj.make_model_cluster).split(","):
                                item = item.strip()
                                if item:
                                    class_to_makes[str(mmc_id)].add(item)

    class_makes_mapping = {k: sorted(list(v)) for k, v in class_to_makes.items()}
    return json.dumps(all_individual_makes), json.dumps(class_makes_mapping), all_individual_makes


# =========================================================
# OCR / EXTRACTION HELPERS
# =========================================================
def normalize_label(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def safe_decimal(value):
    if value in [None, ""]:
        return None
    cleaned = str(value).strip().replace(",", "")
    cleaned = re.sub(r"[^\d.\-]", "", cleaned)
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def safe_date_multi(value):
    if not value:
        return None
    value = str(value).strip()
    formats = [
        "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d",
        "%d/%m/%y", "%d-%m-%y", "%d %b %Y", "%d %B %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except Exception:
            pass
    return None


def extract_text_from_pdf(file_path):
    if not fitz:
        raise Exception("PyMuPDF is not installed. Run: pip install pymupdf")
    text_parts = []
    doc = fitz.open(file_path)
    try:
        for page in doc:
            text_parts.append(page.get_text("text"))
    finally:
        doc.close()
    return "\n".join(text_parts).strip()


def extract_text_from_image(file_path):
    if not Image or not pytesseract:
        raise Exception("Pillow / pytesseract not installed. Run: pip install pillow pytesseract")
    img = Image.open(file_path)
    return pytesseract.image_to_string(img).strip()


def extract_text_from_document(file_path):
    ext = Path(file_path).suffix.lower()
    if ext == ".pdf":
        return extract_text_from_pdf(file_path), "PDF_TEXT"
    if ext in [".png", ".jpg", ".jpeg"]:
        return extract_text_from_image(file_path), "OCR"
    raise Exception("Unsupported file type.")


def build_alias_lookup():
    alias_lookup = {}
    active_aliases = MISFieldAlias.objects.select_related("field_master").filter(
        is_active=True,
        field_master__is_active=True
    )
    for alias in active_aliases:
        alias_lookup[normalize_label(alias.alias_text)] = alias.field_master.field_key
    return alias_lookup


def extract_key_value_candidates(raw_text):
    candidates = {}
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    patterns = [
        r"^\s*(.*?)\s*[:\-]\s*(.+?)\s*$",
        r"^\s*(.*?)\s{2,}(.+?)\s*$",
    ]

    for line in lines:
        for pattern in patterns:
            match = re.match(pattern, line)
            if match:
                left = normalize_label(match.group(1))
                right = match.group(2).strip()
                if left and right and left not in candidates:
                    candidates[left] = right
                break

    return candidates


def map_text_with_aliases(raw_text):
    alias_lookup = build_alias_lookup()
    candidates = extract_key_value_candidates(raw_text)
    mapped = {}

    for left_label, value in candidates.items():
        if left_label in alias_lookup:
            mapped[alias_lookup[left_label]] = value
            continue

        for alias_text, field_key in alias_lookup.items():
            if left_label == alias_text or alias_text in left_label or left_label in alias_text:
                if field_key not in mapped:
                    mapped[field_key] = value
                break

    return mapped


def save_policy_mis_record(document_obj, mapped_data):
    PolicyMISRecord.objects.update_or_create(
        source_document=document_obj,
        defaults={
            "insurer_name": mapped_data.get("insurer_name"),
            "policy_number": mapped_data.get("policy_number"),
            "insured_name": mapped_data.get("insured_name"),
            "vehicle_registration_number": mapped_data.get("vehicle_registration_number"),
            "vehicle_make": mapped_data.get("vehicle_make"),
            "vehicle_model": mapped_data.get("vehicle_model"),
            "vehicle_make_model": mapped_data.get("vehicle_make_model"),
            "engine_number": mapped_data.get("engine_number"),
            "chassis_number": mapped_data.get("chassis_number"),
            "fuel_type": mapped_data.get("fuel_type"),
            "cubic_capacity_cc": mapped_data.get("cubic_capacity_cc"),
            "gross_premium": safe_decimal(mapped_data.get("gross_premium")),
            "net_premium": safe_decimal(mapped_data.get("net_premium")),
            "tax_amount": safe_decimal(mapped_data.get("tax_amount")),
            "policy_start_date": safe_date_multi(mapped_data.get("policy_start_date")),
            "policy_end_date": safe_date_multi(mapped_data.get("policy_end_date")),
            "issue_date": safe_date_multi(mapped_data.get("issue_date")),
            "rto_location": mapped_data.get("rto_location"),
            "raw_ai_json": mapped_data,
            "confidence_notes": "Mapped using MIS aliases.",
            "ai_model_name": "alias-rule-engine",
        }
    )


def process_policy_document(document_obj):
    try:
        document_obj.status = PolicyDocumentUpload.STATUS_PROCESSING
        document_obj.error_message = ""
        document_obj.save(update_fields=["status", "error_message"])

        file_path = document_obj.uploaded_file.path
        extracted_text, method = extract_text_from_document(file_path)
        mapped_data = map_text_with_aliases(extracted_text)

        document_obj.extracted_text = extracted_text
        document_obj.extraction_method = method
        document_obj.parsed_json = mapped_data
        document_obj.status = PolicyDocumentUpload.STATUS_COMPLETED
        document_obj.processed_at = timezone.now()
        document_obj.save()

        save_policy_mis_record(document_obj, mapped_data)

    except Exception as e:
        document_obj.status = PolicyDocumentUpload.STATUS_FAILED
        document_obj.error_message = str(e)
        document_obj.processed_at = timezone.now()
        document_obj.save(update_fields=["status", "error_message", "processed_at"])


# =========================================================
# FORMS
# =========================================================
class MISFieldMasterForm(forms.ModelForm):
    class Meta:
        model = MISFieldMaster
        fields = ["field_key", "field_label", "is_active"]
        widgets = {
            "field_key": forms.TextInput(attrs={"class": "form-control"}),
            "field_label": forms.TextInput(attrs={"class": "form-control"}),
        }

    def clean_field_key(self):
        value = (self.cleaned_data.get("field_key") or "").strip().lower().replace(" ", "_")
        if not value:
            raise forms.ValidationError("Field key is required.")
        return value


class MISFieldAliasForm(forms.ModelForm):
    class Meta:
        model = MISFieldAlias
        fields = ["field_master", "alias_text", "is_active"]
        widgets = {
            "field_master": forms.Select(attrs={"class": "form-control"}),
            "alias_text": forms.TextInput(attrs={"class": "form-control"}),
        }

    def clean_alias_text(self):
        value = (self.cleaned_data.get("alias_text") or "").strip()
        if not value:
            raise forms.ValidationError("Alias text is required.")
        return value


class PolicyDocumentUploadForm(forms.ModelForm):
    class Meta:
        model = PolicyDocumentUpload
        fields = ["uploaded_file"]
        widgets = {
            "uploaded_file": forms.FileInput(attrs={"accept": ".pdf,.png,.jpg,.jpeg"})
        }

    def clean_uploaded_file(self):
        f = self.cleaned_data.get("uploaded_file")
        if not f:
            raise forms.ValidationError("Please select a file.")
        ext = f.name.split(".")[-1].lower() if "." in f.name else ""
        if ext not in ["pdf", "png", "jpg", "jpeg"]:
            raise forms.ValidationError("Only PDF, PNG, JPG, JPEG files are allowed.")
        return f


class RTOForm(forms.ModelForm):
    class Meta:
        model = RTOMaster
        fields = ["rto_name", "rto_cluster"]


class MakeModelForm(forms.ModelForm):
    class Meta:
        model = MakeModelMaster
        fields = ["make_model_name", "make_model_cluster"]


class RateForm(forms.ModelForm):
    product = forms.ModelChoiceField(
        queryset=ProductMaster.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "select2-single"})
    )
    sub_product = forms.ModelChoiceField(
        queryset=SubProductMaster.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "select2-single"})
    )
    policy_type = forms.ModelChoiceField(
        queryset=PolicyTypeMaster.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "select2-single"})
    )
    fuel_type = forms.ModelChoiceField(
        queryset=FuelTypeMaster.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "select2-single"})
    )
    make_model_class = forms.ModelChoiceField(
        queryset=MakeModelClassMaster.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "select2-single"})
    )
    is_ncb = forms.ModelChoiceField(
        queryset=YesNoNAMaster.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "select2-single"})
    )
    is_cpa = forms.ModelChoiceField(
        queryset=YesNoNAMaster.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "select2-single"})
    )
    is_zd = forms.ModelChoiceField(
        queryset=YesNoNAMaster.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "select2-single"})
    )

    new_rto_list = forms.MultipleChoiceField(
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "select2-multiple"})
    )
    new_vehicle_makes = forms.MultipleChoiceField(
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "select2-multiple"})
    )

    class Meta:
        model = RateMaster
        exclude = ["group", "created_at"]

    def __init__(self, *args, **kwargs):
        super(RateForm, self).__init__(*args, **kwargs)

        self.fields["product"].queryset = ProductMaster.objects.all()
        self.fields["sub_product"].queryset = SubProductMaster.objects.all()
        self.fields["policy_type"].queryset = PolicyTypeMaster.objects.all()
        self.fields["fuel_type"].queryset = FuelTypeMaster.objects.all()
        self.fields["make_model_class"].queryset = MakeModelClassMaster.objects.all()
        self.fields["is_ncb"].queryset = YesNoNAMaster.objects.all()
        self.fields["is_cpa"].queryset = YesNoNAMaster.objects.all()
        self.fields["is_zd"].queryset = YesNoNAMaster.objects.all()

        companies = list(
            RateMaster.objects.exclude(insurance_company__isnull=True)
            .exclude(insurance_company__exact="")
            .values_list("insurance_company", flat=True)
            .distinct()
            .order_by("insurance_company")
        )
        if self.instance and self.instance.insurance_company and self.instance.insurance_company not in companies:
            companies.append(self.instance.insurance_company)
        self.fields["insurance_company"].choices = [("", "---------")] + [(c, c) for c in companies]

        makes = list(MakeModelMaster.objects.values_list("make_model_name", flat=True).order_by("make_model_name"))
        initial_makes = kwargs.get("initial", {}).get("new_vehicle_makes", [])
        instance_makes = [x.strip() for x in self.instance.new_vehicle_makes.split(",")] if self.instance and self.instance.new_vehicle_makes else []
        all_makes = set(instance_makes + initial_makes)
        for m in all_makes:
            if m and m not in makes:
                makes.append(m)
        self.fields["new_vehicle_makes"].choices = [(m, m) for m in makes]

        verticals = list(
            RateMaster.objects.exclude(insurer_vertical__isnull=True)
            .exclude(insurer_vertical__exact="")
            .values_list("insurer_vertical", flat=True)
            .distinct()
            .order_by("insurer_vertical")
        )
        if self.instance and self.instance.insurer_vertical and self.instance.insurer_vertical not in verticals:
            verticals.append(self.instance.insurer_vertical)
        self.fields["insurer_vertical"].choices = [("", "---------")] + [(v, v) for v in verticals]

        rtos = list(RTOMaster.objects.values_list("rto_name", flat=True).order_by("rto_name"))
        initial_rtos = kwargs.get("initial", {}).get("new_rto_list", [])
        instance_rtos = [x.strip() for x in self.instance.new_rto_list.split(",")] if self.instance and self.instance.new_rto_list else []
        all_existing_rtos = set(instance_rtos + initial_rtos)
        for r in all_existing_rtos:
            if r and r not in rtos:
                rtos.append(r)
        self.fields["new_rto_list"].choices = [(r, r) for r in rtos]

    def clean_new_rto_list(self):
        data = self.cleaned_data.get("new_rto_list")
        return ", ".join(data) if data else ""

    def clean_new_vehicle_makes(self):
        data = self.cleaned_data.get("new_vehicle_makes")
        return ", ".join(data) if data else ""


# -------------------------
# Upload CSV
# -------------------------
@login_required
@user_passes_test(can_upload)
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
                    if not rto_name:
                        raise ValueError("rto_name is blank")
                    RTOMaster.objects.update_or_create(
                        rto_name=rto_name,
                        defaults={"rto_cluster": rto_cluster or None}
                    )
                    inserted += 1
                except Exception as e:
                    errors.append(f"Row {i}: {str(e)}")

            return render(request, "upload.html", {
                "summary": {"inserted": inserted, "duplicates": 0, "errors": len(errors)},
                "errors": errors
            })

        if upload_type == "make_model_master":
            for i, row in enumerate(reader, start=2):
                try:
                    make_model_name = (row.get("make_model_name") or "").strip()
                    make_model_cluster = (row.get("make_model_cluster") or "").strip()
                    if not make_model_name:
                        raise ValueError("make_model_name is blank")
                    MakeModelMaster.objects.update_or_create(
                        make_model_name=make_model_name,
                        defaults={"make_model_cluster": make_model_cluster or None}
                    )
                    inserted += 1
                except Exception as e:
                    errors.append(f"Row {i}: {str(e)}")

            return render(request, "upload.html", {
                "summary": {"inserted": inserted, "duplicates": 0, "errors": len(errors)},
                "errors": errors
            })

        valid_rtos = set(RTOMaster.objects.values_list("rto_name", flat=True))
        valid_makes = set(MakeModelMaster.objects.values_list("make_model_name", flat=True))

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
                raw_rtos = row.get("new_rto_list") or ""
                rto_items = [x.strip() for x in raw_rtos.split(",") if x.strip()]
                for rto in rto_items:
                    if rto not in valid_rtos:
                        raise ValueError(f"RTO '{rto}' does not exist in RTOMaster. Please add it first.")

                raw_makes = row.get("new_vehicle_makes") or ""
                make_items = [x.strip() for x in raw_makes.split(",") if x.strip()]
                for make in make_items:
                    if make not in valid_makes:
                        raise ValueError(f"Vehicle Make '{make}' does not exist in MakeModelMaster. Please add it first.")

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
                    status="INACTIVE",
                    is_deleted="NO",
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
@user_passes_test(can_view_dashboard)
def dashboard(request):
    qs = RateMaster.objects.select_related(
        "group", "product", "sub_product", "policy_type", "fuel_type",
        "make_model_class", "is_ncb", "is_cpa", "is_zd"
    ).all()

    q = (request.GET.get("q") or "").strip()
    status_filter = (request.GET.get("status") or "").strip()
    is_deleted_filter = (request.GET.get("is_deleted") or "").strip().upper()
    created_date = (request.GET.get("created_date") or "").strip()
    date_range = (request.GET.get("date_range") or "").strip()

    insurance_company = (request.GET.get("insurance_company") or "").strip()
    product = (request.GET.get("product") or "").strip()
    fuel = (request.GET.get("fuel") or "").strip()
    sub_product = (request.GET.get("sub_product") or "").strip()
    make_model_class = (request.GET.get("make_model_class") or "").strip()
    rto_code = (request.GET.get("rto_code") or "").strip()
    make_model_code = (request.GET.get("make_model_code") or "").strip()
    age_range = (request.GET.get("age_range") or "").strip()
    cc_range = (request.GET.get("cc_range") or "").strip()
    sc_range = (request.GET.get("sc_range") or "").strip()
    is_zd = (request.GET.get("is_zd") or "").strip()
    is_ncb = (request.GET.get("is_ncb") or "").strip()
    is_cpa = (request.GET.get("is_cpa") or "").strip()

    if q:
        group_ids = [int(gid.strip()) for gid in q.split(",") if gid.strip().isdigit()]
        if group_ids:
            qs = qs.filter(Q(group_id__in=group_ids) | Q(group_id__isnull=True, id__in=group_ids))
        else:
            qs = qs.none()

    if status_filter:
        qs = qs.filter(status=status_filter)

    if is_deleted_filter:
        qs = qs.filter(is_deleted=is_deleted_filter)
    if created_date:
        qs = qs.filter(created_at__date=created_date)

    if date_range:
        dates = date_range.split(" - ")
        if len(dates) == 2:
            d_from = dates[0].strip()
            d_to = dates[1].strip()
            if d_from and d_to:
                qs = qs.filter(
                    (Q(from_date__lte=d_to) | Q(from_date__isnull=True)) &
                    (Q(to_date__gte=d_from) | Q(to_date__isnull=True))
                )

    if insurance_company:
        qs = qs.filter(insurance_company=insurance_company)
    if product:
        qs = qs.filter(product_id=product)
    if fuel:
        qs = qs.filter(fuel_type_id=fuel)
    if sub_product:
        qs = qs.filter(sub_product_id=sub_product)

    matching_rto_names = []
    if rto_code:
        for rto_record in RTOMaster.objects.filter(rto_cluster__icontains=rto_code):
            if strict_match_in_cluster(rto_code, rto_record.rto_cluster):
                matching_rto_names.append(rto_record.rto_name.strip().upper())
        if matching_rto_names:
            q_rto = Q()
            for rto_name in matching_rto_names:
                q_rto |= Q(new_rto_list__icontains=rto_name)
            qs = qs.filter(q_rto)
        else:
            qs = qs.none()

    matching_make_names = []
    if make_model_code:
        for make_record in MakeModelMaster.objects.filter(make_model_cluster__icontains=make_model_code):
            if strict_match_in_cluster(make_model_code, make_record.make_model_cluster):
                matching_make_names.append(make_record.make_model_name.strip().upper())
        if matching_make_names:
            q_make = Q()
            for make_name in matching_make_names:
                q_make |= Q(new_vehicle_makes__icontains=make_name)
            qs = qs.filter(q_make)
        else:
            qs = qs.none()

    qs = apply_make_model_filter(qs, product, make_model_class)
    qs = apply_range_filter(qs, "vehicle_age_min", "vehicle_age_max", age_range)
    qs = apply_range_filter(qs, "cc_min", "cc_max", cc_range)
    qs = apply_range_filter(qs, "sc_min", "sc_max", sc_range)

    if is_zd:
        qs = qs.filter(is_zd__code=is_zd)
    if is_ncb:
        qs = qs.filter(is_ncb__code=is_ncb)
    if is_cpa:
        qs = qs.filter(is_cpa__code=is_cpa)

    active_count = qs.filter(status="ACTIVE").count()
    inactive_count = qs.filter(status="INACTIVE").count()

    qs = qs.order_by("-id")

    matching_gids_set = set()
    ordered_gids = []

    for row in qs.iterator(chunk_size=2000):
        if rto_code and matching_rto_names:
            if not row.new_rto_list:
                continue
            row_rtos = [x.strip().upper() for x in row.new_rto_list.split(",")]
            if not any(x in matching_rto_names for x in row_rtos):
                continue

        if make_model_code and matching_make_names:
            if not row.new_vehicle_makes:
                continue
            row_makes = [x.strip().upper() for x in row.new_vehicle_makes.split(",")]
            if not any(x in matching_make_names for x in row_makes):
                continue

        gid = row.group_id if row.group_id is not None else row.id
        if gid not in matching_gids_set:
            matching_gids_set.add(gid)
            ordered_gids.append(gid)

        if len(ordered_gids) >= 200:
            break

    buckets = defaultdict(list)
    if ordered_gids:
        full_group_qs = RateMaster.objects.select_related(
            "group", "product", "sub_product", "policy_type", "fuel_type",
            "make_model_class", "is_ncb", "is_cpa", "is_zd"
        ).filter(Q(group_id__in=ordered_gids) | Q(id__in=ordered_gids))

        for row in full_group_qs.iterator(chunk_size=2000):
            gid = row.group_id if row.group_id is not None else row.id
            buckets[gid].append(row)

    grouped_rows = []
    for gid in ordered_gids:
        rows = buckets.get(gid, [])
        if not rows:
            continue

        first = rows[0]
        all_rtos = split_csv_values([r.new_rto_list for r in rows])
        all_fuels = [r.fuel_type.name for r in rows if r.fuel_type]

        first.display_group_id = gid
        first.display_rto_list = unique_join(all_rtos)
        first.display_fuel_types = unique_join(all_fuels)
        first.display_make_model_class = first.make_model_class.name if first.make_model_class else ""

        age_min_str = f"{first.vehicle_age_min:.2f}" if first.vehicle_age_min is not None else ""
        age_max_str = f"{first.vehicle_age_max:.2f}" if first.vehicle_age_max is not None else ""
        first.age_range = f"{age_min_str} - {age_max_str}" if (age_min_str or age_max_str) else ""

        cc_min_str = f"{first.cc_min:.2f}" if first.cc_min is not None else ""
        cc_max_str = f"{first.cc_max:.2f}" if first.cc_max is not None else ""
        first.cc_range = f"{cc_min_str} - {cc_max_str}" if (cc_min_str or cc_max_str) else ""

        sc_min_str = f"{first.sc_min:.2f}" if first.sc_min is not None else ""
        sc_max_str = f"{first.sc_max:.2f}" if first.sc_max is not None else ""
        first.sc_range = f"{sc_min_str} - {sc_max_str}" if (sc_min_str or sc_max_str) else ""

        grouped_rows.append(first)

    field_names = [
        "display_group_id", "new_vehicle_makes", "display_rto_list",
        "insurer_vertical", "insurance_company", "product", "sub_product", "policy_type",
        "display_fuel_types", "display_make_model_class", "age_range",
        "pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5",
        "pi_net_rate", "pi_flat_amount", "pi_vli", "pi_type", "tariff_min", "tariff_max",
        "is_ncb", "is_cpa", "is_zd", "cc_range", "from_date", "to_date", "sc_range",
        "user_id", "veh_use", "remarks", "add_tnc",
        "po_type", "po_od_rate", "po_tp_rate", "po_net_rate", "po_flat_amount",
        "status", "is_deleted"
    ]

    insurance_company_list = RateMaster.objects.exclude(insurance_company="").values_list(
        "insurance_company", flat=True
    ).distinct().order_by("insurance_company")
    product_list = ProductMaster.objects.all().order_by("name")
    fuel_list = FuelTypeMaster.objects.all().order_by("name")
    sub_product_list = SubProductMaster.objects.all().order_by("name")
    make_model_class_list = list(MakeModelClassMaster.objects.all().order_by("name"))
    yes_no_na_list = YesNoNAMaster.objects.all().order_by("code")

    return render(request, "dashboard.html", {
        "data": grouped_rows,
        "field_names": field_names,
        "total": len(grouped_rows),
        "active_count": active_count,
        "inactive_count": inactive_count,
        "insurance_company_list": insurance_company_list,
        "product_list": product_list,
        "fuel_list": fuel_list,
        "sub_product_list": sub_product_list,
        "make_model_class_list": make_model_class_list,
        "yes_no_na_list": yes_no_na_list,
        "selected": {
            "q": q,
            "status": status_filter,
            "is_deleted": is_deleted_filter,
            "created_date": created_date,
            "insurance_company": insurance_company,
            "product": product,
            "fuel": fuel,
            "sub_product": sub_product,
            "make_model_class": make_model_class,
            "date_range": date_range,
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
# Export UNGROUPED to Excel
# -------------------------
@login_required
@user_passes_test(can_view_dashboard)
def export_rates_xlsx(request):
    qs = RateMaster.objects.select_related(
        "product", "sub_product", "policy_type", "fuel_type", "make_model_class", "is_ncb", "is_cpa", "is_zd"
    ).all()

    q = (request.GET.get("q") or "").strip()
    status_filter = (request.GET.get("status") or "").strip()
    is_deleted_filter = (request.GET.get("is_deleted") or "").strip().upper()
    created_date = (request.GET.get("created_date") or "").strip()
    date_range = (request.GET.get("date_range") or "").strip()

    insurance_company = (request.GET.get("insurance_company") or "").strip()
    product = (request.GET.get("product") or "").strip()
    fuel = (request.GET.get("fuel") or "").strip()
    sub_product = (request.GET.get("sub_product") or "").strip()
    make_model_class = (request.GET.get("make_model_class") or "").strip()
    rto_code = (request.GET.get("rto_code") or "").strip()
    make_model_code = (request.GET.get("make_model_code") or "").strip()
    age_range = (request.GET.get("age_range") or "").strip()
    cc_range = (request.GET.get("cc_range") or "").strip()
    sc_range = (request.GET.get("sc_range") or "").strip()
    is_zd = (request.GET.get("is_zd") or "").strip()
    is_ncb = (request.GET.get("is_ncb") or "").strip()
    is_cpa = (request.GET.get("is_cpa") or "").strip()

    if q:
        group_ids = [int(gid.strip()) for gid in q.split(",") if gid.strip().isdigit()]
        if group_ids:
            qs = qs.filter(Q(group_id__in=group_ids) | Q(group_id__isnull=True, id__in=group_ids))
        else:
            qs = qs.none()

    if status_filter:
        qs = qs.filter(status=status_filter)

    if is_deleted_filter:
        qs = qs.filter(is_deleted=is_deleted_filter)
    if created_date:
        qs = qs.filter(created_at__date=created_date)

    if date_range:
        dates = date_range.split(" - ")
        if len(dates) == 2:
            d_from = dates[0].strip()
            d_to = dates[1].strip()
            if d_from and d_to:
                qs = qs.filter(
                    (Q(from_date__lte=d_to) | Q(from_date__isnull=True)) &
                    (Q(to_date__gte=d_from) | Q(to_date__isnull=True))
                )

    if insurance_company:
        qs = qs.filter(insurance_company=insurance_company)
    if product:
        qs = qs.filter(product_id=product)
    if fuel:
        qs = qs.filter(fuel_type_id=fuel)
    if sub_product:
        qs = qs.filter(sub_product_id=sub_product)

    matching_rto_names = []
    if rto_code:
        for rto_record in RTOMaster.objects.filter(rto_cluster__icontains=rto_code):
            if strict_match_in_cluster(rto_code, rto_record.rto_cluster):
                matching_rto_names.append(rto_record.rto_name.strip().upper())
        if matching_rto_names:
            q_rto = Q()
            for rto_name in matching_rto_names:
                q_rto |= Q(new_rto_list__icontains=rto_name)
            qs = qs.filter(q_rto)
        else:
            qs = qs.none()

    matching_make_names = []
    if make_model_code:
        for make_record in MakeModelMaster.objects.filter(make_model_cluster__icontains=make_model_code):
            if strict_match_in_cluster(make_model_code, make_record.make_model_cluster):
                matching_make_names.append(make_record.make_model_name.strip().upper())
        if matching_make_names:
            q_make = Q()
            for make_name in matching_make_names:
                q_make |= Q(new_vehicle_makes__icontains=make_name)
            qs = qs.filter(q_make)
        else:
            qs = qs.none()

    qs = apply_make_model_filter(qs, product, make_model_class)
    qs = apply_range_filter(qs, "vehicle_age_min", "vehicle_age_max", age_range)
    qs = apply_range_filter(qs, "cc_min", "cc_max", cc_range)
    qs = apply_range_filter(qs, "sc_min", "sc_max", sc_range)

    if is_zd:
        qs = qs.filter(is_zd__code=is_zd)
    if is_ncb:
        qs = qs.filter(is_ncb__code=is_ncb)
    if is_cpa:
        qs = qs.filter(is_cpa__code=is_cpa)

    qs = qs.order_by("-id")

    wb = Workbook()
    ws = wb.active
    ws.title = "Rates (Ungrouped)"

    headers = [
        "id", "group_id", "new_vehicle_makes", "new_rto_list", "insurer_vertical",
        "insurance_company", "product", "sub_product", "policy_type", "fuel_type",
        "make_model_class", "vehicle_age_min", "vehicle_age_max", "pi_od_rate", "pi_tp_rate",
        "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5", "pi_net_rate", "pi_flat_amount",
        "pi_vli", "pi_type", "tariff_min", "tariff_max", "cc_min", "cc_max",
        "is_ncb", "is_cpa", "is_zd", "from_date", "to_date", "sc_min", "sc_max",
        "user_id", "veh_use", "remarks", "add_tnc", "po_type", "po_od_rate", "po_tp_rate",
        "po_net_rate", "po_flat_amount", "status", "is_deleted"
    ]
    ws.append(headers)

    for r in qs.iterator(chunk_size=2000):
        if rto_code and matching_rto_names:
            if not r.new_rto_list:
                continue
            row_rtos = [x.strip().upper() for x in r.new_rto_list.split(",")]
            if not any(x in matching_rto_names for x in row_rtos):
                continue

        if make_model_code and matching_make_names:
            if not r.new_vehicle_makes:
                continue
            row_makes = [x.strip().upper() for x in r.new_vehicle_makes.split(",")]
            if not any(x in matching_make_names for x in row_makes):
                continue

        age_min_val = r.vehicle_age_min if r.vehicle_age_min is not None else ""
        age_max_val = r.vehicle_age_max if r.vehicle_age_max is not None else ""
        cc_min_val = r.cc_min if r.cc_min is not None else ""
        cc_max_val = r.cc_max if r.cc_max is not None else ""
        sc_min_val = r.sc_min if r.sc_min is not None else ""
        sc_max_val = r.sc_max if r.sc_max is not None else ""

        makes_list = [x.strip() for x in str(r.new_vehicle_makes or "").split(",")]
        makes_list = [x for x in makes_list if x] or [""]

        rtos_list = [x.strip() for x in str(r.new_rto_list or "").split(",")]
        rtos_list = [x for x in rtos_list if x] or [""]

        for make_item in makes_list:
            for rto_item in rtos_list:
                ws.append([
                    r.id, r.group_id, make_item, rto_item, r.insurer_vertical or "",
                    r.insurance_company or "", r.product.name if r.product else "",
                    r.sub_product.name if r.sub_product else "", r.policy_type.name if r.policy_type else "",
                    r.fuel_type.name if r.fuel_type else "", r.make_model_class.name if r.make_model_class else "",
                    age_min_val, age_max_val,
                    r.pi_od_rate if r.pi_od_rate is not None else "",
                    r.pi_tp_rate if r.pi_tp_rate is not None else "",
                    r.pi_tp_2 if r.pi_tp_2 is not None else "",
                    r.pi_tp_3 if r.pi_tp_3 is not None else "",
                    r.pi_tp_4 if r.pi_tp_4 is not None else "",
                    r.pi_tp_5 if r.pi_tp_5 is not None else "",
                    r.pi_net_rate if r.pi_net_rate is not None else "",
                    r.pi_flat_amount if r.pi_flat_amount is not None else "",
                    r.pi_vli if r.pi_vli is not None else "",
                    r.pi_type or "",
                    r.tariff_min if r.tariff_min is not None else "",
                    r.tariff_max if r.tariff_max is not None else "",
                    cc_min_val, cc_max_val,
                    r.is_ncb.code if r.is_ncb else "",
                    r.is_cpa.code if r.is_cpa else "",
                    r.is_zd.code if r.is_zd else "",
                    r.from_date.strftime("%Y-%m-%d") if r.from_date else "",
                    r.to_date.strftime("%Y-%m-%d") if r.to_date else "",
                    sc_min_val, sc_max_val,
                    r.user_id if r.user_id is not None else "",
                    r.veh_use or "",
                    r.remarks or "",
                    r.add_tnc or "",
                    r.po_type or "",
                    r.po_od_rate if r.po_od_rate is not None else "",
                    r.po_tp_rate if r.po_tp_rate is not None else "",
                    r.po_net_rate if r.po_net_rate is not None else "",
                    r.po_flat_amount if r.po_flat_amount is not None else "",
                    r.status or "",
                    r.is_deleted or "",
                ])

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="rates_ungrouped.xlsx"'
    wb.save(response)
    return response


# -------------------------
# Alias Management
# -------------------------
@login_required
@user_passes_test(can_view_alias_management)
def alias_management(request):
    msg = ""
    error = ""

    field_search = (request.GET.get("field_search") or "").strip()
    alias_search = (request.GET.get("alias_search") or "").strip()
    active_filter = (request.GET.get("active_filter") or "").strip()

    field_form = MISFieldMasterForm(prefix="field")
    alias_form = MISFieldAliasForm(prefix="alias")

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        try:
            if action == "add_field":
                field_form = MISFieldMasterForm(request.POST, prefix="field")
                if field_form.is_valid():
                    field_obj = field_form.save()
                    msg = f"✅ Field '{field_obj.field_label}' added successfully."
                    field_form = MISFieldMasterForm(prefix="field")
                else:
                    error = "⚠️ Please correct the field form errors."

            elif action == "add_alias":
                alias_form = MISFieldAliasForm(request.POST, prefix="alias")
                if alias_form.is_valid():
                    alias_obj = alias_form.save()
                    msg = f"✅ Alias '{alias_obj.alias_text}' added successfully."
                    alias_form = MISFieldAliasForm(prefix="alias")
                else:
                    error = "⚠️ Please correct the alias form errors."

            elif action == "toggle_alias":
                alias_id = request.POST.get("alias_id")
                alias_obj = MISFieldAlias.objects.get(id=alias_id)
                alias_obj.is_active = not alias_obj.is_active
                alias_obj.save()
                msg = f"✅ Alias '{alias_obj.alias_text}' status updated."

            elif action == "delete_alias":
                alias_id = request.POST.get("alias_id")
                alias_obj = MISFieldAlias.objects.get(id=alias_id)
                alias_name = alias_obj.alias_text
                alias_obj.delete()
                msg = f"🗑️ Alias '{alias_name}' deleted successfully."

            elif action == "delete_field":
                field_id = request.POST.get("field_id")
                field_obj = MISFieldMaster.objects.get(id=field_id)
                field_name = field_obj.field_label
                field_obj.delete()
                msg = f"🗑️ Field '{field_name}' deleted successfully."

            elif action == "toggle_field":
                field_id = request.POST.get("field_id")
                field_obj = MISFieldMaster.objects.get(id=field_id)
                field_obj.is_active = not field_obj.is_active
                field_obj.save()
                msg = f"✅ Field '{field_obj.field_label}' status updated."

        except Exception as e:
            error = f"⚠️ {str(e)}"

    fields_qs = MISFieldMaster.objects.prefetch_related("aliases").all().order_by("field_label")
    aliases_qs = MISFieldAlias.objects.select_related("field_master").all().order_by(
        "field_master__field_label", "alias_text"
    )

    if field_search:
        fields_qs = fields_qs.filter(
            Q(field_key__icontains=field_search) |
            Q(field_label__icontains=field_search)
        )

    if alias_search:
        aliases_qs = aliases_qs.filter(
            Q(alias_text__icontains=alias_search) |
            Q(field_master__field_key__icontains=alias_search) |
            Q(field_master__field_label__icontains=alias_search)
        )

    if active_filter == "active":
        fields_qs = fields_qs.filter(is_active=True)
        aliases_qs = aliases_qs.filter(is_active=True)
    elif active_filter == "inactive":
        fields_qs = fields_qs.filter(is_active=False)
        aliases_qs = aliases_qs.filter(is_active=False)

    return render(request, "alias_management.html", {
        "field_form": field_form,
        "alias_form": alias_form,
        "fields": fields_qs,
        "aliases": aliases_qs,
        "msg": msg,
        "error": error,
        "selected": {
            "field_search": field_search,
            "alias_search": alias_search,
            "active_filter": active_filter,
        }
    })


# -------------------------
# Upload & Extract PDF / Image
# -------------------------
@login_required
@user_passes_test(can_view_alias_management)
def upload_extract_pdf(request):
    msg = ""
    error = ""
    upload_form = PolicyDocumentUploadForm()

    if request.method == "POST":
        upload_form = PolicyDocumentUploadForm(request.POST, request.FILES)
        if upload_form.is_valid():
            try:
                doc_obj = upload_form.save(commit=False)
                doc_obj.original_filename = doc_obj.uploaded_file.name
                doc_obj.uploaded_by = request.user
                doc_obj.mime_type = getattr(doc_obj.uploaded_file, "content_type", "") or ""
                doc_obj.status = PolicyDocumentUpload.STATUS_PENDING
                doc_obj.save()

                process_policy_document(doc_obj)
                return redirect("upload_extract_pdf")

            except Exception as e:
                error = f"⚠️ {str(e)}"
        else:
            error = "⚠️ Please correct the upload form."

    documents = PolicyDocumentUpload.objects.select_related("uploaded_by").order_by("-created_at")[:20]
    latest_document = documents[0] if documents else None

    if latest_document and latest_document.status == PolicyDocumentUpload.STATUS_COMPLETED:
        msg = f"✅ Last processed document: {latest_document.original_filename}"

    return render(request, "upload_extract_pdf.html", {
        "upload_form": upload_form,
        "documents": documents,
        "latest_document": latest_document,
        "msg": msg,
        "error": error,
    })


# -------------------------
# My MIS
# -------------------------
@login_required
@user_passes_test(can_view_alias_management)
def my_mis(request):
    qs = PolicyMISRecord.objects.select_related("source_document").order_by("-created_at")

    policy_number = (request.GET.get("policy_number") or "").strip()
    insured_name = (request.GET.get("insured_name") or "").strip()
    insurer_name = (request.GET.get("insurer_name") or "").strip()

    if policy_number:
        qs = qs.filter(policy_number__icontains=policy_number)
    if insured_name:
        qs = qs.filter(insured_name__icontains=insured_name)
    if insurer_name:
        qs = qs.filter(insurer_name__icontains=insurer_name)

    return render(request, "my_mis.html", {
        "records": qs[:200],
        "selected": {
            "policy_number": policy_number,
            "insured_name": insured_name,
            "insurer_name": insurer_name,
        }
    })


# -------------------------
# User Management (ADMIN only)
# -------------------------
@login_required
@user_passes_test(is_admin)
def user_management(request):
    Group.objects.get_or_create(name="ADMIN")
    for group_name in PAGE_GROUPS:
        Group.objects.get_or_create(name=group_name)

    msg = ""
    error = ""

    if request.method == "POST":
        action = request.POST.get("action")
        user_id = request.POST.get("user_id")

        if action == "create_user":
            uname = request.POST.get("new_username", "").strip()
            fname = request.POST.get("full_name", "").strip()
            uemail = request.POST.get("email", "").strip()
            ucontact = request.POST.get("contact_number", "").strip()
            upass = "Changeme@123"

            if uname:
                if not User.objects.filter(username=uname).exists():
                    new_user = User.objects.create_user(username=uname, email=uemail, password=upass)
                    if fname:
                        parts = fname.split(" ", 1)
                        new_user.first_name = parts[0]
                        if len(parts) > 1:
                            new_user.last_name = parts[1]
                    new_user.save()

                    UserProfile.objects.get_or_create(user=new_user, defaults={"contact_number": ucontact})
                    msg = f"✅ User '{uname}' created successfully."
                else:
                    error = f"⚠️ User '{uname}' already exists."

        elif action == "update_user_access":
            u = User.objects.get(id=user_id)
            u.groups.remove(Group.objects.get(name="ADMIN"))
            for pg in PAGE_GROUPS:
                u.groups.remove(Group.objects.get(name=pg))

            selected_pages = request.POST.getlist(f"pages_{user_id}")
            for pg in selected_pages:
                u.groups.add(Group.objects.get(name=pg))

            profile, _ = UserProfile.objects.get_or_create(user=u)
            profile.contact_number = request.POST.get("contact_number", "")
            profile.designation = request.POST.get("designation", "")
            profile.save()

            u.email = request.POST.get("email", "")
            u.save()
            msg = f"✅ Profile and access for '{u.username}' updated."

        elif action == "make_admin" and user_id:
            u = User.objects.get(id=user_id)
            u.groups.clear()
            u.groups.add(Group.objects.get(name="ADMIN"))
            msg = f"✅ User '{u.username}' promoted to Full Admin."

        elif action == "delete_user" and user_id:
            u = User.objects.get(id=user_id)
            if u.is_superuser:
                error = "⚠️ You cannot delete a Super Admin account."
            elif u == request.user:
                error = "⚠️ You cannot delete your own account."
            else:
                uname = u.username
                u.delete()
                msg = f"🗑️ User '{uname}' has been completely removed from the system."

    users = User.objects.select_related("profile").all().order_by("-is_superuser", "username")
    user_rows = []
    for u in users:
        user_rows.append({
            "id": u.id,
            "username": u.username,
            "full_name": u.get_full_name(),
            "email": u.email,
            "contact_number": u.profile.contact_number if hasattr(u, "profile") else "",
            "designation": u.profile.designation if hasattr(u, "profile") else "",
            "is_superuser": u.is_superuser,
            "is_admin": u.groups.filter(name="ADMIN").exists(),
            "pages": list(u.groups.values_list("name", flat=True)),
        })

    return render(request, "user_management.html", {
        "users": user_rows,
        "page_groups": PAGE_GROUPS,
        "msg": msg,
        "error": error
    })


# -------------------------
# RTO DASHBOARD
# -------------------------
@login_required
@user_passes_test(can_view_rto)
def rto_dashboard(request):
    qs = RTOMaster.objects.all().order_by("rto_name")

    rto_names = request.GET.getlist("rto_name")
    cluster_q = (request.GET.get("cluster_q") or "").strip()

    if rto_names and "" not in rto_names:
        qs = qs.filter(rto_name__in=rto_names)
    if cluster_q:
        qs = qs.filter(rto_cluster__icontains=cluster_q)

    rto_name_list = RTOMaster.objects.values_list("rto_name", flat=True).distinct().order_by("rto_name")

    return render(request, "rto_dashboard.html", {
        "data": qs,
        "total": qs.count(),
        "rto_name_list": rto_name_list,
        "selected": {
            "rto_names": rto_names,
            "cluster_q": cluster_q
        },
        "is_admin": is_admin(request.user)
    })


@login_required
@user_passes_test(can_view_rto)
def export_rto_xlsx(request):
    qs = RTOMaster.objects.all().order_by("rto_name")

    rto_names = request.GET.getlist("rto_name")
    cluster_q = (request.GET.get("cluster_q") or "").strip()

    if rto_names and "" not in rto_names:
        qs = qs.filter(rto_name__in=rto_names)
    if cluster_q:
        qs = qs.filter(rto_cluster__icontains=cluster_q)

    wb = Workbook()
    ws = wb.active
    ws.title = "RTO Master"
    ws.append(["ID", "RTO NAME", "RTO CLUSTER"])

    for r in qs:
        ws.append([r.id, r.rto_name, r.rto_cluster or ""])

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="rto_master.xlsx"'
    wb.save(response)
    return response


# -------------------------
# MAKE MODEL DASHBOARD
# -------------------------
@login_required
@user_passes_test(can_view_make_model)
def make_model_dashboard(request):
    qs = MakeModelMaster.objects.all().order_by("make_model_name")

    make_model_names = request.GET.getlist("make_model_name")
    cluster_q = (request.GET.get("cluster_q") or "").strip()

    if make_model_names and "" not in make_model_names:
        qs = qs.filter(make_model_name__in=make_model_names)
    if cluster_q:
        qs = qs.filter(make_model_cluster__icontains=cluster_q)

    make_model_name_list = MakeModelMaster.objects.values_list(
        "make_model_name", flat=True
    ).distinct().order_by("make_model_name")

    return render(request, "make_model_dashboard.html", {
        "data": qs,
        "total": qs.count(),
        "make_model_name_list": make_model_name_list,
        "selected": {
            "make_model_names": make_model_names,
            "cluster_q": cluster_q
        },
        "is_admin": is_admin(request.user)
    })


@login_required
@user_passes_test(can_view_make_model)
def export_make_model_xlsx(request):
    qs = MakeModelMaster.objects.all().order_by("make_model_name")

    make_model_names = request.GET.getlist("make_model_name")
    cluster_q = (request.GET.get("cluster_q") or "").strip()

    if make_model_names and "" not in make_model_names:
        qs = qs.filter(make_model_name__in=make_model_names)
    if cluster_q:
        qs = qs.filter(make_model_cluster__icontains=cluster_q)

    wb = Workbook()
    ws = wb.active
    ws.title = "Make Model Master"
    ws.append(["ID", "MAKE MODEL NAME", "MAKE MODEL CLUSTER"])

    for r in qs:
        ws.append([r.id, r.make_model_name, r.make_model_cluster or ""])

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="make_model_master.xlsx"'
    wb.save(response)
    return response


# -------------------------
# Edit Master Tables
# -------------------------
@login_required
@user_passes_test(is_admin)
def edit_rto(request, pk):
    obj = RTOMaster.objects.get(id=pk)
    if request.method == "POST":
        form = RTOForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return redirect("rto_dashboard")
    else:
        form = RTOForm(instance=obj)

    return render(request, "edit_master.html", {
        "form": form,
        "title": "Edit RTO Record",
        "back_url": "rto_dashboard"
    })


@login_required
@user_passes_test(is_admin)
def edit_make_model(request, pk):
    obj = MakeModelMaster.objects.get(id=pk)
    if request.method == "POST":
        form = MakeModelForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return redirect("make_model_dashboard")
    else:
        form = MakeModelForm(instance=obj)

    return render(request, "edit_master.html", {
        "form": form,
        "title": "Edit Make/Model Record",
        "back_url": "make_model_dashboard"
    })


# -------------------------
# Edit Rate Form
# -------------------------
@login_required
@user_passes_test(is_admin)
def edit_rate(request, group_id):
    records = RateMaster.objects.filter(Q(group_id=group_id) | Q(id=group_id))
    if not records.exists():
        return HttpResponse("Record not found.", status=404)

    first_record = records.first()
    record_count = records.count()

    all_rtos = split_csv_values([r.new_rto_list for r in records])
    unique_rto_list = list(set([r for r in all_rtos if r]))
    rto_display = unique_join(all_rtos)

    all_makes = split_csv_values([r.new_vehicle_makes for r in records])
    unique_makes_list = list(set([m for m in all_makes if m]))

    initial_data = {
        "new_rto_list": unique_rto_list,
        "new_vehicle_makes": unique_makes_list,
        "insurance_company": first_record.insurance_company,
        "insurer_vertical": first_record.insurer_vertical,
        "status": first_record.status,
        "is_deleted": first_record.is_deleted,
        "product": first_record.product_id,
        "sub_product": first_record.sub_product_id,
        "policy_type": first_record.policy_type_id,
        "fuel_type": first_record.fuel_type_id,
        "make_model_class": first_record.make_model_class_id,
        "is_ncb": first_record.is_ncb_id,
        "is_cpa": first_record.is_cpa_id,
        "is_zd": first_record.is_zd_id,
    }

    if request.method == "POST":
        form = RateForm(request.POST, instance=first_record)
        if form.is_valid():
            update_data = {field: value for field, value in form.cleaned_data.items()}
            records.update(**update_data)

            AuditLog.objects.create(
                user=request.user,
                action="MANUAL EDIT",
                details=f"Edited Group/Record ID {group_id} via form. Updated {record_count} rows."
            )
            return redirect("dashboard")
    else:
        form = RateForm(instance=first_record, initial=initial_data)

    return render(request, "edit_rate.html", {
        "form": form,
        "count": records.count(),
        "group_id": group_id,
        "rto_display": rto_display
    })


# -------------------------
# BULK UPDATE RATES
# -------------------------
@login_required
@user_passes_test(is_admin)
def bulk_update_rates(request):
    if request.method == "POST":
        group_ids = request.POST.getlist("selected_groups")
        field_name = request.POST.get("update_field")
        new_value = request.POST.get("update_value", "").strip()

        if not group_ids or not field_name:
            return redirect("dashboard")

        records = RateMaster.objects.filter(Q(group_id__in=group_ids) | Q(id__in=group_ids))
        record_count = records.count()

        parsed_value = new_value
        if field_name == "product":
            parsed_value, _ = ProductMaster.objects.get_or_create(name=new_value) if new_value else (None, False)
        elif field_name == "sub_product":
            parsed_value, _ = SubProductMaster.objects.get_or_create(name=new_value) if new_value else (None, False)
        elif field_name == "policy_type":
            parsed_value, _ = PolicyTypeMaster.objects.get_or_create(name=new_value) if new_value else (None, False)
        elif field_name == "fuel_type":
            parsed_value, _ = FuelTypeMaster.objects.get_or_create(name=new_value) if new_value else (None, False)
        elif field_name == "make_model_class":
            parsed_value, _ = MakeModelClassMaster.objects.get_or_create(name=new_value) if new_value else (None, False)
        elif field_name in ["is_ncb", "is_cpa", "is_zd"]:
            parsed_value = parse_yes_no_na(new_value)
        elif field_name in ["status", "is_deleted"]:
            parsed_value = str(new_value).strip().upper()
        elif field_name in ["vehicle_age_min", "vehicle_age_max", "cc_min", "cc_max", "user_id"]:
            parsed_value = int(float(new_value)) if new_value else None
        elif field_name in [
            "pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5",
            "pi_net_rate", "pi_flat_amount", "pi_vli", "tariff_min", "tariff_max",
            "sc_min", "sc_max", "po_od_rate", "po_tp_rate", "po_net_rate", "po_flat_amount"
        ]:
            parsed_value = float(new_value) if new_value else None

        records.update(**{field_name: parsed_value})

        AuditLog.objects.create(
            user=request.user,
            action="BULK UPDATE",
            details=f"Updated {record_count} rows. Changed '{field_name}' to '{new_value}'."
        )

    return redirect("dashboard")


# -------------------------
# MOTOR PAYOUT RATES
# -------------------------
@login_required
@user_passes_test(can_view_motor_payout)
def motor_payout_rates(request):
    qs = RateMaster.objects.select_related(
        "product", "sub_product", "policy_type", "fuel_type",
        "make_model_class", "is_ncb", "is_cpa", "is_zd"
    ).all()

    qs = qs.exclude(is_deleted="YES")
    qs = qs.filter(status__in=["ACTIVE", "INACTIVE"])

    today_str = datetime.today().strftime("%Y-%m-%d")
    target_date = request.GET.get("target_date", today_str).strip()

    product = (request.GET.get("product") or "").strip()
    make_model_class = (request.GET.get("make_model_class") or "").strip()
    sub_product = (request.GET.get("sub_product") or "").strip()
    make_names = (request.GET.get("make_names") or "").strip()
    rto_code = (request.GET.get("rto_code") or "").strip()
    cc = (request.GET.get("cc") or "").strip()
    fuel = (request.GET.get("fuel") or "").strip()
    sc = (request.GET.get("sc") or "").strip()
    mfg_year = (request.GET.get("mfg_year") or "").strip()
    is_zd = (request.GET.get("is_zd") or "").strip().upper()
    is_cpa = (request.GET.get("is_cpa") or "").strip().upper()
    is_ncb = (request.GET.get("is_ncb") or "").strip().upper()

    if target_date:
        qs = qs.filter(
            (Q(from_date__lte=target_date) | Q(from_date__isnull=True)) &
            (Q(to_date__gte=target_date) | Q(to_date__isnull=True))
        )

    if product:
        qs = qs.filter(product_id=product)

    if make_model_class:
        if str(make_model_class).isdigit():
            qs = qs.filter(make_model_class_id=make_model_class)
        elif make_model_class == "NA":
            qs = qs.filter(make_model_class__name__iexact="NA")

    if sub_product:
        qs = qs.filter(sub_product_id=sub_product)
    if fuel:
        qs = qs.filter(fuel_type_id=fuel)

    matching_rto_names = []
    if rto_code:
        potential_rtos = RTOMaster.objects.filter(rto_cluster__icontains=rto_code)
        for rto_record in potential_rtos:
            if strict_match_in_cluster(rto_code, rto_record.rto_cluster):
                matching_rto_names.append(rto_record.rto_name.strip().upper())
        if matching_rto_names:
            q_rto = Q()
            for rto_name in matching_rto_names:
                q_rto |= Q(new_rto_list__icontains=rto_name)
            qs = qs.filter(q_rto)
        else:
            qs = qs.none()

    matching_make_groups = []
    if make_names:
        potential_makes = MakeModelMaster.objects.filter(make_model_cluster__icontains=make_names)
        for make_record in potential_makes:
            if strict_match_in_cluster(make_names, make_record.make_model_cluster):
                matching_make_groups.append(make_record.make_model_name.strip().upper())
        if matching_make_groups:
            q_make = Q()
            for group_name in matching_make_groups:
                if group_name:
                    q_make |= Q(new_vehicle_makes__icontains=group_name.strip())
            qs = qs.filter(q_make)
        else:
            qs = qs.none()

    if cc and cc.isdigit():
        qs = qs.filter(
            (Q(cc_min__lte=int(cc)) | Q(cc_min__isnull=True)) &
            (Q(cc_max__gte=int(cc)) | Q(cc_max__isnull=True))
        )

    if sc:
        try:
            qs = qs.filter(
                (Q(sc_min__lte=float(sc)) | Q(sc_min__isnull=True)) &
                (Q(sc_max__gte=float(sc)) | Q(sc_max__isnull=True))
            )
        except ValueError:
            pass

    if mfg_year and mfg_year.isdigit() and target_date:
        try:
            target_dt = datetime.strptime(target_date, "%Y-%m-%d")
            mfg_dt = datetime(int(mfg_year), 1, 1)
            exact_age = round((target_dt - mfg_dt).days / 365.25, 2)
            qs = qs.filter(
                (Q(vehicle_age_min__lte=exact_age) | Q(vehicle_age_min__isnull=True)) &
                (Q(vehicle_age_max__gte=exact_age) | Q(vehicle_age_max__isnull=True))
            )
        except ValueError:
            pass

    if is_zd:
        qs = qs.filter(Q(is_zd__code__iexact=is_zd) | Q(is_zd__code__iexact="NA"))
    if is_cpa:
        qs = qs.filter(Q(is_cpa__code__iexact=is_cpa) | Q(is_cpa__code__iexact="NA"))
    if is_ncb:
        qs = qs.filter(Q(is_ncb__code__iexact=is_ncb) | Q(is_ncb__code__iexact="NA"))

    qs = qs.order_by(
        F("po_net_rate").desc(nulls_last=True),
        F("po_od_rate").desc(nulls_last=True),
        F("po_flat_amount").desc(nulls_last=True),
        "-id"
    )

    results = []
    seen_groups = set()

    for row in qs.iterator(chunk_size=2000):
        if rto_code and matching_rto_names:
            if not row.new_rto_list:
                continue
            row_rtos = [x.strip().upper() for x in row.new_rto_list.split(",")]
            if not any(x in matching_rto_names for x in row_rtos):
                continue

        if make_names and matching_make_groups:
            if not row.new_vehicle_makes:
                continue
            row_makes = [x.strip().upper() for x in row.new_vehicle_makes.split(",")]
            if not any(x in matching_make_groups for x in row_makes):
                continue

        gid = row.group_id if row.group_id is not None else row.id
        if gid not in seen_groups:
            row.display_group_id = gid

            if row.po_net_rate and row.po_net_rate > 0:
                row.po_rate = row.po_net_rate
            elif row.po_od_rate and row.po_od_rate > 0:
                row.po_rate = row.po_od_rate
            elif row.po_tp_rate and row.po_tp_rate > 0:
                row.po_rate = row.po_tp_rate
            else:
                row.po_rate = 0.0

            results.append(row)
            seen_groups.add(gid)

        if len(results) >= 300:
            break

    field_names = ["display_group_id", "status", "insurance_company", "po_type", "po_rate", "po_flat_amount", "add_tnc"]
    all_makes_json, class_makes_mapping_json, all_makes = get_make_mapping_context()

    return render(request, "motor_payout_rates.html", {
        "data": results,
        "total_found": len(results),
        "field_names": field_names,
        "product_list": ProductMaster.objects.all().order_by("name"),
        "sub_product_list": SubProductMaster.objects.all().order_by("name"),
        "fuel_list": FuelTypeMaster.objects.all().order_by("name"),
        "make_model_class_list": list(MakeModelClassMaster.objects.all().order_by("name")),
        "all_makes_json": all_makes_json,
        "class_makes_mapping_json": class_makes_mapping_json,
        "selected": {
            "target_date": target_date,
            "product": product,
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


# -------------------------
# POLICY LOCK CHECKER
# -------------------------
@login_required
@user_passes_test(can_view_motor_payout)
def policy_lock_checker(request):
    has_searched = bool(request.GET) # Detects if user clicked 'Check Eligibility'

    # --- UPDATED: Create Audit Log with cleaner dictionary ---
    if has_searched:
        # Create a flat dictionary (no lists) of the search parameters
        flat_params = {}
        for key in request.GET.keys():
            val = request.GET.get(key, "").strip()
            if val and key != "csrfmiddlewaretoken":
                flat_params[key] = val
        
        # Only log if they actually searched with parameters
        if flat_params:
            AuditLog.objects.create(
                user=request.user,
                action="MOTOR_POINTS_SEARCH",
                details=str(flat_params) # Save as stringified flat dict
            )
    # ---------------------------------------------------------

    qs = RateMaster.objects.select_related(
        "product", "sub_product", "policy_type", "fuel_type",
        "make_model_class", "is_ncb", "is_cpa", "is_zd"
    ).all()

    qs = qs.exclude(is_deleted="YES")
    qs = qs.filter(status__in=["ACTIVE", "INACTIVE"])

    today_str = datetime.today().strftime("%Y-%m-%d")
    target_date = (request.GET.get("target_date") or today_str).strip()

    vehicle_no = (request.GET.get("vehicle_no") or "").strip()
    policy_holder_name = (request.GET.get("policy_holder_name") or "").strip()
    product = (request.GET.get("product") or "").strip()
    make_model_class = (request.GET.get("make_model_class") or "").strip()
    sub_product = (request.GET.get("sub_product") or "").strip()
    make_names = (request.GET.get("make_names") or "").strip()
    rto_code = (request.GET.get("rto_code") or "").strip()
    cc = (request.GET.get("cc") or "").strip()
    fuel = (request.GET.get("fuel") or "").strip()
    sc = (request.GET.get("sc") or "").strip()
    mfg_year = (request.GET.get("mfg_year") or "").strip()

    is_zd = (request.GET.get("is_zd") or "NO").strip().upper()
    is_cpa = (request.GET.get("is_cpa") or "NO").strip().upper()
    is_ncb = (request.GET.get("is_ncb") or "NO").strip().upper()

    if target_date:
        qs = qs.filter(
            (Q(from_date__lte=target_date) | Q(from_date__isnull=True)) &
            (Q(to_date__gte=target_date) | Q(to_date__isnull=True))
        )

    if product:
        qs = qs.filter(product_id=product)

    if make_model_class:
        if str(make_model_class).isdigit():
            qs = qs.filter(make_model_class_id=make_model_class)
        elif make_model_class == "NA":
            qs = qs.filter(make_model_class__name__iexact="NA")

    if sub_product:
        qs = qs.filter(sub_product_id=sub_product)

    if fuel:
        qs = qs.filter(fuel_type_id=fuel)

    matching_rto_names = []
    if rto_code:
        potential_rtos = RTOMaster.objects.filter(rto_cluster__icontains=rto_code)
        for rto_record in potential_rtos:
            if strict_match_in_cluster(rto_code, rto_record.rto_cluster):
                matching_rto_names.append(rto_record.rto_name.strip().upper())

        if matching_rto_names:
            q_rto = Q()
            for rto_name in matching_rto_names:
                q_rto |= Q(new_rto_list__icontains=rto_name)
            qs = qs.filter(q_rto)
        else:
            qs = qs.none()

    matching_make_groups = []
    if make_names:
        potential_makes = MakeModelMaster.objects.filter(make_model_cluster__icontains=make_names)
        for make_record in potential_makes:
            if strict_match_in_cluster(make_names, make_record.make_model_cluster):
                matching_make_groups.append(make_record.make_model_name.strip().upper())

        if matching_make_groups:
            q_make = Q()
            for group_name in matching_make_groups:
                if group_name:
                    q_make |= Q(new_vehicle_makes__icontains=group_name.strip())
            qs = qs.filter(q_make)
        else:
            qs = qs.none()

    if cc:
        try:
            cc_val = float(cc)
            qs = qs.filter(
                (Q(cc_min__lte=cc_val) | Q(cc_min__isnull=True)) &
                (Q(cc_max__gte=cc_val) | Q(cc_max__isnull=True))
            )
        except ValueError:
            pass

    if sc:
        try:
            sc_val = float(sc)
            qs = qs.filter(
                (Q(sc_min__lte=sc_val) | Q(sc_min__isnull=True)) &
                (Q(sc_max__gte=sc_val) | Q(sc_max__isnull=True))
            )
        except ValueError:
            pass

    if mfg_year and mfg_year.isdigit() and target_date:
        try:
            target_dt = datetime.strptime(target_date, "%Y-%m-%d")
            mfg_dt = datetime(int(mfg_year), 1, 1)
            exact_age = round((target_dt - mfg_dt).days / 365.25, 2)
            qs = qs.filter(
                (Q(vehicle_age_min__lte=exact_age) | Q(vehicle_age_min__isnull=True)) &
                (Q(vehicle_age_max__gte=exact_age) | Q(vehicle_age_max__isnull=True))
            )
        except ValueError:
            pass

    if is_zd:
        qs = qs.filter(Q(is_zd__code__iexact=is_zd) | Q(is_zd__code__iexact="NA"))

    if is_cpa:
        qs = qs.filter(Q(is_cpa__code__iexact=is_cpa) | Q(is_cpa__code__iexact="NA"))

    if is_ncb:
        qs = qs.filter(Q(is_ncb__code__iexact=is_ncb) | Q(is_ncb__code__iexact="NA"))

    qs = qs.order_by(
        F("po_net_rate").desc(nulls_last=True),
        F("po_od_rate").desc(nulls_last=True),
        F("po_flat_amount").desc(nulls_last=True),
        "-id"
    )

    results = []
    seen_groups = set()

    if has_searched:
        for row in qs.iterator(chunk_size=2000):
            if rto_code and matching_rto_names:
                if not row.new_rto_list:
                    continue
                row_rtos = [x.strip().upper() for x in row.new_rto_list.split(",")]
                if not any(x in matching_rto_names for x in row_rtos):
                    continue

            if make_names and matching_make_groups:
                if not row.new_vehicle_makes:
                    continue
                row_makes = [x.strip().upper() for x in row.new_vehicle_makes.split(",")]
                if not any(x in matching_make_groups for x in row_makes):
                    continue

            gid = row.group_id if row.group_id is not None else row.id
            if gid not in seen_groups:
                row.display_group_id = gid

                if row.po_net_rate and row.po_net_rate > 0:
                    row.po_rate = row.po_net_rate
                elif row.po_od_rate and row.po_od_rate > 0:
                    row.po_rate = row.po_od_rate
                elif row.po_tp_rate and row.po_tp_rate > 0:
                    row.po_rate = row.po_tp_rate
                else:
                    row.po_rate = 0.0

                existing_lock = None
                if vehicle_no and policy_holder_name:
                    existing_lock = LockedPolicy.objects.filter(
                        source_rate=row,
                        vehicle_no__iexact=vehicle_no,
                        policy_holder_name__iexact=policy_holder_name
                    ).order_by("-id").first()

                row.lock_status = existing_lock.status if existing_lock else "UNLOCKED"

                results.append(row)
                seen_groups.add(gid)

            if len(results) >= 300:
                break

    make_name_list = sorted({
        item.strip()
        for value in MakeModelMaster.objects.exclude(make_model_cluster__isnull=True)
        .exclude(make_model_cluster="")
        .values_list("make_model_cluster", flat=True)
        for item in str(value).split(",")
        if item.strip()
    })

    return render(request, "policy_lock_checker.html", {
        "data": results,
        "total_found": len(results),
        "has_searched": has_searched,
        "product_list": ProductMaster.objects.all().order_by("name"),
        "sub_product_list": SubProductMaster.objects.all().order_by("name"),
        "fuel_list": FuelTypeMaster.objects.all().order_by("name"),
        "make_model_class_list": MakeModelClassMaster.objects.all().order_by("name"),
        "make_name_list": make_name_list,
        "selected": {
            "target_date": target_date,
            "vehicle_no": vehicle_no,
            "policy_holder_name": policy_holder_name,
            "product": product,
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


# -------------------------
# LOCK POLICY (AJAX UPDATED)
# -------------------------
@login_required
@user_passes_test(can_view_motor_payout)
def lock_unlock_policy(request, rate_id):
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "Invalid request."})

    action_type = (request.POST.get("action_type") or "").strip().upper()
    vehicle_no = (request.POST.get("vehicle_no") or "").strip()
    policy_holder_name = (request.POST.get("policy_holder_name") or "").strip()
    target_date = (request.POST.get("target_date") or "").strip()

    if not vehicle_no or not policy_holder_name:
        return JsonResponse({"success": False, "message": "Vehicle No. and Policy Holder Name are required."})

    # Only LOCK is allowed from Policy Lock Checker
    if action_type != "LOCK":
        return JsonResponse({"success": False, "message": "Invalid action."})
        
    # --- DATE VALIDATION SECURITY CHECK ---
    today_str = datetime.today().strftime("%Y-%m-%d")
    if target_date != today_str:
        return JsonResponse({
            "success": False, 
            "message": "Policies can only be locked for today's date. Past or future dates are restricted."
        })
    # --------------------------------------

    rate_obj = get_object_or_404(
        RateMaster.objects.select_related("product", "sub_product", "fuel_type"),
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

    obj.product_name = rate_obj.product.name if rate_obj.product else ""
    obj.sub_product_name = rate_obj.sub_product.name if rate_obj.sub_product else ""
    obj.insurance_company = rate_obj.insurance_company
    obj.po_type = rate_obj.po_type
    obj.po_rate = po_rate
    obj.po_flat_amount = rate_obj.po_flat_amount
    obj.add_tnc = rate_obj.add_tnc
    obj.rto_code = request.POST.get("rto_code", "")
    obj.make_name = request.POST.get("make_names", "")
    obj.fuel = request.POST.get("fuel", ""),
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
@login_required
@user_passes_test(can_view_motor_payout)
def locked_policy_dashboard(request):
    qs = LockedPolicy.objects.select_related("source_rate", "locked_by").all()

    # 1. GET FILTER PARAMETERS
    vehicle_no = (request.GET.get("vehicle_no") or "").strip()
    policy_holder_name = (request.GET.get("policy_holder_name") or "").strip()
    insurance_company = (request.GET.get("insurance_company") or "").strip()
    locked_by_user = (request.GET.get("locked_by_user") or "").strip()

    # 2. APPLY FILTERS
    if vehicle_no:
        qs = qs.filter(vehicle_no__iexact=vehicle_no)
    if policy_holder_name:
        qs = qs.filter(policy_holder_name__iexact=policy_holder_name)
    if insurance_company:
        qs = qs.filter(insurance_company__iexact=insurance_company)
    if locked_by_user:
        qs = qs.filter(locked_by__username__iexact=locked_by_user)

    # 3. GET UNIQUE VALUES FOR DROPDOWNS (From all records)
    all_locked = LockedPolicy.objects.select_related("locked_by").all()
    
    unique_vehicles = sorted(list(set(all_locked.exclude(vehicle_no__isnull=True).exclude(vehicle_no="").values_list("vehicle_no", flat=True))))
    unique_holders = sorted(list(set(all_locked.exclude(policy_holder_name__isnull=True).exclude(policy_holder_name="").values_list("policy_holder_name", flat=True))))
    unique_companies = sorted(list(set(all_locked.exclude(insurance_company__isnull=True).exclude(insurance_company="").values_list("insurance_company", flat=True))))
    unique_users = sorted(list(set(all_locked.exclude(locked_by__isnull=True).values_list("locked_by__username", flat=True))))

    return render(request, "locked_policy_dashboard.html", {
        "records": qs.order_by("-created_at")[:300],
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

# -------------------------
# DIRECT PASSWORD RESET
# -------------------------
def direct_password_reset(request):
    if request.method == "POST":
        email = request.POST.get("email")
        user = User.objects.filter(email=email).first()

        if user:
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            return redirect("password_reset_confirm", uidb64=uid, token=token)
        else:
            return render(request, "password_reset.html", {
                "error": "We could not find an account with that email address."
            })

    return render(request, "password_reset.html")


# -------------------------
# EXECUTIVE ANALYSIS DASHBOARD
# -------------------------
@login_required
@user_passes_test(can_view_analysis)
def business_analysis(request):
    qs = RateMaster.objects.filter(
        is_deleted="NO",
        status__in=["ACTIVE", "INACTIVE"]
    )

    insurer_perf = qs.values("insurance_company").annotate(
        avg_payout=Avg("po_net_rate")
    ).order_by("-avg_payout")[:10]

    product_mix = qs.values("product__name").annotate(
        count=Count("id")
    ).order_by("-count")

    rto_data = defaultdict(lambda: {"total_payout": 0, "count": 0})
    for row in qs.values("new_rto_list", "po_net_rate", "po_od_rate", "po_tp_rate").iterator(chunk_size=5000):
        po_rate = max(row["po_net_rate"] or 0, row["po_od_rate"] or 0, row["po_tp_rate"] or 0)
        if po_rate > 0 and row["new_rto_list"]:
            rtos = [rto.strip().upper() for rto in row["new_rto_list"].split(",") if rto.strip()]
            for rto in rtos:
                rto_data[rto]["total_payout"] += po_rate
                rto_data[rto]["count"] += 1

    top_rtos = []
    for rto, stats in rto_data.items():
        top_rtos.append({
            "name": rto,
            "avg_payout": stats["total_payout"] / stats["count"] if stats["count"] > 0 else 0,
            "volume": stats["count"],
        })
    top_rtos = sorted(top_rtos, key=lambda x: x["avg_payout"], reverse=True)[:10]

    return render(request, "analysis.html", {
        "insurer_performance": insurer_perf,
        "product_mix": product_mix,
        "top_rtos": top_rtos,
    })


# -------------------------
# AUDIT TRAIL LOGS
# -------------------------
@login_required
@user_passes_test(can_view_audit_log)
def audit_logs(request):
    logs = AuditLog.objects.all().order_by("-timestamp")[:200]
    return render(request, "audit_log.html", {"logs": logs})


# -------------------------
# EMAIL BACKGROUND
# -------------------------
def send_email_background(subject, message, recipient_list):
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.EMAIL_HOST_USER,
            recipient_list=recipient_list,
            fail_silently=False,
        )
        print("✅ Background email sent successfully!")
    except Exception as e:
        print(f"\n❌ BACKGROUND EMAIL FAILED: {e}\n")


# -------------------------
# GRID MANAGEMENT
# -------------------------
@login_required
@user_passes_test(can_view_grid_management)
def grid_management(request):
    if request.method == "POST":
        action = request.POST.get("action")

        # 1. HANDLE STATUS UPDATE FROM DROPDOWN
        if action == "update_status":
            doc_id = request.POST.get("doc_id")
            new_status = request.POST.get("status")
            if doc_id and new_status:
                doc = get_object_or_404(GridDocument, id=doc_id)
                doc.status = new_status
                doc.save()
            return redirect("grid_management")

        # 2. HANDLE NEW FILE UPLOAD
        insurer_name = request.POST.get("insurer_name")
        remarks = request.POST.get("remarks")
        uploaded_file = request.FILES.get("uploaded_file")

        if insurer_name and uploaded_file:
            GridDocument.objects.create(
                insurer_name=insurer_name,
                remarks=remarks,
                uploaded_file=uploaded_file,
                uploaded_by=request.user,
                status="PENDING"
            )

            uploader_name = request.user.username
            subject = f"🔔 New Grid Uploaded: {insurer_name}"
            message = (
                f"Hello,\n\n"
                f"A new provider grid has just been uploaded to the Insurance Portal.\n\n"
                f"• Insurer: {insurer_name}\n"
                f"• Uploaded By: {uploader_name}\n"
                f"• Remarks: {remarks or 'No remarks provided.'}\n\n"
                f"Please log in to the portal to download it."
            )

            email_thread = threading.Thread(
                target=send_email_background,
                args=(subject, message, ["harsh.t@arhamsecure.com"])
            )
            email_thread.start()

            return redirect("grid_management")

    documents = GridDocument.objects.all().order_by("-uploaded_date")
    return render(request, "grid_management.html", {"documents": documents})


# -------------------------
# MOTOR POINTS AUDIT LOGS (UPDATED PARSING)
# -------------------------
@login_required
@user_passes_test(can_view_motor_payout)
def motor_points_audit_logs(request):
    qs = AuditLog.objects.filter(action="MOTOR_POINTS_SEARCH").select_related("user").order_by("-timestamp")
    
    # 1. GET FILTER PARAMETERS
    vehicle_no_filter = (request.GET.get("vehicle_no") or "").strip()
    policy_holder_name_filter = (request.GET.get("policy_holder_name") or "").strip()
    insurance_company_filter = (request.GET.get("insurance_company") or "").strip()
    username_filter = (request.GET.get("username") or "").strip()

    # 2. APPLY FILTERS
    if vehicle_no_filter:
        qs = qs.filter(details__icontains=vehicle_no_filter)
    if policy_holder_name_filter:
        qs = qs.filter(details__icontains=policy_holder_name_filter)
    if insurance_company_filter:
        qs = qs.filter(details__icontains=insurance_company_filter)
    if username_filter:
        qs = qs.filter(user__username__icontains=username_filter)

    logs = qs[:500]
    
    # 3. GET UNIQUE VALUES FOR DROPDOWNS (From top 1000 logs)
    all_logs_for_dropdowns = AuditLog.objects.filter(action="MOTOR_POINTS_SEARCH").select_related("user").order_by("-timestamp")[:1000]
    unique_vehicles = set()
    unique_holders = set()
    unique_companies = set()
    unique_users = set()

    for log in all_logs_for_dropdowns:
        if log.user and log.user.username:
            unique_users.add(log.user.username)
        
        try:
            clean_str = log.details.replace("Eligibility Check Parameters: ", "")
            params_dict = ast.literal_eval(clean_str)
            
            # Flatten lists if they exist
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
        except:
            pass

    # 4. PARSE THE STRINGIFIED DICTIONARY FOR DISPLAY
    for log in logs:
        try:
            # Handle the old 'Eligibility Check Parameters: { ... }' format
            clean_str = log.details.replace("Eligibility Check Parameters: ", "")
            params_dict = ast.literal_eval(clean_str)
            
            # Flatten lists if they exist (from old format)
            flat_params = {}
            if isinstance(params_dict, dict):
                for k, v in params_dict.items():
                    if isinstance(v, list) and len(v) > 0:
                        flat_params[k] = v[0]
                    else:
                        flat_params[k] = v
            
            # SWAP IDs FOR REAL NAMES
            if flat_params.get("product") and str(flat_params["product"]).isdigit():
                try: flat_params["product"] = ProductMaster.objects.get(id=int(flat_params["product"])).name
                except: pass
                
            if flat_params.get("sub_product") and str(flat_params["sub_product"]).isdigit():
                try: flat_params["sub_product"] = SubProductMaster.objects.get(id=int(flat_params["sub_product"])).name
                except: pass
                
            if flat_params.get("make_model_class") and str(flat_params["make_model_class"]).isdigit():
                try: flat_params["make_model_class"] = MakeModelClassMaster.objects.get(id=int(flat_params["make_model_class"])).name
                except: pass
                
            if flat_params.get("fuel") and str(flat_params["fuel"]).isdigit():
                try: flat_params["fuel"] = FuelTypeMaster.objects.get(id=int(flat_params["fuel"])).name
                except: pass
                
            log.params = flat_params
        except Exception:
            log.params = {} # Fallback if parsing fails

    return render(request, "motor_points_audit_logs.html", {
        "logs": logs,
        "unique_vehicles": sorted(list(unique_vehicles)),
        "unique_holders": sorted(list(unique_holders)),
        "unique_companies": sorted(list(unique_companies)),
        "unique_users": sorted(list(unique_users)),
        "selected": {
            "vehicle_no": vehicle_no_filter,
            "policy_holder_name": policy_holder_name_filter,
            "insurance_company": insurance_company_filter,
            "username": username_filter,
        }
    })


# =========================================================
# REST API ENDPOINTS
# =========================================================
class ExportRatesAPIView(generics.ListAPIView):
    """
    This endpoint returns all active rate data in JSON format.
    Requires a valid API Key to access.
    """
    permission_classes = [HasAPIKey] 
    queryset = RateMaster.objects.filter(is_deleted="NO") 
    serializer_class = RateMasterSerializer