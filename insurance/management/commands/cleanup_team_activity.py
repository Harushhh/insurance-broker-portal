from django.core.management.base import BaseCommand

from insurance.services.activity import retention_cutoff
from insurance.tasks import cleanup_team_activity


class Command(BaseCommand):
    help = "Delete Team Activity events from before the start of last month (the feed keeps last month plus this month)."

    def handle(self, *args, **options):
        cutoff = retention_cutoff()
        deleted_count = cleanup_team_activity()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted_count} Team Activity event(s) from before {cutoff:%d %b %Y}."
            )
        )
