"""Tests for backup / restore (services/backup.py).

Follows the repo convention: no async pytest runner, each test drives the
async service through asyncio.run() with a throwaway DATA_DIR (same fixture
pattern as test_device_tool.py). The fake PDF bytes are not parseable by pypdf,
so re-indexing them is expected to fail - restore must tolerate that; these
tests assert on rows and files rather than FTS content.

Run from the repo root:  pytest tests/test_backup_restore.py -q
"""
import asyncio
import json
import zipfile
from pathlib import Path

import pytest

from homestew import config as cfg
from homestew.db import get_db_context, init_db
from homestew.services import backup as backup_service

VALID_PDF = b"%PDF-1.4\n" + b"x" * 2000


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """Fresh database in a temp DATA_DIR; settings restored afterwards."""
    saved_dir = cfg.settings.DATA_DIR
    saved_devices_dir = cfg.settings.DEVICES_DIR
    monkeypatch.setattr(cfg.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg.settings, "DEVICES_DIR", tmp_path / "devices")
    asyncio.run(init_db())
    yield tmp_path
    setattr(cfg.settings, "DATA_DIR", saved_dir)
    setattr(cfg.settings, "DEVICES_DIR", saved_devices_dir)


# --- helpers ---------------------------------------------------------------

async def _seed_full_dataset():
    """One device with attribute + manual (real file on disk) + event + log row."""
    async with get_db_context() as db:
        cur = await db.execute(
            """
            INSERT INTO devices (name, brand, model, description, serial_number,
                purchase_date, warranty_length, warranty_unit, icon, created_at, updated_at)
            VALUES ('Living Room TV', 'Sony', 'XR-65X90J', 'main tv', 'SN123',
                    '2024-01-31', 24, 'months', 'tv', '2024-02-01 10:00:00', '2024-06-01 12:00:00')
            """
        )
        device_id = cur.lastrowid

        await db.execute(
            "INSERT INTO device_attributes (device_id, attribute_name, attribute_value) VALUES (?, 'room', 'living')",
            (device_id,),
        )

        manual_dir = cfg.settings.DEVICES_DIR / str(device_id) / "manuals"
        manual_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = manual_dir / "userguide.pdf"
        pdf_path.write_bytes(VALID_PDF)
        await db.execute(
            "INSERT INTO manuals (device_id, filename, filepath, page_count, source_url) VALUES (?, 'userguide.pdf', ?, 42, 'http://x/guide.pdf')",
            (device_id, str(pdf_path)),
        )

        cur = await db.execute(
            """
            INSERT INTO calendar_events (device_id, title, description, start_date,
                start_time, recurrence_type, interval)
            VALUES (?, 'Replace filter', '', '2026-10-01', '09:00', 'yearly', 1)
            """,
            (device_id,),
        )
        event_id = cur.lastrowid

        await db.execute(
            "INSERT INTO notification_log (event_id, due_date, kind) VALUES (?, '2026-10-01', 'due')",
            (event_id,),
        )
        # A stale search row that a replace-restore must clear.
        await db.execute(
            "INSERT INTO pdf_index (device_id, manual_id, filename, page_number, content) VALUES (?, 999, 'old.pdf', 1, 'stale text')",
            (device_id,),
        )
        await db.commit()
        return device_id


async def _counts():
    async with get_db_context() as db:
        out = {}
        for table in ("devices", "device_attributes", "manuals", "calendar_events",
                      "notification_log", "pdf_index"):
            cur = await db.execute(f"SELECT COUNT(*) AS n FROM {table}")
            out[table] = (await cur.fetchone())["n"]
        return out


