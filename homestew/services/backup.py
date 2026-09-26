"""Backup / restore of devices, manuals and calendar events (Settings > General).

A backup is a single ``.zip`` archive whose members are stored *uncompressed*
(``ZIP_STORED``): the payloads are already-compressed PDFs, so deflating them
would only burn CPU and memory without shrinking anything - important under the
container's 512 MB cap. The archive layout is::

    manifest.json              # format/app versions, timestamp, counts, missing files
    data.json                  # raw DB rows (devices, attributes, manuals, events, log)
    manuals/<manual_id>.pdf     # one member per manual PDF, keyed by id

``data.json`` stores the database columns verbatim so a restore reproduces the
original rows as faithfully as possible. A manual's absolute ``filepath`` is
machine-specific and never archived; instead each manual carries an
``archive_path`` pointing at its PDF inside the zip, which a restore rewrites
against the *current* ``DEVICES_DIR``.

Settings and secrets are deliberately out of scope: the persisted settings file
holds API keys / webhook tokens encrypted with ``/data/.secrets_key``, so a
downloadable archive must never contain them.
"""
import logging
import re
import shutil
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from fastapi.concurrency import run_in_threadpool

from homestew.config import settings
from homestew.db import get_db_context
from homestew.models.schemas import BackupData
from homestew.services.indexer import (
    get_index_status,
    start_background_reindex,
)
from homestew.services.warranty import compute_warranty_end

logger = logging.getLogger(__name__)

# Bump when the archive layout or data.json shape changes incompatibly. A
# restore refuses archives newer than this version but reads older ones.
BACKUP_FORMAT_VERSION = 1

# Only these members are ever read back; anything else in an uploaded zip is
# rejected (guards against a crafted "zip-slip" archive).
_MEMBER_RE = re.compile(r"^manuals/\d+\.pdf$")

# Cap on how large a single restored PDF may be, mirroring the upload limit so
# a hand-edited archive can't smuggle an oversized file onto disk.
MAX_MEMBER_BYTES = 100 * 1024 * 1024


def backup_dir() -> Path:
    """Directory where backup archives are staged before download."""
    return settings.DATA_DIR / "backups"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# Filename helpers (mirrors api/devices.py; kept local so this service does not
# depend on the API layer - services must stay importable without FastAPI's
# request plumbing, per the warranty.py layering rule).
# ---------------------------------------------------------------------------

def _sanitize_filename(filename: str) -> str:
    """Keep only a safe basename for a restored manual."""
    name = Path(filename or "").name
    name = re.sub(r"[^\w.\- ]+", "_", name).strip() or "manual.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def _unique_filename(
    device_dir: Path, filename: str, reserved: Optional[set] = None
) -> str:
    """Avoid overwriting an existing manual with the same name.

    ``reserved`` additionally holds names already planned earlier in the same
    restore - restored files are only written to disk after the DB commit, so a
    plain on-disk existence check would let two archived manuals of one device
    claim the same target name.
    """
    candidate = device_dir / filename
    if not candidate.exists() and (reserved is None or filename not in reserved):
        return filename
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 2
    while (
        (device_dir / f"{stem}_{counter}{suffix}").exists()
        or (reserved is not None and f"{stem}_{counter}{suffix}" in reserved)
    ):
        counter += 1
    return f"{stem}_{counter}{suffix}"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

