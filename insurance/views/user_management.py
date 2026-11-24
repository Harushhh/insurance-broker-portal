"""User management and per-page permissions (`/user-management/`)."""
from django.contrib.auth.models import User, Group
from django.shortcuts import render
from django import forms
import logging
import secrets
from ..access import PAGE_GROUPS, RATE_CHECKER_GROUPS
from ..models import UserProfile
from ..services.activity import EDIT, audit_change, plain, record_activity, snapshot_changes


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


# What a save in Access Control can change, as the Team Activity page records it. PAN and bank details are logged as
# "changed" only, never with their values: this page is read by managers.
PROFILE_LOGGED_FIELDS = ["contact_number", "designation", *HIERARCHY_FIELDS, *HIERARCHY_FLAGS]
SENSITIVE_PROFILE_FIELDS = {"pan", "bank_account", "bank_name", "ifsc"}


def _page_access(user):
    """The page groups (and ADMIN) a user holds, sorted."""
    return sorted(user.groups.filter(name__in=[*PAGE_GROUPS, "ADMIN"]).values_list("name", flat=True))


def _access_state(user, profile):
    state = {"email": user.email, "page_access": _page_access(user)}
    for field in PROFILE_LOGGED_FIELDS:
        state[field] = plain(getattr(profile, field, None))
    return state


def _access_changes(before, after):
    changes = snapshot_changes(before, after)
    for name in SENSITIVE_PROFILE_FIELDS & set(changes):
        changes[name] = {"from": "(value not logged)", "to": "(changed; value not logged)"}
    return changes


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
                    record_activity(
                        request.user, EDIT, "user.created", f"Created the user '{uname}'", target=new_user,
                        # Never the one-time password.
                        details={"username": uname, "full_name": fname, "email": uemail, "contact_number": ucontact},
                    )
                    msg = f"✅ User '{uname}' created successfully."

        elif action == "update_user_access":
            u = User.objects.get(id=user_id)
            earlier_profile = UserProfile.objects.filter(user=u).first() or UserProfile(user=u)
            before_state = _access_state(u, earlier_profile)         # read first: the groups are changed just below
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

            # "Reports To" posts the manager's USER id (that is what the dropdown
            # lists), but UserProfile.reports_to points at a UserProfile, whose id
            # is not always the same number (an account made through Django's own
            # admin has no profile until someone saves it here). Translate it.
            # No "reports_to" key at all means the select had no option to show
            # (e.g. the current manager was deactivated since): keep what is stored.
            previous_manager_id = profile.reports_to_id
            previous_manager_name = profile.reports_to.user.username if previous_manager_id else ""
            manager_error = ""
            if "reports_to" in request.POST:
                manager_user_id = request.POST.get("reports_to", "").strip()
                if not manager_user_id:
                    profile.reports_to = None
                else:
                    manager_user = (
                        User.objects.filter(id=manager_user_id, is_active=True).first()
                        if manager_user_id.isdigit() else None
                    )
                    if manager_user is None:
                        manager_error = "⚠️ The person chosen in Reports To does not exist or is not active."
                    else:
                        profile.reports_to, _ = UserProfile.objects.get_or_create(user=manager_user)

            new_email = request.POST.get("email", "").strip()
            profile_saved = False
            if manager_error:
                error = manager_error
            elif new_email and User.objects.filter(email__iexact=new_email).exclude(id=u.id).exists():
                error = f"⚠️ An account with email '{new_email}' already exists."
            else:
                try:
                    profile.full_clean(exclude=["user"])
                    profile.save()
                    u.email = new_email
                    u.save()
                    profile_saved = True
                    msg = f"✅ Profile and access for '{u.username}' updated."
                    if profile.reports_to_id != previous_manager_id:
                        # Reports To decides whose activity a manager can read on
                        # the Team Activity page, so every change is on the record.
                        new_manager_name = profile.reports_to.user.username if profile.reports_to_id else ""
                        audit_change(
                            request.user, "TEAM CHANGE",
                            f"'{u.username}' now reports to {new_manager_name or 'nobody'} "
                            f"(was {previous_manager_name or 'nobody'}).",
                            summary=f"'{u.username}' now reports to {new_manager_name or 'nobody'}", target=u,
                            details={"person": u.username,
                                     "reports_to": {"from": previous_manager_name or None,
                                                    "to": new_manager_name or None}},
                        )
                except forms.ValidationError as e:
                    error = "⚠️ " + " ".join(m for msgs in e.message_dict.values() for m in msgs)

            # Everything this save changed: the page access (applied above even when the profile is refused) and, when it
            # was saved, the profile and email.
            after_state = _access_state(u, profile) if profile_saved else {**before_state, "page_access": _page_access(u)}
            access_changes = _access_changes(before_state, after_state)
            if access_changes:
                record_activity(
                    request.user, EDIT, "user.access.updated", f"Updated the access and profile of '{u.username}'",
                    target=u, details={"user": u.username, "profile_saved": profile_saved, "changes": access_changes},
                )

        elif action == "approve_user" and user_id:
            u = User.objects.get(id=user_id)
            u.is_active = True
            u.save()
            record_activity(
                request.user, EDIT, "user.approved", f"Approved the signup of '{u.username}'", target=u,
                details={"user": u.username, "email": u.email, "is_active": {"from": False, "to": True}},
            )
            msg = f"✅ '{u.username}' approved — they can now log in."

        elif action == "reject_user" and user_id:
            u = User.objects.get(id=user_id)
            uname = u.username
            rejected = {"user": uname, "email": u.email, "full_name": u.get_full_name()}
            u.delete()
            record_activity(request.user, EDIT, "user.rejected", f"Rejected the signup of '{uname}'", details=rejected)
            msg = f"🗑️ Signup request from '{uname}' rejected and removed."

        elif action == "reset_password" and user_id:
            u = User.objects.get(id=user_id)
            default_password = secrets.token_urlsafe(9)  # random one-time password, not a shared constant
            u.set_password(default_password)
            u.save()
            logger.info("Password reset by admin for user '%s'", u.username)
            # Never the new password.
            record_activity(
                request.user, EDIT, "user.password_reset", f"Reset the password of '{u.username}'", target=u,
                details={"user": u.username},
            )
            msg = f"✅ Password for '{u.username}' reset to '{default_password}'. Share it with them directly."

        elif action == "make_admin" and user_id:
            u = User.objects.get(id=user_id)
            groups_before = sorted(u.groups.values_list("name", flat=True))
            u.groups.clear()
            u.groups.add(Group.objects.get(name="ADMIN"))
            record_activity(
                request.user, EDIT, "user.made_admin", f"Promoted '{u.username}' to Full Admin", target=u,
                details={"user": u.username, "groups": {"from": groups_before, "to": ["ADMIN"]}},
            )
            msg = f"✅ User '{u.username}' promoted to Full Admin."

        elif action == "delete_user" and user_id:
            u = User.objects.get(id=user_id)
            if u.is_superuser:
                error = "⚠️ You cannot delete a Super Admin account."
            else:
                uname = u.username
                removed = {"user": uname, "email": u.email, "full_name": u.get_full_name(),
                           "page_access": _page_access(u)}
                u.delete()
                record_activity(request.user, EDIT, "user.deleted", f"Deleted the user '{uname}'", details=removed)
                msg = f"🗑️ User '{uname}' has been completely removed from the system."

    all_users = User.objects.select_related("profile__reports_to").all().order_by("-is_superuser", "username")
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
        # The manager's USER id, to match the dropdown's option values (see the
        # save side in update_user_access).
        d["reports_to"] = profile.reports_to.user_id if profile and profile.reports_to_id else None
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
