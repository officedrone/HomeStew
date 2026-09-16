// HomeBrain Frontend Application

document.addEventListener('DOMContentLoaded', () => {
    initializeApp();
});

// ---------------------------------------------------------------------------
// Markdown rendering for chat answers.
// marked.js parses, DOMPurify sanitises (LLM output is untrusted), and both
// libraries are vendored under /static/vendor so everything works offline.
// ---------------------------------------------------------------------------
if (window.marked) {
    marked.setOptions({ gfm: true, breaks: true });
}
if (window.DOMPurify) {
    // Open manual/citation links in a new tab so the chat is never lost,
    // and strip referrer info from outbound links.
    DOMPurify.addHook('afterSanitizeAttributes', (node) => {
        if (node.tagName === 'A') {
            node.target = '_blank';
            node.rel = 'noopener noreferrer';
        }
    });
}

function renderMarkdown(text) {
    const source = text || '';
    // Graceful fallback when the vendor scripts failed to load: plain text.
    if (!window.marked) return escapeHtml(source).replace(/\n/g, '<br>');
    const html = marked.parse(source);
    return window.DOMPurify ? DOMPurify.sanitize(html) : html;
}

let devices = [];
let currentDeviceFilter = null;
// Calendar tab state: device filter and the currently loaded events.
let currentCalendarDeviceFilter = null;
let calendarEvents = [];
// Selected device filter for the AI Chat tab (null = all devices). Mirrors
// currentDeviceFilter, but scoped to chat so the two tabs stay independent.
let currentChatDeviceFilter = null;
// Device id currently targeted by the hidden manual-upload input.
let uploadTargetDeviceId = null;

// Result of the last chat model availability check (see checkChatModelStatus).
// null = unknown/not yet checked; true/false = whether chatting is allowed.
let chatModelAvailable = null;

// While an answer streams in, this controller lets the Stop button abort the
// fetch (which also tears down the SSE stream server-side). Null when idle.
let chatAbortController = null;

// Chat scroll state — pinned-to-bottom with heuristic so user can read mid-stream.
let chatPinned = true;
const CHAT_SCROLL_THRESHOLD = 80; // px from bottom to consider "pinned"
let _chatScrollQueued = false;

function isNearBottom(el, threshold) {
    return (el.scrollHeight - el.scrollTop - el.clientHeight) <= threshold;
}

function scheduleChatScroll() {
    if (_chatScrollQueued) return;
    _chatScrollQueued = true;
    requestAnimationFrame(() => {
        _chatScrollQueued = false;
        const container = document.getElementById('chat-messages');
        if (!container) return;
        if (chatPinned) scrollToBottom();
        updateScrollDownButton();
    });
}

// Always pins; safe as a click handler (the event arg is ignored).
function scrollToBottom() {
    chatPinned = true;
    const container = document.getElementById('chat-messages');
    if (!container) return;
    container.scrollTop = container.scrollHeight;
    updateScrollDownButton();
}

function updateScrollDownButton() {
    const btn = document.getElementById('scroll-down-btn');
    if (!btn) return;
    const container = document.getElementById('chat-messages');
    if (!container) return;
    const distFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
    // Show button when more than ~120px of content is below the current view.
    btn.classList.toggle('hidden', distFromBottom <= 120);
}

async function initializeApp() {
    await loadDevices();
    setupEventListeners();
    // Restore the tab from before a page refresh (URL hash first, then the
    // last tab persisted to localStorage) so F5 no longer dumps the user on
    // the Search tab.
    restoreActiveTab();
}

// ---------------------------------------------------------------------------
// Click deduplication (two layers, both needed)
// ---------------------------------------------------------------------------

// Layer 1 — rapid re-clicks: buttons tagged [data-dedupe] ignore further
// clicks inside a short double-click window. The listener only blocks; it
// never invokes handlers itself, so normal listeners / inline onclick
// attributes keep working untouched on the first click.
const DEDUPE_WINDOW_MS = 500;
document.addEventListener('click', (e) => {
    const el = e.target.closest && e.target.closest('[data-dedupe]');
    if (!el || el.disabled) return;
    if (Date.now() - (el._lastDedupeClick ?? 0) < DEDUPE_WINDOW_MS) {
        e.preventDefault();
        e.stopImmediatePropagation();
        return;
    }
    el._lastDedupeClick = Date.now();
}, true);

// Layer 2 — in-flight operations: server calls (add device, save event,
// upload manual, delete, ...) hold a named lock for the whole async body, so
// even a slow request can't be duplicated by a second click that lands after
// the window above has expired. beginOp returns false when the same key is
// already running; every handler must endOp in a finally block.
const _opsInFlight = new Set();
function beginOp(key) {
    if (_opsInFlight.has(key)) return false;
    _opsInFlight.add(key);
    return true;
}
function endOp(key) {
    _opsInFlight.delete(key);
}

function setupEventListeners() {
    // Tab switching. The buttons now contain an icon/label, so read the tab
    // name from currentTarget — e.target can be the inner <svg>/<span>.
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', (e) => switchTab(e.currentTarget.dataset.tab));
    });
    
    // Add device: both sidebar and in-tab buttons open the same modal.
    document.getElementById('add-device-btn').addEventListener('click', openAddDeviceModal);
    document.getElementById('add-device-btn-devices').addEventListener('click', openAddDeviceModal);
    document.getElementById('add-device-form').addEventListener('submit', handleAddDevice);
    
    // Edit device form
    document.getElementById('edit-device-form').addEventListener('submit', handleEditDevice);

    // Warranty end auto-fills from purchase date + length in both device forms.
    initWarrantyAutoCalc('device');
    initWarrantyAutoCalc('edit-device');
    
    // Search functionality
    document.getElementById('search-btn').addEventListener('click', performSearch);
    document.getElementById('search-input').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') performSearch();
    });

    // Device filters: search and chat keep independent selections.
    document.getElementById('device-filter').addEventListener('change', (e) => {
        currentDeviceFilter = e.target.value || null;
    });
    document.getElementById('chat-device-filter').addEventListener('change', (e) => {
        currentChatDeviceFilter = e.target.value || null;
    });

    // Calendar: tab filter, new-event button and the event form.
    document.getElementById('calendar-device-filter').addEventListener('change', (e) => {
        currentCalendarDeviceFilter = e.target.value || null;
        loadCalendarEvents();
    });
    document.getElementById('add-event-btn').addEventListener('click', () => openEventModal());
    document.getElementById('event-form').addEventListener('submit', handleSaveEvent);
    // "Every N" only makes sense for a repeating event.
    document.getElementById('event-recurrence').addEventListener('change', (e) => {
        document.getElementById('event-interval-field').style.display =
            e.target.value === 'none' ? 'none' : '';
    });
    
    // Chat functionality
    document.getElementById('send-btn').addEventListener('click', sendChatMessage);
    document.getElementById('chat-input').addEventListener('keypress', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendChatMessage();
        }
    });

    // Stop aborts the in-flight stream; New Session clears the conversation.
    document.getElementById('stop-btn').addEventListener('click', stopChatStream);
    document.getElementById('new-session-btn').addEventListener('click', startNewSession);

    // Scroll-to-bottom button for chat messages.
    document.getElementById('scroll-down-btn').addEventListener('click', scrollToBottom);

    // Chat scroll container: track pinned state and wire observers.
    (function initChatScroll() {
        const container = document.getElementById('chat-messages');
        if (!container) return;
        chatPinned = true;
        container.addEventListener('scroll', () => {
            chatPinned = isNearBottom(container, CHAT_SCROLL_THRESHOLD);
            updateScrollDownButton();
        });
        // Re-pins when user expands/collapses a <details> while pinned.
        container.addEventListener('toggle', (e) => {
            if (!chatPinned) return;
            requestAnimationFrame(() => scrollToBottom());
        }, true);
        // Observe new message nodes so their height changes update button visibility and re-pin.
        const ro = new ResizeObserver(() => {
            updateScrollDownButton();
            if (chatPinned) scrollToBottom();
        });
        // The container itself resizes when the model-warning banner shows/hides;
        // without observing it, pinning/button state would go stale until the next scroll.
        ro.observe(container);
        const mo = new MutationObserver((mutations) => {
            for (const m of mutations) {
                for (const node of m.addedNodes) {
                    if (node.nodeType === 1) ro.observe(node);
                }
                for (const node of m.removedNodes) {
                    if (node.nodeType === 1) ro.unobserve(node);
                }
            }
        });
        mo.observe(container, { childList: true });
    })();

    // Manual upload: the hidden input is shared by every device's "Upload
    // Manual" button; the target device id is stashed before opening it.
    document.getElementById('manual-upload-input').addEventListener('change', handleManualFileSelected);

    // The Add Device modal has its own (optional) manual picker: files are
    // uploaded right after the new device row exists.
    document.getElementById('add-device-manuals').addEventListener('change', updateAddDeviceManualNames);

    // Sidebar: docked rail on desktop (expand/collapse), overlay drawer on
    // mobile (hamburger + backdrop). Which mode applies is pure CSS; the JS
    // just manages the two independent body classes.
    applySidebarState();
    document.getElementById('sidebar-toggle-btn').addEventListener('click', toggleSidebar);
    document.getElementById('menu-btn').addEventListener('click', openDrawer);
    document.getElementById('sidebar-backdrop').addEventListener('click', closeDrawer);
    // Crossing back to desktop widths must not leave the drawer state stuck.
    window.addEventListener('resize', () => {
        if (window.innerWidth > 768) closeDrawer();
    });

    // Theme: preference lives in server settings (Settings > General radio
    // buttons); applyTheme() mirrors it onto <html data-theme>.
    applyTheme();

    // Settings modal
    document.getElementById('settings-btn').addEventListener('click', openSettingsModal);
    // Section nav (General / AI): show one panel at a time.
    document.querySelectorAll('.settings-nav-btn').forEach((btn) => {
        btn.addEventListener('click', () => showSettingsSection(btn.dataset.settingsSection));
    });
    document.getElementById('settings-form').addEventListener('submit', handleSettingsSave);
    document.getElementById('fetch-models-btn').addEventListener('click', () => fetchLlmModels());
    document.getElementById('test-webhook-btn').addEventListener('click', () => sendTestNotification());
    // Switching webhook type updates the URL hint and hides the bearer-token
    // field (Synology Chat authenticates via the token= param in its URL).
    document.getElementById('settings-notify-webhook-type').addEventListener(
        'change', updateWebhookTypeHints);
    setupModelDropdownRefresh();

    // Chat model warning banner: takes the user straight to Settings.
    document.getElementById('chat-open-settings-btn').addEventListener('click', openSettingsModal);

    // Model switcher in the chat toolbar: picking a model saves it right away
    // (same settings the modal writes) and re-runs the availability probe.
    document.getElementById('chat-model-select').addEventListener('change', handleChatModelChange);

    // "Restore Default" buttons in Advanced Settings repopulate the built-in
    // prompt text (fetched from the API) into the matching textarea.
    document.querySelectorAll('[data-restore-default]').forEach((btn) => {
        btn.addEventListener('click', () => restorePromptDefault(btn.dataset.restoreDefault));
    });

    // Escape closes any open modal, or the mobile drawer when none is open.
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        for (const id of ['settings-modal', 'add-device-modal', 'edit-device-modal', 'event-modal']) {
            const modal = document.getElementById(id);
            if (modal.style.display === 'flex') {
                modal.style.display = 'none';
                return;
            }
        }
        closeDrawer();
    });
}

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
    } catch (error) {
        console.error('Failed to load devices:', error);
        showToast('Failed to load devices', 'error');
    }
}

