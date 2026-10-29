"""User management and per-page permissions (`/user-management/`)."""
from django.contrib.auth.models import User, Group
from django.shortcuts import render
from django import forms
import logging
import secrets
from ..access import PAGE_GROUPS, RATE_CHECKER_GROUPS
from ..models import UserProfile


logger = logging.getLogger("security")


# -------------------------
# User Management
# -------------------------
# Editable Teams-hierarchy fields on UserProfile (excludes user/contact_number/designation,
# which already have dedicated inputs in the edit modal).
HIERARCHY_FIELDS = [
    "vertical_path", "team", "team_id", "user_type", "emp_id", "code", "role",
    "branch_code", "branch_name",
    "rm_code", "rm_name", "tc_code", "tc_name", "csc_code", "csc_name",
    "posp_code", "posp_name",
    "agent_type",
    "pan", "bank_account", "bank_name", "ifsc",
    "membership_id", "qc_verticals",
    # NOTE: user_id_code is deliberately excluded — it's system-assigned
    # (UserProfile.save() auto-fills it) and unique, not admin-editable.
]


HIERARCHY_FLAGS = ["is_qc", "is_plvc", "personal_qc"]


HIERARCHY_FIELD_LABELS = [
    ("vertical_path", "Vertical Path"), ("team", "Team"), ("team_id", "Team ID"),
    ("user_type", "User Type"), ("emp_id", "Emp ID"), ("code", "Code"), ("role", "Role"),
    ("branch_code", "Branch Code"), ("branch_name", "Branch Name"),
    ("rm_code", "RM Code"), ("rm_name", "RM Name"),
    ("tc_code", "TC Code"), ("tc_name", "TC Name"),
    ("csc_code", "CSC Code"), ("csc_name", "CSC Name"),
    ("posp_code", "Ref/POSP Code"), ("posp_name", "Ref/POSP Name"),
    ("agent_type", "Agent Type"),
    ("pan", "PAN"), ("bank_account", "Bank Account"), ("bank_name", "Bank Name"), ("ifsc", "IFSC"),
    ("membership_id", "Membership ID"), ("qc_verticals", "QC Verticals"),
]


