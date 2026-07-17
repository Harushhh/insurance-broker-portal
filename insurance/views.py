from django.contrib import messages
from django.contrib.auth.models import User, Group
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.utils import timezone
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.db.models import Q, F, Count, Avg, Sum, Case, When, Value, CharField
from django.core.mail import send_mail
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction
from django import forms
import csv
import json
import logging
import re
import hashlib
import threading
import ast
import mimetypes

from datetime import datetime
from decimal import Decimal, InvalidOperation
from collections import defaultdict
from openpyxl import Workbook

# --- DRF & SWAGGER IMPORTS ---
from rest_framework import generics
from rest_framework_api_key.permissions import HasAPIKey
from .serializers import RateMasterSerializer
# -----------------------------

from .models import (
    RTOMaster, MakeModelMaster, RateMaster, YesNoNAMaster,
    ProductMaster, SubProductMaster, PolicyTypeMaster,
    FuelTypeMaster, MakeModelClassMaster,
    RateGroup, AuditLog, GridDocument, UserProfile,
    ExtractionField, FieldSynonym, PolicyDocumentUpload, PolicyMISRecord,
    LockedPolicy, SupportTicket, MISFile, MappingConfiguration
)

# Import our Gemini AI utility and background logic engines
from .utils import extract_data_with_gemini
from .forms import ExtractionFieldForm, MISUploadForm, MappingConfigurationForm
from .mapping_engine import process_mis_mapping

logger = logging.getLogger("security")

# =========================================================
# PAGE-LEVEL ACCESS GROUPS (Retained for legacy UI assignments if needed)
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
    return True # Open access, login completely removed

def can_view_dashboard(user):
    return True # Open access, login completely removed

def can_view_motor_payout(user):
    return True # Open access, login completely removed

def can_upload(user):
    return True # Open access, login completely removed

def can_view_rto(user):
    return True # Open access, login completely removed

def can_view_make_model(user):
    return True # Open access, login completely removed

def can_view_audit_log(user):
    return True # Open access, login completely removed

def can_view_grid_management(user):
    return True # Open access, login completely removed

def can_view_alias_management(user):
    return True # Open access, login completely removed

# =========================================================
# NA CONFIGURATION & HELPERS
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

# Bulletproof case-insensitive dictionary matcher
def get_translated_make_model(product_name):
    if not product_name:
        return None
    # Standardize map keys to uppercase and strip spaces
    normalized_map = {str(k).strip().upper(): v for k, v in NA_MAKE_MODEL_MAP.items()}
    # Standardize the incoming database string
    clean_product = str(product_name).strip().upper()
    
    if clean_product in normalized_map:
        return ", ".join(normalized_map[clean_product])
    return None

def get_dynamic_make_model_class_list(product_id):
    """
    Returns the appropriate make/model classes for the selected product.
    Instead of just renaming 'NA', it explicitly filters the list to show valid individual classes.
    """
    all_classes = list(MakeModelClassMaster.objects.all().order_by("name"))
    
    if not product_id or not str(product_id).isdigit():
        return all_classes

    prod_obj = ProductMaster.objects.filter(id=int(product_id)).first()
    if not prod_obj:
        return all_classes

    product_name = prod_obj.name.strip()

    # If the product exists in our map, filter the dropdown to only show those classes + NA
    if product_name in NA_MAKE_MODEL_MAP:
        valid_names = [c.lower() for c in NA_MAKE_MODEL_MAP[product_name]]
        valid_names.append("na")  # Always include NA as a fallback

        filtered_classes = []
        for m in all_classes:
            if m.name.strip().lower() in valid_names:
                filtered_classes.append(m)
        return filtered_classes

    return all_classes

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
    def _get_or_create_code(code_str):
        obj, _ = YesNoNAMaster.objects.get_or_create(code=code_str)
        return obj

    if not value:
        return _get_or_create_code("NA")
    
    v_clean = str(value).strip().lower()
    if v_clean in ["yes", "y", "true", "1"]:
        return _get_or_create_code("YES")
    elif v_clean in ["no", "n", "false", "0"]:
        return _get_or_create_code("NO")
    else:
        return _get_or_create_code("NA")

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
    """
    Unified filter for Make Model Classes applied across all rate views.
    Handles fallbacks correctly so that 'NA' databases hits are included when a specific sub-class is searched.
    """
    if not make_model_class:
        return qs

    clean_mmc = str(make_model_class).strip()
    
    if clean_mmc.isdigit():
        # If passed an ID (e.g. for "Scooter"), fetch exact ID OR fallback to 'NA' if mapped
        mmc_obj = MakeModelClassMaster.objects.filter(id=int(clean_mmc)).first()
        if mmc_obj:
            mmc_name = mmc_obj.name.strip().upper()
            if mmc_name == "NA":
                qs = qs.filter(make_model_class__name__iexact="NA")
            else:
                is_na_equivalent = any(mmc_name.lower() in [c.lower() for c in mapped_classes] for mapped_classes in NA_MAKE_MODEL_MAP.values())
                if is_na_equivalent:
                    qs = qs.filter(Q(make_model_class_id=clean_mmc) | Q(make_model_class__name__iexact="NA"))
                else:
                    qs = qs.filter(make_model_class_id=clean_mmc)
        else:
            qs = qs.filter(make_model_class_id=clean_mmc)

    elif clean_mmc.upper() == "NA":
        qs = qs.filter(make_model_class__name__iexact="NA")
    else:
        is_na_equivalent = False
        for mapped_classes in NA_MAKE_MODEL_MAP.values():
            if clean_mmc.lower() in [c.lower() for c in mapped_classes]:
                is_na_equivalent = True
                break
        
        if is_na_equivalent:
            qs = qs.filter(Q(make_model_class__name__iexact=clean_mmc) | Q(make_model_class__name__iexact="NA"))
        else:
            qs = qs.filter(make_model_class__name__iexact=clean_mmc)
            
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
# AI EXTRACTION HELPERS
# =========================================================
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

