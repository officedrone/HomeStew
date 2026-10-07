// HomeStew - Devices realm: list/grid rendering, icons, add/edit modals,
// warranty fields, manuals and the Fetch Manuals dialog.


// Ids of devices whose sidebar accordion row is currently expanded. Kept in a
// Set so the open/closed state survives re-renders (e.g. after loadDevices()).
const expandedDeviceIds = new Set();

async function loadDevices() {
    try {
        const response = await fetch('/api/devices');
        devices = await response.json();
        
        renderDeviceList();
        renderDevicesGrid();
        updateDeviceFilter();
        loadUpcomingEvents();
        // The dashboard's Recent Devices feed and device/manual totals live
        // off the same list, so every device refresh updates them too.
        refreshDashboard();
    } catch (error) {
        console.error('Failed to load devices:', error);
        showToast('Failed to load devices', 'error');
    }
}

// One "Label: value" meta line per configured detail of a device (serial,
// product number, custom attributes) - used in the expanded accordion panel.
function deviceDetailLines(device) {
    const lines = [];
    if (device.serial_number) lines.push(`Serial: ${escapeHtml(device.serial_number)}`);
    if (device.product_number) lines.push(`Product: ${escapeHtml(device.product_number)}`);
    for (const line of warrantyDetailLines(device)) lines.push(escapeHtml(line));
    for (const attr of device.attributes || []) {
        lines.push(`${escapeHtml(attr.attribute_name)}: ${escapeHtml(attr.attribute_value)}`);
    }
    return lines.map(line => `<div class="device-meta">${line}</div>`).join('');
}

// Format a SQLite CURRENT_TIMESTAMP string (UTC, "YYYY-MM-DD HH:MM:SS") in
// the viewer's local time zone. Marked as UTC before converting.
function formatTimestamp(ts) {
    if (!ts) return null;
    const d = new Date(ts.includes('T') ? ts : `${ts.replace(' ', 'T')}Z`);
    if (isNaN(d)) return null;
    return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

// Render the read-only audit timestamps into the edit-device modal footer.
function fillDeviceTimestamps(device) {
    const container = document.getElementById('edit-device-timestamps');
    if (!container) return;
    const lines = [];
    const created = formatTimestamp(device.created_at);
    if (created) lines.push(`Created: ${escapeHtml(created)}`);
    const modified = formatTimestamp(device.updated_at);
    if (modified) lines.push(`Modified on: ${escapeHtml(modified)}`);
    container.innerHTML = lines.map(line => `<div>${line}</div>`).join('');
}

// Plain-text warranty lines ("Purchased: ...", "Warranty ends: ...") for the
// accordion and card views. Dates are YYYY-MM-DD strings from the API; they
// are parsed with a noon time so toLocaleDateString can't shift a day.
function warrantyDetailLines(device) {
    const fmt = iso => {
        if (!iso) return null;
        return new Date(`${iso}T12:00:00`).toLocaleDateString();
    };
    const lines = [];
    const purchased = fmt(device.purchase_date);
    if (purchased) lines.push(`Purchased: ${purchased}`);
    const ends = fmt(device.warranty_end);
    if (ends) {
        const length = device.warranty_length != null && device.warranty_unit
            ? ` (${device.warranty_length} ${device.warranty_unit})`
            : '';
        lines.push(`Warranty ends: ${ends}${length}`);
    }
    return lines;
}

// Dashboard "Recent Devices": the five most recently created devices,
// newest first. Each is a single collapsed line with square Edit / Delete
// icon buttons beside the name; clicking the header expands an animated
// panel with the model, configured details (serial / product / custom
// attributes) and manual count. Clicking the expanded body (not a button)
// opens the Edit dialog instead.
function renderDeviceList() {
    const container = document.getElementById('device-list');
    
    if (devices.length === 0) {
        container.innerHTML = '<div class="empty-state">No devices yet. Add your first device!</div>';
        return;
    }
    
    // created_at is a UTC "YYYY-MM-DD HH:MM:SS" string, so plain string
    // comparison sorts chronologically.
    const recent = [...devices]
        .sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))
        .slice(0, 5);
    
    container.innerHTML = recent.map(device => {
        const open = expandedDeviceIds.has(device.id);
        return `
        <div class="device-accordion${open ? ' open' : ''}" data-id="${device.id}">
            <div class="device-row" onclick="toggleDeviceExpansion(${device.id})">
                <span class="device-caret">›</span>
                <button type="button" class="device-icon device-icon-small" title="Change icon" aria-label="Change icon" data-dedupe onclick="event.stopPropagation(); openIconPicker(null, ${device.id}, true)">${deviceIconSvg(device.icon)}</button>
                <span class="device-name">${escapeHtml(device.name)}</span>
                <span class="card-actions">
                    <button type="button" class="card-icon-btn" title="Edit device" aria-label="Edit device" data-dedupe onclick="event.stopPropagation(); editDevice(${device.id})">${CARD_ACTION_ICONS.edit}</button>
                    <button type="button" class="card-icon-btn card-icon-danger" title="Delete device" aria-label="Delete device" data-dedupe onclick="event.stopPropagation(); deleteDevice(${device.id})">${CARD_ACTION_ICONS.trash}</button>
                </span>
            </div>
            <div class="device-details" onclick="onDeviceBodyClick(event, ${device.id})">
                <div class="device-details-inner">
                    <div class="device-meta">${escapeHtml(device.brand)} ${escapeHtml(device.model)}</div>
                    ${deviceDetailLines(device)}
                    <div class="device-meta">${device.manual_count} manual${device.manual_count !== 1 ? 's' : ''}</div>
                </div>
            </div>
        </div>`;
    }).join('');
    renderLucideIcons();
}

// Expand / collapse one sidebar device row (CSS grid-rows transition animates it).
function toggleDeviceExpansion(deviceId) {
    const accordion = document.querySelector(`.device-accordion[data-id="${deviceId}"]`);
    if (!accordion) return;
    const open = accordion.classList.toggle('open');
    if (open) expandedDeviceIds.add(deviceId);
    else expandedDeviceIds.delete(deviceId);
}

// Clicking the expanded accordion body opens the Edit dialog for that device.
// Clicks on buttons inside the panel keep their own handlers.
function onDeviceBodyClick(event, deviceId) {
    if (event.target.closest('button')) return;
    editDevice(deviceId);
}

// ---------------------------------------------------------------------------
// Device icons
//
// Devices store a kebab-case Lucide icon *name* directly (e.g. 'washing-
// machine'); the glyphs come from the FULL Lucide set vendored offline in
// frontend/vendor/lucide.min.js (ISC license, ~2,100 icons). The complete
// name list is generated into frontend/lucide-icon-names.js (loaded before
// app.js) by _gen_icon_names.py at the repo root — there is no hand-curated
// map anymore. deviceIconSvg() emits an <i data-lucide> placeholder and
// renderLucideIcons() swaps every placeholder for the real inline SVG (so
// currentColor keeps working in both themes). A null/unknown name renders
// the default glyph. The backend mirrors this list in
// homestew/services/device_icons.py (lucide_icon_names.txt) — regenerate
// BOTH after upgrading lucide.min.js.
// ---------------------------------------------------------------------------

const DEVICE_ICON_DEFAULT = 'plug';

// Curated household glyphs shown when the picker opens with an empty search.
// This is only the initial view — typing searches ALL of LUCIDE_ICON_NAMES.
// Mirrors DEFAULT_ICON_PICKER in homestew/services/device_icons.py.
const DEVICE_ICON_STARTERS = [
    'plug', 'refrigerator', 'snowflake', 'cooking-pot', 'droplets',
    'microwave', 'washing-machine', 'coffee', 'blender', 'glass-water',
    'air-vent', 'wind', 'cloud-rain', 'heater', 'thermometer', 'fan',
    'robot-vacuum', 'flame', 'tv', 'monitor', 'laptop', 'tablet',
    'smartphone', 'watch', 'headphones', 'keyboard', 'mouse', 'gamepad2',
    'printer', 'projector', 'camera', 'cctv', 'speaker', 'router',
    'hard-drive', 'server', 'lock', 'lightbulb', 'zap', 'plant-pot'
];

