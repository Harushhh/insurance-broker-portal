"""REST API views (server-to-server). The routes are in `api/urls.py`; auth is `HasAPIKey` per view."""
from django.contrib.auth.models import User, Group
from django.conf import settings
from urllib.parse import urlencode
import logging
import secrets
from rest_framework import generics
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework_api_key.permissions import HasAPIKey
from .serializers import RateMasterSerializer
from .. import sso
from ..models import MakeModelMaster, RateMaster, ProductMaster, MakeModelClassMaster, AuditLog, UserProfile
from ..services.health_payout import HEALTH_PAYOUT_MAX_RESULTS, _run_health_payout_search
from ..services.policy_lock import _run_policy_lock_checker_search


logger = logging.getLogger("security")


# =========================================================
# INBOUND PARTNER PORTAL SSO HANDOFF
# The inbound mirror of the outbound handoff above: lets an
# already-authenticated user on an external partner portal (e.g.
# ArhamSecure's partner.arhamsecure.com) land here already logged in,
# scoped to a fixed subset of pages. See insurance/sso.py for the ticket
# signing/verification logic.
# =========================================================

# Pages an ArhamSecure-partner-portal handoff is allowed to grant -- exactly
# the three pages unified under "Rate Checker" (see RATE_CHECKER_TABS
# above), never ADMIN/SUPER_ADMIN or anything outside this set. If a second
# partner ever needs a different set, split this into a per-partner mapping
# instead of widening this one.
PARTNER_ARHAMSECURE_ALLOWED_GROUPS = {
    "Can_View_Policy_Locker",
    "Can_View_Health_Payout_Rates",
    # "Can_View_Life_Payout_Grid" removed 2026-10-11 (Life tab disabled). A
    # partner that still requests it isn't rejected -- the allow-list
    # intersection in IssueSSOTicketAPIView just doesn't grant it.
}


# requested_pages may name this bundle instead of listing the 3 groups
# individually -- expanded to its members in IssueSSOTicketAPIView.post
# before the allow-list intersection below, so the partner only ever needs
# to ask for "Rate_Checker" once access to the unified page is wanted.
SSO_SCOPE_BUNDLES = {
    "Rate_Checker": PARTNER_ARHAMSECURE_ALLOWED_GROUPS,
}


class IssueSSOTicketAPIView(APIView):
    """
    Server-to-server endpoint for the partner portal's backend: authenticated
    with a rest_framework_api_key key (never called from a browser), it
    JIT-provisions/updates the target user and mints a short-lived,
    single-use ticket for the browser-redirect leg (see sso_consume_view).
    """
    permission_classes = [HasAPIKey]

    def post(self, request):
        if not settings.PARTNER_SSO_TICKET_SECRET:
            return Response(
                {"error": "PARTNER_SSO_TICKET_SECRET is not configured on this portal."},
                status=500,
            )
        email = (request.data.get("email") or "").strip().lower()
        if not email:
            return Response({"error": "email is required"}, status=400)
        full_name = (request.data.get("full_name") or "").strip()
        requested_pages = set(request.data.get("requested_pages") or [])
        landing_page = request.data.get("landing_page") or "rate_checker"

        # Expand any bundle name (e.g. "Rate_Checker") into its member
        # groups before clamping -- this is purely a convenience for the
        # caller, the allow-list intersection right after is what actually
        # decides the grant either way.
        for bundle_name, member_groups in SSO_SCOPE_BUNDLES.items():
            if bundle_name in requested_pages:
                requested_pages |= member_groups

        # Server-side clamp: this app decides the actual grant, it never
        # trusts the caller's requested list wholesale.
        granted = sorted(requested_pages & PARTNER_ARHAMSECURE_ALLOWED_GROUPS)

        user, created = User.objects.get_or_create(
            username=email,
            defaults={"email": email},
        )
        if created:
            # No usable password -- this account can only ever be entered
            # via a freshly minted ticket, never the normal login form, so
            # it adds no credential-stuffing / password-guessing surface.
            user.set_unusable_password()
            if full_name:
                first, _, last = full_name.partition(" ")
                user.first_name, user.last_name = first, last
            user.is_active = True
            user.save()
            UserProfile.objects.get_or_create(user=user)
        elif not user.is_active:
            # An existing but deactivated account (e.g. an internal signup
            # pending approval) must not be silently reactivated by a
            # partner handoff.
            return Response({"error": "account is not active"}, status=403)

        user.groups.set(Group.objects.filter(name__in=granted))

        jti = secrets.token_urlsafe(16)
        ticket = sso.mint_ticket(user, landing_page, jti)
        AuditLog.objects.create(
            user=user,
            action="sso_ticket_issued",
            details=f"partner=arhamsecure pages={granted} landing={landing_page}",
        )
        logger.info("SSO ticket issued for %s, pages=%s", email, granted)

        redirect_url = f"{request.build_absolute_uri('/sso/consume/')}?{urlencode({'ticket': ticket})}"
        return Response({"redirect_url": redirect_url})


