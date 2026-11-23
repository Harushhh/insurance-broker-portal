"""Who may read whose activity: the one place the Team Activity page's row rule lives.

Two layers decide what a person sees on /team-activity/. The page layer is the
`Can_View_Team_Activity` group (access.py). This module is the row layer: it
answers "which events may THIS user read", and everything that shows events (the
page, the Excel export, any future API) must start from `visible_events(user)`
and only narrow it further. Never query `ActivityEvent` directly for a person,
and never trust a person id that came from the URL: a filter on an already
scoped queryset returns nothing for someone outside the scope, which is the
point. `test_team_activity.StructureTests` fails if a view reaches around this.

The rule (agreed 2026-11-23):
- the hierarchy is `UserProfile.reports_to` (one manager per person, a tree);
- a manager sees their whole subtree: direct reports and theirs, recursively,
  but not their own activity and never a peer's or a boss's;
- ADMIN (the group, like page_access_required) sees every team;
- fail closed: no profile, nobody reporting to you, or not signed in = nothing.

This module reads models only; it never imports views or the API.
"""
from collections import defaultdict

from django.contrib.auth.models import User

from ..models import ActivityEvent, UserProfile


def is_admin(user):
    """In the ADMIN group, the same test page_access_required applies."""
    return bool(user and user.is_authenticated and user.groups.filter(name="ADMIN").exists())


def team_user_ids(manager):
    """User ids of everyone below `manager`: direct reports, theirs, and so on.

    The manager is never in their own set, even if the data has a loop through
    them. One query reads every link; the walk is in Python with a visited set,
    so a loop elsewhere in the data cannot hang it. Fine for an org this size.
    """
    manager_id = getattr(manager, "pk", None)
    if manager_id is None:
        return set()
    children = defaultdict(list)
    links = UserProfile.objects.filter(reports_to__isnull=False).values_list("reports_to__user_id", "user_id")
    for manager_user_id, member_user_id in links:
        children[manager_user_id].append(member_user_id)

    seen = {manager_id}
    found = set()
    stack = [manager_id]
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in seen:
                seen.add(child)
                found.add(child)
                stack.append(child)
    return found


def visible_events(user):
    """Every ActivityEvent `user` may read. The only entry point for reading events."""
    if not user or not user.is_authenticated:
        return ActivityEvent.objects.none()
    if is_admin(user):
        return ActivityEvent.objects.all()
    ids = team_user_ids(user)
    if not ids:
        return ActivityEvent.objects.none()
    return ActivityEvent.objects.filter(actor_id__in=ids)


def visible_people(user, team_of=None):
    """The people `user` can pick in the Person filter and see counted, as User rows.

    A manager gets their whole subtree, including people with no activity (a
    quiet teammate is worth seeing). An admin gets everyone who has any event,
    or, with `team_of` (a manager), that manager's whole team. `team_of` means
    nothing to anyone else: it never widens a non-admin's scope.
    """
    if not user or not user.is_authenticated:
        return User.objects.none()
    if is_admin(user):
        if team_of is not None:
            return User.objects.filter(pk__in=team_user_ids(team_of)).select_related("profile")
        return User.objects.filter(activity_events__isnull=False).distinct().select_related("profile")
    return User.objects.filter(pk__in=team_user_ids(user)).select_related("profile")


def managers_with_teams():
    """Users who have at least one direct report: the Team filter's choices (admins only)."""
    return User.objects.filter(profile__team_members__isnull=False).distinct().order_by("username")
