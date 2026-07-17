import logging

from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.views import View

from .forms import SignupForm
from .models import UserProfile

logger = logging.getLogger("security")

MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60  # 15 minutes

SIGNUP_MAX_ATTEMPTS = 10
SIGNUP_LOCKOUT_SECONDS = 60 * 60  # 1 hour


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


# The throttle is a defense-in-depth nicety, not core functionality — a
# broken cache backend (missing table, connection blip, etc.) must never be
# able to take down login/signup entirely. Fail open and log loudly instead.
def _safe_cache_get(key, default=None):
    try:
        return cache.get(key, default)
    except Exception:
        logger.exception("Cache read failed for key %r — failing open", key)
        return default


def _safe_cache_set(key, value, timeout):
    try:
        cache.set(key, value, timeout)
    except Exception:
        logger.exception("Cache write failed for key %r", key)


def _safe_cache_delete(key):
    try:
        cache.delete(key)
    except Exception:
        logger.exception("Cache delete failed for key %r", key)


class ThrottledLoginView(LoginView):
    """
    Drop-in replacement for django.contrib.auth.views.LoginView that adds
    a per-IP lockout after repeated failed attempts, and logs auth events.
    """
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def dispatch(self, request, *args, **kwargs):
        ip = _client_ip(request)
        self._throttle_key = f"login_attempts:{ip}"
        attempts = _safe_cache_get(self._throttle_key, 0)

        if attempts >= MAX_ATTEMPTS:
            logger.warning("Login blocked (rate limit) for IP %s", ip)
            return render(
                request,
                self.template_name,
                {"locked_out": True},
                status=429,
            )
        return super().dispatch(request, *args, **kwargs)

    def form_invalid(self, form):
        ip = _client_ip(self.request)
        attempts = _safe_cache_get(self._throttle_key, 0) + 1
        _safe_cache_set(self._throttle_key, attempts, LOCKOUT_SECONDS)
        attempted_username = self.request.POST.get("username", "")
        logger.warning(
            "Failed login attempt %s/%s from %s (username=%r)",
            attempts, MAX_ATTEMPTS, ip, attempted_username,
        )
        return super().form_invalid(form)

    def form_valid(self, form):
        ip = _client_ip(self.request)
        _safe_cache_delete(self._throttle_key)
        # Rotate the session key on login to prevent session fixation.
        self.request.session.cycle_key()
        logger.info("Successful login from %s (user=%s)", ip, form.get_user().username)
        return super().form_valid(form)


class SignupView(View):
    """
    Public self-signup. Creates an inactive account + a bare-bones profile;
    an admin must approve (activate) it from User Management before it can log in.
    """
    template_name = "registration/signup.html"

    def get(self, request):
        return render(request, self.template_name, {"form": SignupForm()})

    def dispatch(self, request, *args, **kwargs):
        ip = _client_ip(request)
        self._throttle_key = f"signup_attempts:{ip}"
        attempts = _safe_cache_get(self._throttle_key, 0)
        if attempts >= SIGNUP_MAX_ATTEMPTS:
            logger.warning("Signup blocked (rate limit) for IP %s", ip)
            return render(request, self.template_name, {"form": SignupForm(), "locked_out": True}, status=429)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request):
        form = SignupForm(request.POST)
        ip = _client_ip(request)

        if not form.is_valid():
            attempts = _safe_cache_get(self._throttle_key, 0) + 1
            _safe_cache_set(self._throttle_key, attempts, SIGNUP_LOCKOUT_SECONDS)
            return render(request, self.template_name, {"form": form})

        _safe_cache_delete(self._throttle_key)
        data = form.cleaned_data
        full_name = data["full_name"].strip()
        parts = full_name.split(" ", 1)

        user = User.objects.create_user(
            username=data["username"],
            email=data["email"],
            password=data["password1"],
            first_name=parts[0],
            last_name=parts[1] if len(parts) > 1 else "",
            is_active=False,
        )
        UserProfile.objects.get_or_create(
            user=user,
            defaults={
                "contact_number": data["mobile"],
                "designation": data.get("designation", ""),
            },
        )

        logger.info("New signup pending approval from %s (username=%s)", ip, user.username)
        messages.success(
            request,
            "Your account request has been submitted. An admin will review and approve it before you can log in.",
        )
        return redirect("login")