// How many icons the picker grid renders at once (the full set is ~2,100 —
// rendering all of them per keystroke would freeze the UI). "Load more"
// appends another page.
const ICON_PAGE_SIZE = 120;

// Names that read better as acronyms in picker labels. Mirrors
// _ICON_LABEL_OVERRIDES in homestew/services/device_icons.py.
const ICON_LABEL_OVERRIDES = {
    cctv: 'CCTV', tv: 'TV', wifi: 'WiFi', led: 'LED', dvd: 'DVD',
    usb: 'USB', pc: 'PC', hd: 'HD', id: 'ID', ai: 'AI', '3d': '3D'
};

// Picker label for a kebab-case name: 'washing-machine' -> 'Washing Machine'.
function iconLabel(name) {
    return name.split('-').map((w) => ICON_LABEL_OVERRIDES[w] || w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
}

// Rank LUCIDE_ICON_NAMES against a query: AND over the words (every word
// must be a substring of the name), phrase hits beat prefix hits beat plain
// substring hits, exact hyphen-words add a bonus and shorter names win ties
// ('coffee' ranks above 'coffee-bean'). Mirrors search_icon_keys() in
// homestew/services/device_icons.py. Returns matching names (sorted).
function filterIconNames(query) {
    const text = (query || '').trim().toLowerCase();
    const words = text.split(/[^a-z0-9]+/).filter(Boolean);
    if (words.length === 0) return DEVICE_ICON_STARTERS.slice();
    const phrase = words.join(' ');
    const scored = [];
    for (const name of LUCIDE_ICON_NAMES) {
        if (!words.every((w) => name.includes(w))) continue;
        let score = 0;
        if (name.includes(phrase)) score += 8;
        if (name.startsWith(phrase)) score += 4;
        else if (words.some((w) => name.startsWith(w))) score += 2;
        const parts = new Set(name.split('-'));
        words.forEach((w) => { if (parts.has(w)) score += 3; });
        scored.push([name, -score, name.length]);
    }
    scored.sort((a, b) => a[1] - b[1] || a[2] - b[2] || (a[0] < b[0] ? -1 : 1));
    return scored.map(([name]) => name);
}

// Placeholder markup for a device's icon name; null/unknown names fall back
// to the default glyph. renderLucideIcons() replaces every <i data-lucide>
// element with the real Lucide SVG after each dynamic render.
function deviceIconSvg(name) {
    const safe = name && LUCIDE_ICON_NAMES.includes(name) ? name : DEVICE_ICON_DEFAULT;
    return `<i data-lucide="${safe}"></i>`;
}

// Replace all <i data-lucide> placeholders in the document with their Lucide
// SVGs. Called after every render that injects device icons (Devices grid,
// sidebar rows, picker grid, form previews). Re-running over already-rendered
// icons is harmless: Lucide swaps the generated <svg> for an identical one.
function renderLucideIcons() {
    if (window.lucide && typeof window.lucide.createIcons === 'function') {
        window.lucide.createIcons();
    }
}

// State of the open icon picker: which form field receives the choice and
// whether picking saves immediately (card/sidebar click) or only fills the
// form (Add/Edit dialog). currentKey keeps the selection highlighted across
// search re-renders; shown is how many of the current matches the grid has
// rendered so far (the full set is ~2,100 — see ICON_PAGE_SIZE).
let _iconPicker = null;

function openIconPicker(prefix, deviceId, saveDirect) {
    let currentKey = '';
    if (saveDirect && deviceId != null) {
        currentKey = devices.find(d => d.id === deviceId)?.icon || '';
    } else if (prefix) {
        currentKey = document.getElementById(`${prefix}-icon`)?.value || '';
    }
    _iconPicker = { prefix, deviceId, saveDirect, currentKey: currentKey || DEVICE_ICON_DEFAULT };
    const search = document.getElementById('icon-picker-search');
    if (search) search.value = '';
    renderIconPickerGrid(_iconPicker.currentKey, '');
    document.getElementById('icon-picker-modal').style.display = 'flex';
    if (search) search.focus();
    renderLucideIcons();
}

function closeIconPicker() {
    document.getElementById('icon-picker-modal').style.display = 'none';
    _iconPicker = null;
}

// Typing in the picker's search box filters the grid live (client-side, so
// every keystroke is instant — no round trip). Escape in the box clears the
// search first and only closes the modal when it is already empty.
function onIconSearchInput() {
    if (!_iconPicker) return;
    const search = document.getElementById('icon-picker-search');
    renderIconPickerGrid(_iconPicker.currentKey, search ? search.value : '');
}

function onIconSearchKeydown(e) {
    if (e.key !== 'Escape') return;
    if (e.target.value) {
        // Clear the query instead of closing the dialog underneath.
        e.stopPropagation();
        e.target.value = '';
        onIconSearchInput();
    }
}

// Render the first page of matches for `query`; "Load more" (renderMoreIcons)
// appends further pages without re-running the search.
function renderIconPickerGrid(currentKey, query) {
    const grid = document.getElementById('icon-picker-grid');
    if (!grid) return;
    const matches = filterIconNames(query);
    if (_iconPicker) {
        _iconPicker.matches = matches;
        _iconPicker.shown = 0;
    }
    const count = document.getElementById('icon-picker-count');
    if (count) {
        count.textContent = query.trim()
            ? `${matches.length} of ${LUCIDE_ICON_NAMES.length} icons`
            : `${DEVICE_ICON_STARTERS.length} common of ${LUCIDE_ICON_NAMES.length} — search for anything`;
    }
    if (matches.length === 0) {
        grid.innerHTML = `<div class="icon-picker-empty">No icon matches "${escapeHtml(query.trim())}". Try a simpler word — the name must contain it, e.g. 'coffee', 'wifi', 'lamp'.</div>`;
        return;
    }
    renderMoreIcons();
}

// Append the next ICON_PAGE_SIZE matches to the grid (also used for the
// first page). Keeps huge result sets responsive: only what is shown gets
// an SVG injected.
function renderMoreIcons() {
    const picker = _iconPicker;
    const grid = document.getElementById('icon-picker-grid');
    if (!picker || !grid || !picker.matches) return;
    const from = picker.shown;
    const to = Math.min(from + ICON_PAGE_SIZE, picker.matches.length);
    const html = picker.matches.slice(from, to).map((name) => `
        <button type="button" class="icon-option${name === picker.currentKey ? ' selected' : ''}"
                title="${escapeHtml(iconLabel(name))}" onclick="selectDeviceIcon('${name}')">
            ${deviceIconSvg(name)}
            <span>${escapeHtml(iconLabel(name))}</span>
        </button>`).join('');
    const more = picker.matches.length > to
        ? `<button type="button" class="icon-load-more" onclick="renderMoreIcons()">Load ${Math.min(ICON_PAGE_SIZE, picker.matches.length - to)} more (${picker.matches.length - to} left)</button>`
        : '';
    if (from === 0) {
        grid.innerHTML = html + more;
    } else {
        // Replace the old "Load more" row, then append the new page + next.
        const oldMore = grid.querySelector('.icon-load-more');
        if (oldMore) oldMore.remove();
        grid.insertAdjacentHTML('beforeend', html + more);
    }
    picker.shown = to;
    renderLucideIcons();
}

// Apply the picked icon. When opened from a card/sidebar row (saveDirect),
// persist it with a PUT built from the cached device and refresh; when opened
// from a form, just update the hidden input + preview and leave the dialog
// underneath open.
async function selectDeviceIcon(key) {
    const picker = _iconPicker;
    closeIconPicker();
    if (!picker) return;

    if (picker.saveDirect && picker.deviceId != null) {
        const device = devices.find(d => d.id === picker.deviceId);
        if (!device) return;
        try {
            const response = await fetch(`/api/devices/${picker.deviceId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: device.name,
                    brand: device.brand,
                    model: device.model,
                    description: device.description || '',
                    serial_number: device.serial_number || null,
                    product_number: device.product_number || null,
                    purchase_date: device.purchase_date || null,
                    warranty_length: device.warranty_length ?? null,
                    warranty_unit: device.warranty_unit || null,
                    warranty_end: device.warranty_end || null,
                    icon: key
                })
            });
            if (!response.ok) throw new Error('Failed to update icon');
            await loadDevices();
        } catch (error) {
            console.error('Failed to update device icon:', error);
            showToast('Failed to update icon', 'error');
        }
        return;
    }

    if (picker.prefix) setDeviceIcon(picker.prefix, key);
}

// Write the icon choice into a device form (hidden input + preview swatch).
function setDeviceIcon(prefix, key) {
    const hidden = document.getElementById(`${prefix}-icon`);
    if (hidden) hidden.value = key || '';
    const preview = document.getElementById(`${prefix}-icon-preview`);
    if (preview) preview.innerHTML = deviceIconSvg(key);
    renderLucideIcons();
}

// Devices tab: card grid with the full device details and actions.
function renderDevicesGrid() {
    const container = document.getElementById('devices-grid');
    if (!container) return;

    if (devices.length === 0) {
        container.innerHTML = '<div class="empty-state">No devices yet. Add your first device!</div>';
        return;
    }

    // The whole card is clickable and opens the Edit dialog; the icon buttons
    // beside the title keep their own actions (stopPropagation keeps a card
    // click from firing twice).
    container.innerHTML = devices.map(device => `
        <div class="device-card" data-id="${device.id}" onclick="editDevice(${device.id})">
            <div class="device-card-header">
                <button type="button" class="device-icon" title="Change icon" aria-label="Change icon" data-dedupe onclick="event.stopPropagation(); openIconPicker(null, ${device.id}, true)">${deviceIconSvg(device.icon)}</button>
                <div class="device-name">${escapeHtml(device.name)}</div>
                <div class="card-actions">
                    <button type="button" class="card-icon-btn" title="Edit device" aria-label="Edit device" data-dedupe onclick="event.stopPropagation(); editDevice(${device.id})">${CARD_ACTION_ICONS.edit}</button>
                    <button type="button" class="card-icon-btn card-icon-danger" title="Delete device" aria-label="Delete device" data-dedupe onclick="event.stopPropagation(); deleteDevice(${device.id})">${CARD_ACTION_ICONS.trash}</button>
                </div>
            </div>
            <div class="device-meta">${escapeHtml(device.brand)} ${escapeHtml(device.model)}</div>
            ${device.serial_number ? `<div class="device-meta">Serial: ${escapeHtml(device.serial_number)}</div>` : ''}
            ${device.product_number ? `<div class="device-meta">Product: ${escapeHtml(device.product_number)}</div>` : ''}
            ${warrantyDetailLines(device).map(line => `<div class="device-meta">${escapeHtml(line)}</div>`).join('')}
            <div class="device-meta">${device.manual_count} manual${device.manual_count !== 1 ? 's' : ''}</div>
        </div>
    `).join('');
    renderLucideIcons();
}

function updateDeviceFilter() {
    const optionsHtml = devices.map(device =>
        `<option value="${device.id}">${escapeHtml(device.name)}</option>`
    ).join('');

    const select = document.getElementById('device-filter');
    select.innerHTML = '<option value="">All Devices</option>' + optionsHtml;

    if (currentDeviceFilter) {
        select.value = currentDeviceFilter;
    }

    // The AI Chat tab gets its own independent device filter, kept in sync
    // with the same device list.
    const chatSelect = document.getElementById('chat-device-filter');
    chatSelect.innerHTML = '<option value="">All Devices</option>' + optionsHtml;

    if (currentChatDeviceFilter) {
        chatSelect.value = currentChatDeviceFilter;
    }

    // Calendar tab filter (events are usually tied to a device).
    const calSelect = document.getElementById('calendar-device-filter');
    calSelect.innerHTML = '<option value="">All Devices</option>' + optionsHtml;
    if (currentCalendarDeviceFilter) {
        calSelect.value = currentCalendarDeviceFilter;
    }

    // Event modal device picker: same list plus an explicit "no device".
    const eventSelect = document.getElementById('event-device');
    const currentEventDevice = eventSelect.value;
    eventSelect.innerHTML = '<option value="">No device</option>' + optionsHtml;
    if (currentEventDevice) {
        eventSelect.value = currentEventDevice;
    }
}

// ---------------------------------------------------------------------------
// Add Device modal
// ---------------------------------------------------------------------------

/**
 * Temporary store for custom attributes entered in the Add Device form.
 * Each entry is { name: string, value: string }.
 */
let _addDeviceAttributes = [];

// Set once "Fetch Manuals" has saved the device straight from the Add Device
// form (manuals can only be stored against an existing device). A later
// "Add Device" click then finishes that record instead of creating a twin.
let _addDeviceCreatedId = null;

// ---------------------------------------------------------------------------
// Warranty fields (both device modals)
// ---------------------------------------------------------------------------

/** Add `months` to a YYYY-MM-DD string, clamping the day (Jan 31 -> Feb 28). */
function addMonthsToDateStr(dateStr, months) {
    const [y, m, d] = dateStr.split('-').map(Number);
    const total = (y * 12 + (m - 1)) + months;
    const year = Math.floor(total / 12);
    const month = (total % 12) + 1;
    // Day count of the target month (Date's day 0 == last day of prev month).
    const lastDay = new Date(year, month, 0).getDate();
    const dt = new Date(Date.UTC(year, month - 1, Math.min(d, lastDay)));
    return dt.toISOString().slice(0, 10);
}

/** Add `days` to a YYYY-MM-DD string. */
function addDaysToDateStr(dateStr, days) {
    const dt = new Date(`${dateStr}T00:00:00Z`);
    dt.setUTCDate(dt.getUTCDate() + days);
    return dt.toISOString().slice(0, 10);
}

/**
 * Warranty end date for a purchase date + length/unit, or '' when incomplete.
 * Mirrors the server-side computation in api/devices.py `_warranty_fields`.
 */
function computeWarrantyEnd(purchaseDate, length, unit) {
    if (!purchaseDate || length === null || length === undefined || length === '') return '';
    const n = parseInt(length, 10);
    if (!Number.isFinite(n) || n < 0) return '';
    if (unit === 'days') return addDaysToDateStr(purchaseDate, n);
    if (unit === 'months') return addMonthsToDateStr(purchaseDate, n);
    if (unit === 'years') return addMonthsToDateStr(purchaseDate, n * 12);
    return '';
}

/**
 * Keep a form's "Warranty End" in sync with its purchase date / length fields.
 * Only overwrites the end field while it is empty or still auto-filled (the
 * last computed value is remembered on the element), so a manually picked
 * date is never clobbered. `prefix` is 'device' | 'edit-device'.
 */
function initWarrantyAutoCalc(prefix) {
    const purchase = document.getElementById(`${prefix}-purchase-date`);
    const length = document.getElementById(`${prefix}-warranty-length`);
    const unit = document.getElementById(`${prefix}-warranty-unit`);
    const end = document.getElementById(`${prefix}-warranty-end`);
    if (!purchase || !length || !unit || !end) return;

    const recalc = () => {
        // Manual override: the field holds something we didn't compute.
        if (end.value && end.value !== end.dataset.autoValue) return;
        const computed = computeWarrantyEnd(purchase.value, length.value, unit.value);
        end.dataset.autoValue = computed;
        end.value = computed;
    };

    purchase.addEventListener('change', recalc);
    length.addEventListener('input', recalc);
    unit.addEventListener('change', recalc);
}

/**
 * Re-baseline the auto-fill tracker after a form is reset or populated. The
 * baseline is the date the inputs would currently compute, so a saved (or
 * hand-picked) Warranty End that matches it keeps updating on later edits,
 * while a genuinely manual date stays protected.
 */
function resetWarrantyAutoCalc(prefix) {
    const end = document.getElementById(`${prefix}-warranty-end`);
    if (!end) return;
    const computed = computeWarrantyEnd(
        document.getElementById(`${prefix}-purchase-date`)?.value || '',
        document.getElementById(`${prefix}-warranty-length`)?.value || '',
        document.getElementById(`${prefix}-warranty-unit`)?.value || 'years'
    );
    end.dataset.autoValue = computed;
}

/** Collect the warranty fields of a device form into a payload fragment. */
function readWarrantyFields(prefix) {
    const value = id => document.getElementById(id)?.value || null;
    const lengthRaw = value(`${prefix}-warranty-length`);
    return {
        purchase_date: value(`${prefix}-purchase-date`),
        warranty_length: lengthRaw ? parseInt(lengthRaw, 10) : null,
        // The unit dropdown always has a value; only meaningful with a length.
        warranty_unit: lengthRaw ? value(`${prefix}-warranty-unit`) : null,
        warranty_end: value(`${prefix}-warranty-end`)
    };
}

/** Populate the warranty fields of a device form from a device object. */
function fillWarrantyFields(prefix, device) {
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v || ''; };
    set(`${prefix}-purchase-date`, device.purchase_date);
    set(`${prefix}-warranty-length`, device.warranty_length ?? '');
    set(`${prefix}-warranty-unit`, device.warranty_unit || 'years');
    set(`${prefix}-warranty-end`, device.warranty_end);
}

// ---------------------------------------------------------------------------
// New Device flow (manual form vs. add-via-AI-Chat)
// ---------------------------------------------------------------------------

// Vision state captured when the chooser was opened, so the AI option's hint
// and the post-navigation tip agree with what the user was just shown (rather
// than a value that could change while the dialog sat open).
let _newDeviceVision = false;

// Entry point for both "New Device" buttons. When an LLM is configured we ask
// how to add the device first - manually or via AI Chat, where the model can
// read the details from a description or a nameplate photo. With no model set
// up there is nothing to offer, so go straight to the manual form. The config
// read hits /api/settings (a local DB read, NOT the slow LLM probe), so the
// click stays responsive.
async function startNewDeviceFlow() {
    let configured = false;
    let vision = chatVisionEnabled;
    try {
        const response = await fetch('/api/settings');
        if (response.ok) {
            const s = await response.json();
            configured = !!s.llm_configured;
            vision = !!s.llm_supports_vision;
        }
    } catch (e) { /* offline / server starting - fall through to the form */ }

    if (!configured) {
        openAddDeviceModal();
        return;
    }

    _newDeviceVision = vision;
    // Tailor the AI option's subtitle to whether photos can be attached.
    const hint = document.getElementById('new-device-choice-ai-hint');
    if (hint) {
        hint.textContent = vision
            ? 'Describe it - or attach a photo of its nameplate - and HomeStew fills everything in.'
            : 'Describe it in the chat and HomeStew fills everything in for you.';
    }
    document.getElementById('new-device-choice-modal').style.display = 'flex';
}

function closeNewDeviceChoice() {
    document.getElementById('new-device-choice-modal').style.display = 'none';
}

// "Enter details manually" - dismiss the chooser and open the Add Device form.
function chooseNewDeviceManual() {
    closeNewDeviceChoice();
    openAddDeviceModal();
}

// "Add with AI Chat" - jump to the chat tab with the add-device prompt already
// typed so the user only has to complete (or replace) it. When the model can
// see images, also surface a hint pointing at the attach buttons.
function chooseNewDeviceChat() {
    closeNewDeviceChoice();
    const prefill = 'Add this device - Name: <Name Here>, Manufacturer: <Manufacturer Here>, Model: <Model Here>';
    switchTab('chat');
    prefillChatForNewDevice(prefill, _newDeviceVision);
}

// Fill the chat input with `text`, select the first <placeholder> so typing
// replaces it, and (optionally) reveal the vision hint. Focus is deferred a
// tick because switchTab is still settling the tab's layout.
function prefillChatForNewDevice(text, showVisionTip) {
    const input = document.getElementById('chat-input');
    if (input) {
        input.value = text;
        const start = text.indexOf('<');
        const end = text.indexOf('>', start + 1);
        if (start !== -1 && end !== -1) input.setSelectionRange(start, end + 1);
        else input.setSelectionRange(text.length, text.length);
        setTimeout(() => input.focus(), 60);
    }
    if (showVisionTip) {
        const tip = document.getElementById('chat-attach-tip');
        if (tip) tip.style.display = 'flex';
    }
}

function hideChatAttachTip() {
    const tip = document.getElementById('chat-attach-tip');
    if (tip) tip.style.display = 'none';
}

function openAddDeviceModal() {
    document.getElementById('add-device-form').reset();
    resetWarrantyAutoCalc('device');
    setDeviceIcon('device', '');
    updateAddDeviceManualNames();
    _addDeviceAttributes = [];
    _addDeviceCreatedId = null;
    renderAddDeviceAttributes();
    document.getElementById('add-device-modal').style.display = 'flex';
    setTimeout(() => document.getElementById('device-name').focus(), 50);
}

function closeAddDeviceModal() {
    document.getElementById('add-device-modal').style.display = 'none';
    _addDeviceAttributes = [];
    _addDeviceCreatedId = null;
}

// "Fetch Manuals" inside the Add Device modal. Manuals are stored per device,
// so this first creates (or, on a second click, updates) the device from the
// current form values, then opens the shared search/approve dialog for it.
async function fetchManualsForNewDevice() {
    const name = document.getElementById('device-name').value.trim();
    const brand = document.getElementById('device-brand').value.trim();
    const model = document.getElementById('device-model').value.trim();
    if (!name || !brand || !model) {
        showToast('Enter the device name, brand and model first', 'error');
        return;
    }
    if (!beginOp('add-device-fetch-manuals')) return;

    const body = JSON.stringify({
        name,
        brand,
        model,
        description: document.getElementById('device-description').value,
        serial_number: document.getElementById('device-serial-number').value || null,
        product_number: document.getElementById('device-product-number').value || null,
        icon: document.getElementById('device-icon').value || null,
        ...readWarrantyFields('device')
    });

    try {
        if (_addDeviceCreatedId) {
            // Keep the record created earlier in sync with later form edits.
            const response = await fetch(`/api/devices/${_addDeviceCreatedId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body
            });
            if (!response.ok) throw new Error(await errorDetailFrom(response, 'Failed to update device'));
        } else {
            const response = await fetch('/api/devices', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body
            });
            if (!response.ok) throw new Error(await errorDetailFrom(response, 'Failed to create device'));
            _addDeviceCreatedId = (await response.json()).id;
            showToast('Device saved - pick the manuals to download', 'success');
        }
    } catch (error) {
        console.error('Failed to save device before fetching manuals:', error);
        showToast(error.message || 'Could not save the device first', 'error');
        return;
    } finally {
        endOp('add-device-fetch-manuals');
    }

    await loadDevices(); // sidebar / grid now show the new device
    openFetchManuals(_addDeviceCreatedId);
}

