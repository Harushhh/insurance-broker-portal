from django.core.management.base import BaseCommand

from insurance.tasks import cleanup_manual_edit_logs


class Command(BaseCommand):
    help = "Delete Security Audit Log entries (AuditLog action=MANUAL EDIT) older than 7 days."

    def handle(self, *args, **options):
        deleted_count = cleanup_manual_edit_logs()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted_count} MANUAL EDIT log(s) older than 7 days."
            )
        )
