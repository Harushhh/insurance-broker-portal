"""Special rate requests: submit, review, and update the status of a request."""
from django.contrib import messages
from django.utils import timezone
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.db.models import Count
import json
from ..models import SpecialRateRequest
from ..forms import MgBgRateRequestForm, BgRateRequestForm
import logging


logger = logging.getLogger(__name__)


# =========================================================
# SPECIAL RATE REQUESTS
# =========================================================
def special_rate_requests(request):
    mgbg_form = MgBgRateRequestForm(prefix="mgbg")
    bg_form = BgRateRequestForm(prefix="bg")
    active_section = "MG-BG"

    if request.method == "POST" and "submit_mgbg" in request.POST:
        active_section = "MG-BG"
        mgbg_form = MgBgRateRequestForm(request.POST, request.FILES, prefix="mgbg")
        if mgbg_form.is_valid():
            req_obj = mgbg_form.save(commit=False)
            req_obj.rate_type = "MG-BG"
            req_obj.requested_by = request.user
            req_obj.save()
            messages.success(request, f"✅ Your Special MG-BG request (Entry No {req_obj.entry_no}) has been submitted.")
            return redirect("special_rate_requests")
        messages.error(request, "⚠️ Please correct the errors below and resubmit.")

    elif request.method == "POST" and "submit_bg" in request.POST:
        active_section = "BG"
        bg_form = BgRateRequestForm(request.POST, request.FILES, prefix="bg")
        if bg_form.is_valid():
            req_obj = bg_form.save(commit=False)
            req_obj.rate_type = "BG"
            req_obj.requested_by = request.user
            req_obj.save()
            messages.success(request, f"✅ Your Special BG request (Entry No {req_obj.entry_no}) has been submitted.")
            return redirect("special_rate_requests")
        messages.error(request, "⚠️ Please correct the errors below and resubmit.")

    my_requests = SpecialRateRequest.objects.filter(requested_by=request.user).order_by("-created_at")

    return render(request, "special_rate_requests.html", {
        "mgbg_form": mgbg_form,
        "bg_form": bg_form,
        "active_section": active_section,
        "my_requests": my_requests,
    })


def special_rate_requests_review(request):
    requests_qs = SpecialRateRequest.objects.select_related("requested_by", "reviewed_by").all()

    status_counts = {"PENDING": 0, "APPROVED": 0, "REJECTED": 0}
    for row in requests_qs.values("status").annotate(n=Count("id")):
        if row["status"] in status_counts:
            status_counts[row["status"]] = row["n"]

    # Keyed without the hyphen ("MG-BG" -> MGBG) since Django template variable
    # lookups can't contain a "-" (`rate_type_counts.MG-BG` is a
    # TemplateSyntaxError, not a lookup miss).
    raw_type_counts = {"MG-BG": 0, "BG": 0}
    for row in requests_qs.values("rate_type").annotate(n=Count("id")):
        if row["rate_type"] in raw_type_counts:
            raw_type_counts[row["rate_type"]] = row["n"]
    rate_type_counts = {
        "MGBG": raw_type_counts["MG-BG"],
        "BG": raw_type_counts["BG"],
    }

    # Every request's approver set is one of exactly two fixed lists (Motor
    # vs. everything else), so resolve both once instead of querying per row.
    approvers_by_product = {
        "MOTOR": list(SpecialRateRequest.assigned_approvers_for_product("MOTOR")),
        "DEFAULT": list(SpecialRateRequest.assigned_approvers_for_product("LIFE")),
    }
    is_full_admin = request.user.groups.filter(name="ADMIN").exists()

    requests_list = list(requests_qs)
    for req in requests_list:
        approvers = approvers_by_product["MOTOR" if req.product == "MOTOR" else "DEFAULT"]
        req.assigned_approver_names = ", ".join(u.get_full_name() or u.username for u in approvers)
        req.can_review = is_full_admin or any(u.pk == request.user.pk for u in approvers)

    return render(request, "special_rate_requests_review.html", {
        "requests": requests_list,
        "status_counts": status_counts,
        "rate_type_counts": rate_type_counts,
        "total_requests": requests_qs.count(),
    })


def update_special_rate_request_status(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            request_id = data.get("request_id")
            new_status = (data.get("status") or "").upper()

            if not request_id or not new_status:
                return JsonResponse({"success": False, "message": "Request ID and status are required."})

            valid_statuses = ["PENDING", "APPROVED", "REJECTED"]
            if new_status not in valid_statuses:
                return JsonResponse({"success": False, "message": "Invalid status."})

            req_obj = SpecialRateRequest.objects.get(id=request_id)
            if not req_obj.can_be_reviewed_by(request.user):
                return JsonResponse({"success": False, "message": "You are not an assigned approver for this request."}, status=403)

            req_obj.status = new_status
            req_obj.reviewed_by = request.user
            req_obj.reviewed_at = timezone.now()
            req_obj.save()

            return JsonResponse({"success": True})

        except SpecialRateRequest.DoesNotExist:
            return JsonResponse({"success": False, "message": "Request not found."})
        except Exception as e:
            logger.exception("Could not update special rate request status")
            return JsonResponse({"success": False, "message": str(e)})
