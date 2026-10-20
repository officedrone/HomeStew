// HomeStew - In-app notification framework: polls the server's condition
// checks and renders each active one as a persistent toast with "Go To" and
// "Dismiss" buttons.
//
// Server side (services/ui_notifications.py) owns the truth: which checks
// exist, their per-check intervals, and dismissal state. An "occurrence" is
// one continuous episode where a condition holds; Dismiss silences THAT
// occurrence permanently (survives reloads/restarts), while the ✕ only hides
// the card for this browser session - it comes back after a reload if the
// condition still holds and was never Dismissed.
//
// Adding a new event needs no change here: the server returns items with
// message/goto/action fields and this file renders whatever arrives.

const UI_NOTIFY_POLL_MS = 60000;
let uiNotifyTimer = null;

// Keys closed with ✕ during this browser session (sessionStorage persists
// across reloads in the same tab but is per-tab and dies when it closes).
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

function rememberClosedUINotification(key) {
    _uiNotifyClosed.add(key);
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
// ones, refresh existing ones in place, and remove cards whose condition has
// resolved server-side (episode closed) - so fixing the problem makes the
// toast vanish without any user action.
async function pollUINotifications() {
    const response = await fetch('/api/notifications/ui');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const items = Array.isArray(data.items) ? data.items : [];
    const activeKeys = new Set(items.map((item) => item.key));

    // Drop cards the server no longer reports (resolved, or dismissed in
    // another tab). Cards hidden with ✕ are already out of the DOM.
    document.querySelectorAll('[data-notification-key]').forEach((el) => {
        if (!activeKeys.has(el.dataset.notificationKey)) el.remove();
    });

    for (const item of items) {
        if (_uiNotifyClosed.has(item.key)) continue;
        const existing = document.querySelector(
            `[data-notification-key="${CSS.escape(item.key)}"]`
        );
        if (existing) {
            // Keep the text current (e.g. the un-indexed count changed).
            const msgEl = existing.querySelector('.toast-message');
            if (msgEl) msgEl.textContent = item.message;
            continue;
        }
        showUINotificationCard(item);
    }
}

// Render one active notification as a persistent toast. The id ties the card
// to its key so showToast() updates rather than duplicates it, and
// data-notification-key lets pollUINotifications() reconcile against the DOM.
function showUINotificationCard(item) {
    const toast = showToast(item.message, item.severity || 'warn', null, {
        persistent: true,
        id: `ui-notify-${item.key}`,
        actions: [
            {
                label: item.action_label || 'Go to Settings',
                variant: 'primary',
                onClick: () => gotoNotificationTarget(item),
            },
            {
                label: 'Dismiss',
                variant: 'ghost',
                onClick: (el) => dismissUINotification(item.key, el),
            },
        ],
        onClose: () => rememberClosedUINotification(item.key),
    });
    if (toast) toast.dataset.notificationKey = item.key;
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

// Server-side dismissal: silences this occurrence forever. The POST is
// op-locked so a double click can't fire twice; the card is removed on any
// outcome - even a 404-ish race means another tab already ended the episode.
async function dismissUINotification(key, toastEl) {
    if (!beginOp(`ui-notify-dismiss-${key}`)) return;
    try {
        await fetch(`/api/notifications/ui/${encodeURIComponent(key)}/dismiss`, {
            method: 'POST',
        });
    } catch (e) { /* server unreachable: still hide locally, next poll reconciles */ }
    finally {
        endOp(`ui-notify-dismiss-${key}`);
    }
    const el = toastEl
        || document.querySelector(`[data-notification-key="${CSS.escape(key)}"]`);
    if (el) el.remove();
}

// Settings > Advanced "Health Notifications": one row per check, rendered
// from the server payload (GET /api/settings -> ui_notifications), so adding
// a check server-side needs no HTML or JS change. Called from loadSettingsPage().
function renderUINotificationSettings(settings) {
    const host = document.getElementById('ui-notification-rows');
    if (!host) return;
    const checks = settings.ui_notifications || {};
    const keys = Object.keys(checks);
    if (!keys.length) {
        host.innerHTML = '<p class="settings-hint">No notification checks are registered.</p>';
        return;
    }
    host.innerHTML = keys.map((key) => {
        const c = checks[key];
        const safeKey = escapeHtml(key);
        const safeLabel = escapeHtml(c.label || key);
        return `
            <div class="settings-field" data-ui-notify-key="${safeKey}">
                <div class="label-row">
                    <label class="checkbox-option" for="ui-notify-enabled-${safeKey}">
                        <input type="checkbox" id="ui-notify-enabled-${safeKey}"
                            ${c.enabled ? 'checked' : ''}>
                        ${safeLabel}
                    </label>
                </div>
                <div class="model-row ui-notify-interval-row">
                    <input type="number" id="ui-notify-interval-${safeKey}"
                        min="5" max="10080" step="5"
                        value="${Number(c.interval_minutes) || 1440}"
                        aria-label="Check interval in minutes for ${safeLabel}">
                    <span class="settings-hint ui-notify-interval-unit">minutes between checks</span>
                </div>
            </div>`;
    }).join('');
}

// Collect the rendered rows into the PUT /api/settings payload shape.
function collectUINotificationSettings() {
    const host = document.getElementById('ui-notification-rows');
    if (!host) return null;
    const out = {};
    let any = false;
    host.querySelectorAll('[data-ui-notify-key]').forEach((row) => {
        const key = row.dataset.uiNotifyKey;
        const enabled = row.querySelector('input[type="checkbox"]');
        const interval = row.querySelector('input[type="number"]');
        if (!enabled || !interval) return;
        out[key] = {
            enabled: enabled.checked,
            interval_minutes: clampInt(interval.value, 5, 10080, 1440),
        };
        any = true;
    });
    return any ? out : null;
}

// Realm wiring hook (called from wireAll()). The rows' listeners are inline
// free (checkboxes/inputs are read at Save time) and the poller is started by
// initializeApp(), so there is nothing global to wire - kept for symmetry.
function wireNotificationEvents() {}
