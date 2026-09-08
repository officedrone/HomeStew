"""Device management API endpoints."""
from fastapi import APIRouter, HTTPException, status
from typing import List

from homebrain.db import get_db_context
from homebrain.models.schemas import Device, DeviceCreate, DeviceResponse, DeviceAttribute

router = APIRouter(prefix="/devices", tags=["devices"])


@router.post("", response_model=DeviceResponse, status_code=status.HTTP_201_CREATED)
async def create_device(device: DeviceCreate):
    """Add a new device to HomeBrain."""
    async with get_db_context() as db:
        cursor = await db.execute(
            """
            INSERT INTO devices (name, brand, model, description, serial_number, product_number)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING id, name, brand, model, description, serial_number, product_number, created_at
            """,
            (
                device.name, 
                device.brand, 
                device.model, 
                device.description or "",
                device.serial_number,
                device.product_number
            )
        )
        
        row = await cursor.fetchone()
        await db.commit()
        
        return DeviceResponse(
            id=row['id'],
            name=row['name'],
            brand=row['brand'],
            model=row['model'],
            description=row['description'],
            serial_number=row['serial_number'],
            product_number=row['product_number'],
            created_at=row['created_at'],
            manual_count=0
        )


@router.get("", response_model=List[DeviceResponse])
async def list_devices():
    """List all devices with their manual counts."""
    async with get_db_context() as db:
        cursor = await db.execute(
            """
            SELECT 
                d.id, d.name, d.brand, d.model, d.description, d.serial_number, d.product_number, d.created_at,
                COUNT(m.id) as manual_count
            FROM devices d
            LEFT JOIN manuals m ON d.id = m.device_id
            GROUP BY d.id
            ORDER BY d.created_at DESC
            """
        )
        
        rows = await cursor.fetchall()
        
        return [
            DeviceResponse(
                id=row['id'],
                name=row['name'],
                brand=row['brand'],
                model=row['model'],
                description=row['description'],
                serial_number=row['serial_number'],
                product_number=row['product_number'],
                created_at=row['created_at'],
                manual_count=row['manual_count'] or 0
            )
            for row in rows
        ]


@router.get("/{device_id}", response_model=DeviceResponse)
async def get_device(device_id: int):
    """Get a specific device with details."""
    async with get_db_context() as db:
        cursor = await db.execute(
            """
            SELECT 
                d.id, d.name, d.brand, d.model, d.description, d.serial_number, d.product_number, d.created_at,
                COUNT(m.id) as manual_count
            FROM devices d
            LEFT JOIN manuals m ON d.id = m.device_id
            WHERE d.id = ?
            GROUP BY d.id
            """,
            (device_id,)
        )
        
        row = await cursor.fetchone()
        
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {device_id} not found"
            )
        
        # Get device attributes
        attr_cursor = await db.execute(
            "SELECT * FROM device_attributes WHERE device_id = ?", (device_id,)
        )
        attrs = await attr_cursor.fetchall()
        
        attributes = [
            DeviceAttribute(
                id=attr['id'],
                device_id=attr['device_id'],
                attribute_name=attr['attribute_name'],
                attribute_value=attr['attribute_value'],
                created_at=attr['created_at']
            )
            for attr in attrs
        ]
        
        return DeviceResponse(
            id=row['id'],
            name=row['name'],
            brand=row['brand'],
            model=row['model'],
            description=row['description'],
            serial_number=row['serial_number'],
            product_number=row['product_number'],
            created_at=row['created_at'],
            manual_count=row['manual_count'] or 0,
            attributes=attributes
        )


@router.put("/{device_id}", response_model=DeviceResponse)
async def update_device(device_id: int, device: DeviceCreate):
    """Update an existing device."""
    async with get_db_context() as db:
        # Check if device exists
        cursor = await db.execute("SELECT id FROM devices WHERE id = ?", (device_id,))
        if not await cursor.fetchone():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {device_id} not found"
            )
        
        # Update device
        cursor = await db.execute(
            """
            UPDATE devices 
            SET name = ?, brand = ?, model = ?, description = ?, serial_number = ?, product_number = ?
            WHERE id = ?
            RETURNING id, name, brand, model, description, serial_number, product_number, created_at
            """,
            (
                device.name, 
                device.brand, 
                device.model, 
                device.description or "",
                device.serial_number,
                device.product_number,
                device_id
            )
        )
        
        row = await cursor.fetchone()
        await db.commit()
        
        # Get updated manual count
        cursor = await db.execute(
            "SELECT COUNT(*) as count FROM manuals WHERE device_id = ?", (device_id,)
        )
        count_row = await cursor.fetchone()
        
        # Get device attributes
        attr_cursor = await db.execute(
            "SELECT * FROM device_attributes WHERE device_id = ?", (device_id,)
        )
        attrs = await attr_cursor.fetchall()
        
        attributes = [
            DeviceAttribute(
                id=attr['id'],
                device_id=attr['device_id'],
                attribute_name=attr['attribute_name'],
                attribute_value=attr['attribute_value'],
                created_at=attr['created_at']
            )
            for attr in attrs
        ]
        
        return DeviceResponse(
            id=row['id'],
            name=row['name'],
            brand=row['brand'],
            model=row['model'],
            description=row['description'],
            serial_number=row['serial_number'],
            product_number=row['product_number'],
            created_at=row['created_at'],
            manual_count=count_row['count'] or 0,
            attributes=attributes
        )


@router.post("/{device_id}/attributes", response_model=DeviceAttribute)
async def add_device_attribute(device_id: int, attribute_name: str, attribute_value: str):
    """Add a custom attribute to a device."""
    async with get_db_context() as db:
        # Check if device exists
        cursor = await db.execute("SELECT id FROM devices WHERE id = ?", (device_id,))
        if not await cursor.fetchone():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {device_id} not found"
            )
        
        # Add attribute
        cursor = await db.execute(
            """
            INSERT INTO device_attributes (device_id, attribute_name, attribute_value)
            VALUES (?, ?, ?)
            RETURNING id, device_id, attribute_name, attribute_value, created_at
            """,
            (device_id, attribute_name, attribute_value)
        )
        
        row = await cursor.fetchone()
        await db.commit()
        
        return DeviceAttribute(
            id=row['id'],
            device_id=row['device_id'],
            attribute_name=row['attribute_name'],
            attribute_value=row['attribute_value'],
            created_at=row['created_at']
        )


@router.delete("/{device_id}/attributes/{attribute_id}")
async def remove_device_attribute(device_id: int, attribute_id: int):
    """Remove a custom attribute from a device."""
    async with get_db_context() as db:
        # Check if attribute exists and belongs to this device
        cursor = await db.execute(
            "SELECT id FROM device_attributes WHERE id = ? AND device_id = ?", 
            (attribute_id, device_id)
        )
        if not await cursor.fetchone():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Attribute {attribute_id} not found for this device"
            )
        
        # Delete attribute
        await db.execute(
            "DELETE FROM device_attributes WHERE id = ?", (attribute_id,)
        )
        await db.commit()


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(device_id: int):
    """Delete a device and all its manuals."""
    async with get_db_context() as db:
        # Check if device exists
        cursor = await db.execute("SELECT id FROM devices WHERE id = ?", (device_id,))
        if not await cursor.fetchone():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {device_id} not found"
            )
        
        # Delete device (cascades to manuals and attributes)
        await db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        await db.commit()
