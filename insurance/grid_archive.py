"""
Rolling retention and restore for Grid Management uploads (GridDocument).

Active window: the current month plus the previous GRID_RETENTION_MONTHS - 1.
With 3 months, on 1 Oct that is Aug/Sep/Oct, so July and anything older is
archived. Selection is "uploaded_date before the window", not "exactly one
month back", so a run that was missed (beat down, R2 outage) is caught up by
the next one.

Archive layout, in STORAGES["grid_archive"]:

    <YYYY-MM>/files/<original storage key>    the uploaded file, byte for byte
    <YYYY-MM>/manifest-<UTC timestamp>.json   one entry per document

A manifest is written once per run and never modified, so a partial run
followed by a later one can't overwrite what an earlier run recorded; a
restore reads every manifest in the month. Manifests carry everything needed
to rebuild the GridDocument row, so the archive is restorable even if the
database (including GridArchiveBatch, which is only a log) is gone.

Per month, the order is what makes this safe to run unattended:
  1. copy each file to the archive and read it back to compare SHA-256
  2. write the manifest and read it back to compare
  3. only then delete the rows (one transaction) and the original files
A document that fails any check stays in the active database untouched and is
retried on the next run.
"""
import hashlib
import json
import logging
import posixpath
import re
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .models import GridArchiveBatch, GridDocument

logger = logging.getLogger(__name__)

# Root of GridDocument.uploaded_file's upload_to ("grid_documents/%Y/%m/").
LIVE_PREFIX = "grid_documents"
MANIFEST_VERSION = 1
# Give up on a month after this many documents in a row fail - that's storage
# being down, and there's no point grinding through the rest of the month.
MAX_CONSECUTIVE_FAILURES = 3
# An orphan scan ignores files this young: a grid upload saves its file to
# storage a moment before its row's uploaded_file is updated (the Celery task
# in tasks.py), and we mustn't mistake that file for a lost one.
ORPHAN_MIN_AGE = timedelta(minutes=10)
UNKNOWN_INSURER = "UNKNOWN - RESTORED FROM STORAGE"


class ArchiveNotConfigured(Exception):
    pass


# ---------------------------------------------------------------------------
# Storage + small helpers
# ---------------------------------------------------------------------------

def get_archive_storage():
    return storages["grid_archive"]


def _live_storage():
    return GridDocument._meta.get_field("uploaded_file").storage


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _read(storage, key):
    with storage.open(key, "rb") as f:
        return f.read()


def _put_verified(storage, key, data, overwrite=True):
    """
    Store `data` at exactly `key`, then read it back. Raises unless the stored
    bytes match. An identical object already there is left alone; a different
    one is replaced, or - with overwrite=False - refused.
    """
    digest = _sha256(data)
    if storage.exists(key):
        if _sha256(_read(storage, key)) == digest:
            return
        if not overwrite:
            raise RuntimeError(f"{key!r} already exists in storage with different content")
        storage.delete(key)
    saved = storage.save(key, ContentFile(data))
    if saved != key:
        raise RuntimeError(f"storage saved {key!r} under a different name, {saved!r}")
    if _sha256(_read(storage, key)) != digest:
        raise RuntimeError(f"read-back of {key!r} does not match what was written")


def _month_start(d):
    return timezone.make_aware(datetime.combine(d, time.min))


