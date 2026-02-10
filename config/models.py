from django.db import models

class RateMaster(models.Model):

    new_vehicle_makes = models.CharField(max_length=100)
    new_rto_list = models.CharField(max_length=100)
    insurer_vertical = models.CharField(max_length=50)
    insurance_company = models.CharField(max_length=100)
    product = models.CharField(max_length=50)
    sub_product = models.CharField(max_length=50)
    policy_type = models.CharField(max_length=50)
    fuel_type = models.CharField(max_length=50)

    vehicle_age_min = models.IntegerField()
    vehicle_age_max = models.IntegerField()

    make_model_class = models.CharField(max_length=100)

    pi_od_rate = models.FloatField()
    pi_tp_rate = models.FloatField()
    pi_tp_2 = models.FloatField(null=True, blank=True)
    pi_tp_3 = models.FloatField(null=True, blank=True)
    pi_tp_4 = models.FloatField(null=True, blank=True)
    pi_tp_5 = models.FloatField(null=True, blank=True)

    pi_net_rate = models.FloatField()
    pi_flat_amount = models.FloatField(null=True, blank=True)
    pi_vli = models.FloatField(null=True, blank=True)
    pi_type = models.CharField(max_length=20)

    tariff_min = models.FloatField()
    tariff_max = models.FloatField()

    is_ncb = models.BooleanField(default=False)
    is_cpa = models.BooleanField(default=False)
    is_zd = models.BooleanField(default=False)

    cc_min = models.IntegerField()
    cc_max = models.IntegerField()

    from_date = models.DateField()
    to_date = models.DateField()

    sc_min = models.FloatField(null=True, blank=True)
    sc_max = models.FloatField(null=True, blank=True)

    veh_use = models.CharField(max_length=50)
    add_tnc = models.TextField(null=True, blank=True)
    remarks = models.TextField(null=True, blank=True)

    po_type = models.CharField(max_length=20, null=True, blank=True)
    po_od_rate = models.FloatField(null=True, blank=True)
    po_tp_rate = models.FloatField(null=True, blank=True)
    po_net_rate = models.FloatField(null=True, blank=True)
    po_flat_amount = models.FloatField(null=True, blank=True)

    def __str__(self):
        return f"{self.insurance_company} - {self.product}"