// One "Label: value" meta line per configured detail of a device (serial,
// product number, custom attributes) — used in the expanded accordion panel.
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

// Sidebar "Recent Devices": only the three most recently created devices,
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
        .slice(0, 3);
    
    container.innerHTML = recent.map(device => {
        const open = expandedDeviceIds.has(device.id);
        return `
        <div class="device-accordion${open ? ' open' : ''}" data-id="${device.id}">
            <div class="device-row" onclick="toggleDeviceExpansion(${device.id})">
                <span class="device-caret">›</span>
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

// Feather-style icons for the square icon-only buttons on device cards.
const CARD_ACTION_ICONS = {
    edit: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"></path></svg>',
    trash: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>',
    check: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>'
};

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
// Calendar (device maintenance reminders)
// ---------------------------------------------------------------------------

// "in 3 days" / "Today" / "2 days overdue" style relative-day text.
function dueLabel(dateStr) {
    if (!dateStr) return 'Done';
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const due = new Date(`${dateStr}T00:00:00`);
    const days = Math.round((due - today) / 86400000);
    if (days === 0) return 'Today';
    if (days === 1) return 'Tomorrow';
    if (days > 1) return `in ${days} days`;
    if (days === -1) return '1 day overdue';
    return `${-days} days overdue`;
}

// Sidebar: events due within the next 7 days (overdue included), shown as a
// compact notification feed under "Recent Devices".
async function loadUpcomingEvents() {
    const container = document.getElementById('upcoming-events');
    try {
        const response = await fetch('/api/calendar/upcoming?days=7');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();

        if (!data.events.length) {
            container.innerHTML = '<div class="empty-state">Nothing due in the next 7 days</div>';
            return;
        }

        container.innerHTML = data.events.map(ev => {
            const overdue = ev.status === 'overdue';
            const today = ev.status === 'today';
            const when = dueLabel(ev.next_due_date);
            const deviceName = ev.device_name ? escapeHtml(ev.device_name) : 'No device';
            return `
            <div class="upcoming-item${overdue ? ' overdue' : ''}${today ? ' today' : ''}"
                 title="${escapeHtml(ev.recurrence_label)}">
                <button type="button" class="upcoming-check" title="Mark done" data-dedupe
                        onclick="completeEvent(${ev.id})">&#10003;</button>
                <div class="upcoming-body" onclick="switchTab('calendar')">
                    <div class="upcoming-title">${escapeHtml(ev.title)}</div>
                    <div class="upcoming-meta">${deviceName} · <span class="upcoming-when">${when}</span></div>
                </div>
            </div>`;
        }).join('');
    } catch (error) {
        console.error('Failed to load upcoming events:', error);
        container.innerHTML = '<div class="empty-state">Could not load events</div>';
    }
}

// Calendar tab: full event list with complete / edit / delete actions.
async function loadCalendarEvents() {
    const container = document.getElementById('calendar-list');
    try {
        const qs = currentCalendarDeviceFilter ? `?device_id=${currentCalendarDeviceFilter}` : '';
        const response = await fetch(`/api/calendar${qs}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        calendarEvents = await response.json();
        renderCalendarList();
    } catch (error) {
        console.error('Failed to load calendar events:', error);
        container.innerHTML = '<div class="empty-state">Could not load calendar events.</div>';
    }
}

function renderCalendarList() {
    const container = document.getElementById('calendar-list');

    if (!calendarEvents.length) {
        container.innerHTML = `
            <div class="empty-state">
                <p>No maintenance events yet.</p>
                <p>Add one to get reminded about things like filter replacements,
                cleaning or firmware updates.</p>
            </div>`;
        return;
    }

    container.innerHTML = calendarEvents.map(ev => {
        const statusClass = ev.status === 'overdue' ? ' overdue'
                          : ev.status === 'today' ? ' today'
                          : ev.status === 'done' ? ' done' : '';
        const when = ev.next_due_date
            ? `${new Date(`${ev.next_due_date}T00:00:00`).toLocaleDateString()} · ${dueLabel(ev.next_due_date)}`
            : 'Completed';
        return `
        <div class="event-card${statusClass}" data-id="${ev.id}" onclick="editCalendarEvent(${ev.id})">
            <div class="event-card-main">
                <div class="event-card-title">${escapeHtml(ev.title)}</div>
                ${ev.description ? `<div class="event-card-desc">${escapeHtml(ev.description)}</div>` : ''}
                <div class="event-card-meta">
                    <span>${ev.device_name ? escapeHtml(ev.device_name) : 'No device'}</span>
                    <span>·</span>
                    <span>${ev.recurrence_label}${ev.start_time ? ` at ${escapeHtml(ev.start_time)}` : ''}</span>
                </div>
            </div>
            <div class="event-card-side">
                <div class="event-when">${when}</div>
                <div class="event-actions">
                    ${ev.status !== 'done' ? `<button type="button" class="card-icon-btn card-icon-success" title="Mark this occurrence as done" aria-label="Mark this occurrence as done" data-dedupe onclick="event.stopPropagation(); completeEvent(${ev.id})">${CARD_ACTION_ICONS.check}</button>` : ''}
                    <button type="button" class="card-icon-btn" title="Edit event" aria-label="Edit event" data-dedupe onclick="event.stopPropagation(); editCalendarEvent(${ev.id})">${CARD_ACTION_ICONS.edit}</button>
                    <button type="button" class="card-icon-btn card-icon-danger" title="Delete event" aria-label="Delete event" data-dedupe onclick="event.stopPropagation(); deleteCalendarEvent(${ev.id})">${CARD_ACTION_ICONS.trash}</button>
                </div>
            </div>
        </div>`;
    }).join('');
}

