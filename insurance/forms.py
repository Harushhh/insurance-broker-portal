from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from .models import (
    ExtractionField, FuelTypeMaster, HealthRateMaster, LifeInsurerAlias, LifeInsurerNote, LifePlanMapping, LifeRate,
    MakeModelClassMaster,
    MakeModelMaster, MappingConfiguration, MISFile, PincodeMaster,
    PolicyDocumentUpload, PolicyTypeMaster, ProductMaster, RateMaster,
    RTOMaster, SpecialRateRequest, SubProductMaster, YesNoNAMaster,
)
from .services import life_mis
from .services.life_rates import BULK_FIELD_LABELS, BULK_OPERATIONS, bulk_field_choices, bulk_field_kind
from .services.masters import get_rto_and_make_choices

class ExtractionFieldForm(forms.ModelForm):
    class Meta:
        model = ExtractionField
        fields = ['category', 'field_name', 'is_mandatory', 'extraction_method', 'is_active', 'has_dropdown', 'dropdown_options']
        widgets = {
            'category': forms.TextInput(attrs={'class': 'dash-input', 'placeholder': 'e.g., Motor'}),
            'field_name': forms.TextInput(attrs={'class': 'dash-input', 'placeholder': 'e.g., Insurance Company'}),
            'extraction_method': forms.Select(attrs={'class': 'dash-input'}),
            'dropdown_options': forms.Textarea(attrs={'class': 'dash-input', 'rows': 2, 'placeholder': 'Option 1, Option 2, Option 3...'}),
        }

# ==========================================
# AUTOMATED MIS PAYOUT FORMS
# ==========================================
class MISUploadForm(forms.ModelForm):
    class Meta:
        model = MISFile
        fields = ['uploaded_file']
        widgets = {
            'uploaded_file': forms.FileInput(attrs={'class': 'dash-input', 'accept': '.csv, .xlsx, .xls'})
        }

# Retained strictly for legacy API or Admin access. UI editing is locked.
class MappingConfigurationForm(forms.ModelForm):
    class Meta:
        model = MappingConfiguration
        fields = ['source_table', 'source_column', 'operator', 'target_table', 'target_column', 'is_active', 'order_index']


# ==========================================
# SIGNUP (public, pending-admin-approval)
# ==========================================
class SignupForm(forms.Form):
    full_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={'class': 'auth-input', 'placeholder': 'Jane Doe', 'autocomplete': 'name'}),
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={'class': 'auth-input', 'placeholder': 'jane@example.com', 'autocomplete': 'email'}),
    )
    mobile = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={'class': 'auth-input', 'placeholder': '9876543210', 'autocomplete': 'tel'}),
    )
    designation = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={'class': 'auth-input', 'placeholder': 'e.g., Relationship Manager'}),
    )
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={'class': 'auth-input', 'placeholder': 'Choose a username', 'autocomplete': 'username'}),
    )
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={'class': 'auth-input', 'placeholder': '••••••••', 'autocomplete': 'new-password'}),
    )
    password2 = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={'class': 'auth-input', 'placeholder': '••••••••', 'autocomplete': 'new-password'}),
    )

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That username is already taken.")
        return username

    def clean_email(self):
        email = self.cleaned_data['email'].strip()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with that email already exists.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get('password1')
        password2 = cleaned_data.get('password2')
        if password1 and password2 and password1 != password2:
            self.add_error('password2', "Passwords do not match.")
        if password1:
            validate_password(password1)
        return cleaned_data


# ==========================================
# SPECIAL RATE REQUESTS
# ==========================================
ALLOWED_INSURER_APPROVAL_EXTENSIONS = ["pdf", "png", "jpg", "jpeg"]


