from django.db import models


# ---------- MASTER TABLE ----------
class YesNoNAMaster(models.Model):
    code = models.CharField(max_length=3, unique=True)
    meaning = models.CharField(max_length=50)

    def __str__(self):
        return self.code


# ---------- MAIN RATE TABLE ----------
class RateMaster(models.Model):

    new_vehicle_makes = models.CharField(max_length=100, null=True, blank=True)
    new_rto_list = models.CharField(max_length=100, null=True, blank=True)

    insurer_vertical = models.CharField(max_length=100, null=True, blank=True)
    insurance_company = models.CharField(max_length=100)

    product = models.CharField(max_length=50)
    sub_product = models.CharField(max_length=50, null=True, blank=True)
    policy_type = models.CharField(max_length=50)

    fuel_type = models.CharField(max_length=50, null=True, blank=True)

    vehicle_age_min = models.IntegerField(null=True, blank=True)
    vehicle_age_max = models.IntegerField(null=True, blank=True)

    make_model_class = models.CharField(max_length=150, null=True, blank=True)

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

    # ---------- CHANGED PART ----------
    is_ncb = models.ForeignKey(YesNoNAMaster, on_delete=models.CASCADE, related_name="ncb")
    is_cpa = models.ForeignKey(YesNoNAMaster, on_delete=models.CASCADE, related_name="cpa")
    is_zd  = models.ForeignKey(YesNoNAMaster, on_delete=models.CASCADE, related_name="zd")

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
