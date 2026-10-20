// HomeStew - Devices drag-and-drop via Pointer Events (mouse + touch, no libs).
// Native HTML5 DnD is deliberately not used: it does not fire on touch at all.
// Instead each .drag-grip button captures its pointer; after a small movement
// threshold a floating clone (.drag-ghost) follows the finger/cursor while
// elementFromPoint() decides what is underneath:
//   - device grip  -> drop into a [data-drop-category] grid (optionally at an
//     insertion point next to another card) => PUT /api/devices/reorder with
//     the target bucket's full ordered id list;
//   - section grip -> reorder category sections => PUT /api/categories/reorder
//     (the virtual Uncategorized section participates too; it has no row on
//     the server, so its slot is saved locally via saveUncategorizedPosition).
// Committed drops animate: renderDevicesGrid() rebuilds every node, so the
// old and new layouts are diffed with a FLIP pass (captureFlipState before,
// playFlip after) to glide displaced cards/sections into place, while the
// ghost flies into the dropped slot instead of vanishing on release.
// The drop is applied optimistically to the local arrays + re-render, and the
// server call follows; a failed call reloads the list so the UI snaps back.
// Grips set touch-action: none in CSS, which is what lets a touch drag start
// instead of scrolling; everything else keeps normal touch scrolling.


const DRAG_THRESHOLD_PX = 5;   // movement before a press becomes a drag
const DRAG_EDGE_PX = 64;       // auto-scroll band near the viewport edges
const DRAG_SCROLL_MAX_PX = 14; // px per frame when pinned to the very edge
const FLIP_MS = 240;           // glide duration for elements displaced by a drop
const GHOST_FLY_MS = 200;      // ghost flight from release point into its slot

