from django.contrib import admin
from .models import (
    RateMaster,
    RTOMaster,
    MakeModelMaster,
    MISFieldMaster,
    MISFieldAlias,
    PolicyDocumentUpload,
    PolicyMISRecord,
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


class MISFieldAliasInline(admin.TabularInline):
    model = MISFieldAlias
    extra = 1


@admin.register(MISFieldMaster)
class MISFieldMasterAdmin(admin.ModelAdmin):
    list_display = ("id", "field_key", "field_label", "is_active")
    search_fields = ("field_key", "field_label")
    list_filter = ("is_active",)
    ordering = ("field_label",)
    inlines = [MISFieldAliasInline]


@admin.register(MISFieldAlias)
class MISFieldAliasAdmin(admin.ModelAdmin):
    list_display = ("id", "alias_text", "field_master", "is_active")
    search_fields = ("alias_text", "field_master__field_key", "field_master__field_label")
    list_filter = ("is_active", "field_master")
    ordering = ("field_master__field_label", "alias_text")


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