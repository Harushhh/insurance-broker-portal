from django.contrib.auth.models import User
from django.db import models


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
    insurance_company = models.CharField(max_length=100)

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
        choices=STATUS_CHOICES
    )

    is_deleted = models.CharField(
        max_length=10,
        default="NO",
        choices=IS_DELETED_CHOICES,
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

    def __str__(self):
        product_name = self.product.name if self.product else "No Product"
        return f"{self.insurance_company} | {product_name}"


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


class UserProfile(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="profile"
    )
    contact_number = models.CharField(max_length=20, blank=True, null=True)
    designation = models.CharField(max_length=100, blank=True, null=True)

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

    # Matches the top text: "Showing fields for Motor"
    category = models.CharField(max_length=50, default="Base", help_text="e.g., Base, Motor, Health")
    
    # Matches the "#" column (Drag to reorder)
    order_index = models.PositiveIntegerField(default=0)
    
    # Matches "Field Name"
    field_name = models.CharField(max_length=150, unique=True)
    
    # For fields that have a dropdown (like "Insurance Company [27]")
    has_dropdown = models.BooleanField(default=False)
    
    # Comma-separated list of options for the dropdown
    dropdown_options = models.TextField(blank=True, null=True, help_text="Comma-separated list of options (e.g., HDFC, ICICI, Tata)")
    
    # Matches "Mandatory" column
    is_mandatory = models.BooleanField(default=True)
    
    # Matches "AI Read" column
    extraction_method = models.CharField(max_length=20, choices=EXTRACTION_CHOICES, default='AI')
    
    # Matches "Active" column
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['category', 'order_index']

    def __str__(self):
        return f"{self.field_name} ({self.category})"


class FieldSynonym(models.Model):
    # Matches the "Synonyms / Aliases" column
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

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.policy_number or f"MIS Record #{self.id}"


# =========================================================
# POLICY LOCK TABLE
# IMPORTANT:
# This status is ONLY for Policy Lock Checker
# It is NOT related to RateMaster.status
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

    # IMPORTANT:
    # This status belongs only to policy lock system
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

    def __str__(self):
        return f"{self.vehicle_no} - {self.policy_holder_name} - {self.status}"