// The single active drag session, or null. Shape:
// { kind: 'device'|'category', handle, sourceEl, pointerId, startX/Y,
//   pointerX/Y, offsetX/Y, started, ghost, targetGrid, hoverCard,
//   dropSection, dropSide, rafId, flyTarget }
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
        flyTarget: null,
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
        // Section reorder: insertion point between sections (Uncategorized
        // included - it drags like any other section), decided by vertical
        // midpoints.
        const others = sectionEls().filter(el => el !== _drag.sourceEl);
        if (others.length === 0) return;
        let target = null;
        for (const el of others) {
            const r = el.getBoundingClientRect();
            if (y < r.top + r.height / 2) { target = el; break; }
        }
        if (target) {
            _drag.dropSection = target;
            _drag.dropSide = 'before';
            target.classList.add('drop-before');
        } else {
            const last = others[others.length - 1];
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

// Every section on screen, in display order. The virtual Uncategorized
// section is included; its data-category-id is '' (no category row behind it).
function sectionEls() {
    return [...document.querySelectorAll('#devices-grid .category-section')];
}


// ---------------------------------------------------------------------------
// Drop animation (FLIP)
// ---------------------------------------------------------------------------

// renderDevicesGrid() rebuilds every node via innerHTML, so a committed drop
// would otherwise snap into place. FLIP fixes that: captureFlipState() records
// where things are BEFORE the re-render, playFlip() diffs against the new
// layout and glides every displaced card/section from its old spot (First-
// Last-Invert-Play). The dragged element itself is excluded - its ghost flies
// into the landing slot instead (flyGhostIntoSlot).
function flipEnabled() {
    return !(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

function captureFlipState() {
    if (!flipEnabled()) return null;
    const state = { cards: new Map(), sections: new Map() };
    document.querySelectorAll('#devices-grid .device-card').forEach(el => {
        state.cards.set(el.dataset.id, el.getBoundingClientRect());
    });
    document.querySelectorAll('#devices-grid .category-section').forEach(el => {
        const key = el.dataset.categoryKey || '';
        if (key) state.sections.set(key, el.getBoundingClientRect());
    });
    return state;
}

function playFlip(state, excludeEl) {
    if (!state) return;
    // Measure EVERYTHING first: getBoundingClientRect() includes ancestor
    // transforms, so applying a section's inverted transform before measuring
    // the cards inside it would corrupt their deltas.
    const moves = [];
    document.querySelectorAll('#devices-grid .category-section').forEach(el => {
        if (el === excludeEl) return;
        const old = state.sections.get(el.dataset.categoryKey || '');
        if (!old) return;
        const r = el.getBoundingClientRect();
        const dx = old.left - r.left;
        const dy = old.top - r.top;
        if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
        moves.push({ el, dx, dy });
    });
    document.querySelectorAll('#devices-grid .device-card').forEach(el => {
        if (el === excludeEl) return;
        const old = state.cards.get(el.dataset.id);
        if (!old) return;
        const r = el.getBoundingClientRect();
        let dx = old.left - r.left;
        let dy = old.top - r.top;
        // A card inside a section that is itself gliding rides along with it:
        // only the leftover displacement relative to its container animates.
        const host = moves.find(m => m.el.contains(el));
        if (host) { dx -= host.dx; dy -= host.dy; }
        if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
        moves.push({ el, dx, dy });
    });
    // Invert: jump every mover to its old spot with transitions off (.device-
    // card has a base transform transition that would animate this jump the
    // wrong way), then play back to zero.
    for (const { el, dx, dy } of moves) {
        el.style.transition = 'none';
        el.style.transform = `translate(${dx}px, ${dy}px)`;
    }
    // Force a reflow so the browser registers the inverted positions before
    // they animate back.
    void document.body.offsetHeight;
    for (const { el } of moves) {
        el.style.transition = `transform ${FLIP_MS}ms cubic-bezier(0.2, 0, 0.2, 1)`;
        el.style.transform = '';
        const done = (event) => {
            if (event && event.propertyName !== 'transform') return;
            el.removeEventListener('transitionend', done);
            el.style.transition = '';
        };
        el.addEventListener('transitionend', done);
        // Fallback: transitionend never fires if the element is removed or
        // hidden mid-flight, so always clean up shortly after it should end.
        setTimeout(done, FLIP_MS + 120);
    }
}

// Section drops: glide the re-rendered dragged section in from where it was
// and park the ghost on top of it until both land (same reveal trick as the
// device-drop path in commitDeviceDrop).
function animateLandedSection(session, flipState) {
    const landed = document.querySelector(
        `#devices-grid .category-section[data-category-key="${session.sourceEl.dataset.categoryKey}"]`);
    playFlip(flipState, landed);
    if (landed && flipEnabled()) {
        landed.style.visibility = 'hidden';
        session.flyTarget = landed;
    }
}

// Fly the ghost from its release position into the re-rendered element's
// slot, then reveal that element and drop the ghost. finishDrag calls this;
// on failure it cleans up synchronously so nothing is left invisible.
function flyGhostIntoSlot(session) {
    const ghost = session.ghost;
    const target = session.flyTarget;
    session.ghost = null;
    if (!ghost || !target || !flipEnabled()) {
        if (ghost) ghost.remove();
        if (target) target.style.visibility = '';
        return;
    }
    // Deltas measured synchronously: the ghost is position:fixed, so a scroll
    // inside the rAF frame below must not shift the target out from under it.
    const g = ghost.getBoundingClientRect();
    const t = target.getBoundingClientRect();
    const dx = t.left - g.left;
    const dy = t.top - g.top;
    requestAnimationFrame(() => {
        ghost.style.transition = `transform ${GHOST_FLY_MS}ms cubic-bezier(0.2, 0, 0.2, 1), opacity ${GHOST_FLY_MS}ms ease`;
        ghost.style.transform = `translate(${dx}px, ${dy}px) rotate(0deg)`;
        ghost.style.opacity = '0.55';
    });
    setTimeout(() => {
        ghost.remove();
        target.style.visibility = '';
    }, GHOST_FLY_MS + 30);
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

    const flipState = captureFlipState();
    renderDevicesGrid();
    // The re-rendered card stands in for the ghost: hidden until the ghost
    // lands on it (finishDrag -> flyGhostIntoSlot reveals it).
    const landed = document.querySelector(
        `#devices-grid .device-card[data-id="${_dragDeviceId(session)}"]`);
    playFlip(flipState, landed);
    if (landed && flipEnabled()) {
        landed.style.visibility = 'hidden';
        session.flyTarget = landed;
    }

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
    // Sections on screen minus the dragged one; the insertion index below is
    // an index into this list, so it matches the highlighted slot exactly.
    const others = sectionEls().filter(el => el !== session.sourceEl);
    if (others.length === 0) return;
    let newIndex = others.length;
    if (session.dropSide === 'before') {
        newIndex = others.indexOf(session.dropSection);
    } else if (session.dropSection) {
        newIndex = others.indexOf(session.dropSection) + 1;
    }
    if (newIndex < 0) return;

    // Where the virtual Uncategorized section sits within `others` (-1 when
    // the dragged section IS Uncategorized, since it was filtered out).
    const uncIdx = others.findIndex(el => el.dataset.categoryId === '');
    const rawCat = session.sourceEl.dataset.categoryId;

    if (rawCat === '' || rawCat === undefined) {
        // Dragging Uncategorized: only its local slot moves, the real
        // categories keep their server-side order. Its final index among the
        // remaining sections is exactly its new slot.
        saveUncategorizedPosition(newIndex);
        const flipState = captureFlipState();
        renderDevicesGrid();
        animateLandedSection(session, flipState);
        return;
    }

    const draggedId = Number(rawCat);
    if (!Number.isInteger(draggedId)) return;
    const oldIndex = categories.findIndex(c => c.id === draggedId);
    if (oldIndex < 0) return;

    // Translate the on-screen slot into an index into `categories` by
    // discounting the Uncategorized section when it sits above the slot.
    const realIndex = newIndex > uncIdx ? newIndex - 1 : newIndex;
    const [moved] = categories.splice(oldIndex, 1);
    categories.splice(Math.min(realIndex, categories.length), 0, moved);
    // With a real section spliced in at newIndex, Uncategorized ends up one
    // lower whenever the insertion happened at or above it.
    saveUncategorizedPosition(uncIdx + (newIndex <= uncIdx ? 1 : 0));
    const flipState = captureFlipState();
    renderDevicesGrid();
    animateLandedSection(session, flipState);

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
    // A committed drop hands the ghost to flyGhostIntoSlot so it glides into
    // the landing slot instead of vanishing on release.
    if (session.flyTarget) flyGhostIntoSlot(session);
    else if (session.ghost) session.ghost.remove();
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
