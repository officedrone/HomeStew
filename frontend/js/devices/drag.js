// HomeStew - Devices drag-and-drop via Pointer Events (mouse + touch, no libs).
// Native HTML5 DnD is deliberately not used: it does not fire on touch at all.
// Instead each .drag-grip button captures its pointer; after a small movement
// threshold a floating clone (.drag-ghost) follows the finger/cursor while
// elementFromPoint() decides what is underneath:
//   - device grip  -> drop into a [data-drop-category] grid (optionally at an
//     insertion point next to another card) => PUT /api/devices/reorder with
//     the target bucket's full ordered id list;
//   - section grip -> reorder category sections => PUT /api/categories/reorder.
// The drop is applied optimistically to the local arrays + re-render, and the
// server call follows; a failed call reloads the list so the UI snaps back.
// Grips set touch-action: none in CSS, which is what lets a touch drag start
// instead of scrolling; everything else keeps normal touch scrolling.


const DRAG_THRESHOLD_PX = 5;   // movement before a press becomes a drag
const DRAG_EDGE_PX = 64;       // auto-scroll band near the viewport edges
const DRAG_SCROLL_MAX_PX = 14; // px per frame when pinned to the very edge

// The single active drag session, or null. Shape:
// { kind: 'device'|'category', handle, sourceEl, pointerId, startX/Y,
//   pointerX/Y, offsetX/Y, started, ghost, targetGrid, hoverCard,
//   dropSection, dropSide, rafId }
let _drag = null;

// After a real drag the browser still synthesises a click on the nearest
// common ancestor of pointerdown/pointerup - that would toggle card expansion
// or collapse a section. Swallow exactly one click after each committed drag.
let _dragSuppressClick = false;
document.addEventListener('click', (event) => {
    if (!_dragSuppressClick) return;
    _dragSuppressClick = false;
    event.preventDefault();
    event.stopImmediatePropagation();
}, true);

// Long-press on touch would otherwise pop the context menu and cancel the drag.
document.addEventListener('contextmenu', (event) => {
    if (_drag && _drag.started) event.preventDefault();
});

// One delegated listener covers every grip, including cards/sections rendered
// later - nothing to wire per render.
document.addEventListener('pointerdown', onDragGripPointerDown);


function onDragGripPointerDown(event) {
    if (_drag) return; // one finger drags at a time
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    const grip = event.target.closest('.drag-grip');
    if (!grip) return;
    const card = grip.closest('.device-card');
    const section = grip.closest('.category-section');
    if (!card && !section) return;

    // Stop text selection / native image drag before the threshold is reached.
    event.preventDefault();
    _drag = {
        kind: card ? 'device' : 'category',
        handle: grip,
        sourceEl: card || section,
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        pointerX: event.clientX,
        pointerY: event.clientY,
        offsetX: 0,
        offsetY: 0,
        started: false,
        ghost: null,
        targetGrid: null,
        hoverCard: null,
        dropSection: null,
        dropSide: null,
        rafId: 0,
    };
    // Pointer capture keeps move/up events flowing even when the finger
    // leaves the (tiny) grip; elementFromPoint() still hit-tests the page.
    try { grip.setPointerCapture(event.pointerId); } catch (e) { /* older engines */ }
    window.addEventListener('pointermove', onDragPointerMove, { passive: false });
    window.addEventListener('pointerup', onDragPointerUp);
    window.addEventListener('pointercancel', cancelDrag);
}


