"""
Inbound SSO handoff from an external partner portal (e.g. ArhamSecure's
partner.arhamsecure.com) into this app. Mirrors the outbound handoff this
app already does into the Life Payout Grid app (see
_life_payout_grid_handoff in views.py) -- an HMAC-signed, short-lived token
carried in a redirect URL -- but adds the properties an *inbound*,
cross-company handoff needs that an internal sibling-app call didn't:
single-use replay protection and an audience claim.

The partner-facing API view (IssueSSOTicketAPIView, in views.py) mints a
ticket after clamping the requested pages to a server-side allow-list;
sso_consume_view (also in views.py) verifies it and logs the user in.
"""
import hashlib
import hmac
import time

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache

TICKET_TTL_SECONDS = 45
AUDIENCE = "portalb-sso-consume"


def _sign(payload: str) -> str:
    secret = settings.PARTNER_SSO_TICKET_SECRET
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def mint_ticket(user: User, landing_page: str, jti: str) -> str:
    expiry = int(time.time()) + TICKET_TTL_SECONDS
    payload = f"{user.username}:{expiry}:{landing_page}:{AUDIENCE}:{jti}"
    return f"{payload}.{_sign(payload)}"


def verify_and_consume_ticket(token: str):
    """
    Returns (User, landing_page) if `token` is a valid, unexpired,
    not-yet-used ticket for this audience; otherwise None.

    Single-use is enforced with cache.add(), which only succeeds the first
    time a given jti is seen -- this must fail CLOSED on any cache error
    (unlike the login-throttle helpers in auth_views.py, which deliberately
    fail open): a broken cache here must not silently disable replay
    protection on a handoff that crosses a real company boundary.
    """
    if not token or "." not in token:
        return None

    payload, _, sig = token.rpartition(".")
    if not hmac.compare_digest(sig, _sign(payload)):
        return None

    try:
        username, expiry_str, landing_page, aud, jti = payload.split(":", 4)
        expiry = int(expiry_str)
    except ValueError:
        return None

    if aud != AUDIENCE or time.time() > expiry:
        return None

    try:
        claimed = cache.add(f"sso_ticket_used:{jti}", True, timeout=TICKET_TTL_SECONDS + 5)
    except Exception:
        return None
    if not claimed:
        return None

    try:
        user = User.objects.get(username=username, is_active=True)
    except User.DoesNotExist:
        return None

    return user, landing_page