// Show which PDFs were picked in the Add Device modal ("Upload Manual"
// label opens the hidden multi-file input next to it).
function updateAddDeviceManualNames() {
    const input = document.getElementById('add-device-manuals');
    const names = [...input.files].map(f => f.name);
    document.getElementById('add-device-manual-names').textContent =
        names.length === 0 ? '' : `${names.length} file${names.length !== 1 ? 's' : ''}: ${names.join(', ')}`;
}

// ---------------------------------------------------------------------------
// Add Device custom attributes (local, pre-creation)
// ---------------------------------------------------------------------------

function renderAddDeviceAttributes() {
    const container = document.getElementById('add-device-attributes-list');
    if (_addDeviceAttributes.length === 0) {
        container.innerHTML = '<div class="empty-state">No custom attributes</div>';
        return;
    }
    container.innerHTML = _addDeviceAttributes.map((attr, i) => `
        <div class="attribute-item">
            <span class="attribute-name">${escapeHtml(attr.name)}:</span>
            <span class="attribute-value">${escapeHtml(attr.value)}</span>
            <button type="button" class="btn btn-danger btn-small" onclick="removeAddDeviceAttribute(${i})">
                ×
            </button>
        </div>
    `).join('');
}

function addAddDeviceAttribute() {
    const nameInput = document.getElementById('add-device-attribute-name');
    const valueInput = document.getElementById('add-device-attribute-value');
    
    const name = nameInput.value.trim();
    const value = valueInput.value.trim();
    
    if (!name || !value) {
        showToast('Please enter both attribute name and value', 'error');
        return;
    }
    
    _addDeviceAttributes.push({ name, value });
    nameInput.value = '';
    valueInput.value = '';
    renderAddDeviceAttributes();
}