def process_policy_document(document_obj):
    try:
        document_obj.status = PolicyDocumentUpload.STATUS_PROCESSING
        document_obj.error_message = ""
        document_obj.save(update_fields=["status", "error_message"])

        file_path = document_obj.uploaded_file.path
        
        mapped_data = extract_data_with_gemini(file_path)

        document_obj.extracted_text = "Extracted directly via Gemini Multimodal API."
        document_obj.extraction_method = "Gemini 2.5 Flash"
        document_obj.parsed_json = mapped_data
        document_obj.status = PolicyDocumentUpload.STATUS_COMPLETED
        document_obj.processed_at = timezone.now()
        document_obj.save()

        PolicyMISRecord.objects.update_or_create(
            source_document=document_obj,
            defaults={
                "insurer_name": mapped_data.get("insurance_company") or mapped_data.get("insurer_name"),
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
                "confidence_notes": "Extracted via Gemini 2.5 Flash with Synonyms injection.",
                "ai_model_name": "gemini-2.5-flash",
            }
        )

    except Exception as e:
        print(f"\n❌ AI EXTRACTION ERROR: {str(e)}\n")
        document_obj.status = PolicyDocumentUpload.STATUS_FAILED
        document_obj.error_message = str(e)
        document_obj.processed_at = timezone.now()
        document_obj.save(update_fields=["status", "error_message", "processed_at"])

# =========================================================
# FORMS
# =========================================================
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

# =========================================================
# UNIFIED HOME DASHBOARD
# =========================================================
def home_dashboard(request):
    is_admin_user = True
    qs = PolicyMISRecord.objects.select_related("source_document", "source_document__uploaded_by")

    total_cases = qs.count()
    total_premium = qs.aggregate(total=Sum('gross_premium'))['total'] or 0

    product_mix_query = qs.annotate(
        category=Case(
            When(
                Q(vehicle_registration_number__isnull=False) & ~Q(vehicle_registration_number=""), 
                then=Value('Motor')
            ),
            When(
                Q(vehicle_make__isnull=False) & ~Q(vehicle_make=""), 
                then=Value('Motor')
            ),
            default=Value('Health'),
            output_field=CharField(),
        )
    ).values('category').annotate(count=Count('id'))

    product_mix = {item['category']: item['count'] for item in product_mix_query}
    motor_count = product_mix.get('Motor', 0)
    health_count = product_mix.get('Health', 0)

    recent_activity = qs.order_by('-created_at')[:5]

    context = {
        'is_admin_user': is_admin_user,
        'total_cases': total_cases,
        'total_premium': total_premium,
        'motor_count': motor_count,
        'health_count': health_count,
        'recent_activity': recent_activity,
    }
    return render(request, 'insurance/home.html', context)

# ---------------------------------------------------------
# NEW ENTERPRISE STREAMING CSV UPLOAD & API CHUNKING LOGIC
# ---------------------------------------------------------

def import_data_view(request):
    """
    Renders the beautiful SaaS upload interface. 
    The heavy lifting is handled via JS (PapaParse) to bypass Server RAM and timeouts.
    """
    return render(request, "upload.html")


