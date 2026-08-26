from django.core.management.base import BaseCommand

from insurance.tasks import cleanup_security_audit_logs


class Command(BaseCommand):
    help = "Delete Security Audit Log entries (MANUAL EDIT, BULK UPDATE, HEALTH RATE EDIT, HEALTH BULK UPDATE) older than 7 days."

    def handle(self, *args, **options):
        deleted_count = cleanup_security_audit_logs()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted_count} Security Audit Log entry(ies) older than 7 days."
            )
        )
