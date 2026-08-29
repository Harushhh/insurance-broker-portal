import random

from django.contrib.auth.models import User
from django.core.validators import RegexValidator
from django.db import models

PAN_VALIDATOR = RegexValidator(
    regex=r'^[A-Z]{5}[0-9]{4}[A-Z]$',
    message="Enter a valid PAN (format: AAAAA9999A).",
)
IFSC_VALIDATOR = RegexValidator(
    regex=r'^[A-Z]{4}0[A-Z0-9]{6}$',
    message="Enter a valid IFSC code (format: AAAA0999999).",
)


def generate_user_id_code():
    """A unique, zero-padded 4-digit code (e.g. '0042'), assigned once per user."""
    while True:
        candidate = f"{random.randint(0, 9999):04d}"
        if not UserProfile.objects.filter(user_id_code=candidate).exists():
            return candidate


# ---------- MASTER TABLE ----------
class YesNoNAMaster(models.Model):
    code = models.CharField(max_length=3, unique=True)
    meaning = models.CharField(max_length=50)

    def __str__(self):
        return self.code


# ---------- OTHER MASTER TABLES ----------
class ProductMaster(models.Model):
    name = models.CharField(max_length=50, unique=True)

    class Meta:
        managed = True
        db_table = "insurance_productmaster"

    def __str__(self):
        return self.name


