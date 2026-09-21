"""Warranty date computation shared by the REST API and the LLM tools.

``warranty_end`` is either given explicitly by the caller or derived from
the purchase date plus a length/unit. The arithmetic lives here (rather than
inside ``api/devices.py``) so the device-management tool can reuse the exact
same rule without importing the API layer - services must not depend on
``homestew.api.*``.

Month/year lengths use :func:`calendar_engine.add_months`, which clamps the
day-of-month (Jan 31 + 1 month -> Feb 28/29) instead of overflowing into the
next month, matching what the device forms do in the browser.
"""
from datetime import date, timedelta
from typing import Optional, Tuple

from homestew.services.calendar_engine import add_months

WARRANTY_UNITS = ("days", "months", "years")


def compute_warranty_end(
    purchase_date: Optional[date],
    warranty_length: Optional[int],
    warranty_unit: Optional[str],
    warranty_end: Optional[date],
) -> Optional[date]:
    """Return the effective warranty end date.

    An explicit ``warranty_end`` always wins. Otherwise the end is derived
    from purchase date + length/unit; returns None when any ingredient is
    missing or the unit is unknown (never raises - callers surface a text
    error themselves).
    """
    if warranty_end is not None:
        return warranty_end
    if purchase_date is None or warranty_length is None:
        return None
    if warranty_unit == "days":
        return purchase_date + timedelta(days=warranty_length)
    if warranty_unit == "months":
        return add_months(purchase_date, warranty_length)
    if warranty_unit == "years":
        return add_months(purchase_date, 12 * warranty_length)
    return None


def warranty_fields(
    purchase_date: Optional[date],
    warranty_length: Optional[int],
    warranty_unit: Optional[str],
    warranty_end: Optional[date],
) -> Tuple[Optional[str], Optional[int], Optional[str], Optional[str]]:
    """Normalised INSERT/UPDATE tuple: (purchase, length, unit, end).

    Dates come back as ISO strings (or None), ready to bind to SQLite. The
    end date is auto-computed when not supplied explicitly.
    """
    end = compute_warranty_end(purchase_date, warranty_length, warranty_unit, warranty_end)
    return (
        purchase_date.isoformat() if purchase_date else None,
        warranty_length,
        warranty_unit,
        end.isoformat() if end else None,
    )
