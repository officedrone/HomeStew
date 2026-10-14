"""Device category management API endpoints.

Categories are user-defined groupings for the Devices page (appliances,
kitchen, vehicles...). A device with ``category_id`` NULL renders in the
"Uncategorized" bucket, which is a frontend concept — there is no row for it.

Runtime connections do not enable the SQLite foreign_keys pragma (see
``db.get_db``), so deleting a category nulls its devices explicitly rather
than relying on ON DELETE SET NULL firing.
"""
import logging
import sqlite3

from fastapi import APIRouter, HTTPException, status
from typing import List, Optional

from homestew.db import get_db_context
from homestew.models.schemas import (
    CategoryCreate,
    CategoryReorderRequest,
    CategoryResponse,
    CategoryUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/categories", tags=["categories"])


async def _fetch_category(db, category_id: int) -> Optional[sqlite3.Row]:
    cursor = await db.execute(
        "SELECT id, name, icon, sort_order, created_at FROM categories WHERE id = ?",
        (category_id,),
    )
    return await cursor.fetchone()


def _row_to_response(row, device_count: int = 0) -> CategoryResponse:
    return CategoryResponse(
        id=row["id"],
        name=row["name"],
        icon=row["icon"],
        sort_order=row["sort_order"],
        device_count=device_count,
    )


@router.get("", response_model=List[CategoryResponse])
async def list_categories():
    """List all categories in display order with their device counts."""
    async with get_db_context() as db:
        cursor = await db.execute(
            """
            SELECT c.id, c.name, c.icon, c.sort_order, COUNT(d.id) AS device_count
            FROM categories c
            LEFT JOIN devices d ON d.category_id = c.id
            GROUP BY c.id
            ORDER BY c.sort_order, c.id
            """
        )
        return [_row_to_response(row, row["device_count"]) for row in await cursor.fetchall()]


@router.post("", response_model=CategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_category(category: CategoryCreate):
    """Create a new (initially empty) category at the end of the list."""
    name = category.name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Category name cannot be blank",
        )
    async with get_db_context() as db:
        cursor = await db.execute("SELECT MAX(sort_order) AS m FROM categories")
        row = await cursor.fetchone()
        next_order = (row["m"] if row and row["m"] is not None else -1) + 1
        try:
            cursor = await db.execute(
                """
                INSERT INTO categories (name, icon, sort_order)
                VALUES (?, ?, ?)
                RETURNING id, name, icon, sort_order, created_at
                """,
                (name, category.icon, next_order),
            )
            new_row = await cursor.fetchone()
            await db.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A category named '{name}' already exists",
            )
        return _row_to_response(new_row, 0)


@router.put("/reorder", status_code=status.HTTP_204_NO_CONTENT)
async def reorder_categories(payload: CategoryReorderRequest):
    """Persist the section order: sort_order = position in ``category_ids``.

    Registered before ``/{category_id}`` so the literal path wins over the
    int-typed route (a "reorder" id would otherwise 422).
    """
    async with get_db_context() as db:
        cursor = await db.execute("SELECT id FROM categories")
        known = {row["id"] for row in await cursor.fetchall()}
        unknown = [cid for cid in payload.category_ids if cid not in known]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown category ids: {unknown}",
            )
        for position, cid in enumerate(payload.category_ids):
            await db.execute(
                "UPDATE categories SET sort_order = ? WHERE id = ?", (position, cid)
            )
        await db.commit()


@router.put("/{category_id}", response_model=CategoryResponse)
async def update_category(category_id: int, category: CategoryUpdate):
    """Rename a category and/or change its icon. Devices are unaffected."""
    name = category.name.strip() if category.name is not None else None
    if category.name is not None and not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Category name cannot be blank",
        )
    async with get_db_context() as db:
        row = await _fetch_category(db, category_id)
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Category {category_id} not found",
            )
        fields, params = [], []
        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if category.icon is not None:
            # An empty string clears the icon (generic glyph).
            fields.append("icon = ?")
            params.append(category.icon or None)
        if fields:
            try:
                await db.execute(
                    f"UPDATE categories SET {', '.join(fields)} WHERE id = ?",
                    (*params, category_id),
                )
                await db.commit()
            except sqlite3.IntegrityError:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"A category named '{name}' already exists",
                )
        updated = await _fetch_category(db, category_id)
        cursor = await db.execute(
            "SELECT COUNT(*) AS n FROM devices WHERE category_id = ?", (category_id,)
        )
        count_row = await cursor.fetchone()
        return _row_to_response(updated, count_row["n"])


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(category_id: int):
    """Delete a category; its devices move to Uncategorized (never deleted)."""
    async with get_db_context() as db:
        row = await _fetch_category(db, category_id)
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Category {category_id} not found",
            )
        # FKs are off on runtime connections: null the devices explicitly.
        await db.execute(
            "UPDATE devices SET category_id = NULL WHERE category_id = ?",
            (category_id,),
        )
        await db.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        await db.commit()