async def create_backup(include_manuals: bool = True) -> dict:
    """Build a backup archive in the backups dir and return its stats.

    The database rows are read here (async), then the zip is written on a worker
    thread so streaming potentially large PDFs never blocks the event loop nor
    loads a whole file into memory. When ``include_manuals`` is False the
    archive holds only devices, attributes and calendar events - no manual rows
    and no PDF members - for a small metadata-only backup (a later full backup
    restores the manuals). Returns ``{filename, devices, manuals,
    calendar_events, missing_files}``.
    """
    out_dir = backup_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    async with get_db_context() as db:
        dev_cur = await db.execute(
            """
            SELECT id, name, brand, model, description, serial_number,
                   product_number, purchase_date, warranty_length, warranty_unit,
                   warranty_end, created_at, updated_at
            FROM devices ORDER BY id
            """
        )
        devices = [dict(r) for r in await dev_cur.fetchall()]

        attr_cur = await db.execute(
            """
            SELECT id, device_id, attribute_name, attribute_value, created_at
            FROM device_attributes ORDER BY id
            """
        )
        attributes = [dict(r) for r in await attr_cur.fetchall()]

        manual_rows: List[dict] = []
        if include_manuals:
            man_cur = await db.execute(
                """
                SELECT id, device_id, filename, page_count, source_url,
                       indexed_at, filepath
                FROM manuals ORDER BY id
                """
            )
            manual_rows = [dict(r) for r in await man_cur.fetchall()]

        ev_cur = await db.execute(
            """
            SELECT id, device_id, title, description, start_date, start_time,
                   recurrence_type, interval, last_completed_at, created_at
            FROM calendar_events ORDER BY id
            """
        )
        events = [dict(r) for r in await ev_cur.fetchall()]

        log_cur = await db.execute(
            "SELECT event_id, due_date, kind, sent_at FROM notification_log ORDER BY id"
        )
        logs = [dict(r) for r in await log_cur.fetchall()]

    # Build manual entries: swap the absolute filepath for an archive-relative
    # path and collect which PDFs actually exist on disk to copy.
    manuals_json: List[dict] = []
    copy_pairs: List[Tuple[str, Optional[str]]] = []  # (archive_name, src abs path)
    missing_files: List[str] = []
    for m in manual_rows:
        archive_path = f"manuals/{m['id']}.pdf"
        src = m.get("filepath") or ""
        exists = bool(src) and Path(src).is_file()
        manuals_json.append(
            {
                "id": m["id"],
                "device_id": m["device_id"],
                "filename": m["filename"],
                "page_count": m["page_count"],
                "source_url": m.get("source_url"),
                "indexed_at": m["indexed_at"],
                # None when the PDF is gone: metadata still survives, restore
                # recreates the row but has no bytes to write.
                "archive_path": archive_path if exists else None,
            }
        )
        if exists:
            copy_pairs.append((archive_path, src))
        else:
            missing_files.append(m["filename"])

    data = BackupData(
        devices=devices,
        device_attributes=attributes,
        manuals=manuals_json,
        calendar_events=events,
        notification_log=logs,
    )
    manifest = {
        "format_version": BACKUP_FORMAT_VERSION,
        "app_version": settings.APP_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "devices": len(devices),
            "manuals": len(manuals_json),
            "calendar_events": len(events),
        },
        "missing_files": missing_files,
    }

    filename = f"homestew-backup-{_utc_stamp()}.zip"
    out_path = out_dir / filename
    await run_in_threadpool(
        _write_zip,
        out_path,
        manifest,
        data.model_dump(mode="json"),
        copy_pairs,
    )

    logger.info(
        "Created backup %s (%d devices, %d manuals, %d events)",
        filename, len(devices), len(manuals_json), len(events),
    )
    return {
        "filename": filename,
        "devices": len(devices),
        "manuals": len(manuals_json),
        "calendar_events": len(events),
        "missing_files": missing_files,
    }


def _write_zip(out_path: Path, manifest: dict, data_dict: dict, copy_pairs) -> None:
    """Blocking zip writer run on a worker thread (streams PDFs, ZIP_STORED)."""
    import json

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("data.json", json.dumps(data_dict, indent=2))
        for archive_name, src in copy_pairs:
            if not src or not Path(src).is_file():
                continue
            # Stream member-by-member so a 100 MB PDF never sits fully in RAM.
            with zf.open(archive_name, "w") as dest, open(src, "rb") as fsrc:
                shutil.copyfileobj(fsrc, dest, length=1024 * 1024)


# ---------------------------------------------------------------------------
# Restore - reading an archive
# ---------------------------------------------------------------------------

def _read_archive(zip_path: Path) -> Tuple[dict, BackupData]:
    """Validate and parse an uploaded backup (blocking; run on a thread).

    Returns ``(manifest, data)``. Raises ``ValueError`` for anything that isn't
    a well-formed HomeStew archive - including members outside the strict
    allow-list, so a malicious zip can never smuggle arbitrary paths.
    """
    import json

    if not zipfile.is_zipfile(zip_path):
        raise ValueError("The file is not a valid backup archive.")

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        for name in names:
            if name not in ("manifest.json", "data.json") and not _MEMBER_RE.match(name):
                raise ValueError(f"Unexpected file inside the archive: {name}")
        if "manifest.json" not in names or "data.json" not in names:
            raise ValueError("Archive is missing its manifest or data.")

        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        version = int(manifest.get("format_version", 0))
        if version > BACKUP_FORMAT_VERSION:
            raise ValueError(
                f"This backup was made by a newer HomeStew "
                f"(format {version}); update the app to restore it."
            )

        raw_data = json.loads(zf.read("data.json").decode("utf-8"))
        data = BackupData.model_validate(raw_data)

    # Every manual's archive_path must be a member we would actually read.
    for m in data.manuals:
        if m.archive_path and not _MEMBER_RE.match(m.archive_path):
            raise ValueError(f"Manual references an invalid path: {m.archive_path}")

    return manifest, data