function removeAddDeviceAttribute(index) {
    _addDeviceAttributes.splice(index, 1);
    renderAddDeviceAttributes();
}

async function handleAddDevice(e) {
    e.preventDefault();
    if (!beginOp('add-device')) return;
    const deviceData = {
        name: document.getElementById('device-name').value,
        brand: document.getElementById('device-brand').value,
        model: document.getElementById('device-model').value,
        description: document.getElementById('device-description').value,
        serial_number: document.getElementById('device-serial-number').value || null,
        product_number: document.getElementById('device-product-number').value || null,
        icon: document.getElementById('device-icon').value || null,
        ...readWarrantyFields('device')
    };
    
    try {
        // "Fetch Manuals" may already have created this device; finish that
        // record (with the latest form values) instead of creating a twin.
        let newDeviceId = _addDeviceCreatedId;
        if (newDeviceId) {
            const putResponse = await fetch(`/api/devices/${newDeviceId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(deviceData)
            });
            if (!putResponse.ok) throw new Error('Failed to update device');
        } else {
            const response = await fetch('/api/devices', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(deviceData)
            });
            if (!response.ok) throw new Error('Failed to add device');
            newDeviceId = (await response.json()).id;
        }

        // Create custom attributes entered in the Add Device form.
        for (const attr of _addDeviceAttributes) {
            const resp = await fetch(
                `/api/devices/${newDeviceId}/attributes?attribute_name=${encodeURIComponent(attr.name)}&attribute_value=${encodeURIComponent(attr.value)}`,
                { method: 'POST' }
            );
            if (!resp.ok) {
                let detail = `Failed to add attribute "${attr.name}"`;
                try { const err = await resp.json(); if (err.detail) detail = err.detail; } catch (_) {}
                showToast(detail, 'error');
            }
        }

        // Upload any manuals picked in the form.
        const manualInput = document.getElementById('add-device-manuals');
        const files = [...manualInput.files];
        for (const file of files) {
            if (!file.name.toLowerCase().endsWith('.pdf')) {
                showToast(`Skipped "${file.name}" - only PDF files are supported`, 'error');
                continue;
            }
            const formData = new FormData();
            formData.append('file', file);
            const uploadResp = await fetch(`/api/devices/${newDeviceId}/manuals/upload`, {
                method: 'POST',
                body: formData
            });
            if (!uploadResp.ok) {
                let detail = `Failed to upload "${file.name}"`;
                try {
                    const err = await uploadResp.json();
                    if (err.detail) detail = err.detail;
                } catch (e) { /* non-JSON error body */ }
                showToast(detail, 'error');
            }
        }

        const attrCount = _addDeviceAttributes.length;
        const msg = [];
        if (attrCount > 0) msg.push(`${attrCount} attribute${attrCount !== 1 ? 's' : ''}`);
        if (files.length > 0) msg.push(`${files.length} manual${files.length !== 1 ? 's' : ''}`);
        showToast(msg.length > 0
            ? `Device added with ${msg.join(' and ')}!`
            : 'Device added successfully!', 'success');
        closeAddDeviceModal();
        await loadDevices();
    } catch (error) {
        console.error('Failed to add device:', error);
        showToast('Failed to add device', 'error');
    } finally {
        endOp('add-device');
    }
}

// Open a device's edit modal from anywhere (chat links): make sure the
// device list is fresh first — editDevice() reads the in-memory `devices`
// array, which may predate a device the LLM just created.
// Deliberately does NOT switch to the Devices tab: the edit modal is a
// global overlay, so opening/saving/cancelling it keeps the user on
// whatever page they came from (e.g. AI Chat) instead of punting them over.
async function openDeviceEditorById(deviceId) {
    if (!devices.some(d => d.id === deviceId)) {
        await loadDevices();
    }
    const device = devices.find(d => d.id === deviceId);
    if (!device) {
        showToast(`Device ${deviceId} no longer exists`, 'error');
        return;
    }
    editDevice(deviceId);
}

// Chat link "#fetch-manuals-<id>" (handed over right after the LLM creates a
// device): open that device's editor AND immediately start the manual search,
// so the user only has to approve which PDFs to store. The Fetch Manuals
// dialog stacks on top of the edit modal - closing it leaves the editor open.
async function openDeviceEditorAndFetchById(deviceId) {
    await openDeviceEditorById(deviceId);
    if (!devices.some(d => d.id === deviceId)) return;
    openFetchManuals(deviceId);
}

async function editDevice(deviceId) {
    const device = devices.find(d => d.id === deviceId);
    if (!device) return;
    
    // Populate edit form
    document.getElementById('edit-device-id').value = device.id;
    document.getElementById('edit-device-name').value = device.name || '';
    document.getElementById('edit-device-brand').value = device.brand || '';
    document.getElementById('edit-device-model').value = device.model || '';
    document.getElementById('edit-device-description').value = device.description || '';
    document.getElementById('edit-device-serial-number').value = device.serial_number || '';
    document.getElementById('edit-device-product-number').value = device.product_number || '';
    setDeviceIcon('edit-device', device.icon || '');
    fillWarrantyFields('edit-device', device);
    resetWarrantyAutoCalc('edit-device');
    fillDeviceTimestamps(device);
    
    // Load and render attributes
    await loadDeviceAttributes(deviceId);

    // Load and render manuals
    await loadManuals(deviceId);
    
    // Show edit modal
    document.getElementById('edit-device-modal').style.display = 'flex';
}

async function handleEditDevice(e) {
    e.preventDefault();
    if (!beginOp('edit-device')) return;
    const deviceId = parseInt(document.getElementById('edit-device-id').value);
    const deviceData = {
        name: document.getElementById('edit-device-name').value,
        brand: document.getElementById('edit-device-brand').value,
        model: document.getElementById('edit-device-model').value,
        description: document.getElementById('edit-device-description').value,
        serial_number: document.getElementById('edit-device-serial-number').value || null,
        product_number: document.getElementById('edit-device-product-number').value || null,
        icon: document.getElementById('edit-device-icon').value || null,
        ...readWarrantyFields('edit-device')
    };
    
    try {
        const response = await fetch(`/api/devices/${deviceId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(deviceData)
        });
        
        if (!response.ok) throw new Error('Failed to update device');
        
        showToast('Device updated successfully!', 'success');
        document.getElementById('edit-device-modal').style.display = 'none';
        await loadDevices();
    } catch (error) {
        console.error('Failed to update device:', error);
        showToast('Failed to update device', 'error');
    } finally {
        endOp('edit-device');
    }
}

async function loadDeviceAttributes(deviceId) {
    const container = document.getElementById('device-attributes-list');

    // The devices array comes from GET /api/devices, which does not include
    // attributes - fetch the single-device endpoint so newly added/removed
    // attributes are reflected immediately.
    let attrs = [];
    try {
        const response = await fetch(`/api/devices/${deviceId}`);
        if (!response.ok) throw new Error('Failed to load attributes');
        const device = await response.json();
        attrs = device.attributes || [];
    } catch (error) {
        console.error('Failed to load device attributes:', error);
        container.innerHTML = '<div class="empty-state">Failed to load attributes.</div>';
        return;
    }

    if (attrs.length === 0) {
        container.innerHTML = '<div class="empty-state">No custom attributes</div>';
        return;
    }
    
    container.innerHTML = attrs.map(attr => `
        <div class="attribute-item">
            <span class="attribute-name">${escapeHtml(attr.attribute_name)}:</span>
            <span class="attribute-value">${escapeHtml(attr.attribute_value)}</span>
            <button type="button" class="card-icon-btn card-icon-danger" title="Remove attribute" aria-label="Remove attribute" data-dedupe onclick="removeDeviceAttribute(${deviceId}, ${attr.id})">${CARD_ACTION_ICONS.trash}</button>
        </div>
    `).join('');
}

async function addDeviceAttribute(deviceId) {
    const nameInput = document.getElementById('attribute-name-input');
    const valueInput = document.getElementById('attribute-value-input');
    
    const attributeName = nameInput.value.trim();
    const attributeValue = valueInput.value.trim();
    
    if (!attributeName || !attributeValue) {
        showToast('Please enter both attribute name and value', 'error');
        return;
    }
    if (!beginOp(`add-attr-${deviceId}`)) return;
    try {
        const response = await fetch(`/api/devices/${deviceId}/attributes?attribute_name=${encodeURIComponent(attributeName)}&attribute_value=${encodeURIComponent(attributeValue)}`, {
            method: 'POST'
        });
        
        if (!response.ok) throw new Error('Failed to add attribute');
        
        showToast('Attribute added successfully!', 'success');
        nameInput.value = '';
        valueInput.value = '';
        await loadDeviceAttributes(deviceId);
        await loadDevices(); // refresh the sidebar accordion details
    } catch (error) {
        console.error('Failed to add attribute:', error);
        showToast('Failed to add attribute', 'error');
    } finally {
        endOp(`add-attr-${deviceId}`);
    }
}

// NOTE: must NOT be named `removeAttribute` — inline onclick handlers resolve
// identifiers against the element's scope chain first, and every DOM element
// has Element.prototype.removeAttribute (removes an HTML attribute). A bare
// `removeAttribute(...)` in an inline handler silently calls that method
// instead of this function, so the delete button would do nothing.
async function removeDeviceAttribute(deviceId, attributeId) {
    if (!beginOp(`remove-attr-${deviceId}-${attributeId}`)) return;
    try {
        const response = await fetch(`/api/devices/${deviceId}/attributes/${attributeId}`, {
            method: 'DELETE'
        });
        
        if (!response.ok) throw new Error('Failed to remove attribute');
        
        showToast('Attribute removed', 'success');
        await loadDeviceAttributes(deviceId);
        await loadDevices(); // refresh the sidebar accordion details
    } catch (error) {
        console.error('Failed to remove attribute:', error);
        showToast('Failed to remove attribute', 'error');
    } finally {
        endOp(`remove-attr-${deviceId}-${attributeId}`);
    }
}

function closeEditModal() {
    document.getElementById('edit-device-modal').style.display = 'none';
}

// ---------------------------------------------------------------------------
// Fetch Manuals: search first, download only what the user approves.
// The dialog lists every PDF found as a Manual | Source | Store Manual table.
// Nothing downloads automatically; rows whose URL is already stored are
// highlighted and skipped by "Download All". Re-fetching a stored manual
// requires an explicit replace confirmation (server also refuses with 409).
// ---------------------------------------------------------------------------

let fetchManualsDeviceId = null;
let fetchCandidates = [];
// Terms the current Fetch Manuals results were found with. Defaults to the
// device's brand + model; "Change Search Terms" lets the user override them
// and re-run the search without closing the dialog.
let fetchSearchTerms = '';
let _fetchSearchInFlight = false;

function _defaultFetchTerms(deviceId) {
    const device = devices.find(d => d.id === deviceId);
    return device ? `${device.brand || ''} ${device.model || ''}`.trim() : '';
}

async function openFetchManuals(deviceId) {
    // One fetch dialog at a time: a double-click must not start two searches.
    if (!beginOp('fetch-manuals')) return;
    fetchManualsDeviceId = deviceId;
    fetchCandidates = [];
    fetchSearchTerms = _defaultFetchTerms(deviceId);

    const modal = document.getElementById('fetch-manuals-modal');
    modal.style.display = 'flex';
    resetFetchTermsRow();
    await _runFetchSearch();
}

// Run (or re-run) the candidate search with the current fetchSearchTerms and
// render into the dialog. The 'fetch-manuals' op lock is already held by
// openFetchManuals for as long as the dialog is open, so re-searches guard
// against double-clicks with _fetchSearchInFlight instead.
async function _runFetchSearch() {
    const statusBox = document.getElementById('fetch-manuals-status');
    const spinner = document.getElementById('fetch-manuals-spinner');
    const message = document.getElementById('fetch-manuals-message');
    const results = document.getElementById('fetch-manuals-results');
    const downloadAllBtn = document.getElementById('download-all-btn');

    statusBox.classList.remove('done-success', 'done-error');
    spinner.style.display = '';
    const label = fetchSearchTerms ? `"${fetchSearchTerms}"` : 'this device';
    message.textContent = `Searching the web for ${label} manuals...`;
    results.innerHTML = '';
    results.hidden = true;
    downloadAllBtn.style.display = 'none';

    _fetchSearchInFlight = true;
    try {
        const qs = fetchSearchTerms ? `?q=${encodeURIComponent(fetchSearchTerms)}` : '';
        const response = await fetch(`/api/downloads/${fetchManualsDeviceId}/search${qs}`);
        if (!response.ok) throw new Error(await errorDetailFrom(response, 'Manual search failed.'));
        const data = await response.json();
        fetchCandidates = data.candidates || [];
        renderFetchResults(data.error_detail);
    } catch (error) {
        console.error('Failed to search manuals:', error);
        spinner.style.display = 'none';
        statusBox.classList.add('done-error');
        message.textContent = error.message || 'Manual search failed.';
    } finally {
        _fetchSearchInFlight = false;
    }
}

// --- Fetch Manuals "search terms" row -------------------------------------
// The greyed read-only input shows exactly what was searched for. Clicking
// it (or "Change Search Terms") switches to edit mode with Search/Cancel;
// Search re-runs the fetch, Cancel restores the previously used terms.
function resetFetchTermsRow() {
    const input = document.getElementById('fetch-search-input');
    input.value = fetchSearchTerms;
    setFetchTermsEditing(false);
}

function setFetchTermsEditing(editing) {
    const input = document.getElementById('fetch-search-input');
    input.readOnly = !editing;
    input.title = editing ? 'Enter new search terms' : 'Click to change the search terms';
    document.getElementById('fetch-change-terms-btn').style.display = editing ? 'none' : 'inline-block';
    document.getElementById('fetch-apply-terms-btn').style.display = editing ? 'inline-block' : 'none';
    document.getElementById('fetch-cancel-terms-btn').style.display = editing ? 'inline-block' : 'none';
}

function startChangeSearchTerms() {
    const input = document.getElementById('fetch-search-input');
    if (!input.readOnly || _fetchSearchInFlight) return; // already editing / searching
    setFetchTermsEditing(true);
    input.focus();
    input.select();
}

function cancelChangeSearchTerms() {
    const input = document.getElementById('fetch-search-input');
    input.value = fetchSearchTerms; // discard the edit, restore last searched terms
    setFetchTermsEditing(false);
}

async function applyNewSearchTerms() {
    if (_fetchSearchInFlight) return;
    const input = document.getElementById('fetch-search-input');
    // Blank means "back to the device's brand + model" (the default query).
    fetchSearchTerms = input.value.trim() || _defaultFetchTerms(fetchManualsDeviceId);
    resetFetchTermsRow();
    await _runFetchSearch();
}

function renderFetchResults(errorDetail) {
    const statusBox = document.getElementById('fetch-manuals-status');
    const spinner = document.getElementById('fetch-manuals-spinner');
    const message = document.getElementById('fetch-manuals-message');
    const results = document.getElementById('fetch-manuals-results');
    const downloadAllBtn = document.getElementById('download-all-btn');

    spinner.style.display = 'none';

    if (fetchCandidates.length === 0) {
        statusBox.classList.add('done-error');
        message.textContent = errorDetail || 'No manuals found for this device.';
        results.hidden = true;
        downloadAllBtn.style.display = 'none';
        return;
    }

    const freshCount = fetchCandidates.filter(c => !c.already_downloaded).length;
    message.textContent = freshCount === 0
        ? `Found ${fetchCandidates.length} manual(s) - all are already stored.`
        : `Found ${fetchCandidates.length} manual(s). Review manuals by clicking on them and download the correct manual by clicking the download button next to it. There are(${freshCount} manuals not stored yet).`;

    results.innerHTML = `
        <table class="fetch-table">
            <thead>
                <tr><th>Manual</th><th>Source</th><th>Store Manual</th></tr>
            </thead>
            <tbody>
                ${fetchCandidates.map((c, i) => fetchRowHtml(c, i)).join('')}
            </tbody>
        </table>`;
    results.hidden = false;

    // Download All only makes sense while something new is left to fetch.
    downloadAllBtn.style.display = freshCount > 0 ? 'inline-block' : 'none';
}

function fetchRowHtml(candidate, index) {
    // The name doubles as a preview link so the user can open the PDF in a
    // new tab and check it is the right manual BEFORE approving the download.
    const href = safePreviewHref(candidate.url);
    const nameCell = href
        ? `<a class="fetch-manual-name fetch-preview-link" href="${href}" target="_blank" rel="noopener noreferrer" title="Open ${escapeHtml(candidate.title || candidate.url)} in a new tab">${escapeHtml(candidate.name)}</a>`
        : `<span class="fetch-manual-name" title="${escapeHtml(candidate.title || candidate.url)}">${escapeHtml(candidate.name)}</span>`;
    // Stored rows stay downloadable on purpose (replace), but only after an
    // explicit confirm - hence the different tooltip.
    const actionTitle = candidate.already_downloaded
        ? 'Replace the stored manual with a fresh copy'
        : 'Download and store this manual';
    return `
        <tr id="fetch-row-${index}" class="${candidate.already_downloaded ? 'existing' : ''}">
            <td>${nameCell}</td>
            <td class="fetch-source">${escapeHtml(candidate.domain)}</td>
            <td class="fetch-action">
                ${candidate.already_downloaded ? '<span class="stored-chip">Stored</span>' : ''}
                <button type="button" class="card-icon-btn" title="${actionTitle}" aria-label="${actionTitle}" data-dedupe
                        onclick="storeManual(${index}, this)">${CARD_ACTION_ICONS.download}</button>
            </td>
        </tr>`;
}

// Per-row Store button. Never overwrites silently: an already-stored manual
// asks for confirmation first, then sends replace=true to the server.
async function storeManual(index, btn) {
    const candidate = fetchCandidates[index];
    if (!candidate || candidate._done) return;

    let replace = false;
    if (candidate.already_downloaded) {
        replace = confirm(`"${candidate.name}" is already downloaded. Replace the existing manual?`);
        if (!replace) return;
    }

    const ok = await _storeOne(index, btn, replace, fetchManualsDeviceId);
    if (ok) {
        showToast(`Stored "${fetchCandidates[index].name}"`, 'success');
        await refreshAfterManualStored();
    }
}

// Download one approved candidate and flip its row to the stored state.
// Shared by the per-row button and Download All; returns true on success.
// A 409 means the URL got stored meanwhile (e.g. another tab): ask to replace.
async function _storeOne(index, btn, replace, deviceId) {
    const candidate = fetchCandidates[index];
    if (!candidate || candidate._done) return false;

    const originalIcon = btn ? btn.innerHTML : null;
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="btn-spinner"></span>'; }

    const body = JSON.stringify({ device_id: deviceId, url: candidate.url, title: candidate.title || null, replace });
    try {
        let response = await fetch('/api/downloads/store', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body
        });

        if (response.status === 409 && !replace) {
            const detail = await response.json().catch(() => null);
            const filename = (detail && detail.detail && detail.detail.filename) || candidate.name;
            if (!confirm(`"${filename}" is already downloaded. Replace the existing manual?`)) {
                if (btn) { btn.disabled = false; btn.innerHTML = originalIcon; }
                return false;
            }
            response = await fetch('/api/downloads/store', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ device_id: deviceId, url: candidate.url, title: candidate.title || null, replace: true })
            });
        }

        if (!response.ok) throw new Error(await errorDetailFrom(response, 'Download failed.'));

        await response.json();
        candidate._done = true;
        candidate.already_downloaded = true;
        _markRowStored(index);
        return true;
    } catch (error) {
        console.error('Failed to store manual:', error);
        _markRowError(index, btn, originalIcon, error.message || 'Download failed.');
        showToast(error.message || 'Download failed.', 'error');
        return false;
    }
}