def user_management(request):
    Group.objects.get_or_create(name="ADMIN")
    # Deliberately not offered as a checkbox below -- grant it via Django's
    # own /admin/ site. See super_admin_required in access.py.
    Group.objects.get_or_create(name="SUPER_ADMIN")
    for group_name in PAGE_GROUPS:
        Group.objects.get_or_create(name=group_name)

    msg = ""
    error = ""

    if request.method == "POST":
        action = request.POST.get("action")
        user_id = request.POST.get("user_id")

        if action == "create_user":
            uname = request.POST.get("new_username", "").strip()
            fname = request.POST.get("full_name", "").strip()
            uemail = request.POST.get("email", "").strip()
            ucontact = request.POST.get("contact_number", "").strip()
            upass = secrets.token_urlsafe(9)  # random one-time password, not a shared constant

            if uname:
                if User.objects.filter(username=uname).exists():
                    error = f"⚠️ User '{uname}' already exists."
                elif uemail and User.objects.filter(email__iexact=uemail).exists():
                    error = f"⚠️ An account with email '{uemail}' already exists."
                else:
                    new_user = User.objects.create_user(username=uname, email=uemail, password=upass)
                    if fname:
                        parts = fname.split(" ", 1)
                        new_user.first_name = parts[0]
                        if len(parts) > 1:
                            new_user.last_name = parts[1]
                    new_user.save()

                    UserProfile.objects.get_or_create(user=new_user, defaults={"contact_number": ucontact})
                    msg = f"✅ User '{uname}' created successfully."

        elif action == "update_user_access":
            u = User.objects.get(id=user_id)
            u.groups.remove(Group.objects.get(name="ADMIN"))
            for pg in PAGE_GROUPS:
                u.groups.remove(Group.objects.get(name=pg))

            selected_pages = set(request.POST.getlist(f"pages_{user_id}"))
            if "Rate_Checker" in selected_pages:
                selected_pages.discard("Rate_Checker")
                selected_pages |= RATE_CHECKER_GROUPS
            for pg in selected_pages:
                u.groups.add(Group.objects.get(name=pg))

            profile, _ = UserProfile.objects.get_or_create(user=u)
            profile.contact_number = request.POST.get("contact_number", "")
            profile.designation = request.POST.get("designation", "")

            for field in HIERARCHY_FIELDS:
                profile.__dict__[field] = request.POST.get(field, "").strip() or None
            for flag in HIERARCHY_FLAGS:
                setattr(profile, flag, request.POST.get(flag) == "on")

            reports_to_id = request.POST.get("reports_to") or None
            profile.reports_to_id = reports_to_id if reports_to_id else None

            new_email = request.POST.get("email", "").strip()
            if new_email and User.objects.filter(email__iexact=new_email).exclude(id=u.id).exists():
                error = f"⚠️ An account with email '{new_email}' already exists."
            else:
                try:
                    profile.full_clean(exclude=["user"])
                    profile.save()
                    u.email = new_email
                    u.save()
                    msg = f"✅ Profile and access for '{u.username}' updated."
                except forms.ValidationError as e:
                    error = "⚠️ " + " ".join(m for msgs in e.message_dict.values() for m in msgs)

        elif action == "approve_user" and user_id:
            u = User.objects.get(id=user_id)
            u.is_active = True
            u.save()
            msg = f"✅ '{u.username}' approved — they can now log in."

        elif action == "reject_user" and user_id:
            u = User.objects.get(id=user_id)
            uname = u.username
            u.delete()
            msg = f"🗑️ Signup request from '{uname}' rejected and removed."

        elif action == "reset_password" and user_id:
            u = User.objects.get(id=user_id)
            default_password = secrets.token_urlsafe(9)  # random one-time password, not a shared constant
            u.set_password(default_password)
            u.save()
            logger.info("Password reset by admin for user '%s'", u.username)
            msg = f"✅ Password for '{u.username}' reset to '{default_password}'. Share it with them directly."

        elif action == "make_admin" and user_id:
            u = User.objects.get(id=user_id)
            u.groups.clear()
            u.groups.add(Group.objects.get(name="ADMIN"))
            msg = f"✅ User '{u.username}' promoted to Full Admin."

        elif action == "delete_user" and user_id:
            u = User.objects.get(id=user_id)
            if u.is_superuser:
                error = "⚠️ You cannot delete a Super Admin account."
            else:
                uname = u.username
                u.delete()
                msg = f"🗑️ User '{uname}' has been completely removed from the system."

    all_users = User.objects.select_related("profile").all().order_by("-is_superuser", "username")
    users = [u for u in all_users if u.is_active]
    pending_users = [u for u in all_users if not u.is_active]

    def profile_dict(u):
        profile = getattr(u, "profile", None)
        d = {
            "id": u.id,
            "username": u.username,
            "full_name": u.get_full_name(),
            "email": u.email,
            "contact_number": profile.contact_number if profile else "",
            "designation": profile.designation if profile else "",
            "is_superuser": u.is_superuser,
            "is_admin": u.groups.filter(name="ADMIN").exists(),
            # Filtered to PAGE_GROUPS -- otherwise this leaks any group the
            # user happens to belong to, including ones that no longer mean
            # anything (like the retired Can_Manage_Life_Payout_Grid) or
            # aren't meant to show here at all (ADMIN, SUPER_ADMIN).
            "pages": list(u.groups.filter(name__in=PAGE_GROUPS).values_list("name", flat=True)),
            "user_id_code": profile.user_id_code if profile else "",
        }
        for field in HIERARCHY_FIELDS:
            d[field] = getattr(profile, field, "") or ""
        for flag in HIERARCHY_FLAGS:
            d[flag] = bool(getattr(profile, flag, False))
        d["reports_to"] = profile.reports_to_id if profile else None
        return d

    user_rows = [profile_dict(u) for u in users]
    pending_rows = [
        {
            "id": u.id,
            "username": u.username,
            "full_name": u.get_full_name(),
            "email": u.email,
            "contact_number": u.profile.contact_number if hasattr(u, "profile") else "",
            "designation": u.profile.designation if hasattr(u, "profile") else "",
            "date_joined": u.date_joined,
        }
        for u in pending_users
    ]

    manager_choices = [{"id": u.id, "username": u.username} for u in users]

    return render(request, "user_management.html", {
        "users": user_rows,
        "pending_users": pending_rows,
        "manager_choices": manager_choices,
        "page_groups": PAGE_GROUPS,
        "hierarchy_field_labels": HIERARCHY_FIELD_LABELS,
        "msg": msg,
        "error": error
    })