class SubProductMaster(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


class PolicyTypeMaster(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


class FuelTypeMaster(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


class MakeModelClassMaster(models.Model):
    name = models.CharField(max_length=150, unique=True)

    def __str__(self):
        return self.name


class RateGroup(models.Model):
    key_hash = models.CharField(max_length=64, unique=True)
    key_text = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.key_hash


# ---------- MAIN RATE TABLE ----------
class RateMaster(models.Model):
    STATUS_CHOICES = [
        ("ACTIVE", "Active"),
        ("INACTIVE", "Inactive"),
    ]

    IS_DELETED_CHOICES = [
        ("YES", "Yes"),
        ("NO", "No"),
    ]

    group = models.ForeignKey(
        RateGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rates"
    )
    new_vehicle_makes = models.CharField(max_length=255, null=True, blank=True)
    new_rto_list = models.CharField(max_length=255, null=True, blank=True)
    insurer_vertical = models.CharField(max_length=100, null=True, blank=True)
    insurance_company = models.CharField(max_length=100, db_index=True)

    product = models.ForeignKey(
        ProductMaster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rates"
    )
    sub_product = models.ForeignKey(
        SubProductMaster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    policy_type = models.ForeignKey(
        PolicyTypeMaster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    fuel_type = models.ForeignKey(
        FuelTypeMaster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    make_model_class = models.ForeignKey(
        MakeModelClassMaster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    # IMPORTANT:
    # This status belongs only to Rate Master dashboard / rate records
    status = models.CharField(
        max_length=20,
        default="INACTIVE",
        choices=STATUS_CHOICES,
        db_index=True
    )

    is_deleted = models.CharField(
        max_length=10,
        default="NO",
        choices=IS_DELETED_CHOICES,
        db_index=True
    )

    vehicle_age_min = models.FloatField(null=True, blank=True)
    vehicle_age_max = models.FloatField(null=True, blank=True)
    pi_od_rate = models.FloatField(null=True, blank=True)
    pi_tp_rate = models.FloatField(null=True, blank=True)
    pi_tp_2 = models.FloatField(null=True, blank=True)
    pi_tp_3 = models.FloatField(null=True, blank=True)
    pi_tp_4 = models.FloatField(null=True, blank=True)
    pi_tp_5 = models.FloatField(null=True, blank=True)
    pi_net_rate = models.FloatField(null=True, blank=True)
    pi_flat_amount = models.FloatField(null=True, blank=True)
    pi_vli = models.FloatField(null=True, blank=True)
    pi_type = models.CharField(max_length=50, null=True, blank=True)
    tariff_min = models.FloatField(null=True, blank=True)
    tariff_max = models.FloatField(null=True, blank=True)

    is_ncb = models.ForeignKey(
        YesNoNAMaster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ncb",
    )
    is_cpa = models.ForeignKey(
        YesNoNAMaster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cpa",
    )
    is_zd = models.ForeignKey(
        YesNoNAMaster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="zd",
    )

    cc_min = models.FloatField(null=True, blank=True)
    cc_max = models.FloatField(null=True, blank=True)
    from_date = models.DateField(null=True, blank=True)
    to_date = models.DateField(null=True, blank=True)
    user_id = models.IntegerField(null=True, blank=True)
    sc_min = models.FloatField(null=True, blank=True)
    sc_max = models.FloatField(null=True, blank=True)
    veh_use = models.CharField(max_length=50, null=True, blank=True)
    add_tnc = models.TextField(null=True, blank=True)
    remarks = models.TextField(null=True, blank=True)
    po_type = models.CharField(max_length=50, null=True, blank=True)
    po_od_rate = models.FloatField(null=True, blank=True)
    po_tp_rate = models.FloatField(null=True, blank=True)
    po_net_rate = models.FloatField(null=True, blank=True)
    po_flat_amount = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        # Matches the exact ORDER BY used by motor_payout_rates and
        # policy_lock_checker (po_net_rate/po_od_rate/po_flat_amount DESC,
        # id DESC) — without it, Postgres has to fully sort every matching
        # row on each search before it can return the first one, regardless
        # of how few rows the view ultimately keeps.
        indexes = [
            models.Index(
                fields=["-po_net_rate", "-po_od_rate", "-po_flat_amount", "-id"],
                name="ratemaster_payout_sort_idx",
            ),
        ]

    def __str__(self):
        product_name = self.product.name if self.product else "No Product"
        return f"{self.insurance_company} | {product_name}"


# ---------- HEALTH RATE TABLE ----------
class HealthRateMaster(models.Model):
    """
    One row per Health commission-grid rule (from the insurer's commission
    grid Excel). Unlike RateMaster, rows aren't exploded/grouped behind a
    separate group table — each imported row already is one rate rule, so
    it's edited and bulk-updated directly by its own id.
    """
    STATUS_CHOICES = [
        ("ACTIVE", "Active"),
        ("INACTIVE", "Inactive"),
    ]

    IS_DELETED_CHOICES = [
        ("YES", "Yes"),
        ("NO", "No"),
    ]

    insurance_company = models.CharField(max_length=255, db_index=True)
    product_name = models.CharField(max_length=50, db_index=True, blank=True, null=True)
    policy_category = models.CharField(max_length=20, blank=True, null=True)
    plan_names = models.TextField(blank=True, null=True)
    business_type = models.CharField(max_length=20, blank=True, null=True)

    min_deductible = models.FloatField(null=True, blank=True)
    max_deductible = models.FloatField(null=True, blank=True)
    min_sum_insured = models.FloatField(null=True, blank=True)
    max_sum_insured = models.FloatField(null=True, blank=True)
    min_age = models.FloatField(null=True, blank=True)
    max_age = models.FloatField(null=True, blank=True)

    # Source grid's "pincode" column — in practice a zone/segment code
    # ('All', 'HDFC_Preferred', 'RGI_Preferred_1', ...), not a literal pincode.
    pincode_zone = models.CharField(max_length=50, blank=True, null=True)

    payin_rate = models.FloatField(null=True, blank=True)
    one_year_rate = models.FloatField(null=True, blank=True)
    multi_year_2_rate = models.FloatField(null=True, blank=True)
    multi_year_3_rate = models.FloatField(null=True, blank=True)
    multi_year_4_rate = models.FloatField(null=True, blank=True)
    multi_year_5_rate = models.FloatField(null=True, blank=True)

    from_date = models.DateField(null=True, blank=True)
    to_date = models.DateField(null=True, blank=True)

    status = models.CharField(
        max_length=20,
        default="ACTIVE",
        choices=STATUS_CHOICES,
        db_index=True,
    )
    is_deleted = models.CharField(
        max_length=10,
        default="NO",
        choices=IS_DELETED_CHOICES,
        db_index=True,
    )
    remarks = models.TextField(null=True, blank=True)

    # Hash of the identity fields (everything except rates/status/remarks) as
    # they appeared in the source grid row. Lets the import command re-run
    # against an updated grid file and update rates in place via
    # update_or_create, instead of duplicating every row on each re-import.
    source_row_hash = models.CharField(max_length=64, blank=True, null=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["insurance_company", "product_name"], name="healthrate_insurer_prod_idx"),
        ]

    def __str__(self):
        return f"{self.insurance_company} | {self.product_name or 'No Product'}"


class RTOMaster(models.Model):
    rto_name = models.CharField(max_length=100, unique=True)
    rto_cluster = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["rto_name"]

    def __str__(self):
        return self.rto_name


class MakeModelMaster(models.Model):
    make_model_name = models.CharField(max_length=150, unique=True)
    make_model_cluster = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["make_model_name"]

    def __str__(self):
        return self.make_model_name


class PincodeMaster(models.Model):
    """
    Health's equivalent of RTOMaster: maps a zone label — a HealthRateMaster.
    pincode_zone value like 'HDFC_Preferred' — to the actual comma-separated
    pincodes that fall in it. Lets a raw pincode search resolve to the right
    zone(s), the same way an RTO code search resolves through RTOMaster's
    rto_cluster to the group name stored on RateMaster.new_rto_list.
    """
    pincode_zone = models.CharField(max_length=100, unique=True)
    pincode_cluster = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["pincode_zone"]

    def __str__(self):
        return self.pincode_zone


class UserProfile(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="profile"
    )
    contact_number = models.CharField(max_length=20, blank=True, null=True)  # "Mobile"
    designation = models.CharField(max_length=100, blank=True, null=True)

    # ---------- Teams hierarchy ----------
    vertical_path = models.CharField(max_length=255, blank=True, null=True)
    team = models.CharField(max_length=100, blank=True, null=True)
    team_id = models.CharField(max_length=50, blank=True, null=True)
    user_type = models.CharField(max_length=50, blank=True, null=True)
    emp_id = models.CharField(max_length=50, blank=True, null=True)
    code = models.CharField(max_length=50, blank=True, null=True)
    role = models.CharField(max_length=100, blank=True, null=True)

    branch_code = models.CharField(max_length=50, blank=True, null=True)
    branch_name = models.CharField(max_length=150, blank=True, null=True)

    rm_code = models.CharField(max_length=50, blank=True, null=True)
    rm_name = models.CharField(max_length=150, blank=True, null=True)
    tc_code = models.CharField(max_length=50, blank=True, null=True)
    tc_name = models.CharField(max_length=150, blank=True, null=True)
    csc_code = models.CharField(max_length=50, blank=True, null=True)
    csc_name = models.CharField(max_length=150, blank=True, null=True)
    posp_code = models.CharField(max_length=50, blank=True, null=True)  # "Ref/POSP code"
    posp_name = models.CharField(max_length=150, blank=True, null=True)  # "Ref/POSP name"

    reports_to = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="team_members",
    )
    agent_type = models.CharField(max_length=50, blank=True, null=True)

    # ---------- Compliance / bank details ----------
    pan = models.CharField(max_length=10, blank=True, null=True, validators=[PAN_VALIDATOR])
    bank_account = models.CharField(max_length=34, blank=True, null=True)
    bank_name = models.CharField(max_length=150, blank=True, null=True)
    ifsc = models.CharField(max_length=11, blank=True, null=True, validators=[IFSC_VALIDATOR])

    # ---------- Flags ----------
    is_admin = models.BooleanField(default=False)
    is_qc = models.BooleanField(default=False)
    is_plvc = models.BooleanField(default=False)
    personal_qc = models.BooleanField(default=False)
    qc_verticals = models.CharField(max_length=255, blank=True, null=True)  # comma-separated

    membership_id = models.CharField(max_length=50, blank=True, null=True)
    user_id_code = models.CharField(max_length=4, unique=True, blank=True, null=True)  # system-assigned 4-digit User ID

    def save(self, *args, **kwargs):
        if not self.user_id_code:
            self.user_id_code = generate_user_id_code()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.user.username


class AuditLog(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    action = models.CharField(max_length=100)
    details = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.action} - {self.timestamp}"


class GridDocument(models.Model):
    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('FOLLOW-UP', 'Follow-up'),
        ('DONE', 'Done'),
    )

    insurer_name = models.CharField(max_length=255)
    remarks = models.TextField(blank=True, null=True)
    uploaded_file = models.FileField(upload_to="grid_documents/%Y/%m/")
    uploaded_date = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    status = models.CharField(
        max_length=20, 
        choices=STATUS_CHOICES, 
        default='PENDING'
    )

    class Meta:
        ordering = ["-uploaded_date"]

    def __str__(self):
        return self.insurer_name


# =========================================================
# AI OCR RULEBOOK MODELS
# =========================================================

class ExtractionField(models.Model):
    EXTRACTION_CHOICES = [
        ('AI', 'AI Read'),
        ('MANUAL', 'Manual'),
    ]

    category = models.CharField(max_length=50, default="Base", help_text="e.g., Base, Motor, Health")
    order_index = models.PositiveIntegerField(default=0)
    field_name = models.CharField(max_length=150, unique=True)
    has_dropdown = models.BooleanField(default=False)
    dropdown_options = models.TextField(blank=True, null=True, help_text="Comma-separated list of options (e.g., HDFC, ICICI, Tata)")
    is_mandatory = models.BooleanField(default=True)
    extraction_method = models.CharField(max_length=20, choices=EXTRACTION_CHOICES, default='AI')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['category', 'order_index']

    def __str__(self):
        return f"{self.field_name} ({self.category})"


class FieldSynonym(models.Model):
    extraction_field = models.ForeignKey(ExtractionField, on_delete=models.CASCADE, related_name="synonyms")
    synonym_text = models.CharField(max_length=255)

    class Meta:
        unique_together = ("extraction_field", "synonym_text")

    def __str__(self):
        return self.synonym_text


class PolicyDocumentUpload(models.Model):
    STATUS_UPLOADED = "UPLOADED"
    STATUS_PENDING = "PENDING"
    STATUS_PROCESSING = "PROCESSING"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_FAILED = "FAILED"

    STATUS_CHOICES = [
        (STATUS_UPLOADED, "Uploaded"),
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    uploaded_file = models.FileField(upload_to="policy_uploads/%Y/%m/")
    original_filename = models.CharField(max_length=255, blank=True, null=True)
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    mime_type = models.CharField(max_length=100, blank=True, null=True)
    extraction_method = models.CharField(max_length=50, blank=True, null=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_UPLOADED
    )
    extracted_text = models.TextField(blank=True, null=True)
    parsed_json = models.JSONField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.original_filename or f"Upload #{self.id}"


class PolicyMISRecord(models.Model):
    source_document = models.OneToOneField(
        PolicyDocumentUpload,
        on_delete=models.CASCADE,
        related_name="mis_record"
    )

    insurer_name = models.CharField(max_length=255, blank=True, null=True)
    policy_number = models.CharField(max_length=100, blank=True, null=True)
    insured_name = models.CharField(max_length=255, blank=True, null=True)
    vehicle_registration_number = models.CharField(max_length=100, blank=True, null=True)
    vehicle_make = models.CharField(max_length=100, blank=True, null=True)
    vehicle_model = models.CharField(max_length=100, blank=True, null=True)
    vehicle_make_model = models.CharField(max_length=255, blank=True, null=True)
    engine_number = models.CharField(max_length=100, blank=True, null=True)
    chassis_number = models.CharField(max_length=100, blank=True, null=True)
    fuel_type = models.CharField(max_length=100, blank=True, null=True)
    cubic_capacity_cc = models.CharField(max_length=50, blank=True, null=True)
    gross_premium = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_premium = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    policy_start_date = models.DateField(null=True, blank=True)
    policy_end_date = models.DateField(null=True, blank=True)
    issue_date = models.DateField(null=True, blank=True)
    rto_location = models.CharField(max_length=255, blank=True, null=True)

    raw_ai_json = models.JSONField(blank=True, null=True)
    confidence_notes = models.TextField(blank=True, null=True)
    ai_model_name = models.CharField(max_length=100, blank=True, null=True)

    expected_payout = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    actual_payout = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    reconciliation_status = models.CharField(
        max_length=20,
        choices=[('PENDING', 'Pending'), ('MATCH', 'Match'), ('DISCREPANCY', 'Discrepancy')],
        default='PENDING'
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.policy_number or f"MIS Record #{self.id}"


# =========================================================
# POLICY LOCK TABLE
# =========================================================
class LockedPolicy(models.Model):
    LOCK_STATUS_CHOICES = [
        ("LOCKED", "Locked"),
        ("UNLOCKED", "Unlocked"),
    ]

    source_rate = models.ForeignKey(
        RateMaster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="locked_policies"
    )

    vehicle_no = models.CharField(max_length=50)
    policy_holder_name = models.CharField(max_length=255)

    product_name = models.CharField(max_length=100, blank=True, null=True)
    sub_product_name = models.CharField(max_length=100, blank=True, null=True)
    insurance_company = models.CharField(max_length=255, blank=True, null=True)
    po_type = models.CharField(max_length=50, blank=True, null=True)
    po_rate = models.FloatField(null=True, blank=True)
    po_flat_amount = models.FloatField(null=True, blank=True)
    add_tnc = models.TextField(blank=True, null=True)

    rto_code = models.CharField(max_length=50, blank=True, null=True)
    make_name = models.CharField(max_length=150, blank=True, null=True)
    fuel = models.CharField(max_length=50, blank=True, null=True)
    cc = models.CharField(max_length=50, blank=True, null=True)
    sc = models.CharField(max_length=50, blank=True, null=True)
    mfg_year = models.CharField(max_length=10, blank=True, null=True)

    locked_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="locked_policies"
    )

    status = models.CharField(
        max_length=20,
        choices=LOCK_STATUS_CHOICES,
        default="UNLOCKED"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    locked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = ("source_rate", "vehicle_no", "policy_holder_name")
        # vehicle_no is only the 2nd column of the unique_together above, so
        # lookups filtering on it alone (locked_policy_dashboard) can't use
        # that composite index efficiently — give it its own.
        indexes = [models.Index(fields=["vehicle_no"])]

    def __str__(self):
        return f"{self.vehicle_no} - {self.policy_holder_name} - {self.status}"


# =========================================================
# SPECIAL RATE REQUESTS
# =========================================================
class SpecialRateRequest(models.Model):
    RATE_TYPE_CHOICES = [
        ("MG-BG", "Special MG-BG"),
        ("BG", "Special BG"),
    ]

    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    ]

    PRODUCT_CHOICES = [
        ("MOTOR", "Motor"),
        ("LIFE", "Life"),
        ("HEALTH", "Health"),
        ("NON_MOTOR", "Non-Motor"),
    ]

    # Approval routing. Matched on *either* email or UserProfile.user_id_code
    # (not username — display casing for the same login varies, e.g.
    # "harsh.t" vs "Harsh.t" both exist as separate accounts in some
    # environments) so a stale ID on one side still resolves via the other.
    # Motor requests route to an extra approver (the Motor HOD); every other
    # product falls back to the pair that already covers finance/full-admin
    # sign-off.
    BASE_APPROVER_EMAILS = ["amir.f@arhamsecure.com", "harsh.t@arhamsecure.com"]
    BASE_APPROVER_USER_IDS = ["0556", "2966"]
    MOTOR_ONLY_APPROVER_EMAILS = ["nikhil.m@arhamsecure.com"]
    MOTOR_ONLY_APPROVER_USER_IDS = ["2453"]

    rate_type = models.CharField(max_length=10, choices=RATE_TYPE_CHOICES, db_index=True)
    product = models.CharField(max_length=20, choices=PRODUCT_CHOICES, default="MOTOR", db_index=True)
    entry_no = models.CharField(max_length=100)
    # Mandatory for MG-BG, optional for BG — enforced in the form layer
    # (SpecialRateRequestForm subclasses), not here, since the same model
    # backs both request types.
    insurer_approval_file = models.FileField(
        upload_to="special_rate_requests/%Y/%m/", blank=True, null=True
    )
    remarks = models.TextField()
    requested_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="special_rate_requests"
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING", db_index=True)
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_special_rate_requests"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_rate_type_display()} #{self.entry_no}"

    @classmethod
    def approver_emails_for_product(cls, product):
        if product == "MOTOR":
            return cls.BASE_APPROVER_EMAILS + cls.MOTOR_ONLY_APPROVER_EMAILS
        return cls.BASE_APPROVER_EMAILS

    @classmethod
    def approver_user_ids_for_product(cls, product):
        """UserProfile.user_id_code values for whoever a request for this product routes to."""
        if product == "MOTOR":
            return cls.BASE_APPROVER_USER_IDS + cls.MOTOR_ONLY_APPROVER_USER_IDS
        return cls.BASE_APPROVER_USER_IDS

    @classmethod
    def assigned_approvers_for_product(cls, product):
        """Users routed to review requests for this product."""
        return User.objects.filter(
            models.Q(email__in=cls.approver_emails_for_product(product))
            | models.Q(profile__user_id_code__in=cls.approver_user_ids_for_product(product))
        ).distinct()

    def approver_emails(self):
        return self.approver_emails_for_product(self.product)

    def approver_user_ids(self):
        return self.approver_user_ids_for_product(self.product)

    def assigned_approvers(self):
        return self.assigned_approvers_for_product(self.product)

    def can_be_reviewed_by(self, user):
        """Full Admins can act on any request; everyone else must be a routed approver."""
        if user.groups.filter(name="ADMIN").exists():
            return True
        if user.email and user.email in self.approver_emails():
            return True
        return UserProfile.objects.filter(user=user, user_id_code__in=self.approver_user_ids()).exists()


# =========================================================
# SUPPORT TICKET SYSTEM
# =========================================================
class SupportTicket(models.Model):
    CATEGORY_CHOICES = [
        ("MOTOR", "Motor"),
        ("HEALTH", "Health"),
        ("LIFE", "Life"),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="support_tickets"
    )
    remarks = models.TextField()
    form_payload = models.JSONField(default=dict, blank=True, null=True)
    status = models.CharField(max_length=20, default="OPEN")
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="MOTOR", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Ticket #{self.id} - {self.user}"


# =========================================================
# AUTOMATED MIS PAYOUT CALCULATION MODELS
# =========================================================
class MISFile(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
        ('CANCELLED', 'Cancelled'),
    ]

    uploaded_file = models.FileField(upload_to="mis_uploads/%Y/%m/")
    processed_file = models.FileField(upload_to="mis_processed/%Y/%m/", blank=True, null=True)
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mis_files"
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    error_message = models.TextField(blank=True, null=True)
    # Set right after process_mis_mapping_task.delay() so a later Cancel action
    # can target the correct Celery task via app.control.revoke().
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)
    # Ranked breakdown of why NO MATCH / MULTIPLE MATCHES rows failed (grouped
    # by rule + value, e.g. "insurer X needs 110 more Rate Master rows"),
    # computed once by process_mis_mapping and stored so the dashboard doesn't
    # need to re-open and re-parse the processed file on every page view.
    coverage_gaps = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    # Set once this file's failed rows have been written to MISFailedRow —
    # either right after process_mis_mapping finishes, or lazily by the Rate
    # Master Health dashboard for files that completed before that table
    # existed. Null means "not synced yet", NOT "zero failed rows".
    health_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        filename = self.uploaded_file.name.split('/')[-1] if self.uploaded_file else f"File #{self.id}"
        return f"{filename} - {self.status}"


# Populated straight from df_final when a file finishes processing (see
# process_mis_mapping) — one row per MIS policy that didn't cleanly resolve
# to a single payout rate. Exists so the Rate Master Health dashboard is a
# normal indexed DB query instead of re-parsing every completed file's
# processed_file Excel/CSV output on every page view.
class MISFailedRow(models.Model):
    STATUS_CHOICES = [
        ("MULTIPLE_MATCHES", "⚠️ Multiple Matches"),
        ("NO_MATCH", "❌ No Match"),
        ("BAD_DATA", "Failed - Bad Data"),
        ("OTHER", "Other"),
    ]

    mis_file = models.ForeignKey(MISFile, on_delete=models.CASCADE, related_name="failed_rows")
    row_id = models.IntegerField()
    status_key = models.CharField(max_length=20, choices=STATUS_CHOICES, db_index=True)
    mapping_status = models.CharField(max_length=50)
    failure_reason = models.TextField()
    # Candidate Rate Master group ids parsed out of failure_reason — only
    # ⚠️ MULTIPLE MATCHES rows ever have any.
    group_ids = models.JSONField(default=list, blank=True)
    insurer = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    # Restricted to the MIS columns process_mis_mapping's RULE 1-6 elimination
    # logic actually reads (see MATCHING_RULE_COLUMNS in mapping_engine.py) —
    # not the ~100 other columns (customer address, agent, QC status, etc.)
    # that have no bearing on why the row didn't map.
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(fields=["mis_file", "row_id"], name="uniq_mis_failed_row"),
        ]

    def __str__(self):
        return f"{self.mis_file_id}:{self.row_id} - {self.status_key}"


class MappingConfiguration(models.Model):
    TABLE_CHOICES = [
        ('MIS_File', 'MIS File (Uploaded Data)'),
        ('RateMaster', 'Rate Master (Grid Data)'),
    ]
    
    OPERATOR_CHOICES = [
        ('EXACT', 'Equals (Exact Match)'),
        ('WORD_MATCH', 'Word Match (Whole Word / Exact List Item)'),
        ('CONTAINS', 'Contains (Substring Match)'),
        ('RANGE_CC', 'CC Range Bound Match'),
        ('RANGE_SC', 'Seating Capacity Range Bound Match'),
        ('RANGE_DATE', 'Date Range Bound Match'),
        ('RANGE_AGE', 'Vehicle Age Bound Match'),
    ]

    source_table = models.CharField(max_length=100, choices=TABLE_CHOICES, default='MIS_File', help_text="Left operand table")
    source_column = models.CharField(max_length=255, help_text="Left operand column", default="", blank=True)
    
    operator = models.CharField(max_length=20, choices=OPERATOR_CHOICES, default='EXACT')
    
    target_table = models.CharField(max_length=100, choices=TABLE_CHOICES, default='RateMaster', help_text="Right operand table")
    target_column = models.CharField(max_length=255, help_text="Right operand column", default="", blank=True)
    
    is_active = models.BooleanField(default=True)
    order_index = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order_index', 'source_column']

    def __str__(self):
        return f"[{self.source_table}].[{self.source_column}] {self.operator} [{self.target_table}].[{self.target_column}]"

    # Properties to maintain backwards compatibility with existing UI dashboards
    @property
    def mis_column_name(self):
        return f"{self.source_table}.{self.source_column}"

    @property
    def grid_field_name(self):
        return f"{self.target_table}.{self.target_column}"

    @property
    def mapping_type(self):
        return self.operator
        
    def get_mapping_type_display(self):
        return dict(self.OPERATOR_CHOICES).get(self.operator, self.operator)

# =========================================================
# RATE MASTER OVERLAP DETECTION
# =========================================================

class RateOverlapScan(models.Model):
    """
    One run of insurance/overlap_utils.py's sweep over the Rate Master.

    Materialized for the same reason MISFailedRow is: the sweep compares every
    pair of active rate groups within an insurer, which is far too expensive to
    redo on each dashboard page view. Each scan owns its own pairs, so a run in
    progress leaves the results already on screen untouched and only replaces
    them once it succeeds.
    """
    STATUS_RUNNING = "RUNNING"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_FAILED = "FAILED"

    STATUS_CHOICES = [
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_RUNNING, db_index=True
    )
    triggered_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rate_overlap_scans",
    )
    groups_scanned = models.IntegerField(default=0)
    # The TRUE number of conflicting pairs the sweep found, which is not
    # necessarily how many were stored - see capped_types.
    pairs_found = models.IntegerField(default=0)
    # {conflict_type: true count}. The dashboard cards read these rather than
    # counting stored RateOverlapPair rows, so a capped bucket still reports
    # its real size.
    type_counts = models.JSONField(default=dict, blank=True)
    # Conflict types whose stored pairs were truncated at their
    # DEFAULT_TYPE_CAPS ceiling, so the drill-down can say it is showing a
    # sample rather than everything.
    capped_types = models.JSONField(default=list, blank=True)
    was_capped = models.BooleanField(default=False)
    # Real conflicts the sweep found and deliberately did not list: two groups
    # identical on every field the engine matches on, differing only in their
    # T&Cs. They are distinct offers rather than duplicates, and the only fix
    # would be re-cutting the insurer's own grid, so listing them would park an
    # unfixable queue beside the fixable ones. Recorded so the dashboard can
    # still say they were seen. See overlap_utils.classify_pair.
    tnc_differing_skipped = models.IntegerField(default=0)
    error_message = models.TextField(blank=True, null=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Overlap scan #{self.id} - {self.status}"


class RateOverlapPair(models.Model):
    """
    Two ACTIVE rate groups that no rule in the MIS Payout Engine's RULE 1-6
    chain can tell apart - so any policy landing in their shared span resolves
    to both and lands on the dashboard as MULTIPLE MATCHES.

    Keys are COALESCE(group_id, id), matching how process_mis_mapping counts
    distinct matches, with group_key_a < group_key_b so one conflict is never
    stored twice under swapped keys.
    """
    CONFLICT_CHOICES = [
        ("DOUBLE_RATE_RISK", "Double Rate Risk"),
        ("EXACT_DUPLICATE", "Exact Duplicate"),
        ("CONTAINED", "Contained Range"),
        ("OPEN_ENDED", "Open-Ended Overlap"),
        ("PARTIAL", "Partial Overlap"),
    ]

    scan = models.ForeignKey(RateOverlapScan, on_delete=models.CASCADE, related_name="pairs")
    insurance_company = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    group_key_a = models.IntegerField()
    group_key_b = models.IntegerField()
    conflict_type = models.CharField(max_length=20, choices=CONFLICT_CHOICES, db_index=True)
    # CONFLICT_SEVERITY rank, denormalized so the drill-down can order by it
    # without a CASE expression on every query.
    severity_rank = models.IntegerField(default=5)
    row_count_a = models.IntegerField(default=0)
    row_count_b = models.IntegerField(default=0)
    # {"axes": [...]} - the per-axis breakdown describe_pair builds, including
    # the exact overlapping span on each range axis, so the drill-down doesn't
    # have to re-derive it.
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["severity_rank", "insurance_company", "group_key_a", "group_key_b"]
        constraints = [
            models.UniqueConstraint(
                fields=["scan", "group_key_a", "group_key_b"], name="uniq_rate_overlap_pair"
            ),
        ]

    def __str__(self):
        return f"{self.group_key_a} <-> {self.group_key_b} ({self.conflict_type})"