@csrf_exempt 
def api_upload_chunk(request):
    """
    Receives JSON chunks from the PapaParse frontend uploader.
    Uses bulk_create to efficiently insert data while mapping your specific schemas.
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            rows = data.get('rows', [])
            target_table = data.get('target_table', 'rate_master')
            
            if not rows:
                return JsonResponse({'status': 'error', 'message': 'No data provided'}, status=400)

            inserted = 0

            with transaction.atomic():
                if target_table == 'rto_master':
                    for row in rows:
                        rto_name = (row.get("rto_name") or "").strip()
                        rto_cluster = (row.get("rto_cluster") or "").strip()
                        if not rto_name:
                            raise ValueError("rto_name is blank")
                        RTOMaster.objects.update_or_create(
                            rto_name=rto_name,
                            defaults={"rto_cluster": rto_cluster or None}
                        )
                        inserted += 1

                elif target_table == 'make_model_master':
                    for row in rows:
                        make_model_name = (row.get("make_model_name") or "").strip()
                        make_model_cluster = (row.get("make_model_cluster") or "").strip()
                        if not make_model_name:
                            raise ValueError("make_model_name is blank")
                        MakeModelMaster.objects.update_or_create(
                            make_model_name=make_model_name,
                            defaults={"make_model_cluster": make_model_cluster or None}
                        )
                        inserted += 1

                elif target_table == 'rate_master':
                    valid_rtos = {str(rto).lower() for rto in RTOMaster.objects.values_list("rto_name", flat=True) if rto}
                    valid_makes = {str(make).lower() for make in MakeModelMaster.objects.values_list("make_model_name", flat=True) if make}

                    products_map = {p.name.lower(): p for p in ProductMaster.objects.all()}
                    sub_products_map = {sp.name.lower(): sp for sp in SubProductMaster.objects.all()}
                    policy_types_map = {pt.name.lower(): pt for pt in PolicyTypeMaster.objects.all()}
                    fuel_types_map = {ft.name.lower(): ft for ft in FuelTypeMaster.objects.all()}
                    mmc_classes_map = {m.name.lower(): m for m in MakeModelClassMaster.objects.all()}
                    
                    ynn_map = {y.code.lower(): y for y in YesNoNAMaster.objects.all()}
                    for code in ["YES", "NO", "NA"]:
                        if code.lower() not in ynn_map:
                            ynn_map[code.lower()] = YesNoNAMaster.objects.create(code=code)

                    existing_groups = {g.key_hash: g for g in RateGroup.objects.all()}

                    def get_ynn(val):
                        if not val: return ynn_map["na"]
                        v = str(val).strip().lower()
                        if v in ["yes", "y", "true", "1"]: return ynn_map["yes"]
                        if v in ["no", "n", "false", "0"]: return ynn_map["no"]
                        return ynn_map["na"]

                    def get_master(val, mapping_dict, ModelClass):
                        if not val: return None
                        v = str(val).strip()
                        if not v: return None
                        if v.isdigit() and ModelClass != ProductMaster:
                            obj = ModelClass.objects.filter(id=int(v)).first()
                            if obj:
                                mapping_dict[obj.name.lower()] = obj
                                return obj
                        v_low = v.lower()
                        if v_low in mapping_dict:
                            return mapping_dict[v_low]
                        obj = ModelClass.objects.create(name=v)
                        mapping_dict[v_low] = obj
                        return obj

                    instances_to_create = []

                    for row in rows:
                        raw_rtos = row.get("new_rto_list") or ""
                        rto_items = [x.strip() for x in raw_rtos.split(",") if x.strip()]
                        for rto in rto_items:
                            if rto.lower() not in valid_rtos:
                                raise ValueError(f"RTO '{rto}' does not exist in RTOMaster. Please add it first.")

                        raw_makes = row.get("new_vehicle_makes") or ""
                        make_items = [x.strip() for x in raw_makes.split(",") if x.strip()]
                        for make in make_items:
                            if make.lower() not in valid_makes:
                                raise ValueError(f"Vehicle Make '{make}' does not exist in MakeModelMaster. Please add it first.")

                        product_val = str(row.get("product", "")).strip()
                        product_obj = None
                        if product_val:
                            if product_val.isdigit():
                                product_obj = ProductMaster.objects.filter(id=int(product_val)).first()
                            else:
                                if product_val.lower() in products_map:
                                    product_obj = products_map[product_val.lower()]
                                else:
                                    product_obj = ProductMaster.objects.create(name=product_val)
                                    products_map[product_val.lower()] = product_obj

                        sub_product_obj = get_master(row.get("sub_product"), sub_products_map, SubProductMaster)
                        policy_type_obj = get_master(row.get("policy_type"), policy_types_map, PolicyTypeMaster)
                        fuel_type_obj = get_master(row.get("fuel_type"), fuel_types_map, FuelTypeMaster)
                        mmc_obj = get_master(row.get("make_model_class"), mmc_classes_map, MakeModelClassMaster)
                        
                        is_ncb_obj = get_ynn(row.get("is_ncb"))
                        is_cpa_obj = get_ynn(row.get("is_cpa"))
                        is_zd_obj = get_ynn(row.get("is_zd"))

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
                            "is_zd": is_zd_obj,
                            "cc_min": float(row.get("cc_min") or 0),
                            "cc_max": float(row.get("cc_max") or 0),
                            "from_date": parse_date(row.get("from_date")),
                            "to_date": parse_date(row.get("to_date")),
                            "sc_min": float(row.get("sc_min") or 0),
                            "sc_max": float(row.get("sc_max") or 0),
                            "user_id": int(float(row.get("user_id") or 0)) if row.get("user_id") else None,
                            "veh_use": row.get("veh_use") or None,
                            "add_tnc": row.get("add_tnc") or None,
                            "remarks": row.get("remarks") or None,
                            "po_type": row.get("po_type") or None,
                            "po_od_rate": float(row.get("po_od_rate") or 0),
                            "po_tp_rate": float(row.get("po_tp_rate") or 0),
                            "po_net_rate": float(row.get("po_net_rate") or 0),
                            "po_flat_amount": float(row.get("po_flat_amount") or 0),
                        }

                        key_hash, key_text = build_key_hash(cleaned)
                        if key_hash in existing_groups:
                            group_obj = existing_groups[key_hash]
                        else:
                            group_obj = RateGroup.objects.create(key_hash=key_hash, key_text=key_text)
                            existing_groups[key_hash] = group_obj

                        instances_to_create.append(
                            RateMaster(
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
                                user_id=cleaned["user_id"],
                                sc_min=cleaned["sc_min"],
                                sc_max=cleaned["sc_max"],
                                veh_use=cleaned["veh_use"],
                                add_tnc=cleaned["add_tnc"],
                                remarks=cleaned["remarks"],
                                po_type=cleaned["po_type"],
                                po_od_rate=cleaned["po_od_rate"],
                                po_tp_rate=cleaned["po_tp_rate"],
                                po_net_rate=cleaned["po_net_rate"],
                                po_flat_amount=cleaned["po_flat_amount"],
                            )
                        )

                    RateMaster.objects.bulk_create(instances_to_create, ignore_conflicts=True)
                    inserted = len(instances_to_create)

                else:
                    return JsonResponse({'status': 'error', 'message': 'Invalid table selected'}, status=400)

            return JsonResponse({'status': 'success', 'inserted': inserted})

        except Exception as e:
            print(f"\n❌ UPLOAD ERROR: {str(e)}\n") 
            return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

    return JsonResponse({'status': 'error', 'message': 'Method not allowed'}, status=405)

# -------------------------
# Dashboard (GROUPED view)
# -------------------------
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

        mmc_name = first.make_model_class.name.strip().upper() if first.make_model_class else "NA"
        
        if mmc_name in ["NA", ""]:
            prod_name = first.product.name if first.product else ""
            translated_name = get_translated_make_model(prod_name)
            if translated_name:
                first.make_model_class = MakeModelClassMaster(name=translated_name)
                first.display_make_model_class = translated_name
            else:
                first.display_make_model_class = "NA"
        else:
            first.display_make_model_class = first.make_model_class.name

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
        "make_model_class_list": get_dynamic_make_model_class_list(product),
        "yes_no_na_list": yes_no_na_list,
        
        "make_class_mapping_json": json.dumps(NA_MAKE_MODEL_MAP),
        "all_make_classes_json": json.dumps(list(MakeModelClassMaster.objects.exclude(name__iexact="NA").values('id', 'name'))),
        
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
# Export UNGROUPED to CSV (Perfect Match 1:1 Schema Update)
# -------------------------
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

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="rates_export.csv"'

    writer = csv.writer(response)
    
    # Header aligned perfectly with the keys processed during `api_upload_chunk`
    writer.writerow([
        "id", "group_id", "status", "is_deleted", "insurance_company", "insurer_vertical", 
        "product", "sub_product", "policy_type", "fuel_type", "make_model_class",
        "new_vehicle_makes", "new_rto_list", "vehicle_age_min", "vehicle_age_max", 
        "cc_min", "cc_max", "sc_min", "sc_max", "pi_od_rate", "pi_tp_rate", 
        "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5", "pi_net_rate", "pi_flat_amount", 
        "pi_vli", "pi_type", "tariff_min", "tariff_max", "is_ncb", "is_cpa", "is_zd", 
        "from_date", "to_date", "user_id", "veh_use", "add_tnc", "remarks", 
        "po_type", "po_od_rate", "po_tp_rate", "po_net_rate", "po_flat_amount"
    ])

    for r in qs.iterator(chunk_size=2000):
        writer.writerow([
            r.id,
            r.group_id,
            r.status,
            r.is_deleted,
            r.insurance_company,
            r.insurer_vertical,
            r.product.name if r.product else "",
            r.sub_product.name if r.sub_product else "",
            r.policy_type.name if r.policy_type else "",
            r.fuel_type.name if r.fuel_type else "",
            r.make_model_class.name if r.make_model_class else "",
            r.new_vehicle_makes,
            r.new_rto_list,
            r.vehicle_age_min,
            r.vehicle_age_max,
            r.cc_min,
            r.cc_max,
            r.sc_min,
            r.sc_max,
            r.pi_od_rate,
            r.pi_tp_rate,
            r.pi_tp_2,
            r.pi_tp_3,
            r.pi_tp_4,
            r.pi_tp_5,
            r.pi_net_rate,
            r.pi_flat_amount,
            r.pi_vli,
            r.pi_type,
            r.tariff_min,
            r.tariff_max,
            r.is_ncb.code if r.is_ncb else "",
            r.is_cpa.code if r.is_cpa else "",
            r.is_zd.code if r.is_zd else "",
            r.from_date,
            r.to_date,
            r.user_id,
            r.veh_use,
            r.add_tnc,
            r.remarks,
            r.po_type,
            r.po_od_rate,
            r.po_tp_rate,
            r.po_net_rate,
            r.po_flat_amount
        ])

    return response

# -------------------------
# Alias / Field Management Configurator
# -------------------------
def field_configurator(request):
    if request.method == 'POST':
        form = ExtractionFieldForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('field_configurator')
    else:
        form = ExtractionFieldForm()

    fields = ExtractionField.objects.prefetch_related('synonyms').order_by('category', 'order_index')

    return render(request, 'insurance/configurator.html', {
        'fields': fields,
        'form': form
    })

def delete_field(request, pk):
    if request.method == 'POST':
        field = get_object_or_404(ExtractionField, pk=pk)
        field.delete()
    return redirect('field_configurator')

# -------------------------
# Edit Field (Configurations & Synonyms)
# -------------------------
def edit_field(request, pk):
    field_obj = get_object_or_404(ExtractionField, pk=pk)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'update_field':
            form = ExtractionFieldForm(request.POST, instance=field_obj)
            if form.is_valid():
                form.save()
                return redirect('edit_field', pk=pk)
        
        elif action == 'add_synonym':
            synonym_text = request.POST.get('synonym_text', '').strip()
            if synonym_text:
                FieldSynonym.objects.get_or_create(extraction_field=field_obj, synonym_text=synonym_text)
            return redirect('edit_field', pk=pk)
            
        elif action == 'delete_synonym':
            syn_id = request.POST.get('synonym_id')
            FieldSynonym.objects.filter(id=syn_id).delete()
            return redirect('edit_field', pk=pk)
            
    else:
        form = ExtractionFieldForm(instance=field_obj)
        
    return render(request, 'insurance/edit_field.html', {
        'form': form,
        'field_obj': field_obj
    })

# -------------------------
# Upload & Extract PDF / Image
# -------------------------
def upload_extract_pdf(request):
    msg = ""
    error = ""
    upload_form = PolicyDocumentUploadForm()

    if request.method == "POST":
        upload_form = PolicyDocumentUploadForm(request.POST, request.FILES)
        if upload_form.is_valid():
            uploaded_file = request.FILES['uploaded_file']
            
            if PolicyDocumentUpload.objects.filter(original_filename=uploaded_file.name).exists():
                error = f"⚠️ Duplicate File: A document named '{uploaded_file.name}' has already been processed."
            else:
                try:
                    doc_obj = upload_form.save(commit=False)
                    doc_obj.original_filename = doc_obj.uploaded_file.name
                    doc_obj.uploaded_by = User.objects.first() # Safe default fallback
                    doc_obj.mime_type = getattr(doc_obj.uploaded_file, "content_type", "") or ""
                    doc_obj.status = PolicyDocumentUpload.STATUS_PENDING
                    doc_obj.save()

                    process_policy_document(doc_obj)
                    return redirect("upload_extract_pdf")

                except Exception as e:
                    error = f"⚠️ {str(e)}"
        else:
            error = "⚠️ Please correct the upload form."

    documents = PolicyDocumentUpload.objects.order_by("-created_at")[:20]
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

    stats = qs.aggregate(
        total_processed=Count('id'),
        total_premium=Sum('gross_premium')
    )

    return render(request, "my_mis.html", {
        "records": qs[:200],
        "stats": stats,
        "selected": {
            "policy_number": policy_number,
            "insured_name": insured_name,
            "insurer_name": insurer_name,
        }
    })

# -------------------------
# User Management
# -------------------------
# Editable Teams-hierarchy fields on UserProfile (excludes user/contact_number/designation,
# which already have dedicated inputs in the edit modal).
HIERARCHY_FIELDS = [
    "vertical_path", "team", "team_id", "user_type", "emp_id", "code", "role",
    "branch_code", "branch_name",
    "rm_code", "rm_name", "tc_code", "tc_name", "csc_code", "csc_name",
    "posp_code", "posp_name",
    "agent_type",
    "pan", "bank_account", "bank_name", "ifsc",
    "membership_id", "user_id_code", "qc_verticals",
]
HIERARCHY_FLAGS = ["is_qc", "is_plvc", "personal_qc"]
HIERARCHY_FIELD_LABELS = [
    ("vertical_path", "Vertical Path"), ("team", "Team"), ("team_id", "Team ID"),
    ("user_type", "User Type"), ("emp_id", "Emp ID"), ("code", "Code"), ("role", "Role"),
    ("branch_code", "Branch Code"), ("branch_name", "Branch Name"),
    ("rm_code", "RM Code"), ("rm_name", "RM Name"),
    ("tc_code", "TC Code"), ("tc_name", "TC Name"),
    ("csc_code", "CSC Code"), ("csc_name", "CSC Name"),
    ("posp_code", "Ref/POSP Code"), ("posp_name", "Ref/POSP Name"),
    ("agent_type", "Agent Type"),
    ("pan", "PAN"), ("bank_account", "Bank Account"), ("bank_name", "Bank Name"), ("ifsc", "IFSC"),
    ("membership_id", "Membership ID"), ("user_id_code", "User ID"), ("qc_verticals", "QC Verticals"),
]


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
                if User.objects.filter(username=uname).exists():
                    error = f"⚠️ User '{uname}' already exists."
                elif uemail and User.objects.filter(email__iexact=uemail).exists():
                    error = f"⚠️ An account with email '{uemail}' already exists."
                else:
                    new_user = User.objects.create_user(username=uname, email=uemail, password=upass)
                    if fname:
                        parts = fname.split(" ", 1)
                        new_user.first_name = parts[0]
                        if len(parts) > 1:
                            new_user.last_name = parts[1]
                    new_user.save()

                    UserProfile.objects.get_or_create(user=new_user, defaults={"contact_number": ucontact})
                    msg = f"✅ User '{uname}' created successfully."

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

            for field in HIERARCHY_FIELDS:
                profile.__dict__[field] = request.POST.get(field, "").strip() or None
            for flag in HIERARCHY_FLAGS:
                setattr(profile, flag, request.POST.get(flag) == "on")

            reports_to_id = request.POST.get("reports_to") or None
            profile.reports_to_id = reports_to_id if reports_to_id else None

            new_email = request.POST.get("email", "").strip()
            if new_email and User.objects.filter(email__iexact=new_email).exclude(id=u.id).exists():
                error = f"⚠️ An account with email '{new_email}' already exists."
            else:
                try:
                    profile.full_clean(exclude=["user"])
                    profile.save()
                    u.email = new_email
                    u.save()
                    msg = f"✅ Profile and access for '{u.username}' updated."
                except forms.ValidationError as e:
                    error = "⚠️ " + " ".join(m for msgs in e.message_dict.values() for m in msgs)

        elif action == "approve_user" and user_id:
            u = User.objects.get(id=user_id)
            u.is_active = True
            u.save()
            msg = f"✅ '{u.username}' approved — they can now log in."

        elif action == "reject_user" and user_id:
            u = User.objects.get(id=user_id)
            uname = u.username
            u.delete()
            msg = f"🗑️ Signup request from '{uname}' rejected and removed."

        elif action == "reset_password" and user_id:
            u = User.objects.get(id=user_id)
            default_password = "Changeme@123"
            u.set_password(default_password)
            u.save()
            logger.info("Password reset by admin for user '%s'", u.username)
            msg = f"✅ Password for '{u.username}' reset to '{default_password}'. Share it with them directly."

        elif action == "make_admin" and user_id:
            u = User.objects.get(id=user_id)
            u.groups.clear()
            u.groups.add(Group.objects.get(name="ADMIN"))
            msg = f"✅ User '{u.username}' promoted to Full Admin."

        elif action == "delete_user" and user_id:
            u = User.objects.get(id=user_id)
            if u.is_superuser:
                error = "⚠️ You cannot delete a Super Admin account."
            else:
                uname = u.username
                u.delete()
                msg = f"🗑️ User '{uname}' has been completely removed from the system."

    all_users = User.objects.select_related("profile").all().order_by("-is_superuser", "username")
    users = [u for u in all_users if u.is_active]
    pending_users = [u for u in all_users if not u.is_active]

    def profile_dict(u):
        profile = getattr(u, "profile", None)
        d = {
            "id": u.id,
            "username": u.username,
            "full_name": u.get_full_name(),
            "email": u.email,
            "contact_number": profile.contact_number if profile else "",
            "designation": profile.designation if profile else "",
            "is_superuser": u.is_superuser,
            "is_admin": u.groups.filter(name="ADMIN").exists(),
            "pages": list(u.groups.values_list("name", flat=True)),
        }
        for field in HIERARCHY_FIELDS:
            d[field] = getattr(profile, field, "") or ""
        for flag in HIERARCHY_FLAGS:
            d[flag] = bool(getattr(profile, flag, False))
        d["reports_to"] = profile.reports_to_id if profile else None
        return d

    user_rows = [profile_dict(u) for u in users]
    pending_rows = [
        {
            "id": u.id,
            "username": u.username,
            "full_name": u.get_full_name(),
            "email": u.email,
            "contact_number": u.profile.contact_number if hasattr(u, "profile") else "",
            "designation": u.profile.designation if hasattr(u, "profile") else "",
            "date_joined": u.date_joined,
        }
        for u in pending_users
    ]

    manager_choices = [{"id": u.id, "username": u.username} for u in users]

    return render(request, "user_management.html", {
        "users": user_rows,
        "pending_users": pending_rows,
        "manager_choices": manager_choices,
        "page_groups": PAGE_GROUPS,
        "hierarchy_field_labels": HIERARCHY_FIELD_LABELS,
        "msg": msg,
        "error": error
    })

# -------------------------
# RTO DASHBOARD
# -------------------------
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
        "is_admin": True
    })

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
        "is_admin": True
    })

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
                user=User.objects.first(),
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
def bulk_update_rates(request):
    if request.method == "POST":
        # 1. Safely extract group IDs regardless of how JS structured the payload
        raw_groups = request.POST.getlist("selected_groups")
        if not raw_groups:
            raw_groups = request.POST.getlist("selected_groups[]")
        if not raw_groups:
            raw_groups = [request.POST.get("selected_groups", "")]

        group_ids = []
        for g in raw_groups:
            for part in str(g).split(","):
                part = part.strip()
                if part.isdigit():
                    group_ids.append(int(part))

        field_name = request.POST.get("update_field")
        new_value = request.POST.get("update_value", "").strip()

        if not group_ids or not field_name:
            return redirect("dashboard")

        # 2. Fetch all exact records mapped to the selected rows
        records = RateMaster.objects.filter(Q(group_id__in=group_ids) | Q(id__in=group_ids))
        record_count = records.count()

        # 3. Safely cast the incoming text value into the correct Python/Django Database Type
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

        # 4. Perform ultra-fast vectorized update to the DB
        records.update(**{field_name: parsed_value})

        # 5. Log the action
        AuditLog.objects.create(
            user=User.objects.first(),
            action="BULK UPDATE",
            details=f"Updated {record_count} rows. Changed '{field_name}' to '{new_value}'."
        )

    return redirect("dashboard")

# -------------------------
# MOTOR PAYOUT RATES
# -------------------------
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
        if str(product).isdigit():
            qs = qs.filter(product_id=product)
        else:
            qs = qs.filter(product__name__iexact=str(product).strip())

    qs = apply_make_model_filter(qs, product, make_model_class)

    if sub_product:
        if str(sub_product).isdigit():
            qs = qs.filter(sub_product_id=sub_product)
        else:
            qs = qs.filter(sub_product__name__iexact=str(sub_product).strip())

    if fuel:
        if str(fuel).isdigit():
            qs = qs.filter(fuel_type_id=fuel)
        else:
            qs = qs.filter(fuel_type__name__iexact=str(fuel).strip())

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
            qs = qs.filter(new_rto_list__icontains=rto_code.strip())

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
            qs = qs.filter(new_vehicle_makes__icontains=make_names.strip())

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

            mmc_name = row.make_model_class.name.strip().upper() if row.make_model_class else "NA"
            
            if mmc_name in ["NA", ""]:
                prod_name = row.product.name if row.product else ""
                translated_name = get_translated_make_model(prod_name)
                if translated_name:
                    row.make_model_class = MakeModelClassMaster(name=translated_name)
                    row.display_make_model_class = translated_name
                else:
                    row.display_make_model_class = "NA"
            else:
                row.display_make_model_class = row.make_model_class.name

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
        "make_model_class_list": get_dynamic_make_model_class_list(product),
        "all_makes_json": all_makes_json,
        "class_makes_mapping_json": class_makes_mapping_json,
        
        "make_class_mapping_json": json.dumps(NA_MAKE_MODEL_MAP),
        "all_make_classes_json": json.dumps(list(MakeModelClassMaster.objects.exclude(name__iexact="NA").values('id', 'name'))),
        
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
def policy_lock_checker(request):
    has_searched = bool(request.GET)

    if has_searched:
        flat_params = {}
        for key in request.GET.keys():
            val = request.GET.get(key, "").strip()
            if val and key != "csrfmiddlewaretoken":
                flat_params[key] = val
        
        if flat_params:
            AuditLog.objects.create(
                user=User.objects.first(),
                action="MOTOR_POINTS_SEARCH",
                details=str(flat_params)
            )

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

    is_zd = (request.GET.get("is_zd") or "").strip().upper()
    is_cpa = (request.GET.get("is_cpa") or "").strip().upper()
    is_ncb = (request.GET.get("is_ncb") or "").strip().upper()

    if target_date:
        qs = qs.filter(
            (Q(from_date__lte=target_date) | Q(from_date__isnull=True)) &
            (Q(to_date__gte=target_date) | Q(to_date__isnull=True))
        )

    if product:
        if str(product).isdigit():
            qs = qs.filter(product_id=product)
        else:
            qs = qs.filter(product__name__iexact=str(product).strip())

    qs = apply_make_model_filter(qs, product, make_model_class)

    if sub_product:
        if str(sub_product).isdigit():
            qs = qs.filter(sub_product_id=sub_product)
        else:
            qs = qs.filter(sub_product__name__iexact=str(sub_product).strip())

    if fuel:
        if str(fuel).isdigit():
            qs = qs.filter(fuel_type_id=fuel)
        else:
            qs = qs.filter(fuel_type__name__iexact=str(fuel).strip())

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
            qs = qs.filter(new_rto_list__icontains=rto_code.strip())

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
            qs = qs.filter(new_vehicle_makes__icontains=make_names.strip())

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

                mmc_name = row.make_model_class.name.strip().upper() if row.make_model_class else "NA"
                
                if mmc_name in ["NA", ""]:
                    prod_name = row.product.name if row.product else ""
                    translated_name = get_translated_make_model(prod_name)
                    if translated_name:
                        row.make_model_class = MakeModelClassMaster(name=translated_name)
                        row.display_make_model_class = translated_name
                    else:
                        row.display_make_model_class = "NA"
                else:
                    row.display_make_model_class = row.make_model_class.name

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
        "make_model_class_list": get_dynamic_make_model_class_list(product),
        "make_name_list": make_name_list,
        
        "make_class_mapping_json": json.dumps(NA_MAKE_MODEL_MAP),
        "all_make_classes_json": json.dumps(list(MakeModelClassMaster.objects.exclude(name__iexact="NA").values('id', 'name'))),
        
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
            "locked_by": User.objects.first(),
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
    obj.locked_by = User.objects.first()
    obj.locked_at = timezone.now()
    obj.save()

    return JsonResponse({"success": True, "message": f"Successfully locked policy for {vehicle_no}."})

# -------------------------
# LOCKED POLICY DASHBOARD
# -------------------------
def locked_policy_dashboard(request):
    qs = LockedPolicy.objects.select_related(
        "source_rate", 
        "source_rate__product", 
        "source_rate__make_model_class", 
        "locked_by"
    ).all()

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

    all_locked = LockedPolicy.objects.select_related("locked_by").all()
    
    unique_vehicles = sorted(list(set(all_locked.exclude(vehicle_no__isnull=True).exclude(vehicle_no="").values_list("vehicle_no", flat=True))))
    unique_holders = sorted(list(set(all_locked.exclude(policy_holder_name__isnull=True).exclude(policy_holder_name="").values_list("policy_holder_name", flat=True))))
    unique_companies = sorted(list(set(all_locked.exclude(insurance_company__isnull=True).exclude(insurance_company="").values_list("insurance_company", flat=True))))
    unique_users = sorted(list(set(all_locked.exclude(locked_by__isnull=True).values_list("locked_by__username", flat=True))))

    records = list(qs.order_by("-created_at")[:300])

    for row in records:
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

# -------------------------
# DIRECT PASSWORD RESET
# -------------------------
def direct_password_reset(request):
    if request.method == "POST":
        email = request.POST.get("email", "").strip()
        matches = User.objects.filter(email__iexact=email)
        user = matches.first()

        if user:
            if matches.count() > 1:
                # Email uniqueness is enforced at creation time (signup, admin
                # create/edit), so this shouldn't happen — but if stale/imported
                # data ever violates that, silently resetting the wrong account
                # is worse than a loud log. `.first()` still picks the
                # lowest-id account, same as before.
                logger.warning(
                    "Multiple accounts share email %r during password reset — resetting '%s' (id=%s)",
                    email, user.username, user.pk,
                )
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            logger.info("Direct password reset link issued for user '%s'", user.username)
            return redirect("password_reset_confirm", uidb64=uid, token=token)
        else:
            return render(request, "password_reset.html", {
                "error": "We could not find an account with that email address."
            })

    return render(request, "password_reset.html")

# -------------------------
# EXECUTIVE ANALYSIS DASHBOARD
# -------------------------
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
def grid_management(request):
    if request.method == "POST":
        action = request.POST.get("action")

        if action == "update_status":
            doc_id = request.POST.get("doc_id")
            new_status = request.POST.get("status")
            if doc_id and new_status:
                doc = get_object_or_404(GridDocument, id=doc_id)
                doc.status = new_status
                doc.save()
            return redirect("grid_management")

        insurer_name = request.POST.get("insurer_name")
        remarks = request.POST.get("remarks")
        uploaded_file = request.FILES.get("uploaded_file")

        if insurer_name and uploaded_file:
            GridDocument.objects.create(
                insurer_name=insurer_name,
                remarks=remarks,
                uploaded_file=uploaded_file,
                uploaded_by=User.objects.first(),
                status="PENDING"
            )

            uploader_name = "System User"
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
# MOTOR POINTS AUDIT LOGS
# -------------------------
def motor_points_audit_logs(request):
    qs = AuditLog.objects.filter(action="MOTOR_POINTS_SEARCH").select_related("user").order_by("-timestamp")
    
    vehicle_no_filter = (request.GET.get("vehicle_no") or "").strip()
    policy_holder_name_filter = (request.GET.get("policy_holder_name") or "").strip()
    insurance_company_filter = (request.GET.get("insurance_company") or "").strip()
    username_filter = (request.GET.get("username") or "").strip()

    if vehicle_no_filter:
        qs = qs.filter(details__icontains=vehicle_no_filter)
    if policy_holder_name_filter:
        qs = qs.filter(details__icontains=policy_holder_name_filter)
    if insurance_company_filter:
        qs = qs.filter(details__icontains=insurance_company_filter)
    if username_filter:
        qs = qs.filter(user__username__icontains=username_filter)

    logs = qs[:500]
    
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
            log.params = {}

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
    permission_classes = [HasAPIKey] 
    queryset = RateMaster.objects.filter(is_deleted="NO") 
    serializer_class = RateMasterSerializer

# -------------------------
# MIS REVIEW 
# -------------------------
def mis_review(request, pk):
    record = get_object_or_404(PolicyMISRecord.objects.select_related('source_document'), pk=pk)
    
    rules = ExtractionField.objects.filter(is_active=True).order_by('category', 'order_index')
    
    if request.method == 'POST':
        updated_json = record.raw_ai_json or {}
        
        for rule in rules:
            field_key = rule.field_name.strip().lower().replace(" ", "_")
            new_val = request.POST.get(field_key)
            if new_val is not None:
                updated_json[field_key] = new_val
                
        record.raw_ai_json = updated_json
        
        record.policy_number = updated_json.get("policy_number", record.policy_number)
        record.insurer_name = updated_json.get("insurance_company", record.insurer_name)
        record.insured_name = updated_json.get("insured_name", record.insured_name)
        record.save()
        
        return redirect('my_mis')

    field_data = []
    parsed = record.raw_ai_json or {}
    
    for rule in rules:
        field_key = rule.field_name.strip().lower().replace(" ", "_")
        val = parsed.get(field_key, "")
        
        is_missing_mandatory = rule.is_mandatory and not val
        
        dropdown_choices = []
        if getattr(rule, 'has_dropdown', False) and getattr(rule, 'dropdown_options', None):
            dropdown_choices = [x.strip() for x in rule.dropdown_options.split(',') if x.strip()]
            
        field_data.append({
            'rule': rule,
            'key': field_key,
            'value': val,
            'is_missing_mandatory': is_missing_mandatory,
            'dropdown_choices': dropdown_choices
        })
        
    return render(request, 'mis_review.html', {
        'record': record,
        'field_data': field_data
    })

# =========================================================
# TICKETING SYSTEM VIEWS
# =========================================================
def ticket_dashboard(request):
    tickets = SupportTicket.objects.select_related('user').all()
    return render(request, "ticket_dashboard.html", {
        "tickets": tickets
    })

@csrf_exempt 
def create_ticket_api(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            remarks = data.get("remarks", "").strip()
            form_payload = data.get("form_payload", {})

            if not remarks:
                return JsonResponse({"success": False, "message": "Remarks are required."})

            # 1. Translate Product ID to Name
            if form_payload.get("Product") and str(form_payload["Product"]).isdigit():
                obj = ProductMaster.objects.filter(id=int(form_payload["Product"])).first()
                if obj: form_payload["Product"] = obj.name
                
            # 2. Translate Sub Product ID to Name
            if form_payload.get("Sub Product") and str(form_payload["Sub Product"]).isdigit():
                obj = SubProductMaster.objects.filter(id=int(form_payload["Sub Product"])).first()
                if obj: form_payload["Sub Product"] = obj.name
                
            # 3. Translate Make Model Class ID to Name
            if form_payload.get("Make Model Class") and str(form_payload["Make Model Class"]).isdigit():
                obj = MakeModelClassMaster.objects.filter(id=int(form_payload["Make Model Class"])).first()
                if obj: form_payload["Make Model Class"] = obj.name
                
            # 4. Translate Fuel ID to Name
            if form_payload.get("Fuel") and str(form_payload["Fuel"]).isdigit():
                obj = FuelTypeMaster.objects.filter(id=int(form_payload["Fuel"])).first()
                if obj: form_payload["Fuel"] = obj.name

            ticket = SupportTicket.objects.create(
                user=User.objects.first(), # Safe default fallback
                remarks=remarks,
                form_payload=form_payload
            )
            return JsonResponse({"success": True, "ticket_id": ticket.id})
        except Exception as e:
            return JsonResponse({"success": False, "message": str(e)})
            
    return JsonResponse({"success": False, "message": "Invalid request method."})

@csrf_exempt 
def update_ticket_status(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            ticket_id = data.get("ticket_id")
            new_status = data.get("status")

            if not ticket_id or not new_status:
                return JsonResponse({"success": False, "message": "Ticket ID and Status are required."})

            ticket = SupportTicket.objects.get(id=ticket_id)

            valid_statuses = ["OPEN", "FOLLOW-UP", "CLOSED"]
            if new_status.upper() not in valid_statuses:
                return JsonResponse({"success": False, "message": "Invalid status."})

            ticket.status = new_status.upper()
            ticket.save()
            
            return JsonResponse({"success": True})
            
        except SupportTicket.DoesNotExist:
             return JsonResponse({"success": False, "message": "Ticket not found."})
        except Exception as e:
            return JsonResponse({"success": False, "message": str(e)})
            
    return JsonResponse({"success": False, "message": "Invalid request method."})

# =========================================================
# AUTOMATED MIS PAYOUT CALCULATION VIEWS
# =========================================================

def mis_payout_automation(request):
    msg = ""
    error = ""
    if request.method == 'POST':
        form = MISUploadForm(request.POST, request.FILES)
        if form.is_valid():
            mis_obj = form.save(commit=False)
            mis_obj.uploaded_by = User.objects.first() # Safe default
            mis_obj.save()
            
            # Spin up the background thread so the UI doesn't block while Pandas does the heavy lifting
            threading.Thread(target=process_mis_mapping, args=(mis_obj.id,)).start()
            
            msg = "File uploaded successfully. The mapping engine has started processing in the background."
            return redirect('mis_payout_automation')
        else:
            error = "Please upload a valid CSV or Excel file."
    else:
        form = MISUploadForm()
    
    files = MISFile.objects.all().order_by('-created_at')[:50]
    
    return render(request, 'insurance/mis_payout_automation.html', {
        'form': form,
        'files': files,
        'msg': msg,
        'error': error
    })

def download_processed_mis(request, file_id):
    mis_obj = get_object_or_404(MISFile, id=file_id)
    if not mis_obj.processed_file:
        return HttpResponse("Processed file is not available yet. Please check back when status is COMPLETED.", status=404)
    
    content_type, _ = mimetypes.guess_type(mis_obj.processed_file.name)
    response = HttpResponse(mis_obj.processed_file, content_type=content_type or 'application/octet-stream')
    response['Content-Disposition'] = f'attachment; filename="{mis_obj.processed_file.name.split("/")[-1]}"'
    return response

def mis_mapping_dashboard(request):
    mappings = MappingConfiguration.objects.all()
    return render(request, 'insurance/mis_mapping_dashboard.html', {'mappings': mappings})


# --- JSON Schema injected dynamically into the Dropdowns for cross-table builder ---
def get_rule_schema_context():
    return {
        'MIS_File': [], 
        'RateMaster': [
            'insurance_company', 'product', 'sub_product', 'policy_type', 
            'fuel_type', 'make_model_class', 'new_vehicle_makes', 'new_rto_list', 
            'cc_range', 'sc_range', 'age_range', 'date_range', 
            'is_ncb', 'is_cpa', 'is_zd'
        ]
    }

def add_mis_mapping(request):
    if request.method == 'POST':
        form = MappingConfigurationForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('mis_mapping_dashboard')
    else:
        form = MappingConfigurationForm()
        
    return render(request, 'insurance/mis_mapping_form.html', {
        'form': form, 
        'title': 'Add Multi-Table Mapping Rule',
        'schema_json': json.dumps(get_rule_schema_context())
    })

def edit_mis_mapping(request, pk):
    mapping = get_object_or_404(MappingConfiguration, pk=pk)
    if request.method == 'POST':
        form = MappingConfigurationForm(request.POST, instance=mapping)
        if form.is_valid():
            form.save()
            return redirect('mis_mapping_dashboard')
    else:
        form = MappingConfigurationForm(instance=mapping)
        
    return render(request, 'insurance/mis_mapping_form.html', {
        'form': form, 
        'title': 'Edit Multi-Table Mapping Rule',
        'schema_json': json.dumps(get_rule_schema_context())
    })

def delete_mis_mapping(request, pk):
    mapping = get_object_or_404(MappingConfiguration, pk=pk)
    mapping.delete()
    return redirect('mis_mapping_dashboard')