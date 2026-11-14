// HomeStew - Unified notification consumption: polls the server's event feed
// and renders each active item as a persistent toast with "Go To" and
// "Dismiss" buttons, plus the Settings > Notifications events table.
//
// Server side (services/ui_notifications.py + services/dispatch.py) owns the
// truth: which events exist, their channel subscriptions, intervals, and
// dismissal state. Items arrive with a per-occurrence ``dedup_key`` - cards
// reconcile by THAT, not by the event key, because several occurrences of one
// event (e.g. two different due calendar reminders) share the same key but
// must render as separate cards. Dismiss silences THAT occurrence permanently
// (survives reloads/restarts: check episodes via server state, calendar
// occurrences via a 'ui' ledger row), while the ✕ only hides the card for
// this browser session - it comes back after a reload if the condition still
// holds and was never Dismissed.
//
// Adding a new event needs no change here: the server returns items with
// message/goto/action fields and this file renders whatever arrives; the
// Settings table builds itself from notify_subscriptions + notification_channels.

const UI_NOTIFY_POLL_MS = 60000;
let uiNotifyTimer = null;

// Latest per-check state ({condition_true, dismissed, last_checked_at}) from
// the poller, so Settings can paint each row's status label and keep it fresh
// without a dedicated request. Only condition checks have status rows - rule
// events (calendar reminders) show a dash in the Status column.
let _uiNotifyStatuses = {};

// Channel registry ([{name, label}]) from the last GET /api/settings, so the
// Test button can name channels in its delivery report. Set by
// renderUINotificationSettings(); empty until Settings has been opened once.
let _notifyChannelDefs = [];

// Dedup keys closed with ✕ during this browser session (sessionStorage
// persists across reloads in the same tab but is per-tab and dies when it
// closes). Keyed by dedup_key: hiding one calendar occurrence never hides
// the next one.
const UI_NOTIFY_CLOSED_KEY = 'uiNotifyClosed';
const _uiNotifyClosed = new Set(
    (() => {
        try {
            const raw = JSON.parse(sessionStorage.getItem(UI_NOTIFY_CLOSED_KEY) || '[]');
            return Array.isArray(raw) ? raw.filter((k) => typeof k === 'string') : [];
        } catch (e) {
            return [];
        }
    })()
);

function rememberClosedUINotification(dedupKey) {
    _uiNotifyClosed.add(dedupKey);
    try {
        sessionStorage.setItem(UI_NOTIFY_CLOSED_KEY, JSON.stringify([..._uiNotifyClosed]));
    } catch (e) { /* private mode: session suppression just won't survive reload */ }
}

// Start the poll loop. Same non-overlapping recursive setTimeout pattern as
// monitorIndexJob(): one request in flight at a time; transient failures
// retry without killing the monitor. Called once from initializeApp().
function startUINotificationMonitor() {
    if (uiNotifyTimer) return;

    const tick = async () => {
        uiNotifyTimer = null;
        try {
            await pollUINotifications();
        } catch (e) { /* offline or transient error - just retry on the next beat */ }
        uiNotifyTimer = setTimeout(tick, UI_NOTIFY_POLL_MS);
    };
    tick();
}

// Fetch the active notifications and reconcile them with the DOM: render new
// ones, refresh existing ones in place, and remove cards whose occurrence has
// resolved server-side (episode closed, event completed, or dismissed in
// another tab) - so fixing the problem makes the toast vanish without any
// user action.
async function pollUINotifications() {
    const response = await fetch('/api/notifications/ui');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    applyNotificationPayload(data);
}

