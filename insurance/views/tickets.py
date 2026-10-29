"""Support ticket dashboard and the ticket endpoints the pages call."""
from django.shortcuts import render
from django.http import JsonResponse
from django.db.models import Count
import json
from ..models import ProductMaster, SubProductMaster, FuelTypeMaster, MakeModelClassMaster, SupportTicket
from ..services.tickets import _build_motor_payout_rates_url
import logging


logger = logging.getLogger(__name__)


def ticket_dashboard(request):
    qs = SupportTicket.objects.select_related('user').all()

    # Keyed without hyphens ("FOLLOW-UP" -> FOLLOWUP) since Django template
    # variable lookups can't contain a "-" (`status_counts.FOLLOW-UP` is a
    # TemplateSyntaxError, not a lookup miss).
    raw_counts = {"OPEN": 0, "FOLLOW-UP": 0, "CLOSED": 0}
    for row in qs.values('status').annotate(n=Count('id')):
        if row['status'] in raw_counts:
            raw_counts[row['status']] = row['n']
    status_counts = {
        "OPEN": raw_counts["OPEN"],
        "FOLLOWUP": raw_counts["FOLLOW-UP"],
        "CLOSED": raw_counts["CLOSED"],
    }

    category_counts = {"MOTOR": 0, "HEALTH": 0, "LIFE": 0}
    for row in qs.values('category').annotate(n=Count('id')):
        if row['category'] in category_counts:
            category_counts[row['category']] = row['n']

    type_counts = {code: 0 for code, _ in SupportTicket.TICKET_TYPE_CHOICES}
    for row in qs.values('ticket_type').annotate(n=Count('id')):
        if row['ticket_type'] in type_counts:
            type_counts[row['ticket_type']] = row['n']

    total_tickets = qs.count()

    tickets = list(qs)
    insurer_set = set()
    for ticket in tickets:
        ticket.motor_payout_rates_url = _build_motor_payout_rates_url(ticket)
        # The ticket-modal Insurer dropdown saves into form_payload rather
        # than a dedicated column (see create_ticket_api) — "Ticket Insurer"
        # is the key health tickets use, since their search form already has
        # its own "Insurer" filter captured under the plain "Insurer" key.
        payload = ticket.form_payload or {}
        ticket.insurer_value = payload.get("Insurer") or payload.get("Ticket Insurer") or ""
        if ticket.insurer_value:
            insurer_set.add(ticket.insurer_value)

    insurer_filter_list = sorted(insurer_set)

    return render(request, "ticket_dashboard.html", {
        "tickets": tickets,
        "status_counts": status_counts,
        "category_counts": category_counts,
        "type_counts": type_counts,
        "insurer_filter_list": insurer_filter_list,
        "total_tickets": total_tickets,
    })


def create_ticket_api(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            remarks = data.get("remarks", "").strip()
            form_payload = data.get("form_payload", {})
            category = (data.get("category") or "MOTOR").strip().upper()
            ticket_type = (data.get("ticket_type") or "").strip().upper()

            if not remarks:
                return JsonResponse({"success": False, "message": "Remarks are required."})

            if category not in dict(SupportTicket.CATEGORY_CHOICES):
                category = "MOTOR"

            if ticket_type not in dict(SupportTicket.TICKET_TYPE_CHOICES):
                ticket_type = ""

            # 1. Translate Product ID to Name
            if form_payload.get("Product") and str(form_payload["Product"]).isdigit():
                obj = ProductMaster.objects.filter(id=int(form_payload["Product"])).first()
                if obj: form_payload["Product"] = obj.name
                
            # 2. Translate Sub Product ID to Name
            if form_payload.get("Sub Product") and str(form_payload["Sub Product"]).isdigit():
                obj = SubProductMaster.objects.filter(id=int(form_payload["Sub Product"])).first()
                if obj: form_payload["Sub Product"] = obj.name
                
            # 3. Translate Make Model Class ID to Name
            if form_payload.get("Make Model Class") and str(form_payload["Make Model Class"]).isdigit():
                obj = MakeModelClassMaster.objects.filter(id=int(form_payload["Make Model Class"])).first()
                if obj: form_payload["Make Model Class"] = obj.name
                
            # 4. Translate Fuel ID to Name
            if form_payload.get("Fuel") and str(form_payload["Fuel"]).isdigit():
                obj = FuelTypeMaster.objects.filter(id=int(form_payload["Fuel"])).first()
                if obj: form_payload["Fuel"] = obj.name

            ticket = SupportTicket.objects.create(
                user=request.user,
                remarks=remarks,
                form_payload=form_payload,
                category=category,
                ticket_type=ticket_type,
            )
            return JsonResponse({"success": True, "ticket_id": ticket.id})
        except Exception as e:
            logger.exception("Could not create support ticket")
            return JsonResponse({"success": False, "message": str(e)})
            
    return JsonResponse({"success": False, "message": "Invalid request method."})


def update_ticket_status(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            ticket_id = data.get("ticket_id")
            new_status = data.get("status")

            if not ticket_id or not new_status:
                return JsonResponse({"success": False, "message": "Ticket ID and Status are required."})

            ticket = SupportTicket.objects.get(id=ticket_id)

            valid_statuses = ["OPEN", "FOLLOW-UP", "CLOSED"]
            if new_status.upper() not in valid_statuses:
                return JsonResponse({"success": False, "message": "Invalid status."})

            ticket.status = new_status.upper()
            ticket.save()
            
            return JsonResponse({"success": True})
            
        except SupportTicket.DoesNotExist:
             return JsonResponse({"success": False, "message": "Ticket not found."})
        except Exception as e:
            logger.exception("Could not update support ticket status")
            return JsonResponse({"success": False, "message": str(e)})
            
    return JsonResponse({"success": False, "message": "Invalid request method."})
