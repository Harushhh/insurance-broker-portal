from django.core.management.base import BaseCommand

from insurance.tasks import cleanup_points_search_logs


class Command(BaseCommand):
    help = "Delete Motor/Health Points Search audit logs (AuditLog action in MOTOR_POINTS_SEARCH, HEALTH_POINTS_SEARCH) older than 7 days."

    def handle(self, *args, **options):
        deleted_count = cleanup_points_search_logs()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted_count} Points Search log(s) older than 7 days."
            )
        )
