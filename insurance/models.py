from django.db import models
import uuid


# ---------- MASTER TABLE ----------
class YesNoNAMaster(models.Model):
    code = models.CharField(max_length=3, unique=True)
    meaning = models.CharField(max_length=50)

    def __str__(self):
        return self.code


# ---------- OTHER MASTER TABLES ----------
class ProductMaster(models.Model):
    # DB already has: id (int auto) + name
    name = models.CharField(max_length=50, unique=True)

    class Meta:
        db_table = "insurance_productmaster"
        managed = False  # IMPORTANT: because table already exists in MySQL

    def __str__(self):
        return self.name


class SubProductMaster(models.Model):
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


class PolicyTypeMaster(models.Model):
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


class FuelTypeMaster(models.Model):
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


class MakeModelClassMaster(models.Model):
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=150, unique=True)

    def __str__(self):
        return self.name


# ✅ Correct RateGroup table (NO self-foreignkey)
class RateGroup(models.Model):
    key_hash = models.CharField(max_length=64, unique=True)
    key_text = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.key_hash

# ---------- MAIN RATE TABLE ----------
class RateMaster(models.Model):


    # ✅ Optional: group link (useful for grouping dashboard)
    group = models.ForeignKey(
        RateGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="group_id",
        related_name="rates"
    )

    new_vehicle_makes = models.CharField(max_length=100, null=True, blank=True)
    new_rto_list = models.CharField(max_length=100, null=True, blank=True)

    insurer_vertical = models.CharField(max_length=100, null=True, blank=True)
    insurance_company = models.CharField(max_length=100)

    product = models.ForeignKey(
        ProductMaster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="product_id",
        related_name="rates",
    )

    sub_product = models.ForeignKey(SubProductMaster, on_delete=models.SET_NULL, null=True, blank=True, db_column="sub_product_id")
    policy_type = models.ForeignKey(PolicyTypeMaster, on_delete=models.SET_NULL, null=True, blank=True, db_column="policy_type_id")
    fuel_type = models.ForeignKey(FuelTypeMaster, on_delete=models.SET_NULL, null=True, blank=True, db_column="fuel_type_id")
    make_model_class = models.ForeignKey(MakeModelClassMaster, on_delete=models.SET_NULL, null=True, blank=True, db_column="make_model_class_id")

    vehicle_age_min = models.IntegerField(null=True, blank=True)
    vehicle_age_max = models.IntegerField(null=True, blank=True)

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

    is_ncb = models.ForeignKey(YesNoNAMaster, on_delete=models.CASCADE, related_name="ncb", db_column="is_ncb_id")
    is_cpa = models.ForeignKey(YesNoNAMaster, on_delete=models.CASCADE, related_name="cpa", db_column="is_cpa_id")
    is_zd = models.ForeignKey(YesNoNAMaster, on_delete=models.CASCADE, related_name="zd", db_column="is_zd_id")

    cc_min = models.IntegerField(null=True, blank=True)
    cc_max = models.IntegerField(null=True, blank=True)

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

    def __str__(self):
        return f"{self.insurance_company} | {self.product}"

from django.db import models

class RTOMaster(models.Model):
    rto_name = models.CharField(max_length=100, unique=True)
    rto_cluster = models.CharField(max_length=1000000, blank=True, null=True)

    def __str__(self):
        return self.rto_name


class MakeModelMaster(models.Model):
    make_model_name = models.CharField(max_length=150, unique=True)
    make_model_cluster = models.CharField(max_length=1000000, blank=True, null=True)

    def __str__(self):
        return self.make_model_name