async def read_backup(zip_path: Path) -> Tuple[dict, BackupData]:
    return await run_in_threadpool(_read_archive, zip_path)


# ---------------------------------------------------------------------------
# Restore - applying parsed rows to the database + disk
# ---------------------------------------------------------------------------

def _identity_key(name: str, brand: str, model: str) -> tuple:
    """Lowercased (name, brand, model) used to match a device across backups."""
    return (
        (name or "").strip().lower(),
        (brand or "").strip().lower(),
        (model or "").strip().lower(),
    )


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


async def restore_backup(zip_path: Path, mode: str) -> dict:
    """Restore a backup archive in ``merge`` or ``replace`` mode.

    merge   - keep existing rows; add what's missing (deduped by device
              identity / manual source_url+filename / event schedule). Idempotent:
              restoring the same file twice adds nothing the second time.
    replace - wipe devices, attributes, manuals, calendar events and the
              notification ledger first, then recreate everything from the
              archive (original PDFs removed from disk too).

    Row writes happen in one short transaction committed *before* any file IO or
    indexing (``index_manual`` opens its own connection). Search re-indexing is
    kicked off afterwards by :func:`start_reindex`. Returns a summary dict plus
    the list of manuals to re-index.
    """
    _manifest, data = await read_backup(zip_path)

    async with get_db_context() as db:
        await db.execute("PRAGMA foreign_keys = ON")

        # Existing device identity -> id (merge dedupe). Read once up front.
        existing_devices = {}
        if mode == "merge":
            cur = await db.execute("SELECT id, name, brand, model FROM devices")
            for r in await cur.fetchall():
                key = _identity_key(r["name"], r["brand"], r["model"])
                existing_devices.setdefault(key, r["id"])

        # Replace mode: capture PDF paths + device ids to purge, then delete in
        # FK-safe order (child -> parent) and clear the search index.
        files_to_delete: List[str] = []
        if mode == "replace":
            cur = await db.execute("SELECT filepath FROM manuals")
            files_to_delete = [r["filepath"] for r in await cur.fetchall() if r["filepath"]]
            for table in (
                "notification_log",
                "calendar_events",
                "manuals",
                "device_attributes",
                "devices",
            ):
                await db.execute(f"DELETE FROM {table}")
            # A whole-table DELETE is valid on an FTS5 vtab (only DELETE with
            # WHERE is not); this also clears rows orphaned by earlier deletes.
            await db.execute("DELETE FROM pdf_index")

        # --- devices -------------------------------------------------------
        device_map: dict[int, int] = {}  # backup id -> live id
        devices_created = devices_merged = 0
        for d in data.devices:
            key = _identity_key(d.name, d.brand, d.model)
            if mode == "merge" and key in existing_devices:
                device_map[d.id] = existing_devices[key]
                devices_merged += 1
                continue

            purchase, length, unit, end = _restore_warranty(d)
            cur = await db.execute(
                """
                INSERT INTO devices (name, brand, model, description, serial_number,
                    product_number, purchase_date, warranty_length, warranty_unit,
                    warranty_end, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        COALESCE(?, CURRENT_TIMESTAMP), COALESCE(?, CURRENT_TIMESTAMP))
                """,
                (
                    d.name, d.brand, d.model, d.description or "",
                    d.serial_number, d.product_number, purchase, length, unit, end,
                    d.created_at, d.updated_at,
                ),
            )
            new_id = cur.lastrowid
            device_map[d.id] = new_id
            existing_devices.setdefault(key, new_id)
            devices_created += 1

        # --- attributes ----------------------------------------------------
        attr_seen: set[tuple] = set()
        if mode == "merge":
            cur = await db.execute(
                "SELECT device_id, attribute_name, attribute_value FROM device_attributes"
            )
            for r in await cur.fetchall():
                attr_seen.add((r["device_id"], r["attribute_name"], r["attribute_value"]))

        skipped = 0
        for a in data.device_attributes:
            target = device_map.get(a.device_id)
            if target is None:
                continue  # its device wasn't restored (shouldn't happen)
            sig = (target, a.attribute_name, a.attribute_value)
            if mode == "merge" and sig in attr_seen:
                skipped += 1
                continue
            await db.execute(
                """
                INSERT INTO device_attributes (device_id, attribute_name, attribute_value, created_at)
                VALUES (?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
                """,
                (target, a.attribute_name, a.attribute_value, a.created_at),
            )
            attr_seen.add(sig)

        # --- manuals -------------------------------------------------------
        # Per-device source_urls / filenames ALREADY in the database, snapshotted
        # once for merge dedupe. Rows inserted by this same pass are NOT added to
        # it: two archived manuals sharing a filename are distinct missing rows
        # and both get restored (the second renamed via `planned_names` below).
        # Re-running the same restore is still idempotent - the snapshot then
        # contains everything from the first run.
        manual_existing: dict[int, dict] = {}
        if mode == "merge":
            cur = await db.execute(
                "SELECT device_id, filename, source_url FROM manuals"
            )
            for r in await cur.fetchall():
                bucket = manual_existing.setdefault(
                    r["device_id"], {"urls": set(), "names": set()}
                )
                if r["source_url"]:
                    bucket["urls"].add(r["source_url"])
                bucket["names"].add((r["filename"] or "").lower())

        files_to_write: List[Tuple[str, str]] = []  # (archive_name, dest abs path)
        pending_index: List[dict] = []
        planned_names: dict[int, set] = {}  # device id -> names claimed this restore
        manuals_restored = 0
        for m in data.manuals:
            target = device_map.get(m.device_id)
            if target is None:
                continue
            if mode == "merge":
                bucket = manual_existing.setdefault(
                    target, {"urls": set(), "names": set()}
                )
                if (m.source_url and m.source_url in bucket["urls"]) or (
                    (m.filename or "").lower() in bucket["names"]
                ):
                    skipped += 1
                    continue

            device_dir = settings.DEVICES_DIR / str(target) / "manuals"
            device_dir.mkdir(parents=True, exist_ok=True)
            reserved = planned_names.setdefault(target, set())
            safe_name = _unique_filename(
                device_dir, _sanitize_filename(m.filename), reserved
            )
            reserved.add(safe_name)
            dest = device_dir / safe_name

            cur = await db.execute(
                """
                INSERT INTO manuals (device_id, filename, filepath, page_count, source_url)
                VALUES (?, ?, ?, ?, ?)
                """,
                (target, safe_name, str(dest), m.page_count, m.source_url),
            )
            new_manual_id = cur.lastrowid

            if m.archive_path:
                files_to_write.append((m.archive_path, str(dest)))
                pending_index.append(
                    {
                        "manual_id": new_manual_id,
                        "device_id": target,
                        "pdf_path": str(dest),
                        "filename": safe_name,
                    }
                )
            manuals_restored += 1

        # --- calendar events ----------------------------------------------
        event_map: dict[int, int] = {}  # backup id -> live id (for the ledger)
        event_seen: set[tuple] = set()
        if mode == "merge":
            cur = await db.execute(
                """
                SELECT device_id, title, start_date, recurrence_type, interval
                FROM calendar_events
                """
            )
            for r in await cur.fetchall():
                event_seen.add(_event_sig(r))

        events_restored = 0
        for e in data.calendar_events:
            target_device = device_map.get(e.device_id) if e.device_id is not None else None
            sig = (target_device, e.title.strip().lower(), str(e.start_date),
                   e.recurrence_type, int(e.interval))
            if mode == "merge" and sig in event_seen:
                # Point the ledger at the already-present event so its log rows
                # still resolve; count as skipped.
                live = await _find_event_id(db, target_device, e)
                if live is not None:
                    event_map[e.id] = live
                skipped += 1
                continue

            cur = await db.execute(
                """
                INSERT INTO calendar_events (device_id, title, description, start_date,
                    start_time, recurrence_type, interval, last_completed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
                """,
                (
                    target_device, e.title, e.description or "",
                    e.start_date.isoformat(), e.start_time, e.recurrence_type,
                    max(1, int(e.interval)), e.last_completed_at, e.created_at,
                ),
            )
            event_map[e.id] = cur.lastrowid
            event_seen.add(sig)
            events_restored += 1

        # --- notification ledger ------------------------------------------
        # INSERT OR IGNORE: the UNIQUE(event_id, due_date, kind) key makes a
        # re-restore of an already-alerted occurrence a no-op.
        for n in data.notification_log:
            live_event = event_map.get(n.event_id)
            if live_event is None:
                continue  # its event wasn't restored/merged
            await db.execute(
                """
                INSERT OR IGNORE INTO notification_log (event_id, due_date, kind, sent_at)
                VALUES (?, ?, ?, ?)
                """,
                (live_event, n.due_date, n.kind, n.sent_at),
            )

        await db.commit()

    # Rows are committed; now move files on disk off the event loop.
    await run_in_threadpool(_apply_files, zip_path, files_to_write, files_to_delete)

    return {
        "mode": mode,
        "devices_created": devices_created,
        "devices_merged": devices_merged,
        "manuals_restored": manuals_restored,
        "events_restored": events_restored,
        "skipped": skipped,
        "index_total": len(pending_index),
        "_pending_index": pending_index,
    }