def _next_month(d):
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def retention_cutoff(months=None, today=None):
    """Start of the oldest month still kept active; anything uploaded before it gets archived."""
    months = settings.GRID_RETENTION_MONTHS if months is None else months
    if months < 1:
        raise ValueError("months must be at least 1")
    today = today or timezone.localdate()
    index = today.year * 12 + (today.month - 1) - (months - 1)
    return _month_start(date(index // 12, index % 12 + 1, 1))


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def _archive_document(doc, label, archive):
    """Copy one document's file into the archive (verified) and return its manifest entry."""
    key = doc.uploaded_file.name or ""
    entry = {
        "id": doc.id,
        "insurer_name": doc.insurer_name,
        "remarks": doc.remarks,
        "work_effected_date": doc.work_effected_date.isoformat() if doc.work_effected_date else None,
        "uploaded_date": doc.uploaded_date.isoformat(),
        "status": doc.status,
        "uploaded_by_username": doc.uploaded_by.username if doc.uploaded_by else None,
        "original_key": key,
        "archive_key": "",
        "size": 0,
        "sha256": "",
    }
    if not key:
        entry["note"] = "no file on record"
        return entry

    live = _live_storage()
    if not live.exists(key):
        # Nothing to preserve but the metadata, which the manifest keeps.
        entry["note"] = "file missing from storage when archived"
        return entry

    data = _read(live, key)
    archive_key = f"{label}/files/{key}"
    _put_verified(archive, archive_key, data)
    entry.update(archive_key=archive_key, size=len(data), sha256=_sha256(data))
    return entry


def _write_manifest(archive, label, entries):
    now = timezone.now()
    manifest = {
        "version": MANIFEST_VERSION,
        "month": label,
        "created_at": now.isoformat(),
        "documents": entries,
    }
    key = f"{label}/manifest-{now:%Y%m%dT%H%M%S%fZ}.json"
    _put_verified(archive, key, json.dumps(manifest, indent=2).encode("utf-8"))
    return key


def _purge(entries):
    """Called only after the archive copies and the manifest are verified."""
    with transaction.atomic():
        GridDocument.objects.filter(id__in=[e["id"] for e in entries]).delete()
    live = _live_storage()
    for e in entries:
        # Rows are already gone, so a failed file delete only leaves a stray
        # copy behind (the archive holds the verified one) - never a broken row.
        if e["archive_key"]:
            try:
                live.delete(e["original_key"])
            except Exception:
                logger.warning("Could not delete archived original %s", e["original_key"], exc_info=True)


def archive_month(month, dry_run=False):
    """Archive and purge one calendar month (`month` is any date in it). Returns a JSON-safe summary."""
    month = month.replace(day=1)
    label = f"{month:%Y-%m}"
    docs = list(
        GridDocument.objects
        .filter(uploaded_date__gte=_month_start(month), uploaded_date__lt=_month_start(_next_month(month)))
        .select_related("uploaded_by")
        .order_by("id")
    )
    summary = {
        "month": label, "status": "DRY_RUN" if dry_run else "COMPLETED",
        "found": len(docs), "documents": 0, "files": 0, "bytes": 0, "errors": [],
    }
    if dry_run:
        summary["files"] = sum(1 for d in docs if d.uploaded_file)
        return summary

    batch = GridArchiveBatch.objects.create(month=month)
    entries = []
    purged = []
    manifest_key = ""
    try:
        archive = get_archive_storage()
        failures = 0
        for doc in docs:
            try:
                entries.append(_archive_document(doc, label, archive))
                failures = 0
            except Exception as exc:
                logger.exception("Archiving GridDocument %s failed", doc.id)
                summary["errors"].append(f"GridDocument {doc.id}: {exc}")
                failures += 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    summary["errors"].append(
                        f"Stopped after {failures} consecutive failures; storage may be unavailable."
                    )
                    break
        if entries:
            manifest_key = _write_manifest(archive, label, entries)
            _purge(entries)
            purged = entries
    except Exception as exc:
        # Failing before _purge means the manifest wasn't verified and nothing
        # was deleted; failing inside it rolls the row delete back. Either way
        # every row is still in the database and the next run redoes the work.
        logger.exception("Archiving %s failed", label)
        summary["errors"].append(str(exc))

    summary.update(
        documents=len(purged),
        files=sum(1 for e in purged if e["archive_key"]),
        bytes=sum(e["size"] for e in purged),
    )
    if summary["errors"]:
        summary["status"] = "PARTIAL" if purged else "FAILED"

    batch.status = summary["status"]
    batch.documents_archived = summary["documents"]
    batch.files_archived = summary["files"]
    batch.bytes_archived = summary["bytes"]
    batch.manifest_key = manifest_key
    batch.error_message = "\n".join(summary["errors"])
    batch.finished_at = timezone.now()
    batch.save()
    return summary


def run_retention(months=None, dry_run=False, allow_local_archive=False, today=None):
    """Archive every month that has fallen out of the active window, oldest first."""
    if not dry_run and not settings.USE_S3_STORAGE and not allow_local_archive:
        raise ArchiveNotConfigured(
            "Cloudflare R2 isn't configured (AWS_STORAGE_BUCKET_NAME / AWS_ACCESS_KEY_ID / "
            "AWS_SECRET_ACCESS_KEY), so the archive would land on this machine's own disk. "
            "Refusing to purge. Pass allow_local_archive=True only for local testing."
        )
    cutoff = retention_cutoff(months, today)
    stale_months = (
        GridDocument.objects.filter(uploaded_date__lt=cutoff)
        .dates("uploaded_date", "month", order="ASC")
    )
    return [archive_month(m, dry_run=dry_run) for m in stale_months]


# ---------------------------------------------------------------------------
# Restore from the archive
# ---------------------------------------------------------------------------

def list_archive_months(archive=None):
    archive = archive or get_archive_storage()
    try:
        dirs, _ = archive.listdir("")
    except FileNotFoundError:
        return []
    return sorted(d for d in dirs if re.fullmatch(r"\d{4}-\d{2}", d))


def _load_entries(archive, label):
    """Every document recorded for a month, across all of its manifests (a later manifest wins)."""
    _, files = archive.listdir(label)
    entries = {}
    for name in sorted(f for f in files if re.fullmatch(r"manifest-.*\.json", f)):
        manifest = json.loads(_read(archive, f"{label}/{name}"))
        for entry in manifest["documents"]:
            entries[entry["original_key"] or f"{label}:{entry['id']}"] = entry
    return list(entries.values())


def _known_keys():
    return set(GridDocument.objects.exclude(uploaded_file="").values_list("uploaded_file", flat=True))


def restore_from_archive(months=None, dry_run=False):
    """
    Put archived documents back into the active database and storage. Skips any
    whose file is already in a GridDocument row, so it's safe to re-run. Note a
    restored month is older than the retention window, so the next scheduled
    archive run will move it out again.
    """
    archive = get_archive_storage()
    live = _live_storage()
    known = _known_keys()
    users = {u.username: u for u in get_user_model().objects.all()}
    result = {"restored": 0, "already_present": 0, "without_file": 0, "errors": []}

    for label in months or list_archive_months(archive):
        for e in _load_entries(archive, label):
            key = e["original_key"]
            if not key or not e["archive_key"]:
                result["without_file"] += 1
                continue
            if key in known:
                result["already_present"] += 1
                continue
            if dry_run:
                result["restored"] += 1
                continue
            try:
                data = _read(archive, e["archive_key"])
                if _sha256(data) != e["sha256"]:
                    raise RuntimeError("archived copy no longer matches its recorded checksum")
                _put_verified(live, key, data, overwrite=False)
                with transaction.atomic():
                    doc = GridDocument.objects.create(
                        insurer_name=e["insurer_name"],
                        remarks=e["remarks"],
                        work_effected_date=parse_date(e["work_effected_date"] or ""),
                        uploaded_file=key,
                        uploaded_by=users.get(e["uploaded_by_username"]),
                        status=e["status"],
                    )
                    # uploaded_date is auto_now_add, so it can only be set after the insert.
                    GridDocument.objects.filter(pk=doc.pk).update(
                        uploaded_date=parse_datetime(e["uploaded_date"])
                    )
                known.add(key)
                result["restored"] += 1
            except Exception as exc:
                logger.exception("Restoring %s failed", key)
                result["errors"].append(f"{key}: {exc}")
    return result


# ---------------------------------------------------------------------------
# Restore from files that lost their row
# ---------------------------------------------------------------------------

_GENERIC_WORDS = {"general", "insurance", "co", "company", "ltd", "limited", "the", "i"}


def _words(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def guess_insurer(filename, choices):
    """
    The one insurer whose distinctive words all appear in the file name, else
    None. Strict on purpose: restored rows can't be edited in the UI, so an
    unknown is better than a plausible-but-wrong name.
    """
    in_name = set(_words(posixpath.basename(filename)))
    matches = [c for c in choices if (w := set(_words(c)) - _GENERIC_WORDS) and w <= in_name]
    return matches[0] if len(matches) == 1 else None


def _walk_files(storage, path):
    try:
        dirs, files = storage.listdir(path)
    except FileNotFoundError:
        return
    for name in files:
        if name:
            yield f"{path}/{name}"
    for name in dirs:
        yield from _walk_files(storage, f"{path}/{name}")


def restore_orphans(months=None, include_all=False, status="PENDING", dry_run=False):
    """
    Re-create rows for files sitting under grid_documents/ in storage that no
    GridDocument points at - which is what's left when the table is emptied
    but the bucket isn't. Only the file itself survives that, so insurer (a
    guess from the file name, flagged in remarks), remarks, work-effected date,
    uploader and original status are NOT recoverable; upload time comes from
    the object's last-modified time. By default only files inside the active
    retention window are brought back.
    """
    from .views import GRID_INSURER_CHOICES

    live = _live_storage()
    known = _known_keys()
    since = None if include_all else retention_cutoff(months)
    too_new_after = timezone.now() - ORPHAN_MIN_AGE
    note_date = timezone.localdate()
    result = {
        "restored": 0, "already_present": 0, "outside_window": 0, "too_recent": 0,
        "errors": [], "rows": [],
    }

    for key in sorted(_walk_files(live, LIVE_PREFIX)):
        if key in known:
            result["already_present"] += 1
            continue
        modified = live.get_modified_time(key)
        if timezone.is_naive(modified):
            modified = timezone.make_aware(modified)
        if since and modified < since:
            result["outside_window"] += 1
            continue
        if modified > too_new_after:
            result["too_recent"] += 1
            continue

        insurer = guess_insurer(key, GRID_INSURER_CHOICES)
        remarks = f"Restored from storage on {note_date:%d %b %Y}; original remarks and status were not recoverable."
        if insurer:
            remarks += " Insurer guessed from the file name."
        result["rows"].append((key, insurer or UNKNOWN_INSURER, modified))
        if dry_run:
            result["restored"] += 1
            continue
        try:
            with transaction.atomic():
                doc = GridDocument.objects.create(
                    insurer_name=insurer or UNKNOWN_INSURER,
                    remarks=remarks,
                    uploaded_file=key,
                    status=status,
                )
                GridDocument.objects.filter(pk=doc.pk).update(uploaded_date=modified)
            result["restored"] += 1
        except Exception as exc:
            logger.exception("Restoring %s failed", key)
            result["errors"].append(f"{key}: {exc}")
    return result