// Reconcile the toast cards and the Settings status labels against a payload
// of the shape {items, statuses}. Shared by the poller and "Check now" so a
// forced re-evaluation repaints identically without a second request.
function applyNotificationPayload(data) {
    const items = Array.isArray(data.items) ? data.items : [];
    if (data.statuses && typeof data.statuses === 'object') {
        _uiNotifyStatuses = data.statuses;
    }
    // Reconcile by dedup_key: all calendar cards share key=calendar_reminders,
    // so keying by item.key would collapse them into a single card.
    const activeKeys = new Set(items.map((item) => item.dedup_key));

    // Drop cards the server no longer reports (resolved, or dismissed in
    // another tab). Cards hidden with ✕ are already out of the DOM, and Test
    // preview cards carry no data-notification-dedup so they are never touched.
    document.querySelectorAll('[data-notification-dedup]').forEach((el) => {
        if (!activeKeys.has(el.dataset.notificationDedup)) el.remove();
    });

    for (const item of items) {
        if (_uiNotifyClosed.has(item.dedup_key)) continue;
        const existing = document.querySelector(
            `[data-notification-dedup="${CSS.escape(item.dedup_key)}"]`
        );
        if (existing) {
            // Keep the text current (e.g. the un-indexed count changed).
            const msgEl = existing.querySelector('.toast-message');
            if (msgEl) msgEl.textContent = item.message;
            continue;
        }
        showUINotificationCard(item);
    }
    updateUINotificationStatusLabels();
}

// Render one active notification as a persistent card through the unified
// toast service. notifyEvent() maps severity -> toast style, makes it
// persistent, and uses the dedup key as the toast id - so a recurring
// calendar event's next occurrence gets its own card instead of updating a
// dismissed one. data-notification-dedup lets applyNotificationPayload()
// reconcile against the DOM. "Go To" navigates AND closes the card like the
// ✕ (hidden for this browser session only) - acting on the problem shouldn't
// leave the toast hanging, but it is NOT a server-side Dismiss: after a
// reload the card returns if the condition still holds and was never
// Dismissed.
function showUINotificationCard(item) {
    const toast = notifyEvent(item, {
        onGoTo: (it, el) => {
            gotoNotificationTarget(it);
            rememberClosedUINotification(it.dedup_key);
            if (el) el.remove();
        },
        onDismiss: (it, el) => dismissUINotification(it.dedup_key, el),
        onClose: (it) => rememberClosedUINotification(it.dedup_key),
    });
    if (toast) toast.dataset.notificationDedup = item.dedup_key;
    return toast;
}

// Deep link for the Go To button. Settings targets reuse the dashboard's
// openSettingsSection() (pendingSettingsSection survives loadSettingsPage());
// any other tab just switches, so future checks can point anywhere in the app.
function gotoNotificationTarget(item) {
    if (item.goto_tab === 'settings' && item.goto_section) {
        openSettingsSection(item.goto_section);
    } else if (item.goto_tab) {
        switchTab(item.goto_tab);
    }
}

// Server-side dismissal: silences this occurrence forever. Accepts whatever
// the item's dedup_key carries - a plain check key, `check:{key}`, or a
// calendar occurrence key (`calendar:{id}:{due}:{kind}`). The POST is
// op-locked so a double click can't fire twice; the card is removed on any
// outcome - even a 404-ish race means another tab already ended the episode.
async function dismissUINotification(dedupKey, toastEl) {
    if (!beginOp(`ui-notify-dismiss-${dedupKey}`)) return;
    try {
        await fetch(`/api/notifications/ui/${encodeURIComponent(dedupKey)}/dismiss`, {
            method: 'POST',
        });
    } catch (e) { /* server unreachable: still hide locally, next poll reconciles */ }
    finally {
        endOp(`ui-notify-dismiss-${dedupKey}`);
    }
    const el = toastEl
        || document.querySelector(`[data-notification-dedup="${CSS.escape(dedupKey)}"]`);
    if (el) el.remove();
}

