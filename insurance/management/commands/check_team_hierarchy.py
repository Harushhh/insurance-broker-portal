"""
Read-only check of the Reports To data behind the Team Activity page.

Before the Team Activity page existed nothing read `UserProfile.reports_to`, and
the "Reports To" dropdown in Access Control posted a USER id into a field that
points at a USER PROFILE. The two ids are the same number only while every
account got its profile at the same moment it was created, so some stored links
may point at the wrong person. This lists every link with both names so an admin
can see each one and re-save the wrong ones in Access Control (which now stores
the right row).

Also reports: accounts with no profile (they cannot manage anyone until they are
saved once in Access Control), links to a deactivated manager, loops, and
self-links. Prints a report; writes nothing.
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from insurance.models import UserProfile


class Command(BaseCommand):
    help = "Read-only: list every Reports To link and flag the ones that look wrong. Writes nothing."

    def handle(self, *args, **options):
        out = self.stdout.write
        profiles = list(UserProfile.objects.select_related("user", "reports_to__user").order_by("user__username"))
        users_without_profile = list(
            User.objects.filter(profile__isnull=True, is_active=True).order_by("username").values_list("username", flat=True)
        )
        drifted = [p for p in profiles if p.id != p.user_id]
        links = [p for p in profiles if p.reports_to_id]

        out(f"Profiles: {len(profiles)}.  With a manager: {len(links)}.")
        out(f"Profiles whose id differs from their user's id: {len(drifted)}"
            + ("  (the dropdown fix matters for these)" if drifted else ""))
        out("")

        problems = 0
        if links:
            out("Reports To links (person -> manager):")
        manager_of = {p.id: p.reports_to_id for p in links}
        for p in links:
            notes = []
            is_problem = False
            manager = p.reports_to
            if p.reports_to_id == p.id:
                notes.append("REPORTS TO THEMSELVES")
                is_problem = True
            elif self._in_loop(p.id, manager_of):
                notes.append("PART OF A LOOP")
                is_problem = True
            if not manager.user.is_active:
                notes.append("manager is deactivated")
                is_problem = True
            if p.id != p.user_id or manager.id != manager.user_id:
                # Informational, not counted: the link may well be right.
                notes.append("ids differ: check this is the manager you meant")
            problems += is_problem
            flag = ("   <-- " + "; ".join(notes)) if notes else ""
            out(f"  {p.user.username} -> {manager.user.username}   "
                f"(profile id {p.id}, manager profile id {manager.id}){flag}")

        if users_without_profile:
            out("")
            out(f"Active accounts with no profile ({len(users_without_profile)}): "
                + ", ".join(users_without_profile))
            out("  They cannot manage anyone, or be placed under a manager, until someone saves them once "
                "in Access Control.")

        out("")
        if problems:
            self.stdout.write(self.style.WARNING(
                f"{problems} link(s) need a look. Re-save them in Access Control (Teams Hierarchy > Reports To)."))
        else:
            self.stdout.write(self.style.SUCCESS("No loops, self-links or deactivated managers found."))

    @staticmethod
    def _in_loop(start_id, manager_of):
        """True when walking up from `start_id` comes back to it."""
        seen = set()
        current = manager_of.get(start_id)
        while current is not None and current not in seen:
            if current == start_id:
                return True
            seen.add(current)
            current = manager_of.get(current)
        return False
