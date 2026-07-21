"""
One-time cutover helper: copies every FileField currently sitting on local
disk (MEDIA_ROOT) up to whatever storage backend STORAGES["default"] now
points at (Cloudflare R2, once settings.py is deployed with that change).

Deliberately does not touch the database. Every FileField's stored value is
just a relative key like "policy_uploads/2026/07/foo.pdf" — that means the
same thing regardless of which backend serves it, so there is nothing to
migrate on the model side, only the underlying file bytes.

Run order matters: this only does something useful once the STORAGES/AWS_*
settings change is already deployed (so default_storage actually points at
R2). Run it immediately after that deploy, before relying on any download
link for a record created before the cutover.

Safe to re-run: already-migrated files are skipped, so an interrupted run
(network blip, etc.) can just be run again to pick up where it left off.
"""
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, default_storage
from django.core.management.base import BaseCommand

from insurance.models import GridDocument, PolicyDocumentUpload, MISFile

FIELDS_TO_MIGRATE = [
    (GridDocument, "uploaded_file"),
    (PolicyDocumentUpload, "uploaded_file"),
    (MISFile, "uploaded_file"),
    (MISFile, "processed_file"),
]


class Command(BaseCommand):
    help = (
        "Copy every FileField currently on local disk up to the configured "
        "object storage backend. Does not modify any database rows."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be copied without uploading anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        # Always read from local disk here regardless of what default_storage
        # currently points at, so this keeps working correctly even after
        # the cutover to R2 is live.
        local_storage = FileSystemStorage()

        copied = skipped = missing = 0

        for model, field_name in FIELDS_TO_MIGRATE:
            queryset = model.objects.exclude(**{field_name: ""}).exclude(
                **{f"{field_name}__isnull": True}
            )
            for obj in queryset:
                name = getattr(obj, field_name).name
                if not name:
                    continue

                label = f"{model.__name__}.{field_name} -> {name}"

                if not local_storage.exists(name):
                    self.stderr.write(self.style.WARNING(f"MISSING on local disk: {label}"))
                    missing += 1
                    continue

                if default_storage.exists(name):
                    skipped += 1
                    continue

                if dry_run:
                    self.stdout.write(f"Would copy: {label}")
                    continue

                with local_storage.open(name, "rb") as f:
                    default_storage.save(name, ContentFile(f.read()))
                copied += 1
                self.stdout.write(f"Copied: {label}")

        summary = f"Done. Copied {copied}, already present {skipped}, missing locally {missing}."
        if dry_run:
            summary = f"[DRY RUN] {summary} (nothing was actually uploaded)"
        self.stdout.write(self.style.SUCCESS(summary))