function onDragPointerMove(event) {
    if (!_drag || event.pointerId !== _drag.pointerId) return;
    const dx = event.clientX - _drag.startX;
    const dy = event.clientY - _drag.startY;
    if (!_drag.started) {
        if (Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return;
        beginDragVisuals();
    }
    event.preventDefault(); // no text selection / pull-to-refresh mid-drag
    _drag.pointerX = event.clientX;
    _drag.pointerY = event.clientY;
    positionGhost(event.clientX, event.clientY);
    updateDropTarget(event.clientX, event.clientY);
}


// Create the floating clone and mark the source dimmed once the threshold
// has been crossed.
function beginDragVisuals() {
    const rect = _drag.sourceEl.getBoundingClientRect();
    const ghostSource = _drag.kind === 'device'
        ? _drag.sourceEl
        : _drag.sourceEl.querySelector('.category-header') || _drag.sourceEl;
    const ghost = ghostSource.cloneNode(true);
    ghost.classList.add('drag-ghost');
    // The clone is decorative: strip ids so nothing queries hit it twice.
    ghost.querySelectorAll('[id]').forEach(node => node.removeAttribute('id'));
    ghost.style.width = `${rect.width}px`;
    document.body.appendChild(ghost);

    _drag.ghost = ghost;
    _drag.offsetX = _drag.pointerX - rect.left;
    _drag.offsetY = _drag.pointerY - rect.top;
    _drag.started = true;
    _drag.sourceEl.classList.add('drag-source');
    document.body.classList.add('is-dragging');
    _drag.rafId = requestAnimationFrame(dragAutoScrollTick);
}


function positionGhost(x, y) {
    if (!_drag || !_drag.ghost) return;
    _drag.ghost.style.left = `${x - _drag.offsetX}px`;
    _drag.ghost.style.top = `${y - _drag.offsetY}px`;
}


// Keep the page scrolling while the pointer hovers near the top/bottom edge,
// so long lists and small phones stay usable.
function dragAutoScrollTick() {
    if (!_drag || !_drag.started) return;
    const y = _drag.pointerY;
    const h = window.innerHeight;
    let delta = 0;
    if (y < DRAG_EDGE_PX) delta = -Math.ceil((DRAG_EDGE_PX - y) / 5);
    else if (y > h - DRAG_EDGE_PX) delta = Math.ceil((y - (h - DRAG_EDGE_PX)) / 5);
    delta = Math.max(-DRAG_SCROLL_MAX_PX, Math.min(DRAG_SCROLL_MAX_PX, delta));
    if (delta) {
        window.scrollBy(0, delta);
        // The page moved under a stationary pointer: refresh ghost + target.
        positionGhost(_drag.pointerX, _drag.pointerY);
        updateDropTarget(_drag.pointerX, _drag.pointerY);
    }
    _drag.rafId = requestAnimationFrame(dragAutoScrollTick);
}


// ---------------------------------------------------------------------------
// Drop-target detection & highlight
// ---------------------------------------------------------------------------

// Strip the highlight classes only - safe to call right before a commit,
// which still needs targetGrid/hoverCard/dropSection on the session.
function removeDropClasses(session) {
    if (!session) return;
    if (session.targetGrid) session.targetGrid.classList.remove('drop-active');
    if (session.hoverCard) session.hoverCard.classList.remove('drop-hover');
    if (session.dropSection) session.dropSection.classList.remove('drop-before', 'drop-after');
}

function clearDropHighlights() {
    if (!_drag) return;
    removeDropClasses(_drag);
    _drag.targetGrid = null;
    _drag.hoverCard = null;
    _drag.dropSection = null;
    _drag.dropSide = null;
}

// elementFromPoint is safe here: the ghost has pointer-events: none, so it is
// never the hit itself.
function updateDropTarget(x, y) {
    clearDropHighlights();
    const under = document.elementFromPoint(x, y);
    if (!under || !under.closest('#devices-grid')) return;

    if (_drag.kind === 'device') {
        // Any point inside a section (header or body) targets that section's
        // grid - dropping onto a collapsed header appends to its category.
        const section = under.closest('.category-section');
        const grid = section ? section.querySelector('.devices-grid[data-drop-category]') : null;
        if (!grid) return;
        _drag.targetGrid = grid;
        grid.classList.add('drop-active');
        // Insert before the card whose centre is nearest, when there is one.
        const near = nearestCardAtPoint(grid, x, y);
        if (near) {
            _drag.hoverCard = near;
            near.classList.add('drop-hover');
        }
    } else {
        // Section reorder: insertion point between real sections, decided by
        // vertical midpoints. Uncategorized has no grip and always trails, so
        // it is skipped - dropping anywhere in/below it appends at the end.
        const reals = realSectionEls().filter(el => el !== _drag.sourceEl);
        let target = null;
        for (const el of reals) {
            const r = el.getBoundingClientRect();
            if (y < r.top + r.height / 2) { target = el; break; }
        }
        if (!target && reals.length === 0) return;
        if (target) {
            _drag.dropSection = target;
            _drag.dropSide = 'before';
            target.classList.add('drop-before');
        } else {
            const last = reals[reals.length - 1];
            _drag.dropSection = last;
            _drag.dropSide = 'after';
            last.classList.add('drop-after');
        }
    }
}

// The card in `grid` (excluding the dragged one) whose centre is closest to
// the pointer, or null when the grid holds no other cards. Dropping "on" a
// card means inserting before it; anything else appends at the end.
function nearestCardAtPoint(grid, x, y) {
    let best = null;
    let bestDist = Infinity;
    grid.querySelectorAll('.device-card').forEach(card => {
        if (card === _drag.sourceEl) return;
        const r = card.getBoundingClientRect();
        const cx = r.left + r.width / 2;
        const cy = r.top + r.height / 2;
        const dist = (x - cx) * (x - cx) + (y - cy) * (y - cy);
        if (dist < bestDist) { bestDist = dist; best = card; }
    });
    return best;
}

// Category sections that map to a real category row (Uncategorized has '').
function realSectionEls() {
    return [...document.querySelectorAll('#devices-grid .category-section')]
        .filter(el => el.dataset.categoryId !== '');
}


// ---------------------------------------------------------------------------
// Drop commit
// ---------------------------------------------------------------------------

function onDragPointerUp(event) {
    if (!_drag || event.pointerId !== _drag.pointerId) return;
    const session = _drag;
    if (!session.started) {
        // Never crossed the threshold: it was a plain click on the grip.
        cancelDrag();
        return;
    }
    // Remove the highlight CLASSES but keep the recorded target on the
    // session - the commit functions read targetGrid/hoverCard/dropSection.
    // (A committed drop re-renders anyway; a dropped-outside one leaves the
    // render untouched, which is exactly right.)
    removeDropClasses(session);
    if (session.kind === 'device') commitDeviceDrop(session);
    else commitCategoryDrop(session);
    finishDrag(session, session.started);
}


function commitDeviceDrop(session) {
    const grid = session.targetGrid;
    if (!grid) return; // dropped outside any section - keep the render as-is

    const rawCat = grid.dataset.dropCategory;
    const categoryId = rawCat === '' ? null : Number(rawCat);

    // New order of the target bucket: its current cards (minus the dragged
    // one, which may still sit in this very grid) with the dragged id spliced
    // in before the hovered card, or appended.
    const bucketIds = [...grid.querySelectorAll('.device-card')]
        .map(card => Number(card.dataset.id))
        .filter(id => id !== Number(_dragDeviceId(session)));
    const insertIdx = session.hoverCard
        ? bucketIds.indexOf(Number(session.hoverCard.dataset.id))
        : -1;
    if (insertIdx >= 0) bucketIds.splice(insertIdx, 0, Number(session.sourceEl.dataset.id));
    else bucketIds.push(Number(session.sourceEl.dataset.id));

    // Rebuild the flat device list so every bucket keeps its on-screen order
    // (renderDevicesGrid filters `devices` per category in array order). The
    // target grid contributes bucketIds - already spliced above.
    const byId = new Map(devices.map(d => [d.id, d]));
    const dragged = byId.get(Number(session.sourceEl.dataset.id));
    if (!dragged) return;
    dragged.category_id = categoryId;
    const next = [];
    document.querySelectorAll('#devices-grid .devices-grid').forEach(bucketEl => {
        const ids = bucketEl === grid
            ? bucketIds
            : [...bucketEl.querySelectorAll('.device-card')].map(card => Number(card.dataset.id));
        ids.forEach(id => { if (byId.has(id) && !next.includes(byId.get(id))) next.push(byId.get(id)); });
    });
    // Any device not rendered in a grid (shouldn't happen) keeps its spot.
    devices.forEach(d => { if (!next.includes(d)) next.push(d); });
    devices = next;

    renderDevicesGrid();
    fetch('/api/devices/reorder', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category_id: categoryId, device_ids: bucketIds }),
    }).catch(() => {
        showToast('Failed to save the new order', 'error');
        loadDevices();
    });
}


