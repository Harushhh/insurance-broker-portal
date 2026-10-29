"""Print who may open each route, as a Markdown table.

    python manage.py show_access_matrix

Read-only: it inspects the URL table and the decorators in insurance/access.py and changes nothing.
docs/access-control.md embeds this output; regenerate that section after adding or re-gating a route.
"""
from django.contrib.auth.decorators import login_required
from django.core.management.base import BaseCommand
from django.urls import URLResolver, get_resolver
from rest_framework_api_key.permissions import HasAPIKey

from insurance import access


def _inner_code(decorator, *args):
    """Code object of the decorator's own wrapper (the one sitting under login_required)."""
    wrapped = decorator(*args)(lambda request: None) if args else decorator(lambda request: None)
    return wrapped.__wrapped__.__code__


PAGE_CODE = _inner_code(access.page_access_required, "any_group")
STAFF_CODE = _inner_code(access.staff_required)
SUPER_ADMIN_CODE = _inner_code(access.super_admin_required)
LOGIN_CODE = login_required(lambda request: None).__code__


def _walk(patterns, prefix=""):
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from _walk(pattern.url_patterns, prefix + str(pattern.pattern))
        else:
            yield prefix + str(pattern.pattern), pattern


def gates_for(callback):
    """Human-readable description of what a route's view is wrapped in."""
    chain, seen, fn = [], set(), callback
    while fn is not None and id(fn) not in seen:
        seen.add(id(fn))
        chain.append(fn)
        fn = getattr(fn, "__wrapped__", None)

    gates = []
    for fn in chain:
        code = getattr(fn, "__code__", None)
        if code is PAGE_CODE:
            cells = dict(zip(fn.__code__.co_freevars, (cell.cell_contents for cell in fn.__closure__)))
            gates.append(f"group `{cells['group_name']}` or ADMIN")
        elif code is STAFF_CODE:
            gates.append("staff, superuser or ADMIN")
        elif code is SUPER_ADMIN_CODE:
            gates.append("SUPER_ADMIN only")

    view_class = getattr(chain[-1], "cls", None) or getattr(callback, "cls", None)
    if view_class is not None and HasAPIKey in (getattr(view_class, "permission_classes", None) or ()):
        gates.append("API key")
    if not gates and any(getattr(fn, "__code__", None) is LOGIN_CODE for fn in chain):
        gates.append("any signed-in user")
    return " + ".join(gates) or "public"


class Command(BaseCommand):
    help = "Print a Markdown table of who may open each route."

    def handle(self, *args, **options):
        rows = []
        for route, pattern in _walk(get_resolver().url_patterns):
            if route.startswith(("admin/", "^media/")):  # Django admin; DEBUG-only media serving
                continue
            gate = gates_for(pattern.callback)
            if pattern.name == "catch_all":
                gate = "public (redirects to the login page)"
            rows.append((route, pattern.name or "", gate))
        rows.sort()

        write = self.stdout.write
        write("| Route | Name | Who can open it |")
        write("|---|---|---|")
        for route, name, gate in rows:
            write(f"| `/{route}` | `{name}` | {gate} |")
        write("")
        write("`/admin/...` is Django's own admin site (staff accounts). With `DEBUG=True` Django also serves "
              "`/media/...` without a login; production serves media from object storage instead.")