// Mark the current occurrence done: recurring events roll to their next date,
// one-time events become 'done'. Refreshes both the tab and the sidebar feed.
async function completeEvent(eventId) {
    if (!beginOp(`complete-event-${eventId}`)) return;
    try {
        const response = await fetch(`/api/calendar/${eventId}/complete`, { method: 'POST' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        showToast('Marked as done');
        loadCalendarEvents();
        loadUpcomingEvents();
    } catch (error) {
        showToast(`Failed to update event: ${error.message}`, 'error');
    } finally {
        endOp(`complete-event-${eventId}`);
    }
}

async function deleteCalendarEvent(eventId) {
    if (!confirm('Delete this calendar event?')) return;
    if (!beginOp(`delete-event-${eventId}`)) return;
    try {
        const response = await fetch(`/api/calendar/${eventId}`, { method: 'DELETE' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        showToast('Event deleted');
        loadCalendarEvents();
        loadUpcomingEvents();
    } catch (error) {
        showToast(`Failed to delete event: ${error.message}`, 'error');
    } finally {
        endOp(`delete-event-${eventId}`);
    }
}

// Open the event modal for a new event, or fill it from an existing one.
function openEventModal(eventId = null) {
    const form = document.getElementById('event-form');
    form.reset();
    document.getElementById('event-id').value = eventId || '';
    document.getElementById('event-modal-title').textContent =
        eventId ? 'Edit Event' : 'New Event';

    // Default schedule anchor: today.
    document.getElementById('event-date').value = new Date().toISOString().slice(0, 10);
    document.getElementById('event-interval-field').style.display = '';

    if (eventId) {
        const ev = calendarEvents.find(e => e.id === eventId);
        if (!ev) return;
        document.getElementById('event-title').value = ev.title;
        document.getElementById('event-description').value = ev.description || '';
        document.getElementById('event-device').value = ev.device_id || '';
        document.getElementById('event-date').value = ev.start_date;
        document.getElementById('event-time').value = ev.start_time || '';
        document.getElementById('event-recurrence').value = ev.recurrence_type;
        document.getElementById('event-interval').value = ev.interval;
        document.getElementById('event-interval-field').style.display =
            ev.recurrence_type === 'none' ? 'none' : '';
    }

    document.getElementById('event-modal').style.display = 'flex';
    document.getElementById('event-title').focus();
}

function editCalendarEvent(eventId) {
    openEventModal(eventId);
}

function closeEventModal() {
    document.getElementById('event-modal').style.display = 'none';
}

async function handleSaveEvent(e) {
    e.preventDefault();
    const eventId = document.getElementById('event-id').value;
    const recurrenceType = document.getElementById('event-recurrence').value;

    const payload = {
        title: document.getElementById('event-title').value.trim(),
        description: document.getElementById('event-description').value.trim() || null,
        device_id: document.getElementById('event-device').value
            ? parseInt(document.getElementById('event-device').value, 10) : null,
        start_date: document.getElementById('event-date').value,
        start_time: document.getElementById('event-time').value || null,
        recurrence_type: recurrenceType,
        interval: Math.max(1, parseInt(document.getElementById('event-interval').value, 10) || 1),
    };

    if (!payload.title || !payload.start_date) {
        showToast('Title and date are required', 'error');
        return;
    }
    if (!beginOp('save-event')) return;

    try {
        const response = await fetch(
            eventId ? `/api/calendar/${eventId}` : '/api/calendar',
            {
                method: eventId ? 'PUT' : 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            }
        );
        if (!response.ok) {
            let detail = `HTTP ${response.status}`;
            try {
                const data = await response.json();
                if (data.detail) detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
            } catch (err) { /* non-JSON error body */ }
            throw new Error(detail);
        }

        closeEventModal();
        showToast(eventId ? 'Event updated' : 'Event created');
        loadCalendarEvents();
        loadUpcomingEvents();
    } catch (error) {
        showToast(`Failed to save event: ${error.message}`, 'error');
    } finally {
        endOp('save-event');
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

function openAddDeviceModal() {
    document.getElementById('add-device-form').reset();
    resetWarrantyAutoCalc('device');
    updateAddDeviceManualNames();
    _addDeviceAttributes = [];
    renderAddDeviceAttributes();
    document.getElementById('add-device-modal').style.display = 'flex';
    setTimeout(() => document.getElementById('device-name').focus(), 50);
}

function closeAddDeviceModal() {
    document.getElementById('add-device-modal').style.display = 'none';
    _addDeviceAttributes = [];
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
        ...readWarrantyFields('device')
    };
    
    try {
        const response = await fetch('/api/devices', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(deviceData)
        });
        
        if (!response.ok) throw new Error('Failed to add device');

        // Upload any manuals picked in the form, now that we have a device id.
        const { id: newDeviceId } = await response.json();

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
                showToast(`Skipped "${file.name}" — only PDF files are supported`, 'error');
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
    // attributes — fetch the single-device endpoint so newly added/removed
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
            <button class="btn btn-danger btn-small" data-dedupe onclick="removeAttribute(${deviceId}, ${attr.id})">
                ×
            </button>
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

async function removeAttribute(deviceId, attributeId) {
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

async function downloadManuals(deviceId) {
    // One download per device at a time: a double-click must not open two
    // SSE streams / progress modals for the same device.
    if (!beginOp(`download-manuals-${deviceId}`)) return;

    // Show progress modal
    const modal = document.getElementById('download-progress-modal');
    const stepsContainer = document.getElementById('download-progress-steps');
    const statusBox = document.getElementById('download-progress-status');
    const spinner = document.getElementById('download-spinner');
    const statusText = document.getElementById('download-current-step');
    const closeBtn = document.getElementById('close-download-modal');

    modal.style.display = 'flex';
    stepsContainer.innerHTML = '';
    closeBtn.style.display = 'none';
    // Reset the status row to its "in progress" look (spinner visible, neutral).
    statusBox.classList.remove('done-success', 'done-error');
    if (spinner) spinner.style.display = '';
    statusText.textContent = 'Initializing...';

    // Guard so a terminal event is only handled once and late messages are ignored.
    let finished = false;

    // Stop the spinner and turn the status row into a clear success/error state.
    function finishDownload({ ok, headline, detail }) {
        endOp(`download-manuals-${deviceId}`);
        if (spinner) spinner.style.display = 'none';
        statusBox.classList.add(ok ? 'done-success' : 'done-error');
        statusText.textContent = headline;
        closeBtn.style.display = 'inline-block';
        // Reuse the same expandable-detail toast as the device-added popup so the
        // real reason (e.g. a DuckDuckGo rate-limit) is visible here too.
        showToast(headline, ok ? 'success' : 'error', detail || null);
    }

    try {
        // Use EventSource for Server-Sent Events streaming
        const eventSource = new EventSource(`/api/downloads/stream/${deviceId}`);

        eventSource.onmessage = async function(event) {
            let data;
            try {
                data = JSON.parse(event.data);
            } catch (e) {
                console.error('Failed to parse progress message:', event.data, e);
                return;
            }

            switch(data.type) {
                case 'step':
                    // Intermediate progress only — ignore once finished.
                    if (finished) break;
                    addProgressStep(stepsContainer, data.message, data.status || 'searching');
                    statusText.textContent = data.message;
                    break;

                case 'complete':
                    if (finished) break;
                    finished = true;
                    eventSource.close();
                    if (data.success) {
                        const count = data.downloaded_count ?? 0;
                        addProgressStep(stepsContainer, `Successfully downloaded ${count} manual(s)`, 'success');
                        finishDownload({ ok: true, headline: `Download complete! ${count} manual(s) downloaded` });
                        // Refresh device counts and the modal's manual list if it is open.
                        await loadDevices();
                        const editModal = document.getElementById('edit-device-modal');
                        if (editModal.style.display === 'flex' &&
                            parseInt(document.getElementById('edit-device-id').value) === deviceId) {
                            await loadManuals(deviceId);
                        }
                    } else {
                        // Backend sends a single terminal event with the real reason.
                        const reason = data.error_detail || 'No manuals were downloaded.';
                        addProgressStep(stepsContainer, reason, 'error');
                        finishDownload({ ok: false, headline: `Download failed: ${reason}`, detail: reason });
                    }
                    break;

                case 'error':
                    if (finished) break;
                    finished = true;
                    eventSource.close();
                    const message = data.message || 'An unexpected error occurred.';
                    addProgressStep(stepsContainer, message, 'error');
                    finishDownload({ ok: false, headline: `Error: ${message}`, detail: message });
                    break;
            }
        };

        eventSource.onerror = function(error) {
            if (finished) return;
            finished = true;
            console.error('EventSource failed:', error);
            eventSource.close();
            const reason = 'Connection to the server was lost while downloading.';
            addProgressStep(stepsContainer, reason, 'error');
            finishDownload({ ok: false, headline: `Connection error: ${reason}`, detail: reason });
        };

    } catch (error) {
        if (finished) return;
        finished = true;
        console.error('Failed to start download:', error);
        const reason = error.message || 'Could not start the download.';
        addProgressStep(stepsContainer, `Failed to start: ${reason}`, 'error');
        finishDownload({ ok: false, headline: `Error: ${reason}`, detail: reason });
    }
}

function addProgressStep(container, message, type = 'searching') {
    const step = document.createElement('div');
    step.className = `progress-step ${type}`;
    
    let icon = '⟳';
    if (type === 'searching') icon = '⌕';
    else if (type === 'found') icon = '✓';
    else if (type === 'downloading') icon = '↓';
    else if (type === 'success') icon = '★';
    else if (type === 'error') icon = '✗';
    
    step.innerHTML = `
        <div class="step-icon">${icon}</div>
        <div class="step-content">
            <div class="step-title">${escapeHtml(message)}</div>
        </div>
    `;
    
    container.appendChild(step);
    container.scrollTop = container.scrollHeight;
}

function closeDownloadModal() {
    document.getElementById('download-progress-modal').style.display = 'none';
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
                <button class="btn btn-danger btn-small" data-dedupe onclick="deleteManual(${deviceId}, ${m.id})">
                    ×
                </button>
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

async function performSearch() {
    const query = document.getElementById('search-input').value.trim();
    
    if (!query) {
        showToast('Please enter a search query', 'error');
        return;
    }
    if (!beginOp('search')) return;
    
    const container = document.getElementById('search-results');
    container.innerHTML = '<div class="loading">Searching...</div>';
    
    try {
        const body = { query };
        if (currentDeviceFilter) {
            body.device_id = currentDeviceFilter;
        }
        
        const response = await fetch('/api/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        if (!response.ok) {
            throw new Error(`Search request failed: ${response.status}`);
        }
        const data = await response.json();
        
        renderSearchResults(data);
    } catch (error) {
        console.error('Search failed:', error);
        container.innerHTML = '<div class="empty-state">Search failed. Please try again.</div>';
    } finally {
        endOp('search');
    }
}

function renderSearchResults(data) {
    const container = document.getElementById('search-results');
    
    if (data.total_results === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <p>No results found for "${escapeHtml(data.query)}"</p>
                <p>Try different keywords or download more manuals.</p>
            </div>
        `;
        return;
    }
    
    container.innerHTML = `
        <div style="margin-bottom: 15px; color: var(--text-secondary);">
            Found ${data.total_results} result${data.total_results !== 1 ? 's' : ''} for "${escapeHtml(data.query)}"
        </div>
        ${data.results.map(result => {
            // manual_id 0 = a hit in the device's own record (details /
            // custom attributes), not a PDF page — no file link to open.
            const header = result.manual_id === 0
                ? `
                    <span class="result-title">${escapeHtml(result.filename)}</span>
                    <a class="result-meta result-page-link" href="#" onclick="editDevice(${result.device_id}); return false;">Device entry &nearr;</a>
                `
                : `
                    <a class="result-title" href="/api/downloads/manuals/${result.manual_id}/file#page=${result.page_number}" target="_blank" rel="noopener">${escapeHtml(result.filename)}</a>
                    <a class="result-meta result-page-link" href="/api/downloads/manuals/${result.manual_id}/file#page=${result.page_number}" target="_blank" rel="noopener">Page ${result.page_number} &nearr;</a>
                `;
            return `
            <div class="result-item${result.manual_id === 0 ? ' result-device-entry' : ''}">
                <div class="result-header">${header}</div>
                <div class="result-snippet">${result.snippet}</div>
            </div>`;
        }).join('')}
    `;
}

async function sendChatMessage() {
    const input = document.getElementById('chat-input');
    const message = input.value.trim();
    
    if (!message) return;

    // If the last model probe failed, don't waste a round-trip: keep the
    // warning banner visible and point the user at Settings instead.
    if (chatModelAvailable === false) {
        showToast('No model is available — update the connection in Settings', 'error');
        return;
    }
    // One request at a time: a second click / Enter while streaming must not
    // start a parallel conversation turn.
    if (!beginOp('send-chat')) return;

    // Add user message to chat
    addMessageToChat('user', message);
    input.value = '';
    
    // Build history BEFORE adding the streaming bubble so its transient
    // content never leaks into the conversation sent to the backend.
    const messages = getChatMessages();

    // Streaming assistant bubble: collapsible thinking / tool-call trace plus
    // the live answer text (see createStreamRenderer).
    const renderer = createStreamRenderer();

    // Abort controller behind the Stop button; kept in a module-level so
    // stopChatStream() can reach it from its click handler.
    chatAbortController = new AbortController();
    setStreamingUi(true);

    try {
        // EventSource can't POST, so consume the SSE stream with fetch + reader.
        const chatPayload = {
            messages: messages.map(msg => ({
                role: msg.role,
                content: msg.content
            }))
        };
        // The chat device filter scopes manual searches to one device.
        if (currentChatDeviceFilter) {
            chatPayload.device_id = parseInt(currentChatDeviceFilter, 10);
        }

        const response = await fetch('/api/chat/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(chatPayload),
            signal: chatAbortController.signal
        });
        
        if (!response.ok || !response.body) {
            throw new Error(`Chat request failed (${response.status})`);
        }
        
        let finished = false;
        await readSseStream(response, (event) => {
            switch (event.type) {
                case 'thinking_start':
                    renderer.openThinking();
                    break;
                case 'thinking_delta':
                    renderer.appendThinking(event.delta || '');
                    break;
                case 'thinking_end':
                    renderer.closeThinking();
                    break;
                case 'tool_call':
                    renderer.addToolCall(event);
                    break;
                case 'tool_result':
                    renderer.finishToolCall(event);
                    break;
                case 'content_delta':
                    renderer.appendContent(event.delta || '');
                    break;
                case 'message':
                    // Authoritative final answer: replaces any streamed text.
                    renderer.finalize(event.content || '');
                    finished = true;
                    break;
                case 'error':
                    console.error('Chat stream error:', event.message);
                    renderer.fail('Sorry, I encountered an error. Please try again.');
                    finished = true;
                    break;
                case 'done':
                    break;
            }
        });

        if (!finished) {
            // Stream ended without a final message (e.g. connection dropped).
            renderer.fail('Sorry, the response was interrupted. Please try again.');
        }
    } catch (error) {
        if (error && error.name === 'AbortError') {
            // User pressed Stop: keep the partial answer, mark it stopped.
            renderer.stop('⏹ Stopped');
        } else {
            console.error('Chat failed:', error);
            renderer.fail('Sorry, I encountered an error. Please try again.');
        }
    } finally {
        chatAbortController = null;
        setStreamingUi(false);
        endOp('send-chat');
    }
}

// Toggle the Send / Stop buttons while an answer streams in. The textarea
// stays editable so the next question can be typed during generation.
function setStreamingUi(streaming) {
    document.getElementById('send-btn').style.display = streaming ? 'none' : '';
    document.getElementById('stop-btn').style.display = streaming ? '' : 'none';
}

// Abort the in-flight stream (Stop button). The fetch rejects with an
// AbortError, which sendChatMessage turns into a "Stopped" note; aborting
// also closes the SSE connection so the server stops generating.
function stopChatStream() {
    if (chatAbortController) chatAbortController.abort();
}

// Clear the conversation back to the welcome message. Any in-flight answer is
// stopped first so its partial output can't be appended to a fresh session.
function startNewSession() {
    stopChatStream();

    const container = document.getElementById('chat-messages');
    container.innerHTML = `
        <div class="message assistant">
            <div class="message-content">
                Hello! I'm HomeBrain. Ask me anything about your devices, and I'll search through your manuals to find answers.
            </div>
        </div>
    `;
    const input = document.getElementById('chat-input');
    if (input) input.value = '';
    chatPinned = true;
    updateScrollDownButton();
}

// Build the streaming assistant bubble and its event handlers.
//
// Layout inside one assistant message:
//   - a <details> "Thinking..." block that streams live while the model
//     reasons (auto-expanded during thinking, collapsed when done);
//   - one collapsed <details> per tool call showing a short summary in the
//     summary line; expanding reveals the full arguments and result info;
//   - the answer text itself, streamed into .message-content.
function createStreamRenderer() {
    const container = document.getElementById('chat-messages');

    const messageDiv = document.createElement('div');
    messageDiv.className = 'message assistant streaming';

    // Trace (thinking + tool calls) sits above the answer bubble.
    const trace = document.createElement('div');
    trace.className = 'trace';
    trace.style.display = 'none';

    const contentEl = document.createElement('div');
    contentEl.className = 'message-content markdown-body';

    const wrapper = document.createElement('div');
    wrapper.className = 'message-body';
    wrapper.appendChild(trace);
    wrapper.appendChild(contentEl);
    messageDiv.appendChild(wrapper);
    container.appendChild(messageDiv);
    scheduleChatScroll();

    let thinkingDetails = null;
    let thinkingPre = null;
    // Tool-call <details> elements keyed by tool call id.
    const toolCallEls = new Map();
    let contentText = '';
    let settled = false;

    function showTrace() {
        trace.style.display = 'block';
    }

    // Placeholder status shown until there is anything concrete to display.
    const placeholder = document.createElement('div');
    placeholder.className = 'loading-status';
    placeholder.textContent = 'Thinking';
    contentEl.appendChild(placeholder);

    function removePlaceholder() {
        if (placeholder.parentNode) placeholder.remove();
    }

    // Re-render the accumulated answer as markdown at most once per frame;
    // _raw keeps the original markdown text for conversation history.
    let renderQueued = false;
    function renderNow() {
        renderQueued = false;
        contentEl.innerHTML = renderMarkdown(contentText);
        contentEl._raw = contentText;
    }
    function scheduleRender() {
        if (renderQueued) return;
        renderQueued = true;
        requestAnimationFrame(renderNow);
    }

    return {
        openThinking() {
            showTrace();
            thinkingDetails = document.createElement('details');
            thinkingDetails.className = 'trace-item trace-thinking';
            // Expanded while streaming so the user watches reasoning arrive.
            thinkingDetails.open = true;
            const summary = document.createElement('summary');
            summary.textContent = 'Thinking…';
            thinkingPre = document.createElement('pre');
            thinkingDetails.appendChild(summary);
            thinkingDetails.appendChild(thinkingPre);
            trace.appendChild(thinkingDetails);
            scheduleChatScroll();
        },

        appendThinking(delta) {
            if (!thinkingPre) return;
            thinkingPre.textContent += delta;
            // Keep the newest reasoning line in view inside the block.
            thinkingPre.scrollTop = thinkingPre.scrollHeight;
            scheduleChatScroll();
        },

        closeThinking() {
            if (thinkingDetails) {
                // Collapse once finished; user can expand to review.
                thinkingDetails.open = false;
                const summary = thinkingDetails.querySelector('summary');
                if (summary) summary.textContent = 'Thought for a moment';
            }
            thinkingDetails = null;
            thinkingPre = null;
        },

        addToolCall(event) {
            showTrace();
            let argsText = event.arguments || '{}';
            try {
                argsText = JSON.stringify(JSON.parse(argsText), null, 2);
            } catch (e) {
                /* keep raw string if not valid JSON */
            }

            const details = document.createElement('details');
            details.className = 'trace-item trace-tool';
            details.open = false;

            const summary = document.createElement('summary');
            // Summary line: tool name plus a compact argument preview.
            let argPreview = '';
            try {
                const parsed = JSON.parse(event.arguments || '{}');
                if (parsed.query) argPreview = `: ${parsed.query}`;
                else if (event.name === 'manage_calendar')
                    // "manage_calendar: create" reads better than raw JSON.
                    argPreview = `: ${parsed.action || ''}`;
                else if (Object.keys(parsed).length) argPreview = `: ${event.arguments}`;
            } catch (e) {
                /* no preview */
            }
            summary.textContent = `${event.name || 'tool'}${argPreview}`;

            const body = document.createElement('div');
            body.className = 'trace-tool-body';

            const argsLabel = document.createElement('div');
            argsLabel.className = 'trace-label';
            argsLabel.textContent = 'Arguments';
            const argsPre = document.createElement('pre');
            argsPre.textContent = argsText;

            const resultLine = document.createElement('div');
            resultLine.className = 'trace-tool-result pending';
            resultLine.textContent = 'Running…';

            body.appendChild(argsLabel);
            body.appendChild(argsPre);
            body.appendChild(resultLine);
            details.appendChild(summary);
            details.appendChild(body);
            trace.appendChild(details);

            if (event.id) toolCallEls.set(event.id, { details, resultLine });
            scheduleChatScroll();
        },

        finishToolCall(event) {
            const entry = toolCallEls.get(event.id);
            if (!entry) return;
            if (event.name === 'manage_calendar') {
                // Calendar calls report a one-line outcome from the backend
                // instead of a result count.
                if (event.ok) {
                    entry.resultLine.textContent = event.summary || 'Done';
                    entry.resultLine.classList.remove('pending');
                    // The calendar changed: refresh the sidebar feed and, if
                    // that tab is open behind the chat, its list too.
                    loadUpcomingEvents();
                    const calTab = document.getElementById('calendar-tab');
                    if (calTab && calTab.classList.contains('active')) {
                        loadCalendarEvents();
                    }
                } else {
                    entry.resultLine.textContent = event.summary || 'Calendar action failed';
                    entry.resultLine.classList.add('failed');
                }
            } else if (event.ok) {
                const n = event.result_count ?? 0;
                entry.resultLine.textContent = n > 0
                    ? `Found ${n} result${n !== 1 ? 's' : ''}`
                    : 'No results found';
                entry.resultLine.classList.remove('pending');
            } else {
                entry.resultLine.textContent = 'Search failed';
                entry.resultLine.classList.add('failed');
            }
            scheduleChatScroll();
        },

        appendContent(delta) {
            removePlaceholder();
            contentText += delta;
            // Live markdown preview, throttled to one re-render per animation
            // frame so fast token streams don't thrash the DOM.
            scheduleRender();
            scheduleChatScroll();
        },

        finalize(finalContent) {
            if (settled) return;
            settled = true;
            removePlaceholder();
            this.closeThinking();
            contentText = finalContent;
            renderNow();
            messageDiv.classList.remove('streaming');
            scheduleChatScroll();
        },

        fail(message) {
            if (settled) return;
            settled = true;
            this.closeThinking();
            removePlaceholder();
            // Keep whatever partial answer streamed in, then note the failure.
            contentEl.innerHTML = renderMarkdown(contentText);
            if (contentText) {
                const errNote = document.createElement('p');
                errNote.className = 'stream-error-note';
                errNote.textContent = message;
                contentEl.appendChild(errNote);
            } else {
                contentEl.textContent = message;
            }
            contentEl._raw = contentText ? `${contentText}\n\n${message}` : message;
            messageDiv.classList.remove('streaming');
            scheduleChatScroll();
        },

        // User pressed Stop: keep the partial answer (if any) and mark it as
        // stopped. With no partial output the bubble is removed entirely so a
        // cancelled request leaves no empty assistant message behind.
        stop(note) {
            if (settled) return;
            settled = true;
            this.closeThinking();
            removePlaceholder();
            if (!contentText) {
                messageDiv.remove();
                return;
            }
            contentEl.innerHTML = renderMarkdown(contentText);
            const stopNote = document.createElement('p');
            stopNote.className = 'stream-stop-note';
            stopNote.textContent = note;
            contentEl.appendChild(stopNote);
            contentEl._raw = `${contentText}\n\n${note}`;
            messageDiv.classList.remove('streaming');
            scheduleChatScroll();
        }
    };
}

// Parse an SSE response body, invoking onEvent for each JSON data frame.
// Frames can split across network chunks, so buffer until the "\n\n" boundary.
async function readSseStream(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        
        buffer += decoder.decode(value, { stream: true });
        
        let sepIndex;
        while ((sepIndex = buffer.indexOf('\n\n')) !== -1) {
            const frame = buffer.slice(0, sepIndex);
            buffer = buffer.slice(sepIndex + 2);
            
            for (const line of frame.split('\n')) {
                if (!line.startsWith('data: ')) continue;
                try {
                    onEvent(JSON.parse(line.slice(6)));
                } catch (e) {
                    console.error('Failed to parse chat stream message:', line, e);
                }
            }
        }
    }
}

function getChatMessages() {
    const container = document.getElementById('chat-messages');
    const messages = [];
    
    container.querySelectorAll('.message').forEach(msgEl => {
        // Skip the in-progress streaming bubble: its thinking/tool trace and
        // partial answer must not leak into the conversation history.
        if (msgEl.classList.contains('streaming')) return;
        
        const role = msgEl.classList.contains('user') ? 'user' : 'assistant';
        const contentEl = msgEl.querySelector('.message-content');
        // Assistant bubbles hold rendered markdown HTML; _raw keeps the
        // original markdown so history sent back to the LLM stays faithful.
        const content = contentEl._raw != null ? contentEl._raw : contentEl.textContent;
        
        // Skip welcome message
        if (content.includes('Hello! I\'m HomeBrain')) return;
        
        messages.push({ role, content });
    });
    
    return messages;
}

function addMessageToChat(role, content) {
    const container = document.getElementById('chat-messages');
    
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${role}`;
    // Assistant answers are markdown; user messages stay plain escaped text.
    const contentHtml = role === 'assistant' ? renderMarkdown(content) : escapeHtml(content);
    messageDiv.innerHTML = `
        <div class="message-content${role === 'assistant' ? ' markdown-body' : ''}">${contentHtml}</div>
    `;
    messageDiv.querySelector('.message-content')._raw = content;

    container.appendChild(messageDiv);
    chatPinned = true;
    scrollToBottom();
}

function switchTab(tabName) {
    // On phones the tab was picked from the drawer — dismiss it so the
    // content is visible right away.
    closeDrawer();

    // Remember the active tab across page refreshes: the hash survives F5,
    // localStorage also covers in-app revisits where the URL never changed.
    if (history.replaceState) history.replaceState(null, '', `#${tabName}`);
    else location.hash = tabName;
    try { localStorage.setItem('activeTab', tabName); } catch (e) { /* private mode */ }

    // Update tab buttons
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tabName);
    });
    
    // Update tab content
    document.querySelectorAll('.tab-content').forEach(content => {
        content.classList.toggle('active', content.id === `${tabName}-tab`);
    });

    // Opening the chat re-runs the settings "model refresh" probe against the
    // saved configuration so a broken/unconfigured LLM is surfaced up front.
    if (tabName === 'chat') {
        checkChatModelStatus();
    }

    // The calendar tab loads its full event list when opened.
    if (tabName === 'calendar') {
        loadCalendarEvents();
    }
}

// Valid tab names, in markup order — also the fallback used by restoreActiveTab.
const TAB_NAMES = ['search', 'chat', 'devices', 'calendar'];

// Re-activate the tab from the URL hash (set by switchTab) or, if absent,
// the last one persisted to localStorage. Called once during startup.
function restoreActiveTab() {
    const hash = location.hash.replace('#', '');
    const stored = (() => { try { return localStorage.getItem('activeTab'); } catch (e) { return null; } })();
    const tab = TAB_NAMES.includes(hash) ? hash
              : TAB_NAMES.includes(stored) ? stored
              : 'search';
    if (tab !== 'search') switchTab(tab);
}

// Ask the backend to probe the saved LLM connection (the same model-list call
// the Settings "Fetch Models" button uses) and reflect the outcome in the chat:
//  - endpoint unreachable            -> warn + point to Settings, block sending
//  - selected model missing          -> warn + point to Settings, block sending
//  - reachable and model present      -> clear any warning, allow chatting
async function checkChatModelStatus() {
    const banner = document.getElementById('chat-model-warning');
    const textEl = document.getElementById('chat-model-warning-text');

    // Show a neutral "checking" state so the tab doesn't look broken while the
    // probe (up to MODEL_LIST_TIMEOUT) is in flight.
    chatModelAvailable = null;
    banner.style.display = 'flex';
    banner.classList.remove('is-error');
    textEl.textContent = 'Checking model availability...';

    let status;
    try {
        const response = await fetch('/api/settings/model-status');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        status = await response.json();
        // Keep the toolbar model switcher in sync with what the server offers
        // (and at least show the saved selection when the probe failed).
        populateChatModelSelect(status);
    } catch (error) {
        // The status probe itself failed — treat as unavailable but keep the
        // chat usable-looking rather than blocking on our own error.
        console.error('Model status check failed:', error);
        chatModelAvailable = false;
        banner.classList.add('is-error');
        textEl.textContent =
            'Could not verify the model connection. Open Settings to check your LLM configuration.';
        return;
    }

    if (!status.reachable) {
        chatModelAvailable = false;
        banner.classList.add('is-error');
        const detail = status.error
            ? ` (${escapeHtml(status.error)})`
            : '';
        textEl.innerHTML =
            `Cannot reach the configured model server at ${escapeHtml(status.llm_base_url)}.` +
            `<br>Open Settings to update your LLM connection details.${detail}`;
        return;
    }

    if (!status.model_configured) {
        chatModelAvailable = false;
        banner.classList.add('is-error');
        textEl.innerHTML =
            'No model is selected yet. Open Settings and pick one from the connected server.';
        return;
    }

    if (status.model_available === false) {
        chatModelAvailable = false;
        banner.classList.add('is-error');
        textEl.innerHTML =
            `The selected model "${escapeHtml(status.llm_model)}" is not available on the server at ${escapeHtml(status.llm_base_url)}.` +
            `<br>Open Settings to choose a different model.`;
        return;
    }

    // Reachable and the selected model is present: clear the warning.
    chatModelAvailable = true;
    banner.style.display = 'none';
}

// Fill the chat toolbar's model switcher from a /model-status result. The
// probe already returns the server's model list, so no extra request is
// needed; when the server was unreachable only the saved model is shown
// (marked) so the current choice stays visible.
function populateChatModelSelect(status) {
    const select = document.getElementById('chat-model-select');
    if (!select) return;

    const current = (status.llm_model || '').trim();
    const models = status.available_models || [];

    select.innerHTML = '';
    for (const modelId of models) {
        const option = document.createElement('option');
        option.value = modelId;
        option.textContent = modelId;
        select.appendChild(option);
    }

    // Keep the saved selection visible even if the server no longer lists it.
    if (current && !models.some((m) => m.toLowerCase() === current.toLowerCase())) {
        const staleOption = document.createElement('option');
        staleOption.value = current;
        staleOption.textContent = `${current} (not on this server)`;
        select.appendChild(staleOption);
    }

    if (!select.options.length) {
        const emptyOption = document.createElement('option');
        emptyOption.value = '';
        emptyOption.textContent = 'No model selected';
        select.appendChild(emptyOption);
    }

    // Select the saved model case-insensitively (the list may differ in case).
    for (const option of select.options) {
        if (option.value.toLowerCase() === current.toLowerCase()) {
            select.value = option.value;
            break;
        }
    }
}

// Persist a model picked in the chat toolbar. Blank values are the empty
// placeholder and never saved; after saving, re-probe so the warning banner
// (and this dropdown) reflect the new selection.
async function handleChatModelChange() {
    const select = document.getElementById('chat-model-select');
    const model = select.value.trim();
    if (!model) return;

    select.disabled = true;
    try {
        const response = await fetch('/api/settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ llm_model: model }),
        });
        if (!response.ok) {
            let detail = `HTTP ${response.status}`;
            try {
                const data = await response.json();
                if (data.detail) detail = data.detail;
            } catch (e) { /* non-JSON error body */ }
            throw new Error(detail);
        }
        showToast(`Model switched to ${model}`);
        checkChatModelStatus();
    } catch (error) {
        console.error('Failed to switch model:', error);
        showToast('Failed to switch model', 'error', error.message);
        // Restore the dropdown to what the server actually has saved.
        checkChatModelStatus();
    } finally {
        select.disabled = false;
    }
}

function showToast(message, type = 'success', detailedError = null) {
    const container = document.getElementById('toast-container');
    
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    
    // Create message content
    const messageDiv = document.createElement('div');
    messageDiv.className = 'toast-message';
    messageDiv.textContent = message;
    toast.appendChild(messageDiv);
    
    // Add expandable error details if available
    if (type === 'error' && detailedError) {
        const toggleBtn = document.createElement('button');
        toggleBtn.className = 'toast-toggle';
        toggleBtn.innerHTML = '▼';
        toggleBtn.title = 'View details';
        
        const detailsDiv = document.createElement('div');
        detailsDiv.className = 'toast-details';
        detailsDiv.style.display = 'none';
        detailsDiv.textContent = detailedError;
        
        toggleBtn.addEventListener('click', () => {
            const isVisible = detailsDiv.style.display === 'block';
            detailsDiv.style.display = isVisible ? 'none' : 'block';
            toggleBtn.innerHTML = isVisible ? '▼' : '▲';
        });
        
        toast.appendChild(toggleBtn);
        toast.appendChild(detailsDiv);
    }
    
    container.appendChild(toast);
    
    // Auto-remove after 5 seconds for errors with details, 3 seconds otherwise
    const timeout = (type === 'error' && detailedError) ? 5000 : 3000;
    setTimeout(() => {
        toast.remove();
    }, timeout);
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Sidebar: docked rail (desktop) / overlay drawer (mobile)
// ---------------------------------------------------------------------------

// Two independent body classes drive both modes:
//   .sidebar-collapsed — desktop icon-rail width (persisted, CSS-only effect)
//   .sidebar-open      — mobile drawer visibility (CSS media query decides)
function openDrawer() {
    document.body.classList.add('sidebar-open');
}

function closeDrawer() {
    document.body.classList.remove('sidebar-open');
}

// The docked sidebar defaults to expanded; the collapsed/expanded choice
// persists across visits.
function applySidebarState() {
    const collapsed = localStorage.getItem('sidebarCollapsed') === '1';
    document.body.classList.toggle('sidebar-collapsed', collapsed);
    updateSidebarToggle();
}

function toggleSidebar() {
    const collapsed = document.body.classList.toggle('sidebar-collapsed');
    localStorage.setItem('sidebarCollapsed', collapsed ? '1' : '0');
    updateSidebarToggle();
}

function updateSidebarToggle() {
    const collapsed = document.body.classList.contains('sidebar-collapsed');
    const btn = document.getElementById('sidebar-toggle-btn');
    // Chevron points in the direction the toggle action takes the sidebar.
    // Chevron Left (collapse) / Chevron Right (expand), Feather-style icons.
    const points = collapsed ? '9 18 15 12 9 6' : '15 18 9 12 15 6';
    btn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="${points}"></polyline></svg>`;
    btn.title = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
}

// ---------------------------------------------------------------------------
// Theme (Auto / Light / Dark)
// ---------------------------------------------------------------------------

// The preference is a server-side setting ('THEME', edited under
// Settings > General): 'auto' follows prefers-color-scheme; 'light'/'dark'
// pin it via <html data-theme>. It is mirrored to localStorage so the inline
// snippet in index.html can apply it before first paint (no flash).
const THEME_ORDER = ['auto', 'light', 'dark'];
function currentThemePref() {
    try {
        const pref = localStorage.getItem('theme');
        return THEME_ORDER.includes(pref) ? pref : 'auto';
    } catch (e) {
        return 'auto';
    }
}

// Apply a theme preference everywhere it matters: the live document, the
// localStorage mirror used before first paint, and the radio buttons if the
// Settings modal happens to be open.
function setThemePref(pref) {
    if (!THEME_ORDER.includes(pref)) pref = 'auto';
    try { localStorage.setItem('theme', pref); } catch (e) { /* private mode */ }
    applyTheme();
}

function applyTheme() {
    const pref = currentThemePref();
    if (pref === 'auto') {
        document.documentElement.removeAttribute('data-theme');
    } else {
        document.documentElement.setAttribute('data-theme', pref);
    }
}

// ---------------------------------------------------------------------------
// Settings modal (sections: General / AI)
// ---------------------------------------------------------------------------

// Shown inside the API key field when a key is already configured. The real
// value is never sent to the browser — an obscured placeholder just signals
// "a key exists"; leaving the field blank keeps it, typing replaces it.
const API_KEY_PLACEHOLDER = '********';

// Built-in prompt defaults fetched from the API, used by the "Restore
// Default" buttons in Advanced Settings. Keyed by setting name.
let promptDefaults = {};

const PROMPT_FIELD_IDS = {
    chat_system_prompt: 'settings-chat-system-prompt',
    search_tool_description: 'settings-search-tool-description',
    calendar_tool_description: 'settings-calendar-tool-description',
};

function restorePromptDefault(settingName) {
    const fieldId = PROMPT_FIELD_IDS[settingName];
    if (!fieldId) return;
    document.getElementById(fieldId).value = promptDefaults[settingName] || '';
}

// Show one settings panel at a time ("general" | "ai") and highlight the
// matching sidebar button. The form itself is shared, so Save submits both.
function showSettingsSection(name) {
    document.querySelectorAll('.settings-nav-btn').forEach((btn) => {
        btn.classList.toggle('active', btn.dataset.settingsSection === name);
    });
    for (const panel of document.querySelectorAll('.settings-panel')) {
        panel.hidden = panel.id !== `settings-panel-${name}`;
    }
}

async function openSettingsModal() {
    let settings;
    try {
        const response = await fetch('/api/settings');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        settings = await response.json();
    } catch (error) {
        console.error('Failed to load settings:', error);
        showToast('Failed to load settings', 'error');
        return;
    }

    // General: theme radios (server value wins; localStorage mirror covers
    // the brief window before this fetch completes).
    const themePref = THEME_ORDER.includes(settings.theme) ? settings.theme : 'auto';
    setThemePref(themePref);

    document.getElementById('settings-llm-base-url').value = settings.llm_base_url || '';

    const keyInput = document.getElementById('settings-llm-api-key');
    keyInput.value = '';
    if (settings.llm_api_key_set) {
        keyInput.placeholder = API_KEY_PLACEHOLDER;
        document.getElementById('settings-api-key-hint').textContent =
            'An API key is configured. Leave blank to keep it, or type a new one to replace it.';
    } else {
        keyInput.placeholder = '';
        // No hint when nothing is configured — the empty field says it all.
        document.getElementById('settings-api-key-hint').textContent = '';
    }

    // The model field is a plain <select>: seed it with the saved value so it
    // displays before any refresh; clicking the dropdown loads fresh options.
    const modelSelect = document.getElementById('settings-llm-model');
    modelSelect.innerHTML = '';
    const savedModelOption = document.createElement('option');
    savedModelOption.value = settings.llm_model || '';
    savedModelOption.textContent = settings.llm_model || 'No model selected';
    modelSelect.appendChild(savedModelOption);

    document.getElementById('model-list-hint').textContent =
        'Click the dropdown to load models from the server.';

    // Advanced settings: current prompts + built-in defaults (for Restore).
    promptDefaults = {
        chat_system_prompt: settings.chat_system_prompt_default || '',
        search_tool_description: settings.search_tool_description_default || '',
        calendar_tool_description: settings.calendar_tool_description_default || '',
    };
    document.getElementById('settings-chat-system-prompt').value =
        settings.chat_system_prompt || '';
    document.getElementById('settings-search-tool-description').value =
        settings.search_tool_description || '';
    document.getElementById('settings-calendar-tool-description').value =
        settings.calendar_tool_description || '';
    // Notifications: enable toggle, cadence, lead time and webhook fields.
    document.getElementById('settings-notify-enabled').checked = !!settings.notify_enabled;
    document.getElementById('settings-notify-interval').value =
        settings.notify_check_interval_minutes ?? 15;
    document.getElementById('settings-notify-lead-value').value =
        settings.notify_lead_value ?? 24;
    document.getElementById('settings-notify-lead-unit').value =
        settings.notify_lead_unit === 'days' ? 'days' : 'hours';
    document.getElementById('settings-notify-webhook-enabled').checked =
        !!settings.notify_webhook_enabled;
    document.getElementById('settings-notify-webhook-type').value =
        settings.notify_webhook_type === 'synology' ? 'synology' : 'generic';
    updateWebhookTypeHints();
    document.getElementById('settings-notify-webhook-url').value =
        settings.notify_webhook_url || '';
    // SSL validation defaults to on; only an explicit false unchecks it.
    document.getElementById('settings-notify-webhook-verify-ssl').checked =
        settings.notify_webhook_verify_ssl !== false;

    // Webhook bearer token: same placeholder treatment as the LLM API key —
    // the value is never sent to the client, blank on save keeps it.
    const tokenInput = document.getElementById('settings-notify-webhook-token');
    tokenInput.value = '';
    if (settings.notify_webhook_token_set) {
        tokenInput.placeholder = API_KEY_PLACEHOLDER;
        document.getElementById('settings-webhook-token-hint').textContent =
            'A token is configured. Leave blank to keep it, or type a new one to replace it.';
    } else {
        tokenInput.placeholder = '';
        document.getElementById('settings-webhook-token-hint').textContent = '';
    }
    document.getElementById('webhook-test-hint').textContent = '';

    // Always open the modal with Advanced collapsed, on the General section.
    document.getElementById('advanced-settings-section').open = false;
    showSettingsSection('general');

    document.getElementById('settings-modal').style.display = 'flex';
}

function closeSettingsModal() {
    document.getElementById('settings-modal').style.display = 'none';
}

// Per-type help text for the webhook fields. Synology Chat incoming webhooks
// take their token in the URL and want form-encoded payload={text} bodies,
// so the generic bearer-token field is hidden for them.
function updateWebhookTypeHints() {
    const type = document.getElementById('settings-notify-webhook-type').value;
    const urlHint = document.getElementById('webhook-url-hint');
    const tokenField = document.getElementById(
        'settings-notify-webhook-token').closest('.settings-field');
    if (type === 'synology') {
        urlHint.textContent =
            'Paste the full Incoming Webhook URL from DSM > Chat > Integration, '
            + 'including its token= parameter. HomeBrain sends Chat\u2019s '
            + 'payload={"text": ...} format.';
        tokenField.style.display = 'none';
    } else {
        urlHint.textContent =
            'HomeBrain POSTs a JSON body (event title, due date, device\u2026) to '
            + 'this URL. Works with custom receivers, Node-RED, Home Assistant '
            + 'webhooks and similar.';
        tokenField.style.display = '';
    }
}

// Fill the LLM Model <select> from a model id list. The currently selected
// value is preserved; if the server no longer offers it, it stays visible
// (marked) so saving does not silently drop the configured model.
function populateModelSelect(modelIds) {
    const select = document.getElementById('settings-llm-model');
    const current = select.value;

    select.innerHTML = '';
    for (const modelId of modelIds) {
        const option = document.createElement('option');
        option.value = modelId;
        option.textContent = modelId;
        select.appendChild(option);
    }

    if (current && !modelIds.includes(current)) {
        const staleOption = document.createElement('option');
        staleOption.value = current;
        staleOption.textContent = `${current} (not on this server)`;
        select.appendChild(staleOption);
    }

    if (!select.options.length) {
        const emptyOption = document.createElement('option');
        emptyOption.value = '';
        emptyOption.textContent = 'No models available';
        select.appendChild(emptyOption);
    }

    if (current) {
        select.value = current;
    }
}

// Parse an <input type=number> into an integer clamped to [min, max]; a
// blank/invalid value falls back to `fallback` so saving never sends NaN.
function clampInt(raw, min, max, fallback) {
    const n = parseInt(String(raw).trim(), 10);
    if (Number.isNaN(n)) return fallback;
    return Math.min(max, Math.max(min, n));
}

// Guards against overlapping refreshes when the dropdown is clicked rapidly.
let modelRefreshInFlight = false;

// Refresh the LLM Model <select> from the server's /models endpoint, using
// whatever base URL / API key are currently in the form (saved or not).
// When openPicker is true, pop the native list open right after loading so a
// click on the dropdown feels like a normal (but always fresh) select.
async function fetchLlmModels({ openPicker = false } = {}) {
    if (modelRefreshInFlight) return;

    const hint = document.getElementById('model-list-hint');
    const btn = document.getElementById('fetch-models-btn');
    const select = document.getElementById('settings-llm-model');

    const baseUrl = document.getElementById('settings-llm-base-url').value.trim();
    if (!baseUrl) {
        hint.textContent = 'Enter the LLM Base URL first, then pick a model.';
        return;
    }

    const keyInput = document.getElementById('settings-llm-api-key');
    const payload = { llm_base_url: baseUrl };
    // Only send a freshly typed key; if blank, the server probes with the saved one.
    if (keyInput.value.trim()) {
        payload.llm_api_key = keyInput.value.trim();
    }

    modelRefreshInFlight = true;
    btn.disabled = true;
    hint.textContent = 'Loading models...';
    try {
        const response = await fetch('/api/settings/models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!response.ok) {
            let detail = `HTTP ${response.status}`;
            try {
                const data = await response.json();
                if (data.detail) detail = data.detail;
            } catch (e) { /* non-JSON error body */ }
            throw new Error(detail);
        }
        const data = await response.json();
        populateModelSelect(data.models);

        if (openPicker && data.models.length) {
            try {
                // showPicker needs a fresh user gesture; if the request took
                // too long, just leave the loaded list for manual opening.
                select.showPicker();
            } catch (e) { /* activation expired — list is still populated */ }
        }

        hint.textContent = data.models.length
            ? `Loaded ${data.models.length} model${data.models.length === 1 ? '' : 's'} from the server.`
            : 'Server responded, but no models were listed.';
    } catch (error) {
        hint.textContent = error.message;
    } finally {
        modelRefreshInFlight = false;
        btn.disabled = false;
    }
}

// Guards against overlapping test sends from the Notifications panel.
let webhookTestInFlight = false;

// POST a sample payload to /api/notifications/test using whatever URL/token
// are currently in the form (saved or freshly typed), mirroring fetchLlmModels.
async function sendTestNotification() {
    if (webhookTestInFlight) return;

    const hint = document.getElementById('webhook-test-hint');
    const btn = document.getElementById('test-webhook-btn');

    const url = document.getElementById('settings-notify-webhook-url').value.trim();
    if (!url) {
        hint.textContent = 'Enter the Webhook URL first, then send a test.';
        return;
    }

    const payload = { webhook_url: url };
    // Only send a freshly typed token; if blank, the server uses the saved one.
    const token = document.getElementById('settings-notify-webhook-token').value.trim();
    if (token) payload.webhook_token = token;
    // Send the checkbox state so an unsaved change is testable immediately.
    payload.verify_ssl = document.getElementById('settings-notify-webhook-verify-ssl').checked;
    // Same for the webhook type dropdown (generic JSON vs Synology Chat).
    payload.webhook_type = document.getElementById('settings-notify-webhook-type').value;

    webhookTestInFlight = true;
    btn.disabled = true;
    hint.textContent = 'Sending test notification...';
    try {
        const response = await fetch('/api/notifications/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!response.ok) {
            let detail = `HTTP ${response.status}`;
            try {
                const data = await response.json();
                if (data.detail) detail = data.detail;
            } catch (e) { /* non-JSON error body */ }
            throw new Error(detail);
        }
        hint.textContent = 'Test notification sent — check your webhook receiver.';
    } catch (error) {
        hint.textContent = error.message;
    } finally {
        webhookTestInFlight = false;
        btn.disabled = false;
    }
}

// Clicking (or keyboard-opening) the model dropdown refreshes its options
// from the server first, then opens the list — so users always see fresh
// models without a separate "fetch" step.
function setupModelDropdownRefresh() {
    const select = document.getElementById('settings-llm-model');

    // mousedown fires before the native picker opens: cancel that open and
    // re-open it ourselves once the fresh list has loaded.
    select.addEventListener('mousedown', (e) => {
        e.preventDefault();
        select.focus();
        fetchLlmModels({ openPicker: true });
    });

    // Keyboard access: Enter / Space / ArrowDown open the picker natively —
    // intercept them the same way.
    select.addEventListener('keydown', (e) => {
        if (!['Enter', ' ', 'ArrowDown'].includes(e.key)) return;
        e.preventDefault();
        fetchLlmModels({ openPicker: true });
    });
}

async function handleSettingsSave(event) {
    event.preventDefault();

    const themeInput = document.querySelector('#settings-form input[name="theme"]:checked');
    const payload = {
        theme: themeInput ? themeInput.value : 'auto',
        llm_base_url: document.getElementById('settings-llm-base-url').value.trim(),
        llm_model: document.getElementById('settings-llm-model').value.trim(),
        // Prompts are always sent; the server treats a blank value as
        // "restore the built-in default".
        chat_system_prompt: document.getElementById('settings-chat-system-prompt').value.trim(),
        search_tool_description: document.getElementById('settings-search-tool-description').value.trim(),
        calendar_tool_description: document.getElementById('settings-calendar-tool-description').value.trim(),
        notify_enabled: document.getElementById('settings-notify-enabled').checked,
        notify_check_interval_minutes:
            clampInt(document.getElementById('settings-notify-interval').value, 1, 1440, 15),
        notify_lead_value:
            clampInt(document.getElementById('settings-notify-lead-value').value, 1, 365, 24),
        notify_lead_unit: document.getElementById('settings-notify-lead-unit').value,
        notify_webhook_enabled: document.getElementById('settings-notify-webhook-enabled').checked,
        notify_webhook_type: document.getElementById('settings-notify-webhook-type').value,
        // Blank URL intentionally clears it (server treats blank as "remove"),
        // matching how the field round-trips its real value from GET.
        notify_webhook_url: document.getElementById('settings-notify-webhook-url').value.trim(),
        notify_webhook_verify_ssl:
            document.getElementById('settings-notify-webhook-verify-ssl').checked,
    };
    // Blank API key field means "keep the existing key" — omit it entirely.
    // (The obscured placeholder is only a visual hint, never a value.)
    const apiKey = document.getElementById('settings-llm-api-key').value.trim();
    if (apiKey) payload.llm_api_key = apiKey;

    // Same keep-existing semantics for the webhook bearer token.
    const webhookToken = document.getElementById('settings-notify-webhook-token').value.trim();
    if (webhookToken) payload.notify_webhook_token = webhookToken;

    const saveBtn = document.getElementById('settings-save-btn');
    saveBtn.disabled = true;
    try {
        const response = await fetch('/api/settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!response.ok) {
            let detail = `HTTP ${response.status}`;
            try {
                const data = await response.json();
                if (data.detail) detail = data.detail;
            } catch (e) { /* non-JSON error body */ }
            throw new Error(detail);
        }
        // Apply the (possibly changed) theme immediately — before closing,
        // so the new colors are already visible when the modal disappears.
        setThemePref(payload.theme);
        closeSettingsModal();
        showToast('Settings saved');
        // If the chat tab is open behind the modal, re-probe so a fixed
        // configuration clears its warning banner right away.
        if (document.getElementById('chat-tab').classList.contains('active')) {
            checkChatModelStatus();
        }
    } catch (error) {
        console.error('Failed to save settings:', error);
        showToast('Failed to save settings', 'error', error.message);
    } finally {
        saveBtn.disabled = false;
    }
}