# =========================================================
# REST API ENDPOINTS
# =========================================================
class ExportRatesAPIView(generics.ListAPIView):
    permission_classes = [HasAPIKey]
    queryset = RateMaster.objects.filter(is_deleted="NO")
    serializer_class = RateMasterSerializer


class PolicyLockCheckerAPIView(APIView):
    """
    Server-to-server JSON equivalent of policy_lock_checker (the Motor
    Points page): same search/filter/dedup logic via
    _run_policy_lock_checker_search, secured with a rest_framework_api_key
    key instead of a session login -- same pattern as IssueSSOTicketAPIView.
    """
    permission_classes = [HasAPIKey]

    def get(self, request):
        results, has_searched, selected = _run_policy_lock_checker_search(request)

        def serialize_row(row):
            # Mirrors the "Eligibility Results" table in policy_lock_checker.html
            # (see the {% for row in data %} block there) so the API's numbers
            # render exactly like the web page's, string-for-string.
            if row.po_type == "On OD and TP":
                po_rate = f"{row.po_od_rate or 0}+{row.po_tp_rate or 0}%"
            else:
                po_rate = f"{row.po_rate or 0}%"

            return {
                "group_id": row.display_group_id,
                "insurance_company": row.insurance_company,
                "tariff_range": f"{row.tariff_min or 0}-{row.tariff_max or 0}",
                "po_type": row.po_type,
                "po_rate": po_rate,
                "add_tnc": row.add_tnc or "-",
            }

        return Response({
            "has_searched": has_searched,
            "total_found": len(results),
            "filters": selected,
            "results": [serialize_row(row) for row in results],
        })


class HealthPayoutRatesAPIView(APIView):
    """
    Server-to-server JSON equivalent of health_payout_rates (the Health
    Points Checker's "Check Eligibility" search): same eligibility-matching
    logic via _run_health_payout_search / _build_health_payout_queryset,
    secured with a rest_framework_api_key key instead of a session login --
    same pattern as PolicyLockCheckerAPIView.
    """
    permission_classes = [HasAPIKey]

    def get(self, request):
        results, total_found, has_more, has_searched, selected = _run_health_payout_search(request)

        def serialize_row(row):
            # Mirrors the "Valid Matches Found" table in health_payout_rates.html
            # (see the {% for row in data %} block there) so the API's values
            # render exactly like the web page's, string-for-string -- payouts
            # are always on a NET 1 Year basis regardless of the policy term
            # searched, so this is intentionally not derived from policy_term.
            rate = row.applicable_rate
            applicable_rate = f"{rate:.2f}%" if rate is not None else "-%"

            return {
                "id": f"h-{row.id}",
                "insurer": row.insurance_company,
                "applicable_rate": applicable_rate,
                "payout_type": "NET 1 Year",
                "plans": row.plan_names or "-",
            }

        return Response({
            "has_searched": has_searched,
            "total_found": total_found,
            "has_more": has_more,
            "max_results": HEALTH_PAYOUT_MAX_RESULTS,
            "filters": selected,
            "results": [serialize_row(row) for row in results],
        })


class MakeModelMasterAPIView(APIView):
    """
    Reference-data helper for external API consumers (e.g. the tech
    manager's system calling PolicyLockCheckerAPIView): returns the exact
    strings valid for that endpoint's make_names, make_model_class, and
    product query params, so callers don't have to guess spellings. Purely
    read-only reference data -- secured the same way as the other
    server-to-server endpoints.
    """
    permission_classes = [HasAPIKey]

    def get(self, request):
        # Same cluster-token extraction as make_name_list in
        # policy_lock_checker -- these are the individual tokens
        # strict_match_in_cluster actually matches against, not the raw
        # make_model_name column.
        make_names = sorted({
            item.strip()
            for value in MakeModelMaster.objects.exclude(make_model_cluster__isnull=True)
            .exclude(make_model_cluster="")
            .values_list("make_model_cluster", flat=True)
            for item in str(value).split(",")
            if item.strip()
        })

        make_model_class = list(
            MakeModelClassMaster.objects.exclude(name__iexact="NA")
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )

        product = list(
            ProductMaster.objects.values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )

        return Response({
            "make_names": make_names,
            "make_model_class": make_model_class,
            "product": product,
        })