// Settings > Notifications "Events": one TABLE row per subscribable EVENT,
// built from the server payload (GET /api/settings -> notify_subscriptions +
// notification_channels), so adding an event or a delivery channel
// server-side needs no HTML or JS change. Called from loadSettingsPage().
// Columns: Event | Status | Interval (min) | Notify before due (hrs) |
// Notify via (one checkbox per channel) | Check Now | Test. There is NO
// Enable column: ticking any "Notify via" checkbox enables the event (the
// server derives enabled = channels non-empty), and unticking all of them
// switches it off. Both kinds carry an editable scan interval; only rule
// events (kind "rule", e.g. calendar reminders) have a due date, so their
// lead window is editable while condition checks show a dash. Rule rows
// have no persisted status either (the notifier scan drives them), so the
// Status cell shows a dash. The row keeps data-ui-notify-key +
// data-ui-notify-kind; collectUINotificationSettings() finds the inputs by
// their .ui-notify-interval-input / .ui-notify-lead-input classes.
function renderUINotificationSettings(settings) {
    const host = document.getElementById('ui-notification-rows');
    if (!host) return;
    const events = settings.notify_subscriptions || {};
    const channels = Array.isArray(settings.notification_channels)
        ? settings.notification_channels : [];
    _notifyChannelDefs = channels;
    const keys = Object.keys(events);
    if (!keys.length) {
        host.innerHTML = '<p class="settings-hint">No notification events are registered.</p>';
        return;
    }
    const rows = keys.map((key) => {
        const ev = events[key];
        const safeKey = escapeHtml(key);
        const safeLabel = escapeHtml(ev.label || key);
        const isRule = ev.kind === 'rule';
        const subscribed = new Set(ev.channels || []);
        const channelCells = channels.map((ch) => {
            const safeCh = escapeHtml(ch.name);
            return `
                <label class="checkbox-option notify-channel-option">
                    <input type="checkbox" data-notify-channel="${safeCh}"
                        ${subscribed.has(ch.name) ? 'checked' : ''}>
                    ${escapeHtml(ch.label || ch.name)}
                </label>`;
        }).join('');
        const intervalCell = `<input type="number" class="ui-notify-interval-input"
                min="5" max="10080" step="5"
                value="${Number(ev.interval_minutes) || 1440}"
                aria-label="Check interval in minutes for ${safeLabel}">`;
        // Lead window only makes sense for events with a due date; condition
        // checks (manuals etc.) have none, so the cell reads "Not Applicable"
        // instead of an input. Rule rows likewise have no persisted check
        // status (the notifier scan drives them), so their Status cell does too.
        // Two stacked lines ("Not" / "Applicable") so the placeholder never
        // forces its column wide - same two-line shape as the status cells.
        const naCell = '<span class="ui-notify-na">Not<br>Applicable</span>';
        const leadCell = isRule
            ? `<input type="number" class="ui-notify-lead-input"
                    min="0" max="8760" step="1"
                    value="${Number(ev.notify_before_due_hours) || 0}"
                    aria-label="Notify this many hours before due for ${safeLabel}">`
            : naCell;
        const statusCell = isRule
            ? naCell
            : `<span class="ui-notify-status" data-ui-notify-status="${safeKey}"></span>`;
        return `
            <tr data-ui-notify-key="${safeKey}" data-ui-notify-kind="${escapeHtml(ev.kind || 'check')}">
                <td class="col-name">${safeLabel}</td>
                <td class="col-status">${statusCell}</td>
                <td class="col-interval">${intervalCell}</td>
                <td class="col-lead">${leadCell}</td>
                <td class="col-channels">${channelCells}</td>
                <td class="col-check">
                    <button type="button" class="card-icon-btn ui-notify-check-btn"
                        title="Check now (ignores the interval)"
                        aria-label="Check ${safeLabel} now"
                        onclick="checkNowUINotification('${safeKey}', this)">${CARD_ACTION_ICONS.refresh}</button>
                </td>
                <td class="col-test">
                    <button type="button" class="btn btn-secondary btn-small"
                        title="Preview this event and send a test through its subscribed channels"
                        onclick="testUINotification('${safeKey}', this)">Test</button>
                </td>
            </tr>`;
    }).join('');
    host.innerHTML = `
        <div class="ui-notify-wrap">
            <table class="ui-notify-table">
                <thead>
                    <tr>
                        <th class="col-name">Event</th>
                        <th class="col-status">Status</th>
                        <th class="col-interval">Interval (min)</th>
                        <th class="col-lead">Notify before due (hrs)</th>
                        <th class="col-channels">Notify via</th>
                        <th class="col-check">Check Now</th>
                        <th class="col-test">Test</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        </div>`;
    updateUINotificationStatusLabels();
}