function commitCategoryDrop(session) {
    const rawCat = session.sourceEl.dataset.categoryId;
    if (rawCat === '' || rawCat === undefined) return; // Uncategorized has no grip
    const draggedId = Number(rawCat);
    if (!Number.isInteger(draggedId)) return;

    // Index among real categories, matching the highlighted insertion point.
    const reals = realSectionEls().filter(el => el !== session.sourceEl);
    let newIndex = reals.length;
    if (session.dropSide === 'before') {
        newIndex = reals.indexOf(session.dropSection);
    } else if (session.dropSection) {
        newIndex = reals.indexOf(session.dropSection) + 1;
    }

    const oldIndex = categories.findIndex(c => c.id === draggedId);
    if (oldIndex < 0) return;
    const [moved] = categories.splice(oldIndex, 1);
    categories.splice(Math.min(newIndex, categories.length), 0, moved);
    renderDevicesGrid();

    fetch('/api/categories/reorder', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category_ids: categories.map(c => c.id) }),
    }).catch(() => {
        showToast('Failed to save the section order', 'error');
        loadDevices();
    });
}


function _dragDeviceId(session) {
    return session.sourceEl.dataset.id;
}


// ---------------------------------------------------------------------------
// Teardown
// ---------------------------------------------------------------------------

function finishDrag(session, wasClickSuppressed) {
    if (session.rafId) cancelAnimationFrame(session.rafId);
    if (session.ghost) session.ghost.remove();
    document.body.classList.remove('is-dragging');
    // A committed drop re-renders (new elements); a cancelled one leaves the
    // original in place, so its dimmed class must come off here.
    session.sourceEl.classList.remove('drag-source');
    // The source element may already be gone (re-render replaced it).
    window.removeEventListener('pointermove', onDragPointerMove);
    window.removeEventListener('pointerup', onDragPointerUp);
    window.removeEventListener('pointercancel', cancelDrag);
    if (wasClickSuppressed) {
        _dragSuppressClick = true;
        setTimeout(() => { _dragSuppressClick = false; }, 0);
    }
    _drag = null;
}


function cancelDrag() {
    if (!_drag) return;
    clearDropHighlights();
    finishDrag(_drag, _drag.started);
}
