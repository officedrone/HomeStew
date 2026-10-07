// HomeStew - Dashboard realm: stat cards and the system-status strip.


// ---------------------------------------------------------------------------
// Dashboard tab (stat cards above the Recent Devices / Upcoming feeds)
// ---------------------------------------------------------------------------

// Recompute both dashboard cards: device/manual totals come from the cached
// device list, the reminder buckets from a fresh calendar fetch. Called by
// loadDevices() and whenever the Dashboard tab is opened.
function refreshDashboard() {
    const set = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.textContent = String(value);
    };
    set('dash-device-count', devices.length);
    set('dash-manual-count', devices.reduce((n, d) => n + (d.manual_count || 0), 0));
    loadDashboardStats();
    refreshSystemStatus();
}

// ---------------------------------------------------------------------------
// Dashboard system-status strip (LLM integration / notifications)
// ---------------------------------------------------------------------------

// Paint one status value and its colour class. States: on (green), warn
// (amber - configured but broken), off (neutral).
function setDashStatus(id, text, state) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.classList.remove('status-on', 'status-warn', 'status-off');
    if (state) el.classList.add(`status-${state}`);
}

// Fill the status strip from GET /api/settings. The LLM row needs a live
// connectivity probe, so it shows "Checking..." until /model-status answers:
//   no URL configured            -> Disabled (neutral)
//   URL set + server reachable   -> Enabled  (green)
//   URL set + probe fails        -> Warning  (amber)
async function refreshSystemStatus() {
    let settings;
    try {
        const response = await fetch('/api/settings');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        settings = await response.json();
    } catch (error) {
        console.error('Failed to load system status:', error);
        setDashStatus('status-llm', 'Unknown', null);
        setDashStatus('status-notifications', 'Unknown', null);
        setDashStatus('status-notify-type', 'Unknown', null);
        return;
    }

    // Notifications: the master toggle, plus the delivery channel's type.
    const notifyOn = !!settings.notify_enabled;
    setDashStatus('status-notifications', notifyOn ? 'On' : 'Off', notifyOn ? 'on' : 'off');
    // Webhook Type "none" (the default) means no webhook at all: show a grey
    // "None", the same neutral state as Notifications being switched off.
    const wtype = settings.notify_webhook_type || 'none';
    if (wtype === 'none') {
        setDashStatus('status-notify-type', 'None', 'off');
    } else {
        const typeName = wtype === 'synology' ? 'Synology Chat' : 'Generic (JSON POST)';
        setDashStatus('status-notify-type', typeName, settings.notify_webhook_enabled ? 'on' : 'off');
    }

    // LLM integration: configured URL is the gate; when present, probe it.
    if (!(settings.llm_base_url || '').trim()) {
        setDashStatus('status-llm', 'Disabled', 'off');
        return;
    }
    setDashStatus('status-llm', 'Checking...', null);
    try {
        const response = await fetch('/api/settings/model-status');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const status = await response.json();
        if (status.reachable) setDashStatus('status-llm', 'Enabled', 'on');
        else setDashStatus('status-llm', 'Warning - server unreachable', 'warn');
    } catch (error) {
        // Probe request failed: same meaning as an unreachable server.
        console.error('LLM status probe failed:', error);
        setDashStatus('status-llm', 'Warning - server unreachable', 'warn');
    }
}

// Deep link from the dashboard status strip into a Settings section
// ("ai", "notifications", ...). switchTab loads the page; the section tab is
// activated immediately and re-asserted once loadSettingsPage() finishes.
function openSettingsSection(name) {
    pendingSettingsSection = name;
    switchTab('settings');
    showSettingsSection(name);
}

// Event buckets for the Events card, counted client-side from GET /api/calendar
// (which already returns each event's computed next_due_date). An "active"
// reminder is any event with a due date - completed one-time events report
// null and are excluded. The 7/30-day buckets include overdue and today,
// matching the backend within_days semantics (due <= today + N).
async function loadDashboardStats() {
    try {
        const response = await fetch('/api/calendar');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const events = await response.json();

        // Local-midnight day diff, the same convention as dueLabel().
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        let total = 0;
        let week = 0;
        let month = 0;
        for (const ev of events) {
            if (!ev.next_due_date) continue;
            total++;
            const days = Math.round((new Date(`${ev.next_due_date}T00:00:00`) - today) / 86400000);
            if (days <= 7) week++;
            if (days <= 30) month++;
        }

        const set = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.textContent = String(value);
        };
        set('dash-total-events', total);
        set('dash-week', week);
        set('dash-month', month);
    } catch (error) {
        console.error('Failed to load dashboard stats:', error);
    }
}

// Deep link from a stat number into the Calendar tab with its range filter
// selected. Month maps to the existing 31-day ("Next Month") horizon.
// switchTab's calendar hook reloads the list with the new range.
function openCalendarRange(bucket) {
    const ranges = { all: 'all', week: '7', month: '31' };
    currentCalendarRange = ranges[bucket] ?? 'all';
    const select = document.getElementById('calendar-range-filter');
    if (select) select.value = currentCalendarRange;
    switchTab('calendar');
}