class SpecialRateRequestForm(forms.ModelForm):
    """
    Shared base for the two Special Rate Request sections. rate_type is
    deliberately excluded from the fields below — the view sets it
    server-side based on which submit button fired, so it can't be spoofed
    via a tampered hidden input to dodge the MG-BG mandatory-file rule.
    """
    # Declared explicitly (rather than left to ModelForm's auto-generated
    # ChoiceField) so a blank placeholder option can be prepended — required=True
    # still rejects that blank value, so the user must make a real choice
    # instead of silently submitting whatever choice happens to render first.
    product = forms.ChoiceField(
        choices=[("", "Select Product")] + list(SpecialRateRequest.PRODUCT_CHOICES),
        widget=forms.Select(attrs={"class": "field-input"}),
    )

    class Meta:
        model = SpecialRateRequest
        fields = ["product", "entry_no", "insurer_approval_file", "remarks"]
        widgets = {
            "entry_no": forms.TextInput(attrs={"class": "field-input", "placeholder": "e.g. ENT-2026-0001"}),
            "insurer_approval_file": forms.FileInput(attrs={"class": "field-file", "accept": ".pdf,.png,.jpg,.jpeg"}),
            "remarks": forms.Textarea(attrs={"class": "field-textarea", "rows": 4, "placeholder": "Add context for this request..."}),
        }

    def clean_insurer_approval_file(self):
        f = self.cleaned_data.get("insurer_approval_file")
        if f:
            ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
            if ext not in ALLOWED_INSURER_APPROVAL_EXTENSIONS:
                raise forms.ValidationError("Only PDF, PNG, JPG, or JPEG files are allowed.")
        return f


