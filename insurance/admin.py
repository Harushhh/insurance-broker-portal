from django.contrib import admin
from .models import (
    RateMaster,
    RTOMaster,
    MakeModelMaster,
    ExtractionField,
    FieldSynonym,
    PolicyDocumentUpload,
    PolicyMISRecord,
    MappingConfiguration,
    MISFile,
)


@admin.register(RateMaster)
class RateMasterAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "group",
        "new_vehicle_makes",
        "new_rto_list",
        "insurer_vertical",
        "insurance_company",
        "product",
        "sub_product",
        "policy_type",
        "fuel_type",
        "vehicle_age_min",
        "vehicle_age_max",
        "make_model_class",
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
        "cc_min",
        "cc_max",
        "is_zd",
        "from_date",
        "to_date",
        "user_id",
        "sc_min",
        "sc_max",
        "veh_use",
        "add_tnc",
        "remarks",
        "po_type",
        "po_od_rate",
        "po_tp_rate",
        "po_net_rate",
        "po_flat_amount",
        "status",
        "is_deleted",
        "created_at",
    )
    search_fields = (
        "insurance_company",
        "new_vehicle_makes",
        "new_rto_list",
        "remarks",
    )
    list_filter = (
        "status",
        "is_deleted",
        "insurance_company",
        "product",
        "sub_product",
        "policy_type",
        "fuel_type",
        "make_model_class",
        "is_ncb",
        "is_cpa",
        "is_zd",
        "created_at",
    )
    ordering = ("-id",)


@admin.register(RTOMaster)
class RTOMasterAdmin(admin.ModelAdmin):
    list_display = ("id", "rto_name", "rto_cluster")
    search_fields = ("rto_name", "rto_cluster")
    ordering = ("rto_name",)


@admin.register(MakeModelMaster)
class MakeModelMasterAdmin(admin.ModelAdmin):
    list_display = ("id", "make_model_name", "make_model_cluster")
    search_fields = ("make_model_name", "make_model_cluster")
    ordering = ("make_model_name",)


# =========================================================
# AI OCR RULEBOOK ADMIN
# =========================================================

class FieldSynonymInline(admin.TabularInline):
    model = FieldSynonym
    extra = 1

@admin.register(ExtractionField)
class ExtractionFieldAdmin(admin.ModelAdmin):
    list_display = ("id", "field_name", "category", "order_index", "is_mandatory", "extraction_method", "is_active")
    search_fields = ("field_name", "category")
    list_filter = ("category", "is_mandatory", "extraction_method", "is_active", "has_dropdown")
    ordering = ("category", "order_index")
    inlines = [FieldSynonymInline]

@admin.register(FieldSynonym)
class FieldSynonymAdmin(admin.ModelAdmin):
    list_display = ("id", "synonym_text", "extraction_field")
    search_fields = ("synonym_text", "extraction_field__field_name")
    list_filter = ("extraction_field__category",)
    ordering = ("extraction_field__field_name", "synonym_text")


# =========================================================
# DOCUMENT & MIS ADMIN
# =========================================================

@admin.register(PolicyDocumentUpload)
class PolicyDocumentUploadAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "original_filename",
        "uploaded_by",
        "mime_type",
        "extraction_method",
        "status",
        "created_at",
        "processed_at",
    )
    search_fields = ("original_filename", "mime_type", "error_message")
    list_filter = ("status", "extraction_method", "created_at", "processed_at")
    ordering = ("-created_at",)
    readonly_fields = (
        "created_at",
        "processed_at",
        "extracted_text",
        "parsed_json",
        "error_message",
    )


@admin.register(PolicyMISRecord)
class PolicyMISRecordAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "policy_number",
        "insured_name",
        "insurer_name",
        "vehicle_registration_number",
        "vehicle_make_model",
        "gross_premium",
        "net_premium",
        "policy_start_date",
        "policy_end_date",
        "created_at",
    )
    search_fields = (
        "policy_number",
        "insured_name",
        "insurer_name",
        "vehicle_registration_number",
        "vehicle_make_model",
    )
    list_filter = ("policy_start_date", "policy_end_date", "created_at")
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "raw_ai_json")


# =========================================================
# AUTOMATED MIS PAYOUT CALCULATION ADMIN
# =========================================================

@admin.register(MISFile)
class MISFileAdmin(admin.ModelAdmin):
    list_display = ("id", "uploaded_file", "status", "created_at", "processed_at")
    list_filter = ("status", "created_at")
    ordering = ("-created_at",)


@admin.register(MappingConfiguration)
class MappingConfigurationAdmin(admin.ModelAdmin):
    list_display = (
        "order_index",
        "source_table",
        "source_column",
        "operator",
        "target_table",
        "target_column",
        "is_active",
    )
    list_filter = ("is_active", "operator", "source_table", "target_table")
    search_fields = ("source_column", "target_column")
    ordering = ("order_index",)