def _restore_warranty(d) -> tuple:
    """Normalised (purchase, length, unit, end) ISO strings for a backup device.

    Archived values are reused verbatim; ``warranty_end`` is only recomputed when
    it was never stored but the ingredients exist (legacy rows).
    """
    purchase = d.purchase_date or None
    end = d.warranty_end or None
    if not end and purchase and d.warranty_length is not None and d.warranty_unit:
        computed = compute_warranty_end(
            _parse_date(purchase), d.warranty_length, d.warranty_unit, None
        )
        end = computed.isoformat() if computed else None
    return (purchase, d.warranty_length, d.warranty_unit, end)


def _event_sig(row) -> tuple:
    """Dedupe signature for an existing calendar_events row."""
    return (
        row["device_id"],
        (row["title"] or "").strip().lower(),
        str(row["start_date"]),
        row["recurrence_type"],
        int(row["interval"]),
    )


async def _find_event_id(db, device_id, e) -> Optional[int]:
    """Locate an existing event matching a backup event (merge dedupe)."""
    cur = await db.execute(
        """
        SELECT id FROM calendar_events
        WHERE ((device_id IS ?) OR device_id = ?)
          AND lower(trim(title)) = ?
          AND start_date = ? AND recurrence_type = ? AND interval = ?
        LIMIT 1
        """,
        (device_id, device_id, e.title.strip().lower(),
         e.start_date.isoformat(), e.recurrence_type, int(e.interval)),
    )
    row = await cur.fetchone()
    return row["id"] if row else None


