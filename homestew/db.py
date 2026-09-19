"""Database initialization and utilities."""
import aiosqlite
import sqlite3
from contextlib import asynccontextmanager

from homestew.config import settings


@asynccontextmanager
async def get_db():
    """Get a database connection as an async context manager."""
    db_path = settings.DATA_DIR / "homestew.db"
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    # SQLite has one writer at a time; without this, a second concurrent write
    # (e.g. indexing a manual while another request updates a row) fails
    # instantly with "database is locked" instead of waiting briefly.
    await db.execute("PRAGMA busy_timeout = 5000")

    try:
        yield db
    finally:
        await db.close()


# Alias for backwards compatibility
get_db_context = get_db


async def init_db():
    """Initialize the database with required tables."""
    # Ensure data directory exists
    settings.DEVICES_DIR.mkdir(parents=True, exist_ok=True)
    
    db_path = settings.DATA_DIR / "homestew.db"
    
    async with aiosqlite.connect(db_path) as db:
        # Enable foreign keys
        await db.execute("PRAGMA foreign_keys = ON")
        
        # Create devices table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                brand TEXT NOT NULL,
                model TEXT NOT NULL,
                description TEXT,
                serial_number TEXT,
                product_number TEXT,
                purchase_date DATE,
                warranty_length INTEGER,
                warranty_unit TEXT,
                warranty_end DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Migrate existing devices table to add new columns if they don't exist
        try:
            await db.execute("ALTER TABLE devices ADD COLUMN serial_number TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        try:
            await db.execute("ALTER TABLE devices ADD COLUMN product_number TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        # Warranty fields: purchase date + length (value & unit) + computed end date
        for col in (
            "purchase_date DATE",
            "warranty_length INTEGER",
            "warranty_unit TEXT",
            "warranty_end DATE",
        ):
            try:
                await db.execute(f"ALTER TABLE devices ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # Column already exists
        
        # Audit timestamps: SQLite rejects CURRENT_TIMESTAMP as an ALTER
        # default, so add the column plainly and backfill existing rows from
        # created_at. New inserts/updates always set it explicitly in SQL.
        try:
            await db.execute("ALTER TABLE devices ADD COLUMN updated_at TIMESTAMP")
        except sqlite3.OperationalError:
            pass  # Column already exists
        await db.execute(
            """
            UPDATE devices
            SET updated_at = COALESCE(created_at, CURRENT_TIMESTAMP)
            WHERE updated_at IS NULL
            """
        )
        
        # Create manuals table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS manuals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                page_count INTEGER,
                indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
            )
        """)
        
        # Create custom attributes table for user-defined fields
        await db.execute("""
            CREATE TABLE IF NOT EXISTS device_attributes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL,
                attribute_name TEXT NOT NULL,
                attribute_value TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
            )
        """)
        
        # Create index for device attributes
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_device_attributes_device 
            ON device_attributes(device_id)
        """)

        # Create calendar events table - periodic maintenance reminders tied
        # (usually) to a device. device_id is nullable so an event can exist
        # without one; when set it cascades on device delete. The schedule is
        # stored as an anchor date + recurrence rule and the next due date is
        # always computed from it (see services/calendar_engine.py), never
        # mutated, so editing/completing can't drift the series.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS calendar_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER,
                title TEXT NOT NULL,
                description TEXT,
                start_date DATE NOT NULL,
                start_time TIME,
                recurrence_type TEXT NOT NULL DEFAULT 'none',
                interval INTEGER NOT NULL DEFAULT 1,
                last_completed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
            )
        """)

        # Create index for calendar events by device
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_calendar_events_device 
            ON calendar_events(device_id)
        """)

        # Notification ledger - one row per (event, occurrence, kind) already
        # delivered. The background loop re-scans every tick, so this is what
        # makes a due/overdue alert fire exactly once; recurring events re-alert
        # on future occurrences because the due date differs. Rows cascade with
        # their event. See services/notifier.py.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS notification_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                due_date DATE NOT NULL,
                kind TEXT NOT NULL,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(event_id, due_date, kind),
                FOREIGN KEY (event_id) REFERENCES calendar_events(id) ON DELETE CASCADE
            )
        """)
        
        # Create FTS5 virtual table for PDF content search.
        #
        # The `trigram` tokenizer indexes every 3-character sequence, which
        # gives us substring / partial-word matching (e.g. searching for
        # "processor" also finds "Microprocessor"). Metadata columns are
        # UNINDEXED so they are stored for display/linking but never matched,
        # keeping results scoped to the actual page text.
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS pdf_index USING fts5(
                device_id UNINDEXED,
                manual_id UNINDEXED,
                filename UNINDEXED,
                page_number UNINDEXED,
                content,
                tokenize = 'trigram'
            )
        """)

        # Create indexes for better performance
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_manuals_device 
            ON manuals(device_id)
        """)

        await db.commit()

    # If an older index (no trigram tokenizer / no manual_id column) exists it
    # cannot be altered in place, so rebuild it and re-index every manual.
    if await _fts_index_needs_rebuild():
        await rebuild_search_index()


async def _fts_index_needs_rebuild() -> bool:
    """Return True if pdf_index predates the trigram/manual_id schema."""
    db_path = settings.DATA_DIR / "homestew.db"
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='pdf_index'"
        )
        row = await cursor.fetchone()
    if not row or not row[0]:
        return False
    ddl = row[0].lower()
    return ("trigram" not in ddl) or ("manual_id" not in ddl)


async def rebuild_search_index():
    """Drop and recreate the FTS index, then re-index every stored manual."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = settings.DATA_DIR / "homestew.db"

    async with aiosqlite.connect(db_path) as db:
        await db.execute("DROP TABLE IF EXISTS pdf_index")
        await db.execute("""
            CREATE VIRTUAL TABLE pdf_index USING fts5(
                device_id UNINDEXED,
                manual_id UNINDEXED,
                filename UNINDEXED,
                page_number UNINDEXED,
                content,
                tokenize = 'trigram'
            )
        """)
        await db.commit()

    logger.info("Rebuilt search index with trigram tokenizer; re-indexing manuals...")
    from homestew.services.indexer import reindex_all_manuals

    count = await reindex_all_manuals()
    logger.info(f"Search index rebuild complete: {count} manual(s) re-indexed")


# Alias for backwards compatibility
get_db_context = get_db
