"""Database initialization and utilities."""
import aiosqlite
import sqlite3
from pathlib import Path
from contextlib import asynccontextmanager

from homebrain.config import settings


@asynccontextmanager
async def get_db():
    """Get a database connection as an async context manager."""
    db_path = settings.DATA_DIR / "homebrain.db"
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    
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
    
    db_path = settings.DATA_DIR / "homebrain.db"
    
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        
        # Create FTS5 virtual table for PDF content search
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS pdf_index USING fts5(
                device_id,
                filename,
                page_number,
                content,
                content_rowid=rowid
            )
        """)
        
        # Create indexes for better performance
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_manuals_device 
            ON manuals(device_id)
        """)
        
        await db.commit()


# Alias for backwards compatibility
get_db_context = get_db