def _apply_files(zip_path: Path, writes, deletes) -> None:
    """Blocking file pass run on a worker thread after the DB commit.

    Removes replaced-mode orphan PDFs and streams each restored manual from the
    archive straight to its destination (never fully buffered).
    """
    for path in deletes:
        try:
            p = Path(path)
            if p.is_file():
                p.unlink()
        except OSError as exc:
            logger.warning("Could not delete replaced manual %s: %s", path, exc)

    if not writes:
        return

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        for archive_name, dest in writes:
            if archive_name not in names:
                logger.warning("Archive is missing member %s", archive_name)
                continue
            info = zf.getinfo(archive_name)
            if info.file_size > MAX_MEMBER_BYTES:
                logger.error(
                    "Refusing to restore oversized member %s (%d bytes)",
                    archive_name, info.file_size,
                )
                continue
            dest_path = Path(dest)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(archive_name) as src, open(dest_path, "wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)


# ---------------------------------------------------------------------------
# Background search re-index after a restore
# ---------------------------------------------------------------------------
#
# Progress lives in the indexer's shared state (services/indexer.py) so the
# same persistent toast can follow a restore re-index, a manual re-index or a
# full rebuild. get_restore_index_status() stays as the /api/backup/
# restore-status read path for backwards compatibility.


def get_restore_index_status() -> dict:
    return get_index_status()


async def start_reindex(pending: List[dict]) -> None:
    """Re-index restored manuals in the background so the UI stays responsive."""
    if not pending:
        return
    queued = start_background_reindex(pending)
    if queued < 0:
        # Another indexing job is already running; its progress covers this
        # window, and a manual re-index from Settings can fill any gap.
        logger.warning(
            "Restore re-index skipped: another index job is running (%d manuals)",
            len(pending),
        )
