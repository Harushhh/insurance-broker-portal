from .views import PAGE_GROUPS


def sidebar_access(request):
    """
    Exposes which pages the current user can see, so base.html's sidebar can
    hide links to pages they don't have access to. Mirrors the same rules as
    page_access_required/staff_required in insurance/urls.py: ADMIN group
    bypasses every per-page check, while is_staff/is_superuser only matters
    for the one staff_required page (Access Control) since page_access_required
    deliberately doesn't give is_superuser a blanket pass.
    """
    user = request.user
    if not user.is_authenticated:
        return {}

    in_admin_group = user.groups.filter(name="ADMIN").exists()
    if in_admin_group:
        nav_pages = set(PAGE_GROUPS)
    else:
        nav_pages = set(user.groups.values_list("name", flat=True)) & set(PAGE_GROUPS)

    return {
        "nav_pages": nav_pages,
        "nav_can_access_control": in_admin_group or user.is_staff or user.is_superuser,
    }
