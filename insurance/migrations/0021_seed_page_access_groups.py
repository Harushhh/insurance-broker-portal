from django.db import migrations


# Mirrors insurance/views.py PAGE_GROUPS at the time this migration was
# written. Migrations shouldn't import from app code that can change later,
# so this list is a frozen copy, not a live reference.
PAGE_GROUPS = [
    "Can_View_Dashboard",
    "Can_View_Analysis",
    "Can_View_Motor_Payout_Rates",
    "Can_Upload_CSV",
    "Can_View_RTO_Dashboard",
    "Can_View_Make_Model_Dashboard",
    "Can_View_Audit_Log",
    "Can_View_Grid_Management",
    "Can_View_Alias_Management",
    "Can_View_Tickets",
    "Can_View_Policy_Locker",
    "Can_View_Locked_Policies",
    "Can_View_Motor_Points_Logs",
    "Can_View_AI_Rulebook",
    "Can_View_OCR_Pipeline",
    "Can_View_MIS_Register",
    "Can_View_MIS_Payout_Engine",
]


def seed_page_access(apps, schema_editor):
    """
    Real page-access enforcement (page_access_required in insurance/urls.py)
    goes live alongside this migration. Before it, every login_required page
    was open to any logged-in user regardless of the Access Control
    checkboxes, and the ADMIN group didn't unlock the staff_required pages
    either. Seed every existing active user with the access they effectively
    already had, so nobody is locked out the moment enforcement starts —
    admins dial individual access down from here via Access Control
    (/user-management/).
    """
    Group = apps.get_model('auth', 'Group')
    User = apps.get_model('auth', 'User')

    admin_group, _ = Group.objects.get_or_create(name="ADMIN")
    page_group_objs = []
    for name in PAGE_GROUPS:
        group, _ = Group.objects.get_or_create(name=name)
        page_group_objs.append(group)

    for user in User.objects.filter(is_active=True):
        user.groups.add(*page_group_objs)
        if user.is_staff or user.is_superuser:
            user.groups.add(admin_group)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('insurance', '0020_add_ratemaster_payout_sort_index'),
    ]

    operations = [
        migrations.RunPython(seed_page_access, noop),
    ]
