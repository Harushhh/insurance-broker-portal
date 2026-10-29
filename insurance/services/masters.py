"""Helpers for the RTO / Make-Model / Pincode masters: cached form choices and the pincode-zone sync."""
from django.core.cache import cache
from ..models import RTOMaster, MakeModelMaster, PincodeMaster, HealthRateMaster


# RTOMaster/MakeModelMaster back the new_rto_list/new_vehicle_makes multi-select
# choices on RateForm, rebuilt on every GET and POST of the (frequently used)
# Bulk Edit Rate Group page. Both tables are near-static reference data, so a
# short cache avoids re-querying them on every request; invalidated explicitly
# wherever these tables are written to (see api_upload_chunk) rather than
# relying solely on the TTL, so new master rows show up immediately.
RTO_MAKE_CHOICES_CACHE_KEY = "rate_form_rto_make_choices_v1"


RTO_MAKE_CHOICES_CACHE_TTL = 600  # seconds


def get_rto_and_make_choices():
    cached = cache.get(RTO_MAKE_CHOICES_CACHE_KEY)
    if cached is not None:
        return cached
    data = {
        "rtos": list(RTOMaster.objects.values_list("rto_name", flat=True).order_by("rto_name")),
        "makes": list(MakeModelMaster.objects.values_list("make_model_name", flat=True).order_by("make_model_name")),
    }
    cache.set(RTO_MAKE_CHOICES_CACHE_KEY, data, RTO_MAKE_CHOICES_CACHE_TTL)
    return data


# -------------------------
# PINCODE DASHBOARD (Health's equivalent of RTO Dashboard)
# -------------------------
def _sync_pincode_zones_from_health_data():
    """Ensures every pincode_zone value actually used in HealthRateMaster has
    a PincodeMaster row (blank cluster if new), so the dashboard always shows
    every zone that needs its real pincode list filled in — rather than
    silently missing one after a Health grid re-import introduces a new zone."""
    existing = set(PincodeMaster.objects.values_list("pincode_zone", flat=True))
    used = set(
        HealthRateMaster.objects.exclude(is_deleted="YES")
        .exclude(pincode_zone__isnull=True)
        .exclude(pincode_zone="")
        .values_list("pincode_zone", flat=True)
        .distinct()
    )
    for zone in used - existing:
        PincodeMaster.objects.get_or_create(pincode_zone=zone)