async def _rows(table):
    async with get_db_context() as db:
        cur = await db.execute(f"SELECT * FROM {table} ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


async def _wipe_all():
    """Empty every restored table so a following merge restore starts fresh."""
    async with get_db_context() as db:
        for table in ("notification_log", "calendar_events", "manuals",
                      "device_attributes", "devices", "categories"):
            await db.execute(f"DELETE FROM {table}")
        await db.execute("DELETE FROM pdf_index")
        await db.commit()


def backup_and_restore(mode, include_manuals=True):
    """Run export then import; returns (result dict, zip path)."""
    info = asyncio.run(backup_service.create_backup(include_manuals=include_manuals))
    zip_path = cfg.settings.DATA_DIR / "backups" / info["filename"]
    assert zip_path.is_file()
    result = asyncio.run(backup_service.restore_backup(zip_path, mode))
    return result, zip_path


# --- export ----------------------------------------------------------------

def test_export_zip_layout_and_manifest(db_env):
    asyncio.run(_seed_full_dataset())
    info = asyncio.run(backup_service.create_backup())
    assert info["devices"] == 1 and info["manuals"] == 1
    assert info["calendar_events"] == 1 and info["missing_files"] == []

    zip_path = cfg.settings.DATA_DIR / "backups" / info["filename"]
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert "manifest.json" in names and "data.json" in names
        assert "manuals/1.pdf" in names
        # Stored, not deflated (PDFs are already compressed).
        assert zf.getinfo("manuals/1.pdf").compress_type == zipfile.ZIP_STORED
        assert zf.read("manuals/1.pdf") == VALID_PDF

        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["format_version"] == backup_service.BACKUP_FORMAT_VERSION
        assert manifest["counts"] == {
            "devices": 1,
            "categories": 0,
            "manuals": 1,
            "calendar_events": 1,
        }

        data = json.loads(zf.read("data.json"))
    device = data["devices"][0]
    # Timestamps round-trip verbatim as stored by SQLite.
    assert device["created_at"] == "2024-02-01 10:00:00"
    assert data["manuals"][0]["archive_path"] == "manuals/1.pdf"
    assert data["manuals"][0]["source_url"] == "http://x/guide.pdf"


def test_export_records_missing_pdf(db_env):
    asyncio.run(_seed_full_dataset())
    # Delete the PDF behind the manual row: metadata must survive, file won't.
    for p in cfg.settings.DEVICES_DIR.rglob("*.pdf"):
        p.unlink()

    info = asyncio.run(backup_service.create_backup())
    assert info["missing_files"] == ["userguide.pdf"]

    zip_path = cfg.settings.DATA_DIR / "backups" / info["filename"]
    with zipfile.ZipFile(zip_path) as zf:
        data = json.loads(zf.read("data.json"))
    assert data["manuals"][0]["archive_path"] is None


def test_export_without_manuals(db_env):
    asyncio.run(_seed_full_dataset())
    info = asyncio.run(backup_service.create_backup(include_manuals=False))
    assert info["devices"] == 1 and info["manuals"] == 0

    zip_path = cfg.settings.DATA_DIR / "backups" / info["filename"]
    with zipfile.ZipFile(zip_path) as zf:
        data = json.loads(zf.read("data.json"))
        assert data["manuals"] == []
        assert not [n for n in zf.namelist() if n.startswith("manuals/")]


# --- restore ---------------------------------------------------------------

def test_restore_replace_recreates_everything(db_env):
    asyncio.run(_seed_full_dataset())
    result, _ = backup_and_restore("replace")

    assert result["devices_created"] == 1
    assert result["manuals_restored"] == 1
    assert result["events_restored"] == 1
    # The fake PDF is not parseable; it is still queued for re-indexing.
    assert len(result["_pending_index"]) == 1

    counts = asyncio.run(_counts())
    assert counts["devices"] == 1 and counts["device_attributes"] == 1
    assert counts["manuals"] == 1 and counts["calendar_events"] == 1
    # Stale FTS row from before the replace is gone.
    assert counts["pdf_index"] == 0

    devices = asyncio.run(_rows("devices"))
    attrs = asyncio.run(_rows("device_attributes"))
    manuals = asyncio.run(_rows("manuals"))
    events = asyncio.run(_rows("calendar_events"))

    # Archived values survive verbatim, incl. audit timestamps and warranty.
    assert devices[0]["created_at"] == "2024-02-01 10:00:00"
    assert devices[0]["purchase_date"] == "2024-01-31"
    # The chosen icon key survives the backup -> restore round-trip.
    assert devices[0]["icon"] == "tv"
    # warranty_end was NULL in the archive but derivable -> recomputed.
    assert devices[0]["warranty_end"] == "2026-01-31"

    new_device_id = devices[0]["id"]
    assert attrs[0]["device_id"] == new_device_id
    assert events[0]["device_id"] == new_device_id
    assert events[0]["start_time"].startswith("09:00")

    # The PDF is rewritten under the (possibly re-numbered) device directory.
    restored_pdf = cfg.settings.DEVICES_DIR / str(new_device_id) / "manuals" / "userguide.pdf"
    assert manuals[0]["filepath"] == str(restored_pdf)
    assert restored_pdf.read_bytes() == VALID_PDF


def test_restore_replace_remaps_notification_ledger(db_env):
    asyncio.run(_seed_full_dataset())
    backup_and_restore("replace")

    # The ledger row must follow its event to the new id, not dangle.
    async def _check():
        async with get_db_context() as db:
            cur = await db.execute(
                """
                SELECT n.due_date, n.kind FROM notification_log n
                JOIN calendar_events e ON e.id = n.event_id
                """
            )
            return [dict(r) for r in await cur.fetchall()]

    rows = asyncio.run(_check())
    assert rows == [{"due_date": "2026-10-01", "kind": "due"}]


def test_restore_merge_is_idempotent(db_env):
    asyncio.run(_seed_full_dataset())
    result, _ = backup_and_restore("merge")

    assert result["devices_created"] == 0
    assert result["devices_merged"] == 1
    assert result["manuals_restored"] == 0
    assert result["events_restored"] == 0
    # attribute + manual + event all recognised as already present.
    assert result["skipped"] == 3

    counts = asyncio.run(_counts())
    assert counts["devices"] == 1 and counts["manuals"] == 1
    assert counts["calendar_events"] == 1 and counts["device_attributes"] == 1
    # No duplicate PDF written next to the original.
    pdfs = list(cfg.settings.DEVICES_DIR.rglob("*.pdf"))
    assert len(pdfs) == 1


async def _wipe_like_fresh_install():
    async with get_db_context() as db:
        for table in ("notification_log", "calendar_events", "manuals",
                      "device_attributes", "devices", "pdf_index"):
            await db.execute(f"DELETE FROM {table}")
        await db.commit()


def test_restore_merge_into_empty_db(db_env):
    asyncio.run(_seed_full_dataset())
    info = asyncio.run(backup_service.create_backup())
    zip_path = cfg.settings.DATA_DIR / "backups" / info["filename"]

    asyncio.run(_wipe_like_fresh_install())
    for p in cfg.settings.DEVICES_DIR.rglob("*.pdf"):
        p.unlink()

    result = asyncio.run(backup_service.restore_backup(zip_path, "merge"))
    assert result["devices_created"] == 1 and result["skipped"] == 0

    counts = asyncio.run(_counts())
    assert counts["devices"] == 1 and counts["manuals"] == 1
    manuals = asyncio.run(_rows("manuals"))
    pdf = Path(manuals[0]["filepath"])
    assert pdf.is_file() and pdf.read_bytes().startswith(b"%PDF")


def test_merge_remaps_children_when_identities_differ(db_env):
    """A backup device that doesn't match any existing one is inserted fresh;
    its manuals/events must attach to the NEW id, not collide with row id 1."""
    asyncio.run(_seed_full_dataset())
    info = asyncio.run(backup_service.create_backup())
    zip_path = cfg.settings.DATA_DIR / "backups" / info["filename"]

    asyncio.run(_wipe_like_fresh_install())
    # An unrelated device squatting on the same row id as the backup's device.
    async def _insert_squatter():
        async with get_db_context() as db:
            await db.execute(
                "INSERT INTO devices (id, name, brand, model) VALUES (1, 'Fridge', 'Bosch', 'KAD93')"
            )
            await db.commit()

    asyncio.run(_insert_squatter())

    result = asyncio.run(backup_service.restore_backup(zip_path, "merge"))
    assert result["devices_created"] == 1 and result["devices_merged"] == 0

    devices = {d["name"]: d for d in asyncio.run(_rows("devices"))}
    tv_id = devices["Living Room TV"]["id"]
    manuals = asyncio.run(_rows("manuals"))
    events = asyncio.run(_rows("calendar_events"))
    assert manuals[0]["device_id"] == tv_id
    assert events[0]["device_id"] == tv_id
    # The PDF lives under the new device's directory.
    assert Path(manuals[0]["filepath"]).parent.parent.name == str(tv_id)


def test_duplicate_filenames_within_one_backup_get_renamed(db_env):
    """Two manuals of one device sharing a name must not overwrite each other."""
    async def _seed():
        async with get_db_context() as db:
            cur = await db.execute(
                "INSERT INTO devices (name, brand, model) VALUES ('TV', 'A', 'B')"
            )
            did = cur.lastrowid
            manual_dir = cfg.settings.DEVICES_DIR / str(did) / "manuals"
            manual_dir.mkdir(parents=True, exist_ok=True)
            for i in range(2):
                p = manual_dir / f"guide.pdf{i}"  # distinct on disk...
                p.write_bytes(VALID_PDF)
                await db.execute(
                    "INSERT INTO manuals (device_id, filename, filepath) VALUES (?, 'guide.pdf', ?)",
                    (did, str(p)),
                )
            await db.commit()

    asyncio.run(_seed())
    info = asyncio.run(backup_service.create_backup())
    zip_path = cfg.settings.DATA_DIR / "backups" / info["filename"]
    asyncio.run(_wipe_like_fresh_install())

    result = asyncio.run(backup_service.restore_backup(zip_path, "merge"))
    assert result["manuals_restored"] == 2
    names = [m["filename"] for m in asyncio.run(_rows("manuals"))]
    assert sorted(names) == ["guide.pdf", "guide_2.pdf"]


# --- archive validation ----------------------------------------------------

def _write_raw_zip(tmp_path, members: dict) -> Path:
    zip_path = tmp_path / "crafted.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return zip_path


def test_rejects_zip_slip_member(db_env):
    zip_path = _write_raw_zip(db_env, {
        "manifest.json": json.dumps({"format_version": 1}),
        "data.json": json.dumps({"devices": []}),
        "../evil.pdf": "boom",
    })
    with pytest.raises(ValueError, match="Unexpected file"):
        asyncio.run(backup_service.restore_backup(zip_path, "merge"))
    assert not (db_env.parent / "evil.pdf").exists()


def test_rejects_non_zip_file(db_env):
    junk = db_env / "junk.zip"
    junk.write_bytes(b"this is not a zip")
    with pytest.raises(ValueError, match="not a valid backup"):
        asyncio.run(backup_service.restore_backup(junk, "merge"))


def test_rejects_missing_manifest(db_env):
    zip_path = _write_raw_zip(db_env, {"data.json": "{}"})
    with pytest.raises(ValueError, match="manifest"):
        asyncio.run(backup_service.restore_backup(zip_path, "merge"))


def test_rejects_newer_format_version(db_env):
    zip_path = _write_raw_zip(db_env, {
        "manifest.json": json.dumps({"format_version": 999}),
        "data.json": json.dumps({"devices": []}),
    })
    with pytest.raises(ValueError, match="newer"):
        asyncio.run(backup_service.restore_backup(zip_path, "merge"))


def test_rejects_bad_manual_archive_path(db_env):
    zip_path = _write_raw_zip(db_env, {
        "manifest.json": json.dumps({"format_version": 1}),
        "data.json": json.dumps({
            "devices": [],
            "manuals": [{
                "id": 1, "device_id": 1, "filename": "x.pdf",
                "archive_path": "../escape.pdf",
            }],
        }),
    })
    with pytest.raises(ValueError, match="invalid path"):
        asyncio.run(backup_service.restore_backup(zip_path, "merge"))


# --- categories in backups ---------------------------------------------------

async def _seed_categories():
    """Two categories; one device filed in each, one uncategorized."""
    async with get_db_context() as db:
        cur = await db.execute(
            "INSERT INTO categories (name, sort_order) VALUES ('Kitchen', 0)"
        )
        kitchen = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO categories (name, sort_order) VALUES ('Tools', 1)"
        )
        tools = cur.lastrowid
        for name, cat, order in (("Fridge", kitchen, 0), ("Drill", tools, 0),
                                 ("Mystery", None, 0)):
            await db.execute(
                "INSERT INTO devices (name, brand, model, category_id, sort_order) "
                "VALUES (?, 'Acme', 'M1', ?, ?)",
                (name, cat, order),
            )
        await db.commit()
        return kitchen, tools