// "5m ago" style text for a stored ISO timestamp; blank when never checked.
function uiNotifyTimeAgo(iso) {
    const then = Date.parse(iso);
    if (!Number.isFinite(then)) return '';
    const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    return `${Math.round(hours / 24)}d ago`;
}

// Status badge markup for one check, from the latest poll's statuses.
function uiNotifyStatusLabel(status) {
    if (!status || !status.last_checked_at) {
        return '<span class="ui-notify-badge status-unknown">Not checked yet</span>';
    }
    const when = escapeHtml(uiNotifyTimeAgo(status.last_checked_at));
    // Two lines: badge on top, bare "Xh ago" timestamp under it (CSS stacks
    // them; the word "checked" is dropped to keep the second line short).
    if (status.condition_true) {
        if (status.dismissed) {
            return `<span class="ui-notify-badge status-dismissed">Dismissed</span>`
                + `<span class="settings-hint">${when}</span>`;
        }
        return `<span class="ui-notify-badge status-problem">Problem detected</span>`
            + `<span class="settings-hint">${when}</span>`;
    }
    return `<span class="ui-notify-badge status-ok">OK</span>`
        + `<span class="settings-hint">${when}</span>`;
}

// Repaint every rendered status badge from the cached statuses. Called after
// each poll and after the rows render, so Settings shows live state while open.
function updateUINotificationStatusLabels() {
    document.querySelectorAll('[data-ui-notify-status]').forEach((el) => {
        el.innerHTML = uiNotifyStatusLabel(_uiNotifyStatuses[el.dataset.uiNotifyStatus]);
    });
}

// "Check now": force one immediate evaluation of the event (server ignores
// its interval), then reconcile toasts + status labels from the response.
// For the calendar row the server runs one notifier scan instead, which
// also delivers due occurrences to its subscribed external channels.
async function checkNowUINotification(key, btn) {
    if (!beginOp(`ui-notify-check-${key}`)) return;
    if (btn) {
        btn.disabled = true;
        btn.classList.add('spinning');
    }
    try {
        const response = await fetch(
            `/api/notifications/ui/${encodeURIComponent(key)}/check`,
            { method: 'POST' }
        );
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        applyNotificationPayload(data);
        const firing = (data.items || []).some((item) => item.key === key);
        // Calendar rule rows aren't "problems" - they report events inside the
        // row's lead window, found or not. Read the value from the row itself
        // so the message matches what's on screen.
        const row = btn && btn.closest ? btn.closest('[data-ui-notify-key]') : null;
        const lead = row && row.querySelector
            ? row.querySelector('.ui-notify-lead-input') : null;
        if (row && row.dataset.uiNotifyKind === 'rule' && lead) {
            const hours = clampInt(lead.value, 0, 8760, 24);
            if (firing) {
                notifyWarn(hours > 0
                    ? `There are events due in ${hours} hour${hours === 1 ? '' : 's'}`
                    : 'There are events due now');
            } else {
                notifySuccess(hours > 0
                    ? `No events in the next ${hours} hour${hours === 1 ? '' : 's'}`
                    : 'No events due now');
            }
        } else if (firing) notifyWarn('Problem found');
        else notifySuccess('No problems found');
    } catch (e) {
        notifyError('Check failed');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.classList.remove('spinning');
        }
        endOp(`ui-notify-check-${key}`);
    }
}

// The "Notify via" checkboxes of the row a button lives in, as channel ids -
// read live from the DOM so Test exercises the state on screen, not just the
// last saved one. Returns null when no row can be found (caller then omits
// the override and the server uses the saved subscription).
function rowNotifyChannels(btn) {
    const row = btn && btn.closest ? btn.closest('[data-ui-notify-key]') : null;
    if (!row) return null;
    return [...row.querySelectorAll('[data-notify-channel]')]
        .filter((cb) => cb.checked)
        .map((cb) => cb.dataset.notifyChannel);
}