function _markRowStored(index) {
    const row = document.getElementById(`fetch-row-${index}`);
    if (!row) return; // dialog was closed / re-opened for another device
    row.classList.add('existing');
    row.classList.remove('failed');
    const actionCell = row.querySelector('.fetch-action');
    if (actionCell) actionCell.innerHTML = '<span class="stored-chip">Stored</span>';
}

function _markRowError(index, btn, originalIcon, reason) {
    const row = document.getElementById(`fetch-row-${index}`);
    if (row) row.classList.add('failed');
    if (btn && document.contains(btn)) {
        btn.disabled = false;
        btn.innerHTML = originalIcon ?? CARD_ACTION_ICONS.download;
        btn.title = reason;
    }
}

// Keep manual counts and the edit dialog's stored-manual list in sync after
// a successful store.
async function refreshAfterManualStored() {
    await loadDevices();
    const editModal = document.getElementById('edit-device-modal');
    if (editModal.style.display === 'flex' &&
        parseInt(document.getElementById('edit-device-id').value) === fetchManualsDeviceId) {
        await loadManuals(fetchManualsDeviceId);
    }
}

// Sequentially store every candidate that is not already on file. Stored
// manuals are deliberately skipped - replacing one is a per-row, confirmed
// action only.
async function downloadAllManuals() {
    if (!beginOp('download-all-manuals')) return;
    const btn = document.getElementById('download-all-btn');
    const deviceId = fetchManualsDeviceId;
    const targets = fetchCandidates
        .map((c, i) => ({ c, i }))
        .filter(x => !x.c.already_downloaded && !x.c._done);

    btn.disabled = true;
    let okCount = 0;
    for (let n = 0; n < targets.length; n++) {
        // Bail out if the dialog was closed or re-opened for another device.
        const modalOpen = document.getElementById('fetch-manuals-modal').style.display === 'flex';
        if (!modalOpen || fetchManualsDeviceId !== deviceId) break;

        btn.textContent = `Downloading ${n + 1}/${targets.length}...`;
        const rowBtn = document.querySelector(`#fetch-row-${targets[n].i} .card-icon-btn`);
        if (await _storeOne(targets[n].i, rowBtn, false, deviceId)) okCount++;
    }

    btn.disabled = false;
    btn.textContent = 'Download All';
    endOp('download-all-manuals');

    if (okCount > 0) {
        showToast(`Stored ${okCount} manual${okCount !== 1 ? 's' : ''}`, 'success');
        await refreshAfterManualStored();
    }
    if (!fetchCandidates.some(c => !c.already_downloaded)) {
        btn.style.display = 'none';
        const message = document.getElementById('fetch-manuals-message');
        if (message) message.textContent = 'All found manuals are stored.';
    }
}