class MgBgRateRequestForm(SpecialRateRequestForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["insurer_approval_file"].required = True


class BgRateRequestForm(SpecialRateRequestForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["insurer_approval_file"].required = False


# ---------------------------------------------------------------------------
# Forms moved here from views.py
# ---------------------------------------------------------------------------

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


class PincodeForm(forms.ModelForm):
    class Meta:
        model = PincodeMaster
        fields = ["pincode_zone", "pincode_cluster"]


# Rate fields that must never carry more than 2 decimal places — paise/cents-level
# precision is meaningless for these payout rates and extra digits are almost
# always a fat-fingered or pasted-in artifact. Enforced both on the single-record
# edit form (RateForm.clean below) and on the bulk-update path (bulk_update_rates).
RATE_DECIMAL_FIELDS = (
    "pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5",
    "pi_net_rate", "pi_flat_amount", "pi_vli",
    "po_od_rate", "po_tp_rate", "po_net_rate", "po_flat_amount",
)


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
        exclude = ["group", "created_at", "updated_at"]
        widgets = {
            field_name: forms.NumberInput(attrs={"step": "0.01"})
            for field_name in RATE_DECIMAL_FIELDS
        }

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

        # insurance_company/insurer_vertical are plain CharField/TextInput on
        # this form (never declared as ChoiceField above), so `.choices` here
        # was previously computed — via a DISTINCT scan of the ~100k-row
        # RateMaster table, on every single GET and POST — and then silently
        # discarded: TextInput ignores it for rendering, and CharField.clean()
        # never consults it either. Intentionally not rebuilding it.

        rto_make_choices = get_rto_and_make_choices()

        makes = list(rto_make_choices["makes"])
        initial_makes = kwargs.get("initial", {}).get("new_vehicle_makes", [])
        instance_makes = [x.strip() for x in self.instance.new_vehicle_makes.split(",")] if self.instance and self.instance.new_vehicle_makes else []
        all_makes = set(instance_makes + initial_makes)
        for m in all_makes:
            if m and m not in makes:
                makes.append(m)
        self.fields["new_vehicle_makes"].choices = [(m, m) for m in makes]

        rtos = list(rto_make_choices["rtos"])
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

    def clean(self):
        cleaned_data = super().clean()
        for field_name in RATE_DECIMAL_FIELDS:
            value = cleaned_data.get(field_name)
            if value is not None:
                cleaned_data[field_name] = round(value, 2)
        return cleaned_data


class HealthRateForm(forms.ModelForm):
    class Meta:
        model = HealthRateMaster
        exclude = ["created_at", "updated_at", "source_row_hash"]
        widgets = {
            "plan_names": forms.Textarea(attrs={"rows": 4}),
            "remarks": forms.Textarea(attrs={"rows": 3}),
            "from_date": forms.DateInput(attrs={"type": "date"}),
            "to_date": forms.DateInput(attrs={"type": "date"}),
        }


class LifeRateForm(forms.ModelForm):
    """Edit one Life row on screen: what it is, its MG (what the insurer pays the broker) and its BG (the partner
    payout the Rate Checker shows). Both are percent numbers (47.5 means 47.5 %) and are edited independently."""

    class Meta:
        model = LifeRate
        fields = ["category", "product", "variant", "ppt", "pt", "condition", "from_date", "to_date",
                  *LifeRate.MG_FIELDS, *LifeRate.PAYOUT_FIELDS, "remarks"]
        widgets = {
            "remarks": forms.Textarea(attrs={"rows": 3}),
            "from_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "to_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            **{name: forms.NumberInput(attrs={"step": "any", "min": "0", "max": "100"})
               for name in (*LifeRate.MG_FIELDS, *LifeRate.PAYOUT_FIELDS)},
        }
        labels = {
            "ppt": "PPT", "pt": "PT", "from_date": "Valid from", "to_date": "Valid to",
            "mg_yr1": "Year 1 MG (%)", "mg_deferred": "At 2nd year MG (%)", "mg_yr2": "Year 2 MG (%)",
            "mg_yr3": "Year 3 MG (%)", "mg_yr4": "Year 4 MG (%)", "mg_yr5": "Year 5 MG (%)",
            "mg_yr6_plus": "Year 6+ MG (%)",
            "payout_yr1": "Year 1 BG payout (%)", "payout_deferred": "At 2nd year BG payout (%)",
            "payout_yr2": "Year 2 BG payout (%)", "payout_yr3": "Year 3 BG payout (%)",
            "payout_yr4": "Year 4 BG payout (%)", "payout_yr5": "Year 5 BG payout (%)",
            "payout_yr6_plus": "Year 6+ BG payout (%)",
        }

    def clean(self):
        cleaned = super().clean()
        for name in (*LifeRate.MG_FIELDS, *LifeRate.PAYOUT_FIELDS):
            value = cleaned.get(name)
            if value is not None and not 0 <= value <= 100:
                self.add_error(name, "Enter a percent between 0 and 100.")
        start, end = cleaned.get("from_date"), cleaned.get("to_date")
        if start and end and start > end:
            self.add_error("to_date", "The end date is before the start date.")
        return cleaned


class LifeRateAddForm(LifeRateForm):
    """Type in a new Life payout row on the Life Rate Master: the editable fields of a row, plus its insurer. A row
    needs an insurer, a product and at least one payout."""

    class Meta(LifeRateForm.Meta):
        fields = ["insurer", *LifeRateForm.Meta.fields]
        widgets = {**LifeRateForm.Meta.widgets, "insurer": forms.TextInput(attrs={"list": "lifeInsurers", "autocomplete": "off"})}

    def clean_insurer(self):
        return (self.cleaned_data.get("insurer") or "").strip()

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("insurer") and "insurer" not in self.errors:
            self.add_error("insurer", "Enter the insurer.")
        if not (cleaned.get("product") or "").strip():
            self.add_error("product", "Enter the product (plan) name.")
        no_payout = all(cleaned.get(name) is None for name in LifeRate.PAYOUT_FIELDS)
        if no_payout and not any(name in self.errors for name in LifeRate.PAYOUT_FIELDS):
            self.add_error(None, "Enter at least one BG payout.")    # (a payout that was typed but is invalid has its own error)
        return cleaned


class LifeBulkUpdateForm(forms.Form):
    """What a bulk edit on the Life Rate Master does: one field (only the ones services.life_rates allows), one
    operation, one value. Payout values are Decimals once cleaned, so the arithmetic is exact."""
    field = forms.ChoiceField(choices=bulk_field_choices())
    operation = forms.ChoiceField(choices=[
        (name, name) for name in dict.fromkeys(op for ops in BULK_OPERATIONS.values() for op in ops)])
    value = forms.CharField(required=False)

    PAYOUT_LIMITS = {"set": (0, 100), "add": (-100, 100), "multiply": (0, 100)}

    def clean(self):
        cleaned = super().clean()
        column, operation = cleaned.get("field"), cleaned.get("operation")
        if not column or not operation:
            return cleaned
        kind = bulk_field_kind(column)
        label = BULK_FIELD_LABELS[column]
        if operation not in BULK_OPERATIONS[kind]:
            raise forms.ValidationError(f"'{operation}' cannot be used on {label}.")
        raw = (cleaned.get("value") or "").strip()
        if operation == "clear":
            cleaned["value"] = None
        elif kind == "payout":
            try:
                number = Decimal(raw.rstrip("%").strip())
            except InvalidOperation:
                raise forms.ValidationError(f"Enter a number for {label}.")
            low, high = self.PAYOUT_LIMITS[operation]
            if not number.is_finite() or not low <= number <= high:
                raise forms.ValidationError(f"Enter a number between {low} and {high} for '{operation}' on {label}.")
            cleaned["value"] = number
        else:
            if not raw:
                raise forms.ValidationError(f"Enter the new text for {label}.")
            max_length = LifeRate._meta.get_field(column).max_length
            if max_length and len(raw) > max_length:
                raise forms.ValidationError(f"{label} can be at most {max_length} characters.")
            cleaned["value"] = raw
        return cleaned


class LifeInsurerNoteForm(forms.ModelForm):
    """Edit an insurer's notes (the terms and conditions written under its tables in the workbook) on screen.
    Emptying the box is allowed: the Rate Checker shows no notes for that insurer then."""
    text = forms.CharField(required=False, label="Notes and conditions", widget=forms.Textarea(attrs={"rows": 18}))

    class Meta:
        model = LifeInsurerNote
        fields = ["text"]


class _LifeMappingForm(forms.Form):
    """What the two Life Mapping forms share: the live Life grid they check against, and the insurer picker."""
    id = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, grid, **kwargs):
        super().__init__(*args, **kwargs)
        self.grid = grid
        insurers = sorted(grid.insurers) if grid is not None else []
        self.fields["insurer"].choices = [("", "Choose the insurer")] + [(name, name) for name in insurers]
        self.fields["insurer"].widget.attrs.update({"class": "form-select"})

    def clean(self):
        cleaned = super().clean()
        if self.grid is None:
            raise forms.ValidationError(
                "No Life grid is live, so there is nothing to map to yet: upload this month's workbook on Life Rate Master.")
        return cleaned


class LifePlanMappingForm(_LifeMappingForm):
    """Map one MIS plan name of a Life insurer to the Life grid products it stands for (the Life Plan Mapping screen).
    Every line is checked against the live grid: matching ignores case and punctuation, so a typo would otherwise
    just never match."""
    insurer = forms.ChoiceField(label="Life insurer")
    plan_name = forms.CharField(
        label="Plan name as the MIS writes it", max_length=255,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}))
    grid_products = forms.CharField(
        label="Life grid products",
        widget=forms.Textarea(attrs={"rows": 5, "class": "form-control", "spellcheck": "false"}),
        help_text="One per line, exactly as the grid writes it. End a line with * to take every product that "
                  "starts with that text (SRP* takes SRP 2 Pay, SRP 3 Pay ...).")
    note = forms.CharField(
        label="Note", max_length=255, required=False, widget=forms.TextInput(attrs={"class": "form-control"}))

    def clean_plan_name(self):
        plan = (self.cleaned_data.get("plan_name") or "").strip()
        if not life_mis.fold(plan):
            raise forms.ValidationError("Enter the plan name.")
        return plan

    def clean(self):
        cleaned = super().clean()
        insurer, plan = cleaned.get("insurer"), cleaned.get("plan_name")
        if self.grid is None or not insurer:
            return cleaned
        for problem in life_mis.grid_product_problems(self.grid.rows.get(insurer, []), cleaned.get("grid_products")):
            self.add_error("grid_products", problem)
        if plan:
            clash = LifePlanMapping.objects.filter(insurer=insurer, plan_key=life_mis.fold(plan))
            if cleaned.get("id"):
                clash = clash.exclude(pk=cleaned["id"])
            if clash.exists():
                self.add_error("plan_name", "This plan is already mapped for this insurer: edit that mapping instead.")
        return cleaned


class LifeInsurerAliasForm(_LifeMappingForm):
    """Say which Life grid insurer one spelling of an insurer's name in the MIS stands for."""
    insurer = forms.ChoiceField(label="Life grid insurer")
    mis_name = forms.CharField(
        label="Insurer name as the MIS writes it", max_length=255,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}))

    def clean_mis_name(self):
        name = (self.cleaned_data.get("mis_name") or "").strip()
        if not life_mis.fold(name):
            raise forms.ValidationError("Enter the insurer name.")
        return name

    def clean(self):
        cleaned = super().clean()
        name = cleaned.get("mis_name")
        if self.grid is not None and name:
            clash = LifeInsurerAlias.objects.filter(name_key=life_mis.fold(name))
            if cleaned.get("id"):
                clash = clash.exclude(pk=cleaned["id"])
            if clash.exists():
                self.add_error("mis_name", "This name is already mapped: edit that one instead.")
        return cleaned