// "Test": preview the event as a real-looking toast AND fire the same sample
// payload through its actually-subscribed external channels (server-side).
// The response is {item, deliveries}: item renders as a preview-only card
// (data-notification-test, never reconciled by polls; Dismiss just closes it,
// it must NOT silence a live episode), and deliveries reports per-channel
// send success so a broken webhook fails visibly instead of only previewing
// happily. Works while the event is disabled - verifying before enabling is
// the point.
//
// The request body carries the row's "Notify via" checkboxes exactly as
// currently shown, so ticking Webhook and clicking Test fires the webhook
// immediately - you don't have to hit Save first. If the row can't be found
// (unexpected render), the body is omitted and the server falls back to the
// saved subscription.
async function testUINotification(key, btn) {
    if (!beginOp(`ui-notify-test-${key}`)) return;
    if (btn) btn.disabled = true;
    try {
        const channels = rowNotifyChannels(btn);
        const init = { method: 'POST' };
        if (channels) {
            init.headers = { 'Content-Type': 'application/json' };
            init.body = JSON.stringify({ channels });
        }
        const response = await fetch(
            `/api/notifications/ui/${encodeURIComponent(key)}/test`,
            init
        );
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const body = await response.json();
        const item = body.item || {};
        const deliveries = body.deliveries || {};
        // Override the id: a preview must never reuse (or be reconciled as) a
        // live card's node, even when it previews the same occurrence.
        const toast = notifyEvent(item, {
            onGoTo: (it, el) => {
                gotoNotificationTarget(it);
                if (el) el.remove();
            },
            // Preview-only: closing must NOT silence a live episode server-side.
            onDismiss: (it, el) => { if (el) el.remove(); },
        }, { id: `ui-notify-test-${item.key || key}` });
        if (toast) toast.dataset.notificationTest = item.key || key;

        const names = Object.keys(deliveries);
        if (!names.length) {
            notifyInfo('Preview shown - no external channels are subscribed for this event');
        } else {
            const labelFor = (name) => {
                const def = _notifyChannelDefs.find((c) => c.name === name);
                return def ? def.label : name;
            };
            const failed = names.filter((n) => !deliveries[n]).map(labelFor);
            if (failed.length) notifyError(`Test send failed: ${failed.join(', ')}`);
            else notifySuccess(`Test sent via ${names.map(labelFor).join(', ')}`);
        }
    } catch (e) {
        notifyError('Could not run the notification test');
    } finally {
        if (btn) btn.disabled = false;
        endOp(`ui-notify-test-${key}`);
    }
}

// Collect the rendered rows into the PUT /api/settings payload shape:
// per-event {channels, interval_minutes} plus notify_before_due_hours for
// rule events (only rows that render a lead input send it). There is no
// "enabled" field any more: channels are always sent in full, the server
// replaces the list, and an explicitly emptied one disables the event -
// ticking a channel IS enabling it. Omitted entirely when the host is
// missing or empty so a broken render never wipes stored overrides.
function collectUINotificationSettings() {
    const host = document.getElementById('ui-notification-rows');
    if (!host) return null;
    const out = {};
    let any = false;
    host.querySelectorAll('[data-ui-notify-key]').forEach((row) => {
        const key = row.dataset.uiNotifyKey;
        const channels = [...row.querySelectorAll('[data-notify-channel]')]
            .filter((cb) => cb.checked)
            .map((cb) => cb.dataset.notifyChannel);
        const entry = { channels };
        const interval = row.querySelector('.ui-notify-interval-input');
        if (interval) {
            entry.interval_minutes = clampInt(interval.value, 5, 10080, 1440);
        }
        const lead = row.querySelector('.ui-notify-lead-input');
        if (lead) {
            entry.notify_before_due_hours = clampInt(lead.value, 0, 8760, 24);
        }
        out[key] = entry;
        any = true;
    });
    return any ? out : null;
}

// Realm wiring hook (called from wireAll()). The rows' listeners are inline
// free (checkboxes/inputs are read at Save time) and the poller is started by
// initializeApp(), so there is nothing global to wire - kept for symmetry.
function wireNotificationEvents() {}