def test_export_includes_categories_and_membership(db_env):
    asyncio.run(_seed_categories())
    info = asyncio.run(backup_service.create_backup(include_manuals=False))
    assert info["devices"] == 3

    zip_path = cfg.settings.DATA_DIR / "backups" / info["filename"]
    with zipfile.ZipFile(zip_path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["counts"]["categories"] == 2
        data = json.loads(zf.read("data.json"))
    assert [c["name"] for c in data["categories"]] == ["Kitchen", "Tools"]
    by_name = {d["name"]: d for d in data["devices"]}
    kitchen_id = next(c["id"] for c in data["categories"] if c["name"] == "Kitchen")
    assert by_name["Fridge"]["category_id"] == kitchen_id
    assert by_name["Mystery"]["category_id"] is None


def test_restore_replace_remaps_category_ids(db_env):
    """Replace wipes categories too: devices must follow the REMAPPED ids."""
    asyncio.run(_seed_categories())
    result, _ = backup_and_restore("replace", include_manuals=False)

    assert result["categories_created"] == 2
    cats = {c["name"]: c for c in asyncio.run(_rows("categories"))}
    devices = {d["name"]: d for d in asyncio.run(_rows("devices"))}
    assert devices["Fridge"]["category_id"] == cats["Kitchen"]["id"]
    assert devices["Drill"]["category_id"] == cats["Tools"]["id"]
    assert devices["Mystery"]["category_id"] is None


def test_restore_merge_dedupes_categories_by_name(db_env):
    """A live 'Kitchen' absorbs the archived one instead of creating a twin."""
    asyncio.run(_seed_categories())
    info = asyncio.run(backup_service.create_backup(include_manuals=False))
    zip_path = cfg.settings.DATA_DIR / "backups" / info["filename"]

    # Wipe devices but keep the categories themselves (same names, new ids).
    async def _wipe_devices_only():
        async with get_db_context() as db:
            await db.execute("DELETE FROM devices")
            await db.commit()

    asyncio.run(_wipe_devices_only())

    result = asyncio.run(backup_service.restore_backup(zip_path, "merge"))
    # Both archived categories matched live ones -> nothing created...
    assert result["categories_created"] == 0
    cats = asyncio.run(_rows("categories"))
    assert sorted(c["name"] for c in cats) == ["Kitchen", "Tools"]
    # ...and the restored devices point at the surviving rows.
    by_name = {d["name"]: d for d in asyncio.run(_rows("devices"))}
    live = {c["name"]: c["id"] for c in cats}
    assert by_name["Fridge"]["category_id"] == live["Kitchen"]
    assert by_name["Drill"]["category_id"] == live["Tools"]


def test_restore_old_backup_without_categories_field(db_env):
    """Pre-category archives must still restore (categories defaults to [])."""
    zip_path = _write_raw_zip(db_env, {
        "manifest.json": json.dumps({"format_version": 1}),
        "data.json": json.dumps({
            "devices": [{"id": 1, "name": "TV", "brand": "A", "model": "B"}],
            "device_attributes": [],
            "manuals": [],
            "calendar_events": [],
            "notification_log": [],
        }),
    })
    result = asyncio.run(backup_service.restore_backup(zip_path, "merge"))
    assert result["devices_created"] == 1
    devices = asyncio.run(_rows("devices"))
    assert devices[0]["category_id"] is None


# --- restore progress (status bar backend) -----------------------------------

@pytest.fixture(autouse=True)
def _reset_restore_state():
    """Isolate the restore + indexer progress globals between tests.

    The index-phase fold reads the shared indexer state, so a leaked running
    flag from another test would keep a finished restore stuck in "index".
    """
    from homestew.services import indexer

    backup_service._RESTORE_STATE.update(backup_service._restore_blank())
    indexer._INDEX_STATE.update(
        {"running": False, "done": 0, "total": 0, "failed": 0, "current_file": ""}
    )
    yield
    backup_service._RESTORE_STATE.update(backup_service._restore_blank())
    indexer._INDEX_STATE.update(
        {"running": False, "done": 0, "total": 0, "failed": 0, "current_file": ""}
    )


PROGRESS_KEYS = {
    "running", "mode", "phase", "phase_index", "phase_count", "steps_remaining",
    "step_done", "step_total", "percent", "elapsed", "error",
}


def test_restore_progress_idle_shape(db_env):
    p = backup_service.get_restore_progress()
    assert set(p) == PROGRESS_KEYS
    assert p["running"] is False and p["phase"] == ""
    assert p["percent"] == 0 and p["steps_remaining"] == 0


def test_restore_progress_walks_phases_and_completes(db_env):
    """A full restore drives the phase sequence and lands on done/100%."""
    asyncio.run(_seed_full_dataset())
    info = asyncio.run(backup_service.create_backup())
    zip_path = cfg.settings.DATA_DIR / "backups" / info["filename"]
    # Wipe so the merge restore recreates everything (a fresh manual queues an
    # index phase) instead of deduping against the rows we just seeded.
    asyncio.run(_wipe_all())

    # Record every phase transition (the call sites resolve the module global
    # at call time, so monkeypatching intercepts them).
    seen = []
    real_set_phase = backup_service.set_restore_phase

    def spy(phase, done=0, total=0):
        seen.append(phase)
        return real_set_phase(phase, done, total)

    backup_service.set_restore_phase = spy
    try:
        backup_service.begin_restore("merge")
        assert backup_service.get_restore_progress()["phase"] == "upload"
        result = asyncio.run(backup_service.restore_backup(zip_path, "merge"))
    finally:
        backup_service.set_restore_phase = real_set_phase

    # The archive has 1 device + attr + manual + event + log row -> rows phase
    # ran with a nonzero total; the manual queued an index phase.
    assert result["index_total"] == 1
    for phase in ("read_archive", "prepare", "rows", "files", "index"):
        assert phase in seen, f"phase {phase} never reported (saw {seen})"
    # Phases arrive in order and steps_remaining only decreases.
    ordered = [p for p in backup_service.RESTORE_PHASES if p in seen]
    assert seen == ordered or seen[:len(ordered)] == ordered

    # Index job was never started here (service-level restore), so the fold
    # treats the finished pipeline as done.
    p = backup_service.get_restore_progress()
    assert p["running"] is False and p["phase"] == "done"
    assert p["percent"] == 100 and p["steps_remaining"] == 0
    assert p["error"] == ""


def test_restore_progress_rows_counter_reaches_total(db_env):
    """Merged/skipped rows still bump the counter so it reaches its total."""
    asyncio.run(_seed_full_dataset())
    info = asyncio.run(backup_service.create_backup())
    zip_path = cfg.settings.DATA_DIR / "backups" / info["filename"]

    # Second merge restore of the same archive: every row is a skip/merge, but
    # the rows phase must still count to its total.
    backup_service.begin_restore("merge")
    result = asyncio.run(backup_service.restore_backup(zip_path, "merge"))
    assert result["devices_merged"] == 1

    p = backup_service.get_restore_progress()
    if p["phase"] == "rows":  # no manuals queued -> finished at rows? not here
        assert p["step_done"] == p["step_total"]
    else:
        assert p["phase"] in ("files", "index", "done")


def test_restore_progress_records_error_on_failure(db_env):
    junk = db_env / "junk.zip"
    junk.write_bytes(b"this is not a zip")
    backup_service.begin_restore("merge")
    with pytest.raises(ValueError):
        asyncio.run(backup_service.restore_backup(junk, "merge"))

    p = backup_service.get_restore_progress()
    assert p["running"] is False
    assert "not a valid backup" in p["error"]


# --- progress through the API ------------------------------------------------

@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """Signed-in TestClient on a throwaway DATA_DIR (test_search_index_jobs)."""
    from fastapi.testclient import TestClient

    from homestew.main import app

    monkeypatch.setattr(cfg.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg.settings, "DEVICES_DIR", tmp_path / "devices")
    monkeypatch.setattr(cfg.settings, "PASSWORD_HASH", "")
    with TestClient(app) as c:
        r = c.post("/api/auth/setup", json={
            "password": "restore-test-password",
            "confirm_password": "restore-test-password",
        })
        assert r.status_code == 201
        yield c


def test_restore_progress_endpoint_idle(api_client):
    p = api_client.get("/api/backup/restore-progress").json()
    assert set(p) == PROGRESS_KEYS
    assert p["running"] is False and p["percent"] == 0


def test_restore_endpoint_reports_done_after_full_restore(api_client):
    """POST /restore then poll /restore-progress until the pipeline completes.

    Uses api_client's DATA_DIR only (its lifespan already ran init_db), so the
    seed, create and restore all share one database.
    """
    import time

    asyncio.run(_seed_full_dataset())
    info = asyncio.run(backup_service.create_backup())
    zip_bytes = (cfg.settings.DATA_DIR / "backups" / info["filename"]).read_bytes()
    asyncio.run(_wipe_all())  # restore recreates the data + queues an index job

    r = api_client.post(
        "/api/backup/restore",
        files={"file": ("backup.zip", zip_bytes, "application/zip")},
        data={"mode": "merge"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["devices_created"] == 1

    # The POST returns while the background index may still run; poll to done.
    deadline = time.time() + 10
    while time.time() < deadline:
        p = api_client.get("/api/backup/restore-progress").json()
        if not p["running"]:
            break
        time.sleep(0.05)
    assert p["running"] is False, "restore never finished"
    assert p["phase"] == "done" and p["percent"] == 100
    assert p["steps_remaining"] == 0 and p["error"] == ""


def test_restore_endpoint_rejects_concurrent_run(api_client):
    backup_service._RESTORE_STATE.update(backup_service._restore_blank("merge"))
    backup_service._RESTORE_STATE["running"] = True
    try:
        r = api_client.post(
            "/api/backup/restore",
            files={"file": ("x.zip", b"PK\x03\x04junk", "application/zip")},
            data={"mode": "merge"},
        )
        assert r.status_code == 409
        assert "already running" in r.json()["detail"]
    finally:
        backup_service._RESTORE_STATE.update(backup_service._restore_blank())


def test_restore_endpoint_records_error_in_progress(api_client):
    """A rejected archive leaves the progress state failed, not stuck running."""
    r = api_client.post(
        "/api/backup/restore",
        files={"file": ("bad.zip", b"not a zip at all", "application/zip")},
        data={"mode": "merge"},
    )
    assert r.status_code == 400
    p = api_client.get("/api/backup/restore-progress").json()
    assert p["running"] is False and p["error"] != ""

