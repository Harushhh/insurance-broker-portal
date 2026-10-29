"""Unified home dashboard."""
from django.utils import timezone
from django.shortcuts import render
from django.db.models import Q, Count, Sum, Case, When, Value, CharField
from datetime import timedelta
from ..models import PolicyMISRecord, LockedPolicy, SupportTicket


# =========================================================
# UNIFIED HOME DASHBOARD
# =========================================================
def home_dashboard(request):
    is_admin_user = True
    qs = PolicyMISRecord.objects.select_related("source_document", "source_document__uploaded_by")

    total_cases = qs.count()
    total_premium = qs.aggregate(total=Sum('gross_premium'))['total'] or 0

    product_mix_query = qs.annotate(
        category=Case(
            When(
                Q(vehicle_registration_number__isnull=False) & ~Q(vehicle_registration_number=""), 
                then=Value('Motor')
            ),
            When(
                Q(vehicle_make__isnull=False) & ~Q(vehicle_make=""), 
                then=Value('Motor')
            ),
            default=Value('Health'),
            output_field=CharField(),
        )
    ).values('category').annotate(count=Count('id'))

    product_mix = {item['category']: item['count'] for item in product_mix_query}
    motor_count = product_mix.get('Motor', 0)
    health_count = product_mix.get('Health', 0)

    recent_activity = qs.order_by('-created_at')[:5]

    my_tickets_qs = SupportTicket.objects.filter(
        user=request.user,
        created_at__gte=timezone.now() - timedelta(hours=72)
    ).order_by('-created_at')
    my_open_ticket_count = my_tickets_qs.exclude(status="CLOSED").count()
    my_tickets = my_tickets_qs[:20]

    my_locked_qs = LockedPolicy.objects.filter(locked_by=request.user, status="LOCKED").order_by('-locked_at')
    my_locked_count = my_locked_qs.count()
    my_locked_policies = my_locked_qs[:20]

    context = {
        'is_admin_user': is_admin_user,
        'total_cases': total_cases,
        'total_premium': total_premium,
        'motor_count': motor_count,
        'health_count': health_count,
        'recent_activity': recent_activity,
        'my_tickets': my_tickets,
        'my_open_ticket_count': my_open_ticket_count,
        'my_locked_policies': my_locked_policies,
        'my_locked_count': my_locked_count,
        'my_notification_count': my_open_ticket_count + my_locked_count,
    }
    return render(request, 'insurance/home.html', context)