function closeFetchManualsModal() {
    document.getElementById('fetch-manuals-modal').style.display = 'none';
    endOp('fetch-manuals');
}

// Candidate URLs come from web search results, so only http(s) may become a
// clickable href - anything else (javascript:, data:) renders as plain text.
function safePreviewHref(url) {
    try {
        const parsed = new URL(String(url || ''), window.location.origin);
        return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : '';
    } catch (e) {
        return '';
    }
}



// Open the file picker for a specific device's manual upload.
function triggerUploadManual(deviceId) {
    uploadTargetDeviceId = deviceId;
    const input = document.getElementById('manual-upload-input');
    input.value = ''; // allow re-selecting the same file after a cancel
    input.click();
}

// Handle a PDF chosen via the hidden upload input.
async function handleManualFileSelected(event) {
    const file = event.target.files[0];
    const deviceId = uploadTargetDeviceId;
    uploadTargetDeviceId = null;
    if (!file || !deviceId) return;

    if (!file.name.toLowerCase().endsWith('.pdf')) {
        showToast('Only PDF files are supported', 'error');
        return;
    }
    if (!beginOp(`upload-manual-${deviceId}`)) return;

    showToast(`Uploading ${file.name}...`, 'success');
    try {
        const formData = new FormData();
        formData.append('file', file);

        const response = await fetch(`/api/devices/${deviceId}/manuals/upload`, {
            method: 'POST',
            body: formData
        });

        if (!response.ok) {
            let detail = 'Failed to upload manual';
            try {
                const err = await response.json();
                if (err.detail) detail = err.detail;
            } catch (e) { /* non-JSON error body */ }
            throw new Error(detail);
        }

        const manual = await response.json();
        showToast(`Uploaded "${manual.filename}"`, 'success');
        await loadDevices(); // refresh the manual count on the device card
        // If the edit modal is open for this device, show the new manual too.
        if (document.getElementById('edit-device-modal').style.display === 'flex' &&
            parseInt(document.getElementById('edit-device-id').value) === deviceId) {
            await loadManuals(deviceId);
        }
    } catch (error) {
        console.error('Failed to upload manual:', error);
        showToast(error.message || 'Failed to upload manual', 'error');
    } finally {
        endOp(`upload-manual-${deviceId}`);
    }
}

