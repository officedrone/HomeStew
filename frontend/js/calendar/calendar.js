// HomeStew - Calendar realm: device maintenance reminders.


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
        const params = new URLSearchParams();
        if (currentCalendarDeviceFilter) params.set('device_id', currentCalendarDeviceFilter);
        // 'all' means no horizon: the API returns every event, past or future.
        if (currentCalendarRange !== 'all') params.set('within_days', currentCalendarRange);
        const qs = params.toString() ? `?${params}` : '';
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
        // Distinguish "nothing exists" from "nothing in this window", which
        // would otherwise look like an empty calendar.
        const rangeLabel = document.getElementById('calendar-range-filter')
            .selectedOptions[0]?.textContent ?? '';
        container.innerHTML = currentCalendarRange !== 'all'
            ? `
            <div class="empty-state">
                <p>Nothing due in the next ${escapeHtml(rangeLabel.replace(/^Next\s/i, '').toLowerCase())}.</p>
                <p>Switch the range filter to "All Events" to see everything.</p>
            </div>`
            : `
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
        loadDashboardStats();
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
        loadDashboardStats();
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
        loadDashboardStats();
    } catch (error) {
        showToast(`Failed to save event: ${error.message}`, 'error');
    } finally {
        endOp('save-event');
    }
}

// ---------------------------------------------------------------------------
// Calendar wiring (was part of setupEventListeners in the single-file app).
// ---------------------------------------------------------------------------

function wireCalendarEvents() {
    // Calendar: tab filter, new-event button and the event form.
    document.getElementById('calendar-device-filter').addEventListener('change', (e) => {
        currentCalendarDeviceFilter = e.target.value || null;
        loadCalendarEvents();
    });
    // Range filter: 'all' loads every event, otherwise only those due within
    // the horizon. Overdue events always stay visible so nothing is hidden.
    document.getElementById('calendar-range-filter').addEventListener('change', (e) => {
        currentCalendarRange = e.target.value || 'all';
        loadCalendarEvents();
    });
    document.getElementById('add-event-btn').addEventListener('click', () => openEventModal());
    document.getElementById('event-form').addEventListener('submit', handleSaveEvent);
    // "Every N" only makes sense for a repeating event.
    document.getElementById('event-recurrence').addEventListener('change', (e) => {
        document.getElementById('event-interval-field').style.display =
            e.target.value === 'none' ? 'none' : '';
    });
}
