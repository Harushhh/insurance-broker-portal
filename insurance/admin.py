from django.contrib import admin
from .models import RateMaster

@admin.register(RateMaster)
class RateMasterAdmin(admin.ModelAdmin):
    list_display = (
        "id",
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
    )

from django.contrib import admin
from .models import RTOMaster, MakeModelMaster

admin.site.register(RTOMaster)
admin.site.register(MakeModelMaster)