// Manuals currently shown in the edit modal, so the delete confirm can name
// the file without embedding filenames into inline onclick handlers.
let currentManuals = [];

// Render the manuals of a device inside the edit modal.
async function loadManuals(deviceId) {
    const container = document.getElementById('device-manuals-list');
    container.innerHTML = '<div class="loading">Loading manuals...</div>';
    currentManuals = [];

    try {
        const response = await fetch(`/api/downloads/${deviceId}/manuals`);
        if (!response.ok) throw new Error('Failed to load manuals');
        const manuals = await response.json();
        currentManuals = manuals;

        if (manuals.length === 0) {
            container.innerHTML = '<div class="empty-state">No manuals yet. Download or upload one below.</div>';
            return;
        }

        container.innerHTML = manuals.map(m => `
            <div class="manual-item">
                <a class="manual-filename manual-link" href="/api/downloads/manuals/${m.id}/file"
                   target="_blank" rel="noopener" title="Open ${escapeHtml(m.filename)} (stored at ${escapeHtml(m.filepath)})">
                    ${escapeHtml(m.filename)}
                </a>
                <button type="button" class="card-icon-btn card-icon-danger" title="Delete manual" aria-label="Delete manual" data-dedupe onclick="deleteManual(${deviceId}, ${m.id})">${CARD_ACTION_ICONS.trash}</button>
            </div>
        `).join('');
    } catch (error) {
        console.error('Failed to load manuals:', error);
        container.innerHTML = '<div class="empty-state">Failed to load manuals.</div>';
    }
}

