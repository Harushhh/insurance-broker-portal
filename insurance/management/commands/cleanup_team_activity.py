from django.conf import settings
from django.core.management.base import BaseCommand

from insurance.tasks import cleanup_team_activity


class Command(BaseCommand):
    help = "Delete Team Activity events older than TEAM_ACTIVITY_RETENTION_DAYS (90 by default)."

    def handle(self, *args, **options):
        deleted_count = cleanup_team_activity()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted_count} Team Activity event(s) older than "
                f"{settings.TEAM_ACTIVITY_RETENTION_DAYS} days."
            )
        )