async function deleteManual(deviceId, manualId) {
    const manual = currentManuals.find(m => m.id === manualId);
    const label = manual ? manual.filename : `manual ${manualId}`;
    if (!confirm(`Delete "${label}"?`)) return;
    if (!beginOp(`delete-manual-${deviceId}-${manualId}`)) return;

    try {
        const response = await fetch(`/api/devices/${deviceId}/manuals/${manualId}`, {
            method: 'DELETE'
        });
        if (!response.ok) throw new Error('Failed to delete manual');

        showToast('Manual deleted', 'success');
        await loadManuals(deviceId);
        await loadDevices(); // refresh the manual count on the device card
    } catch (error) {
        console.error('Failed to delete manual:', error);
        showToast('Failed to delete manual', 'error');
    } finally {
        endOp(`delete-manual-${deviceId}-${manualId}`);
    }
}

async function deleteDevice(deviceId) {
    if (!confirm('Are you sure you want to delete this device and all its manuals?')) {
        return;
    }
    if (!beginOp(`delete-device-${deviceId}`)) return;
    
    try {
        const response = await fetch(`/api/devices/${deviceId}`, {
            method: 'DELETE'
        });
        
        if (!response.ok) throw new Error('Failed to delete device');
        
        expandedDeviceIds.delete(deviceId);
        showToast('Device deleted', 'success');
        await loadDevices();
    } catch (error) {
        console.error('Failed to delete device:', error);
        showToast('Failed to delete device', 'error');
    } finally {
        endOp(`delete-device-${deviceId}`);
    }
}

// ---------------------------------------------------------------------------
// Devices wiring (was part of setupEventListeners in the single-file app).
// ---------------------------------------------------------------------------

function wireDevicesEvents() {
    // Add device: both sidebar and in-tab buttons run the New Device flow,
    // which offers an AI-Chat shortcut when a model is configured (falling
    // back straight to the manual form otherwise).
    document.getElementById('add-device-btn').addEventListener('click', startNewDeviceFlow);
    document.getElementById('add-device-btn-devices').addEventListener('click', startNewDeviceFlow);
    document.getElementById('add-device-form').addEventListener('submit', handleAddDevice);

    // Edit device form
    document.getElementById('edit-device-form').addEventListener('submit', handleEditDevice);

    // Warranty end auto-fills from purchase date + length in both device forms.
    initWarrantyAutoCalc('device');
    initWarrantyAutoCalc('edit-device');

    // Manual upload: the hidden input is shared by every device's "Upload
    // Manual" button; the target device id is stashed before opening it.
    document.getElementById('manual-upload-input').addEventListener('change', handleManualFileSelected);

    // The Add Device modal has its own (optional) manual picker: files are
    // uploaded right after the new device row exists.
    document.getElementById('add-device-manuals').addEventListener('change', updateAddDeviceManualNames);

    // Fetch Manuals search-terms box: click to edit, Enter to re-search.
    document.getElementById('fetch-search-input').addEventListener('click', startChangeSearchTerms);
    document.getElementById('fetch-search-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault(); // the input is outside a form, but be safe
            applyNewSearchTerms();
        }
    });
}
