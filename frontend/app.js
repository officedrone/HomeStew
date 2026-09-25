// HomeStew Frontend Application

document.addEventListener('DOMContentLoaded', () => {
    // Auth gate listeners must exist before anything else can run: the rest
    // of setupEventListeners() only wires up once a session is confirmed.
    setupAuthGate();
    loadAppVersion();
    initializeApp();
});

// Sidebar footer version line: "Version X.Y.Z" from /health, which is auth-
// exempt, so it also fills in behind the login screen. Non-blocking; the
// element keeps its static "HomeStew" fallback when offline.
async function loadAppVersion() {
    try {
        const response = await fetch('/health');
        if (!response.ok) return;
        const data = await response.json();
        if (data.version) {
            document.getElementById('app-version').textContent = `Version ${data.version}`;
        }
    } catch (e) { /* keep the fallback label */ }
}

// ---------------------------------------------------------------------------
// Authentication gate (single-user account, services/auth.py on the server)
//
// Every API call requires a valid session cookie. Two mechanisms keep the UI
// honest:
//  * checkAuth() runs before any other initialization and shows #auth-modal
//    (create-account on first run, sign-in afterwards) when unauthenticated.
//  * The global fetch patch below catches late 401s - an expired session or a
//    second tab logging out - without touching the 42 call sites individually
//    (several loaders swallow errors in catch blocks, so per-site handling
//    would be unreliable).
// ---------------------------------------------------------------------------
let authMode = null; // 'create' | 'login', set by renderAuthGate()
let authInFlight = false;

(function installAuthFetchPatch() {
    const nativeFetch = window.fetch.bind(window);
    window.fetch = async function (input, init) {
        const response = await nativeFetch(input, init);
        try {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            // These three 401s are meaningful to the caller itself (a wrong
            // password shows an inline error), so they must not re-open the
            // gate. A 401 from PUT /api/auth/password (session expired
            // mid-edit) does, like any other app endpoint.
            const selfHandled = ['/api/auth/status', '/api/auth/login', '/api/auth/setup']
                .some((p) => url.includes(p));
            if (response.status === 401 && !selfHandled) {
                handleUnauthenticated();
            }
        } catch (e) { /* never break the original request */ }
        return response;
    };
})();

function handleUnauthenticated() {
    const modal = document.getElementById('auth-modal');
    if (modal.style.display === 'flex') return; // gate already up
    showAuthGate();
}

// Decide which auth screen to show and raise the gate. Safe to call anytime.
async function showAuthGate() {
    let statusData = { password_configured: true, authenticated: false };
    try {
        const response = await fetch('/api/auth/status');
        if (response.ok) statusData = await response.json();
    } catch (e) { /* offline: fall back to the login form */ }
    if (statusData.authenticated) return; // raced a successful login - stay put
    renderAuthGate(statusData.password_configured ? 'login' : 'create');
}

function renderAuthGate(mode) {
    authMode = mode;
    const create = mode === 'create';
    document.getElementById('auth-section-create').hidden = !create;
    document.getElementById('auth-section-login').hidden = create;
    document.getElementById('auth-title').textContent =
        create ? 'Create your account' : 'Welcome back';
    document.getElementById('auth-subtitle').textContent = create
        ? 'Welcome! Please create your HomeStew password below'
        : 'Please Enter your HomeStew password.';
    const btn = document.getElementById('auth-submit-btn');
    btn.textContent = create ? 'Create Account' : 'Sign In';
    btn.disabled = false;
    setAuthError(null);
    document.getElementById('auth-modal').style.display = 'flex';
    const focusId = create ? 'auth-password' : 'auth-login-password';
    setTimeout(() => document.getElementById(focusId).focus(), 50);
}

function setAuthError(message) {
    const el = document.getElementById('auth-error');
    el.textContent = message || '';
    el.hidden = !message;
}

async function authRequest(path, method, body) {
    const response = await fetch(`/api/auth/${path}`, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    let data = null;
    try { data = await response.json(); } catch (e) { /* no body */ }
    if (!response.ok) {
        const detail = data && typeof data.detail === 'string'
            ? data.detail : `HTTP ${response.status}`;
        throw new Error(detail);
    }
    return data;
}

async function handleAuthSubmit() {
    if (authInFlight || !authMode) return;
    const btn = document.getElementById('auth-submit-btn');
    setAuthError(null);
    try {
        if (authMode === 'create') {
            const password = document.getElementById('auth-password').value;
            const confirm = document.getElementById('auth-confirm').value;
            if (password.length < 8) throw new Error('Password must be at least 8 characters.');
            if (password !== confirm) throw new Error('The passwords do not match.');
            authInFlight = true;
            btn.disabled = true;
            await authRequest('setup', 'POST', { password, confirm_password: confirm });
        } else {
            const password = document.getElementById('auth-login-password').value;
            if (!password) throw new Error('Enter your password.');
            authInFlight = true;
            btn.disabled = true;
            await authRequest('login', 'POST', {
                password,
                remember_me: document.getElementById('auth-remember').checked,
            });
        }
        // Full reload (not a re-init): every listener/loader then runs once
        // against an authenticated session, exactly like a fresh page load.
        location.reload();
    } catch (error) {
        setAuthError(error.message);
        authInFlight = false;
        btn.disabled = false;
    }
}

async function handleLogout() {
    try { await fetch('/api/auth/logout', { method: 'POST' }); } catch (e) { /* cookie cleared client-side anyway */ }
    location.reload();
}

function setupAuthGate() {
    document.getElementById('auth-submit-btn').addEventListener('click', handleAuthSubmit);
    // Enter in any auth field submits, like a real login form.
    for (const id of ['auth-password', 'auth-confirm', 'auth-login-password']) {
        document.getElementById(id).addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); handleAuthSubmit(); }
        });
    }
}

// Returns true when the caller's session is valid; raises the gate otherwise.
async function checkAuth() {
    let statusData;
    try {
        const response = await fetch('/api/auth/status');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        statusData = await response.json();
    } catch (e) {
        console.error('Auth check failed:', e);
        renderAuthGate('login'); // assume login mode; the fetch patch retries later
        setAuthError('Could not reach HomeStew. Check that the container is running, then reload.');
        return false;
    }
    if (statusData.authenticated) {
        document.getElementById('auth-modal').style.display = 'none';
        return true;
    }
    renderAuthGate(statusData.password_configured ? 'login' : 'create');
    return false;
}

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
    // and strip referrer info from outbound links. Internal app links
    // (href="#...", e.g. the #edit-device-<id> links the device tool hands
    // back) must stay in-page - they are handled by a click listener.
    DOMPurify.addHook('afterSanitizeAttributes', (node) => {
        if (node.tagName !== 'A') return;
        // Internal app links stay in-page: they are routed by handleInternalLink
        // rather than followed. Checked on the RESOLVED hash because a model may
        // emit an absolute URL for them (http://host/#fetch-manuals-3).
        if (internalDeviceLink(node.getAttribute('href'))) return;
        node.target = '_blank';
        node.rel = 'noopener noreferrer';
    });
}

// ---------------------------------------------------------------------------
// Internal device links handed out by the manage_devices tool.
// ---------------------------------------------------------------------------

// Maps an anchor href to { action, deviceId }, or null when it is not one of
// our internal links. Deliberately resolves the href first: small models often
// write the absolute form ('http://localhost:8000/#fetch-manuals-8') instead of
// a bare '#fetch-manuals-8', and an attribute-prefix selector would miss it.
function internalDeviceLink(href) {
    const raw = String(href || '');
    if (!raw) return null;
    let hash;
    try {
        const url = new URL(raw, window.location.origin);
        // Only our own origin counts - never route a link to another host.
        if (url.origin !== window.location.origin) return null;
        hash = url.hash;
    } catch (e) {
        return null;
    }
    const m = /^#(edit-device|fetch-manuals)-(\d+)$/.exec(hash);
    if (!m) return null;
    return { action: m[1], deviceId: parseInt(m[2], 10) };
}

// Click handler for those links, whatever container rendered them (chat bubbles,
// tool traces, ...). Returns true when the click was consumed.
function handleInternalLink(e) {
    const link = e.target.closest && e.target.closest('a');
    if (!link) return false;
    const target = internalDeviceLink(link.getAttribute('href'));
    if (!target) return false;
    // Never let the browser touch the URL hash: restoreActiveTab() reads it on
    // the next load and would otherwise reopen this tab/modal state.
    e.preventDefault();
    if (target.action === 'fetch-manuals') {
        openDeviceEditorAndFetchById(target.deviceId);
    } else {
        openDeviceEditorById(target.deviceId);
    }
    return true;
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
// Calendar tab state: device filter, date-range filter and loaded events.
// The range is a day horizon ('all', 7, 31, 182 or 365) sent as within_days.
let currentCalendarDeviceFilter = null;
let currentCalendarRange = 'all';
let calendarEvents = [];
// Selected device filter for the AI Chat tab (null = all devices). Mirrors
// currentDeviceFilter, but scoped to chat so the two tabs stay independent.
let currentChatDeviceFilter = null;
// Device id currently targeted by the hidden manual-upload input.
let uploadTargetDeviceId = null;

// Result of the last chat model availability check (see checkChatModelStatus).
// null = unknown/not yet checked; true/false = whether chatting is allowed.
let chatModelAvailable = null;

// Whether the saved config marks the model as vision-capable ("Model supports
// Vision" in Settings > AI / the first-run wizard). The attach & camera
// buttons are hidden while false, and sendChatMessage refuses images so an
// old tab can't push a photo to a text-only model. Seeded from
// /api/settings/model-status (see checkChatModelStatus) and refreshed after a
// settings save; the server independently rejects images too (chat API).
let chatVisionEnabled = false;

// Show/hide the chat image controls to match chatVisionEnabled: a body class
// hides the picker strip and reclaims the textarea's right padding, so typed
// text uses the full width when there are no buttons to avoid.
function updateChatImageControls() {
    document.body.classList.toggle('no-vision', !chatVisionEnabled);
}

// While an answer streams in, this controller lets the Stop button abort the
// fetch (which also tears down the SSE stream server-side). Null when idle.
let chatAbortController = null;

// Images picked for the NEXT message (data URLs after client-side
// downscaling). Cleared on send and by New Session. The backend caps the
// same limits (MAX_CHAT_IMAGES / size) - these guards just fail fast.
let pendingChatImages = [];
const MAX_CHAT_IMAGES = 4;
// Longest edge after downscale: enough for reading labels/nameplates, small
// enough that base64 stays a few hundred KB per photo for local models.
const CHAT_IMAGE_MAX_DIM = 1024;

// Chat scroll state - "stick to bottom" while an answer streams, released as
// soon as the user genuinely scrolls up and restored when they return to the
// bottom (or press the scroll-to-bottom button).
let chatPinned = true;
const CHAT_SCROLL_THRESHOLD = 80; // px from bottom that still counts as "at bottom"
let _chatScrollQueued = false;
// Last scrollTop we know about, so incoming scroll events can be read by
// DIRECTION. Distance-from-bottom alone is not enough: while an answer streams,
// content grows between a programmatic pin and the scroll event the browser
// fires for it, which then looks like "the user scrolled away" and silently
// kills auto-follow for the rest of the conversation.
let _lastChatScrollTop = 0;

function chatMessagesEl() {
    return document.getElementById('chat-messages');
}

function isNearBottom(el, threshold) {
    return (el.scrollHeight - el.scrollTop - el.clientHeight) <= threshold;
}

// Coalesce scroll work to one pass per frame: token streams and ResizeObserver
// callbacks would otherwise fight over scrollTop.
function scheduleChatScroll() {
    if (_chatScrollQueued) return;
    _chatScrollQueued = true;
    requestAnimationFrame(() => {
        _chatScrollQueued = false;
        const container = chatMessagesEl();
        if (!container) return;
        // Pin first, then refresh the button - in between it would briefly
        // flash into view on frames where new tokens pushed content down.
        if (chatPinned) pinToBottom(container);
        updateScrollDownButton();
    });
}

// Scrolls to the newest message and records the position we asked for, so the
// resulting scroll event is recognised as ours instead of a user gesture.
function pinToBottom(container) {
    container.scrollTop = container.scrollHeight;
    // Read back: the browser clamps scrollTop to the scrollable range, and the
    // clamped value is what the scroll event will report.
    _lastChatScrollTop = container.scrollTop;
}

// Always pins; safe as a click handler (the event arg is ignored).
function scrollToBottom() {
    chatPinned = true;
    const container = chatMessagesEl();
    if (!container) return;
    pinToBottom(container);
    updateScrollDownButton();
}

// Scroll events from the messages area. Only a real upward scroll releases the
// pin, so neither our own programmatic pins nor reflows (a <details> collapsing,
// an image finishing to load, content shrinking below the viewport) can leave
// auto-scroll stuck off - or, in reverse, yank the view back down while the user
// is trying to read earlier messages.
function handleChatMessagesScroll() {
    const container = chatMessagesEl();
    if (!container) return;
    const top = container.scrollTop;
    const scrolledUp = top < _lastChatScrollTop - 1;
    _lastChatScrollTop = top;

    if (isNearBottom(container, CHAT_SCROLL_THRESHOLD)) {
        // Back at the bottom: start following again. Checked first so a clamp
        // caused by content shrinking never counts as scrolling away.
        chatPinned = true;
    } else if (scrolledUp) {
        chatPinned = false;
    }
    updateScrollDownButton();
}

function updateScrollDownButton() {
    const btn = document.getElementById('scroll-down-btn');
    if (!btn) return;
    const container = chatMessagesEl();
    if (!container) return;
    const distFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
    // Hidden while pinned (the next frame pins us back anyway, so showing it
    // mid-stream would only flicker) and when less than ~120px is below view.
    btn.classList.toggle('hidden', chatPinned || distFromBottom <= 120);
}

async function initializeApp() {
    // Nothing below may run without a session: the loaders would only 401
    // (and several swallow errors), so gate first and stop. On successful
    // create/login the page reloads and this runs again, authenticated.
    if (!(await checkAuth())) return;
    await loadDevices();
    setupEventListeners();
    // Restore the tab from before a page refresh (URL hash first, then the
    // last tab persisted to localStorage) so F5 no longer dumps the user on
    // the Search tab.
    restoreActiveTab();
    // One-time setup wizard (encryption key / AI model / first device).
    // Runs after loadDevices() so the "first device" step can see whether
    // any devices exist yet; skipped/saved steps are persisted server-side.
    checkSetupWizard();
}

// ---------------------------------------------------------------------------
// Click deduplication (two layers, both needed)
// ---------------------------------------------------------------------------

// Layer 1 - rapid re-clicks: buttons tagged [data-dedupe] ignore further
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

// Layer 2 - in-flight operations: server calls (add device, save event,
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
    // name from currentTarget - e.target can be the inner <svg>/<span>.
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

    // Chat image capture: attach opens a file picker, camera asks for the
    // rear camera (phones); both feed the same downscale+preview pipeline.
    document.getElementById('chat-attach-btn').addEventListener('click', () => {
        document.getElementById('chat-image-input').click();
    });
    document.getElementById('chat-camera-btn').addEventListener('click', () => {
        document.getElementById('chat-camera-input').click();
    });
    document.getElementById('chat-image-input').addEventListener('change', handleChatImageSelected);
    document.getElementById('chat-camera-input').addEventListener('change', handleChatImageSelected);

    // Scroll-to-bottom button for chat messages.
    document.getElementById('scroll-down-btn').addEventListener('click', scrollToBottom);

    // Chat scroll container: track pinned state and wire observers.
    (function initChatScroll() {
        const container = chatMessagesEl();
        if (!container) return;
        chatPinned = true;
        _lastChatScrollTop = container.scrollTop;
        container.addEventListener('scroll', handleChatMessagesScroll);
        // Re-pins when user expands/collapses a <details> while pinned. Deferred
        // through scheduleChatScroll so no layout write happens inside the
        // toggle handler, and so a burst of events collapses to one pass.
        container.addEventListener('toggle', () => {
            if (chatPinned) scheduleChatScroll();
        }, true);
        // Observe new message nodes so their height changes update button visibility and re-pin.
        const ro = new ResizeObserver(() => {
            // Deferred on purpose: writing scrollTop from inside a RO callback
            // can trip the browser's "ResizeObserver loop" guard, after which
            // later notifications are dropped - i.e. auto-scroll quietly stops.
            scheduleChatScroll();
        });
        // The container itself resizes when the model-warning banner shows/hides,
        // the window changes size or the chat tab is shown again; without
        // observing it, pinning/button state would go stale until the next scroll.
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
    initCollapsibleSections();
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

    // Settings page section tabs (General / AI / Notifications / Advanced):
    // show one panel at a time. The gear itself is a .tab-btn wired above.
    document.querySelectorAll('.settings-tab-btn').forEach((btn) => {
        btn.addEventListener('click', () => showSettingsSection(btn.dataset.settingsSection));
    });
    document.getElementById('settings-form').addEventListener('submit', handleSettingsSave);
    document.getElementById('fetch-models-btn').addEventListener('click', () => fetchLlmModels());
    document.getElementById('test-connection-btn').addEventListener('click', () => testLlmConnection({
        urlId: 'settings-llm-base-url', keyId: 'settings-llm-api-key',
        btnId: 'test-connection-btn', resultId: 'settings-conn-test-result',
    }));
    resetConnTestOnEdit('settings-llm-base-url', 'settings-llm-api-key', 'settings-conn-test-result');
    document.getElementById('test-webhook-btn').addEventListener('click', () => sendTestNotification());

    // Backup & Restore (Settings > General). Buttons are type="button" so the
    // surrounding settings form never submits when they're clicked.
    document.getElementById('backup-create-btn').addEventListener('click', createBackup);
    document.getElementById('restore-btn').addEventListener('click', triggerRestoreFilePick);
    document.getElementById('restore-file-input').addEventListener('change', handleRestoreFileSelected);
    // "Remove" buttons next to the write-only secret fields schedule deletion
    // for save; typing into a cleared field cancels that removal (without
    // re-rendering, so the in-progress value survives).
    document.querySelectorAll('.secret-remove-btn').forEach((btn) => {
        btn.addEventListener('click', () => clearSecret(btn.dataset.clearSecret));
    });
    for (const [name, field] of Object.entries(SECRET_FIELDS)) {
        document.getElementById(field.inputId).addEventListener('input', () => {
            if (!secretsState.cleared.delete(name)) return;
            const btn = document.querySelector(`.secret-remove-btn[data-clear-secret="${name}"]`);
            if (btn) btn.hidden = !(secretsState.configured[name]);
            document.getElementById(field.hintId).textContent = secretHintText(name);
        });
    }
    // Switching webhook type updates the URL hint and hides the bearer-token
    // field (Synology Chat authenticates via the token= param in its URL).
    document.getElementById('settings-notify-webhook-type').addEventListener(
        'change', updateWebhookTypeHints);
    setupModelDropdownRefresh();

    // Chat model warning banner: takes the user straight to the Settings page.
    document.getElementById('chat-open-settings-btn').addEventListener('click', () => switchTab('settings'));

    // Internal links produced by the device tool:
    //   #edit-device-<id>   - the LLM refuses a delete and hands over the editor
    //   #fetch-manuals-<id> - a freshly created device: open its editor AND start
    //                         the manual search so the user only has to approve.
    // Bound on document (capture) rather than on #chat-messages: answers are
    // rendered dynamically, and links can also appear in tool traces.
    document.addEventListener('click', handleInternalLink, true);

    // Model switcher in the chat toolbar: picking a model saves it right away
    // (same settings the Settings page writes) and re-runs the availability probe.
    document.getElementById('chat-model-select').addEventListener('change', handleChatModelChange);

    // "Restore Default" buttons in Advanced Settings repopulate the built-in
    // prompt text (fetched from the API) into the matching textarea.
    document.querySelectorAll('[data-restore-default]').forEach((btn) => {
        btn.addEventListener('click', () => restorePromptDefault(btn.dataset.restoreDefault));
    });

    // Secrets master key management: Settings > Advanced create/delete.
    document.getElementById('create-key-btn').addEventListener('click', handleCreateKeyFromSettings);
    document.getElementById('delete-key-btn').addEventListener('click', handleDeleteKeyFromSettings);

    // Account password change (Settings > Advanced) and sidebar sign-out.
    document.getElementById('change-password-btn').addEventListener('click', handleChangePassword);
    document.getElementById('logout-btn').addEventListener('click', handleLogout);

    // First-run setup wizard footer + AI step's model picker. The wizard is
    // deliberately not closable via Escape (see below): every step must end
    // with Skip or Save so its resolution gets persisted.
    document.getElementById('wizard-skip-btn').addEventListener('click', handleWizardSkip);
    document.getElementById('wizard-save-btn').addEventListener('click', handleWizardSave);
    document.getElementById('wizard-fetch-models-btn').addEventListener('click', fetchWizardLlmModels);
    document.getElementById('wizard-test-connection-btn').addEventListener('click', () => testLlmConnection({
        urlId: 'wizard-llm-base-url', keyId: 'wizard-llm-api-key',
        btnId: 'wizard-test-connection-btn', resultId: 'wizard-conn-test-result',
    }));
    resetConnTestOnEdit('wizard-llm-base-url', 'wizard-llm-api-key', 'wizard-conn-test-result');

    // Escape closes any open modal, or the mobile drawer when none is open.
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        // The fetch dialog needs its close function (releases the op lock),
        // so it is handled before the generic display:none loop below.
        const fetchModal = document.getElementById('fetch-manuals-modal');
        if (fetchModal.style.display === 'flex') {
            closeFetchManualsModal();
            return;
        }
        // The setup wizard is intentionally absent: skipping/saving a step
        // must persist its resolution, so it can't be dismissed with Escape.
        for (const id of ['add-device-modal', 'edit-device-modal', 'event-modal']) {
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
    check: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>',
    download: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>'
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

function openAddDeviceModal() {
    document.getElementById('add-device-form').reset();
    resetWarrantyAutoCalc('device');
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

async function openFetchManuals(deviceId) {
    // One fetch dialog at a time: a double-click must not start two searches.
    if (!beginOp('fetch-manuals')) return;
    fetchManualsDeviceId = deviceId;
    fetchCandidates = [];

    const modal = document.getElementById('fetch-manuals-modal');
    const statusBox = document.getElementById('fetch-manuals-status');
    const spinner = document.getElementById('fetch-manuals-spinner');
    const message = document.getElementById('fetch-manuals-message');
    const results = document.getElementById('fetch-manuals-results');
    const downloadAllBtn = document.getElementById('download-all-btn');

    modal.style.display = 'flex';
    statusBox.classList.remove('done-success', 'done-error');
    spinner.style.display = '';
    const device = devices.find(d => d.id === deviceId);
    const label = device ? `"${device.brand} ${device.model}"` : 'this device';
    message.textContent = `Searching the web for ${label} manuals...`;
    results.innerHTML = '';
    results.hidden = true;
    downloadAllBtn.style.display = 'none';

    try {
        const response = await fetch(`/api/downloads/${deviceId}/search`);
        if (!response.ok) throw new Error(await errorDetailFrom(response, 'Manual search failed.'));
        const data = await response.json();
        fetchCandidates = data.candidates || [];
        renderFetchResults(data.error_detail);
    } catch (error) {
        console.error('Failed to search manuals:', error);
        spinner.style.display = 'none';
        statusBox.classList.add('done-error');
        message.textContent = error.message || 'Manual search failed.';
    }
}

// Pull FastAPI's {detail} body into a readable string; detail may be a plain
// string or, for the 409 replace flow, an object with reason/filename.
async function errorDetailFrom(response, fallback) {
    try {
        const err = await response.json();
        if (typeof err.detail === 'string') return err.detail;
        if (err.detail && err.detail.message) return err.detail.message;
        if (err.detail) return JSON.stringify(err.detail);
    } catch (e) { /* non-JSON error body */ }
    return fallback;
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
// Search results: client-side pagination.
// The API returns every match for a query (no limit), so switching pages or
// per-page size never re-queries. 'searchPerPage' persists the user's choice
// (10 / 25 / 50 / all) across reloads, same localStorage pattern as theme and
// sidebar prefs (wrapped in try/catch for private-browsing mode).
let lastSearchData = null;
let searchPage = 1;

function getSearchPerPage() {
    let saved = null;
    try { saved = localStorage.getItem('searchPerPage'); } catch (e) { /* private mode */ }
    if (saved === 'all') return Infinity;
    const n = parseInt(saved, 10);
    return [10, 25, 50].includes(n) ? n : 10;
}

function saveSearchPerPage(value) {
    try { localStorage.setItem('searchPerPage', value); } catch (e) { /* private mode */ }
}

// Windowed page list with ellipses: 1 … 4 [5] 6 … 20. Returns strings; '…'
// marks a collapsed gap.
function searchPageNumbers(current, total) {
    const pages = [];
    for (let p = 1; p <= total; p++) {
        if (total <= 7 || p === 1 || p === total || Math.abs(p - current) <= 1) {
            pages.push(String(p));
        } else if (pages[pages.length - 1] !== '\u2026') {
            pages.push('\u2026');
        }
    }
    return pages;
}

function goToSearchPage(page) {
    searchPage = page;
    renderSearchResults(lastSearchData);
    // Bring the top of the results back into view after flipping a page.
    document.getElementById('search-results').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function changeSearchPerPage(value) {
    saveSearchPerPage(value);
    searchPage = 1;
    renderSearchResults(lastSearchData);
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
        
        // Cache the full result set and start on page 1; paging from here
        // on is pure re-render (see goToSearchPage / changeSearchPerPage).
        lastSearchData = data;
        searchPage = 1;
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
    
    if (!data || data.total_results === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <p>No results found for "${escapeHtml(data ? data.query : '')}"</p>
                <p>Try different keywords or download more manuals.</p>
            </div>
        `;
        return;
    }
    
    // Slice the cached result set for the current page. perPage = Infinity
    // ('All') shows everything on a single page.
    const perPage = getSearchPerPage();
    const total = data.results.length;
    const totalPages = perPage === Infinity ? 1 : Math.max(1, Math.ceil(total / perPage));
    searchPage = Math.min(Math.max(searchPage, 1), totalPages);
    const start = perPage === Infinity ? 0 : (searchPage - 1) * perPage;
    const pageResults = perPage === Infinity
        ? data.results
        : data.results.slice(start, start + perPage);
    const rangeEnd = start + pageResults.length;

    // Page numbers / Prev / Next only exist when there is more than one page;
    // the bar itself always renders so the Per-page select stays reachable.
    let nav = '';
    if (totalPages > 1) {
        nav += `<button type="button" class="page-btn" ${searchPage === 1 ? 'disabled' : ''} onclick="goToSearchPage(${searchPage - 1})">&laquo; Prev</button>`;
        for (const p of searchPageNumbers(searchPage, totalPages)) {
            if (p === '\u2026') {
                nav += '<span class="page-ellipsis">\u2026</span>';
            } else {
                const active = parseInt(p, 10) === searchPage ? ' active' : '';
                nav += `<button type="button" class="page-btn${active}" ${active ? 'aria-current="page"' : ''} onclick="goToSearchPage(${p})">${p}</button>`;
            }
        }
        nav += `<button type="button" class="page-btn" ${searchPage === totalPages ? 'disabled' : ''} onclick="goToSearchPage(${searchPage + 1})">Next &raquo;</button>`;
    }
    const perPageValue = perPage === Infinity ? 'all' : String(perPage);
    const perPageSelect = `
        <label class="per-page-label">Per page
            <select class="per-page-select" onchange="changeSearchPerPage(this.value)">
                ${['10', '25', '50', 'all'].map(v =>
                    `<option value="${v}"${v === perPageValue ? ' selected' : ''}>${v === 'all' ? 'All' : v}</option>`
                ).join('')}
            </select>
        </label>`;
    const pagination = `
        <div class="search-pagination">
            ${nav}
            ${perPageSelect}
        </div>`;

    container.innerHTML = `
        <div class="search-results-count">
            Showing ${start + 1}\u2013${rangeEnd} of ${total} result${total !== 1 ? 's' : ''} for "${escapeHtml(data.query)}"
        </div>
        ${pageResults.map(result => {
            // manual_id 0 = a hit in the device's own record (details /
            // custom attributes), not a PDF page - no file link to open.
            const header = result.manual_id === 0
                ? `
                    <span class="result-title">${escapeHtml(result.filename)}</span>
                    <a class="result-meta result-page-link" href="#" onclick="editDevice(${result.device_id}); return false;">Device entry &nearr;</a>
                `
                : `
                    <a class="result-title" href="/api/downloads/manuals/${result.manual_id}/file#page=${result.page_number}" target="_blank" rel="noopener">${escapeHtml(result.filename)}</a>
                    <a class="result-meta result-page-link" href="/api/downloads/manuals/${result.manual_id}/file#page=${result.page_number}" target="_blank" rel="noopener">Page ${result.page_number} &nearr;</a>
                `;
            // Footer naming the device this hit belongs to. The #edit-device-
            // <id> href is intercepted globally (handleInternalLink) and opens
            // that device's Edit modal, same convention as chat citations.
            const deviceLine = result.device_name ? `
                <div class="result-device">Relevant Device URL: <a href="#edit-device-${result.device_id}" title="Open ${escapeHtml(result.device_name)}">${escapeHtml(result.device_name)}</a></div>` : '';
            return `
            <div class="result-item${result.manual_id === 0 ? ' result-device-entry' : ''}">
                <div class="result-header">${header}</div>
                <div class="result-snippet">${result.snippet}</div>${deviceLine}
            </div>`;
        }).join('')}
        ${pagination}
    `;
}

// A file was picked via the attach or camera button: downscale each image
// and queue it as a data URL for the next message. The inputs are reset so
// picking the same file twice still fires 'change'.
async function handleChatImageSelected(event) {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    if (!files.length) return;
    // The pickers are hidden while the model is not marked vision-capable, but
    // a stale file dialog can still deliver a drop - refuse it here too.
    if (!chatVisionEnabled) {
        showToast('The model does not support images. Enable "Model supports Vision" in Settings > AI.', 'error');
        return;
    }

    for (const file of files) {
        if (!file.type.startsWith('image/')) {
            showToast('Only image files can be attached', 'error');
            continue;
        }
        if (pendingChatImages.length >= MAX_CHAT_IMAGES) {
            showToast(`At most ${MAX_CHAT_IMAGES} images per message`, 'error');
            break;
        }
        try {
            const dataUrl = await downscaleImageToDataUrl(file);
            pendingChatImages.push(dataUrl);
        } catch (e) {
            console.error('Failed to read image:', e);
            showToast('Could not read that image', 'error', String(e));
        }
    }
    renderPendingChatPreviews();
}

// Resize an image file to at most CHAT_IMAGE_MAX_DIM on its longest edge and
// re-encode as JPEG. Phone photos are several MB; a downscaled 1024px JPEG is
// ~100-300 KB of base64 - still sharp enough for the model to read labels and
// nameplates, but small enough not to blow up local-model context.
function downscaleImageToDataUrl(file) {
    return new Promise((resolve, reject) => {
        const url = URL.createObjectURL(file);
        const img = new Image();
        img.onload = () => {
            try {
                const scale = Math.min(1, CHAT_IMAGE_MAX_DIM / Math.max(img.width, img.height));
                const w = Math.max(1, Math.round(img.width * scale));
                const h = Math.max(1, Math.round(img.height * scale));
                const canvas = document.createElement('canvas');
                canvas.width = w;
                canvas.height = h;
                const ctx = canvas.getContext('2d');
                // JPEG has no alpha: paint white so transparent PNGs don't
                // turn black when re-encoded.
                ctx.fillStyle = '#fff';
                ctx.fillRect(0, 0, w, h);
                ctx.drawImage(img, 0, 0, w, h);
                resolve(canvas.toDataURL('image/jpeg', 0.85));
            } catch (e) {
                reject(e);
            } finally {
                URL.revokeObjectURL(url);
            }
        };
        img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('Image failed to load')); };
        img.src = url;
    });
}

// Thumbnail strip above the input showing what will be sent, each with an ×
// so a wrong photo can be dropped before sending.
function renderPendingChatPreviews() {
    const strip = document.getElementById('chat-attachment-preview');
    if (!strip) return;
    strip.innerHTML = '';
    if (!pendingChatImages.length) {
        strip.style.display = 'none';
        return;
    }
    pendingChatImages.forEach((dataUrl, i) => {
        const item = document.createElement('div');
        item.className = 'chat-attachment-item';
        const img = document.createElement('img');
        img.src = dataUrl;
        img.alt = `Attachment ${i + 1}`;
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'chat-attachment-remove';
        remove.title = 'Remove image';
        remove.setAttribute('aria-label', 'Remove image');
        remove.textContent = '\u00d7';
        remove.addEventListener('click', () => {
            pendingChatImages.splice(i, 1);
            renderPendingChatPreviews();
        });
        item.appendChild(img);
        item.appendChild(remove);
        strip.appendChild(item);
    });
    strip.style.display = 'flex';
}

function clearPendingChatImages() {
    pendingChatImages = [];
    renderPendingChatPreviews();
}

async function sendChatMessage() {
    const input = document.getElementById('chat-input');
    const message = input.value.trim();
    
    // An image-only message is valid: the model reads the photo itself.
    if (!message && !pendingChatImages.length) return;

    // Belt & braces: pending images cannot normally exist while vision is
    // off (the pickers are hidden), but never send them to a text-only model.
    if (pendingChatImages.length && !chatVisionEnabled) {
        showToast('The model does not support images. Enable "Model supports Vision" in Settings > AI.', 'error');
        return;
    }

    // If the last model probe failed, don't waste a round-trip: keep the
    // warning banner visible and point the user at Settings instead.
    if (chatModelAvailable === false) {
        showToast('No model is available - update the connection in Settings', 'error');
        return;
    }
    // One request at a time: a second click / Enter while streaming must not
    // start a parallel conversation turn.
    if (!beginOp('send-chat')) return;

    // Add user message to chat (with thumbnails of any attached images).
    addMessageToChat('user', message, pendingChatImages);
    input.value = '';

    // The photos now live in the sent bubble; the picker is empty for the
    // next message. History re-sends them only on the newest user turn.
    clearPendingChatImages();

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
            messages: messages.map((msg, i) => {
                const out = { role: msg.role, content: msg.content };
                if (msg.images && msg.images.length) {
                    // Only the NEWEST turn carries its images to the LLM:
                    // re-sending base64 on every follow-up would multiply
                    // request size and overflow small local-model contexts.
                    // Older image turns instead get a text marker so the
                    // model still knows a photo was part of the conversation
                    // (and, per the system prompt's Image rules, asks for it
                    // again rather than pretending to re-read it). The exact
                    // string matches the backend's own collapse in
                    // to_openai_messages() so both paths stay consistent.
                    if (i === messages.length - 1) {
                        out.images = msg.images;
                    } else {
                        out.content = `${msg.content}\n[image attached]`.trim();
                    }
                }
                return out;
            })
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
                    // The server maps known failures (e.g. a text-only model
                    // rejecting an image) to actionable guidance - show it
                    // verbatim instead of the generic apology.
                    renderer.fail(event.message || 'Sorry, I encountered an error. Please try again.');
                    if (event.message && /image/i.test(event.message)) {
                        showToast(event.message, 'error');
                    }
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
                Hello! I'm HomeStew. Ask me anything about your devices, and I'll search through your manuals to find answers.
            </div>
        </div>
    `;
    const input = document.getElementById('chat-input');
    if (input) input.value = '';
    clearPendingChatImages();
    // Replacing innerHTML clamps scrollTop to 0; pin explicitly so the fresh
    // session starts at its (single) message with auto-follow armed.
    scrollToBottom();
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
                else if (event.name === 'manage_calendar' || event.name === 'manage_devices')
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
            if (event.name === 'manage_devices') {
                // Device mutations report a one-line outcome; on success the
                // sidebar / Devices grid must show it live. loadDevices()
                // refreshes both plus the chat device filters.
                if (event.ok) {
                    entry.resultLine.textContent = event.summary || 'Done';
                    entry.resultLine.classList.remove('pending');
                    loadDevices();
                } else {
                    entry.resultLine.textContent = event.summary || 'Device action failed';
                    entry.resultLine.classList.add('failed');
                }
            } else if (event.name === 'manage_calendar') {
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
        if (content.includes('Hello! I\'m HomeStew')) return;
        
        messages.push({ role, content, images: contentEl._images || [] });
    });
    
    return messages;
}

function addMessageToChat(role, content, images = []) {
    const container = document.getElementById('chat-messages');
    
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${role}`;
    // Assistant answers are markdown; user messages stay plain escaped text.
    const contentHtml = role === 'assistant' ? renderMarkdown(content) : escapeHtml(content);
    // Attached photos render as thumbnails above the caption inside the
    // bubble. src is set programmatically: data URLs must never go through
    // innerHTML, and an empty caption collapses instead of showing a gap.
    const thumbsHtml = images.length
        ? `<div class="message-images">${images.map(() => '<img alt="Attached image">').join('')}</div>`
        : '';
    messageDiv.innerHTML = `
        <div class="message-content${role === 'assistant' ? ' markdown-body' : ''}">${thumbsHtml}${content ? `<div class="message-text">${contentHtml}</div>` : ''}</div>
    `;
    const contentEl = messageDiv.querySelector('.message-content');
    if (images.length) {
        contentEl.querySelectorAll('.message-images img').forEach((img, i) => {
            img.src = images[i];
        });
    }
    // _raw keeps the original markdown for history; _images keeps the data
    // URLs so a re-sent turn can include them (newest user turn only).
    contentEl._raw = content;
    if (images.length) contentEl._images = [...images];

    container.appendChild(messageDiv);
    // Sending always jumps to the newest message, even if the user was reading
    // further up when they hit Send.
    scrollToBottom();
}

function switchTab(tabName) {
    // On phones the tab was picked from the drawer - dismiss it so the
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

    // The Settings page re-reads the saved configuration on every visit, so
    // fields never show stale or half-typed values from a previous visit.
    if (tabName === 'settings') {
        loadSettingsPage();
    }
}

// Valid tab names, in markup order - also the fallback used by restoreActiveTab.
const TAB_NAMES = ['search', 'chat', 'devices', 'calendar', 'settings'];

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
        // The vision toggle is an independent saved setting: apply it even on
        // the unreachable/no-model paths below, so the attach & camera buttons
        // match Settings > AI regardless of probe outcome. (A failed status
        // request keeps whatever state was last known.)
        chatVisionEnabled = !!status.llm_supports_vision;
        updateChatImageControls();
    } catch (error) {
        // The status probe itself failed - treat as unavailable but keep the
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
//   .sidebar-collapsed - desktop icon-rail width (persisted, CSS-only effect)
//   .sidebar-open      - mobile drawer visibility (CSS media query decides)
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

// ---------------------------------------------------------------------------
// Collapsible sidebar sections (Recent Devices / Upcoming Events)
// ---------------------------------------------------------------------------

// Each .section header is a toggle button; the collapsed state of every
// section is remembered per key in localStorage so the choice survives
// reloads. Independent of the whole-sidebar collapse above.
function initCollapsibleSections() {
    document.querySelectorAll('.nav-sections .section-toggle').forEach((btn) => {
        const key = `sectionCollapsed:${btn.dataset.collapseKey}`;
        const section = btn.closest('.section');
        let collapsed = false;
        try { collapsed = localStorage.getItem(key) === '1'; } catch (e) { /* private mode */ }
        applySectionCollapsed(section, btn, collapsed);

        btn.addEventListener('click', () => {
            const nowCollapsed = !section.classList.contains('section-collapsed');
            applySectionCollapsed(section, btn, nowCollapsed);
            try { localStorage.setItem(key, nowCollapsed ? '1' : '0'); } catch (e) { /* private mode */ }
        });
    });
}

function applySectionCollapsed(section, btn, collapsed) {
    section.classList.toggle('section-collapsed', collapsed);
    // aria-expanded drives the chevron rotation in CSS; keep it truthful.
    btn.setAttribute('aria-expanded', String(!collapsed));
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
// localStorage mirror used before first paint, and the Settings > General
// radio group. The server-side copy is saved by the Settings page.
function setThemePref(pref) {
    if (!THEME_ORDER.includes(pref)) pref = 'auto';
    try { localStorage.setItem('theme', pref); } catch (e) { /* private mode */ }
    applyTheme();
    syncThemeRadios(pref);
}

// Reflect the active preference in the Settings > General radio group so the
// correct option is selected when the page loads (and after a save).
function syncThemeRadios(pref) {
    const checked = document.querySelector(
        `#settings-form input[name="theme"][value="${pref}"]`);
    if (checked) checked.checked = true;
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
// Settings page (tabs: General / AI / Notifications / Advanced)
// ---------------------------------------------------------------------------

// Shown inside the API key field when a key is already configured. The real
// value is never sent to the browser - an obscured placeholder just signals
// "a key exists"; leaving the field blank keeps it, typing replaces it.
const API_KEY_PLACEHOLDER = '********';

// ---------------------------------------------------------------------------
// Write-only secret fields (LLM API key, webhook URL / bearer token). The
// server never sends their values; a placeholder signals "configured", a
// blank field keeps the stored value, typing replaces it and Remove schedules
// deletion via clear_secrets on save. Synology webhook URLs embed their
// secret token in the query string, so the URL field is obscured too.
// ---------------------------------------------------------------------------
const SECRET_LABELS = {
    llm_api_key: 'API key',
    notify_webhook_url: 'Webhook URL',
    notify_webhook_token: 'Bearer token',
};

// Labels for the setting names reported in unreadable_secrets (server-side
// env-style names).
const UNREADABLE_SECRET_LABELS = {
    LLM_API_KEY: 'API key',
    NOTIFY_WEBHOOK_URL: 'webhook URL',
    NOTIFY_WEBHOOK_TOKEN: 'webhook token',
};

const SECRET_FIELDS = {
    llm_api_key: { inputId: 'settings-llm-api-key', hintId: 'settings-api-key-hint' },
    notify_webhook_url: { inputId: 'settings-notify-webhook-url', hintId: 'webhook-url-hint' },
    notify_webhook_token: { inputId: 'settings-notify-webhook-token', hintId: 'settings-webhook-token-hint' },
};

// Per-webhook-type help text for the URL field (composed with the
// configured/cleared status by webhookUrlHintText()).
const WEBHOOK_URL_HINTS = {
    generic: 'HomeStew POSTs a JSON body (event title, due date, device\u2026) to '
        + 'this URL. Works with custom receivers, Node-RED, Home Assistant '
        + 'webhooks and similar.',
    synology: 'Paste the full Incoming Webhook URL from DSM > Chat > Integration, '
        + 'including its token= parameter. HomeStew sends Chat\u2019s '
        + 'payload={"text": ...} format.',
};

// What the server reports as configured (from GET /api/settings) plus what
// the user marked for removal on the Settings page since it was loaded.
const secretsState = {
    configured: { llm_api_key: false, notify_webhook_url: false, notify_webhook_token: false },
    cleared: new Set(),
};

function webhookUrlHintText() {
    const type = document.getElementById('settings-notify-webhook-type').value;
    let text = WEBHOOK_URL_HINTS[type] || '';
    if (secretsState.cleared.has('notify_webhook_url')) {
        text += ' The stored URL will be removed when you save.';
    } else if (secretsState.configured.notify_webhook_url) {
        text += ' A URL is configured \u2014 leave blank to keep it, or type a new one to replace it.';
    }
    return text;
}

function secretHintText(name) {
    if (name === 'notify_webhook_url') return webhookUrlHintText();
    if (secretsState.cleared.has(name)) {
        return `${SECRET_LABELS[name]} will be removed when you save.`;
    }
    if (!secretsState.configured[name]) return '';
    return `${SECRET_LABELS[name]} is configured. Leave blank to keep it, or type a new one to replace it.`;
}

// Sync one secret input (placeholder + hint + Remove button) with the
// configured/cleared state. Never touches a value the user is typing.
function renderSecretField(name) {
    const field = SECRET_FIELDS[name];
    const input = document.getElementById(field.inputId);
    const hint = document.getElementById(field.hintId);
    const removeBtn = document.querySelector(
        `.secret-remove-btn[data-clear-secret="${name}"]`);

    if (!secretsState.cleared.has(name) && secretsState.configured[name]) {
        input.placeholder = API_KEY_PLACEHOLDER;
    } else {
        // No secret configured - the URL field keeps its example placeholder.
        input.placeholder =
            name === 'notify_webhook_url' ? 'https://example.com/hooks/homestew' : '';
    }
    hint.textContent = secretHintText(name);
    if (removeBtn) {
        removeBtn.hidden = !(secretsState.configured[name] && !secretsState.cleared.has(name));
    }
}

// "Remove" clicked: schedule deletion on save and blank the field so a stale
// placeholder can't be mistaken for a value.
function clearSecret(name) {
    secretsState.cleared.add(name);
    document.getElementById(SECRET_FIELDS[name].inputId).value = '';
    renderSecretField(name);
}

// Warning banner above the Settings tabs: plaintext storage (no key file
// mounted) and/or stored secrets that failed to decrypt.
function renderSecretsBanner(apiSettings) {
    const banner = document.getElementById('secrets-status-banner');
    const unreadable = apiSettings.unreadable_secrets || [];
    if (apiSettings.secrets_encrypted && !unreadable.length) {
        banner.hidden = true;
        return;
    }
    const parts = [];
    if (!apiSettings.secrets_encrypted) {
        parts.push('No secrets master key is available (the data directory '
            + 'may not be writable): API keys and tokens are saved as '
            + 'plaintext in settings.json. See the README \u201cSecrets\u201d '
            + 'section.');
    }
    if (unreadable.length) {
        const names = unreadable
            .map((key) => UNREADABLE_SECRET_LABELS[key] || key).join(', ');
        parts.push(`Stored secret(s) could not be decrypted with the current `
            + `key file and are treated as unset: ${names}. Re-enter them below.`);
    }
    banner.textContent = parts.join(' ');
    banner.classList.toggle('is-error', unreadable.length > 0);
    banner.hidden = false;
}

// Built-in prompt defaults fetched from the API, used by the "Restore
// Default" buttons in Advanced Settings. Keyed by setting name.
let promptDefaults = {};

const PROMPT_FIELD_IDS = {
    chat_system_prompt: 'settings-chat-system-prompt',
    search_tool_description: 'settings-search-tool-description',
    calendar_tool_description: 'settings-calendar-tool-description',
    device_tool_description: 'settings-device-tool-description',
};

function restorePromptDefault(settingName) {
    const fieldId = PROMPT_FIELD_IDS[settingName];
    if (!fieldId) return;
    document.getElementById(fieldId).value = promptDefaults[settingName] || '';
}

// Show one settings panel at a time ("general" | "ai" | ...) and highlight the
// matching tab. The form itself is shared, so Save submits every section.
function showSettingsSection(name) {
    document.querySelectorAll('.settings-tab-btn').forEach((btn) => {
        btn.classList.toggle('active', btn.dataset.settingsSection === name);
    });
    for (const panel of document.querySelectorAll('.settings-panel')) {
        panel.hidden = panel.id !== `settings-panel-${name}`;
    }
}

// Fill the Settings page from the saved configuration. Called each time the
// user switches to the tab, mirroring how Calendar reloads its events.
async function loadSettingsPage() {
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

    // Write-only secret fields: remember what the server reports as
    // configured, reset any pending removals and paint placeholder/hint/Remove
    // for each. Values themselves never reach the browser.
    secretsState.configured = {
        llm_api_key: !!settings.llm_api_key_set,
        notify_webhook_url: !!settings.notify_webhook_url_set,
        notify_webhook_token: !!settings.notify_webhook_token_set,
    };
    secretsState.cleared.clear();
    renderSecretsBanner(settings);

    const keyInput = document.getElementById('settings-llm-api-key');
    keyInput.value = '';
    renderSecretField('llm_api_key');

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

    // Vision toggle: only an explicit true checks it (missing key = off).
    document.getElementById('settings-llm-vision').checked =
        settings.llm_supports_vision === true;

    // Advanced settings: current prompts + built-in defaults (for Restore).
    promptDefaults = {
        chat_system_prompt: settings.chat_system_prompt_default || '',
        search_tool_description: settings.search_tool_description_default || '',
        calendar_tool_description: settings.calendar_tool_description_default || '',
        device_tool_description: settings.device_tool_description_default || '',
    };
    document.getElementById('settings-chat-system-prompt').value =
        settings.chat_system_prompt || '';
    document.getElementById('settings-search-tool-description').value =
        settings.search_tool_description || '';
    document.getElementById('settings-calendar-tool-description').value =
        settings.calendar_tool_description || '';
    document.getElementById('settings-device-tool-description').value =
        settings.device_tool_description || '';
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
    // The URL is write-only too: blank it (a previously typed, unsaved value
    // must not linger) and paint placeholder/hint from the configured state.
    document.getElementById('settings-notify-webhook-url').value = '';
    renderSecretField('notify_webhook_url');
    updateWebhookTypeHints();
    // SSL validation defaults to on; only an explicit false unchecks it.
    document.getElementById('settings-notify-webhook-verify-ssl').checked =
        settings.notify_webhook_verify_ssl !== false;

    // Account panel: password fields never keep a previous visit's values,
    // and the lockout numbers come from the server (clamped client-side too).
    document.getElementById('settings-current-password').value = '';
    document.getElementById('settings-new-password').value = '';
    document.getElementById('settings-confirm-password').value = '';
    const changeHint = document.getElementById('change-password-hint');
    if (changeHint) changeHint.textContent = '';
    document.getElementById('settings-auth-max-attempts').value =
        settings.auth_max_failed_attempts ?? 8;
    document.getElementById('settings-auth-lockout-minutes').value =
        settings.auth_lockout_minutes ?? 5;

    // Webhook bearer token: same placeholder treatment as the LLM API key -
    // the value is never sent to the client, blank on save keeps it.
    const tokenInput = document.getElementById('settings-notify-webhook-token');
    tokenInput.value = '';
    renderSecretField('notify_webhook_token');
    document.getElementById('webhook-test-hint').textContent = '';

    // Connection-test status describes a previous probe; clear it so reopening
    // Settings never shows a stale "Connection Successful".
    const connResult = document.getElementById('settings-conn-test-result');
    connResult.className = 'conn-test-result';
    connResult.textContent = '';

    // Backup & Restore hints describe a previous run; start each visit blank.
    document.getElementById('backup-hint').textContent = '';
    document.getElementById('restore-hint').textContent = '';

    // Advanced: master-key status + create/delete buttons.
    renderAdvancedKeyPanel(settings);

    // Always land on the General tab with the prompt accordion collapsed.
    document.getElementById('advanced-settings-section').open = false;
    showSettingsSection('general');
}

// Per-type help text for the webhook fields. Synology Chat incoming webhooks
// take their token in the URL and want form-encoded payload={text} bodies,
// so the generic bearer-token field is hidden for them. The URL hint also
// carries the configured/cleared status of the write-only URL field.
function updateWebhookTypeHints() {
    const type = document.getElementById('settings-notify-webhook-type').value;
    document.getElementById('webhook-url-hint').textContent = webhookUrlHintText();
    const tokenField = document.getElementById(
        'settings-notify-webhook-token').closest('.settings-field');
    tokenField.style.display = type === 'synology' ? 'none' : '';
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
    // Only send a freshly typed key. If blank, the server probes with the
    // saved key - but only when the URL matches the saved base URL (a foreign
    // URL never receives the stored credential).
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
            } catch (e) { /* activation expired - list is still populated */ }
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

// "Test Connection": probes the LLM server's /models endpoint with whatever
// base URL / API key are currently in the form (saved or freshly typed),
// reusing the same /api/settings/models call as the "Models" button. A 200
// means the server answered, so we report success even when it lists no
// models - reachability is what this checks. Shared by Settings and the wizard.
let connTestInFlight = false;

async function testLlmConnection({ urlId, keyId, btnId, resultId }) {
    if (connTestInFlight) return;

    const btn = document.getElementById(btnId);
    const result = document.getElementById(resultId);
    const baseUrl = document.getElementById(urlId).value.trim();
    if (!baseUrl) {
        result.className = 'conn-test-result error';
        result.textContent = 'Enter the LLM Base URL first.';
        return;
    }

    const payload = { llm_base_url: baseUrl };
    // Only send a freshly typed key. If blank, the server probes with the
    // saved key - but only when the URL matches the saved base URL (a foreign
    // URL never receives the stored credential).
    const keyInput = document.getElementById(keyId);
    if (keyInput && keyInput.value.trim()) {
        payload.llm_api_key = keyInput.value.trim();
    }

    connTestInFlight = true;
    btn.disabled = true;
    result.className = 'conn-test-result';
    result.textContent = 'Testing...';
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
        result.className = 'conn-test-result ok';
        result.textContent = 'Connection Successful';
    } catch (error) {
        result.className = 'conn-test-result error';
        result.textContent = error.message;
    } finally {
        connTestInFlight = false;
        btn.disabled = false;
    }
}

// Clear a stale "Connection Successful"/error label once the URL or key it
// described is edited, so the status never outlives the settings shown.
function resetConnTestOnEdit(urlId, keyId, resultId) {
    const result = document.getElementById(resultId);
    for (const id of [urlId, keyId]) {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', () => {
            result.className = 'conn-test-result';
            result.textContent = '';
        });
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

    // The URL field is write-only (blank = stored one). A test needs either a
    // freshly typed URL or a configured stored URL that wasn't just cleared.
    const url = document.getElementById('settings-notify-webhook-url').value.trim();
    if (!url && (secretsState.cleared.has('notify_webhook_url')
        || !secretsState.configured.notify_webhook_url)) {
        hint.textContent = 'Enter the Webhook URL first, then send a test.';
        return;
    }

    const payload = {};
    if (url) payload.webhook_url = url;
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
        hint.textContent = 'Test notification sent - check your webhook receiver.';
    } catch (error) {
        hint.textContent = error.message;
    } finally {
        webhookTestInFlight = false;
        btn.disabled = false;
    }
}

// ---------------------------------------------------------------------------
// Backup & Restore (Settings > General)
// ---------------------------------------------------------------------------

// Build the archive server-side, then trigger a plain browser download via a
// temporary <a download>. Two-step (create -> fetch file) so the hint can show
// exactly what was captured before anything downloads.
async function createBackup() {
    if (!beginOp('backup-create')) return;

    const btn = document.getElementById('backup-create-btn');
    const hint = document.getElementById('backup-hint');
    const includeManuals = document.getElementById('backup-include-manuals').checked;

    btn.disabled = true;
    hint.textContent = 'Creating backup...';
    try {
        const formData = new FormData();
        formData.append('include_manuals', includeManuals ? 'true' : 'false');

        const response = await fetch('/api/backup/create', { method: 'POST', body: formData });
        if (!response.ok) throw new Error(await errorDetailFrom(response, 'Backup failed'));

        const info = await response.json();
        const parts = [`${info.devices} device(s)`, `${info.calendar_events} calendar event(s)`];
        if (includeManuals) parts.push(`${info.manuals} manual(s)`);

        // Kick off the download; the server sends Content-Disposition: attachment.
        const link = document.createElement('a');
        link.href = info.url;
        link.download = info.filename;
        document.body.appendChild(link);
        link.click();
        link.remove();

        hint.textContent = `Backup ready (${parts.join(', ')}). Your browser should start the download.`;
        if (info.missing_files && info.missing_files.length) {
            showToast(
                `${info.missing_files.length} manual file(s) were missing on disk and are not in the backup`,
                'error',
                info.missing_files.join('\n'),
            );
        } else {
            showToast('Backup created', 'success');
        }
    } catch (error) {
        console.error('Backup failed:', error);
        hint.textContent = '';
        showToast(error.message || 'Backup failed', 'error');
    } finally {
        btn.disabled = false;
        endOp('backup-create');
    }
}

function triggerRestoreFilePick() {
    const input = document.getElementById('restore-file-input');
    input.value = ''; // allow re-selecting the same file after a cancel
    input.click();
}

// Restore an archive chosen via the hidden input. Replace mode is destructive,
// so it asks for confirmation first; afterwards the sidebar/devices/calendar
// are refreshed and background search re-indexing is polled into the hint.
async function handleRestoreFileSelected(event) {
    const file = event.target.files[0];
    if (!file) return;

    const mode = document.querySelector('input[name="restore-mode"]:checked')?.value || 'merge';
    if (mode === 'replace' && !confirm(
        'Replace mode deletes ALL current devices, manuals and calendar events before restoring. Continue?'
    )) {
        return;
    }
    if (!beginOp('backup-restore')) return;

    const btn = document.getElementById('restore-btn');
    const hint = document.getElementById('restore-hint');
    btn.disabled = true;
    hint.textContent = `Restoring "${file.name}" (${mode})...`;
    try {
        const formData = new FormData();
        formData.append('file', file);
        formData.append('mode', mode);

        const response = await fetch('/api/backup/restore', { method: 'POST', body: formData });
        if (!response.ok) throw new Error(await errorDetailFrom(response, 'Restore failed'));

        const result = await response.json();
        hint.textContent = `Restored ${result.devices_created} device(s), ` +
            `${result.manuals_restored} manual(s), ${result.events_restored} event(s)` +
            (result.skipped ? ` (${result.skipped} already present, skipped)` : '') + '.';
        showToast('Backup restored', 'success');

        // Refresh everything the restore may have changed.
        await loadDevices();
        if (document.getElementById('calendar-tab').classList.contains('active')) {
            loadCalendarEvents();
        }

        if (result.index_total > 0) pollRestoreIndex(hint);
    } catch (error) {
        console.error('Restore failed:', error);
        hint.textContent = '';
        showToast(error.message || 'Restore failed', 'error');
    } finally {
        btn.disabled = false;
        endOp('backup-restore');
    }
}

// Poll the background search re-index that follows a restore until it finishes,
// showing progress in the restore hint line.
function pollRestoreIndex(hint) {
    const tick = async () => {
        try {
            const response = await fetch('/api/backup/restore-status');
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const status = await response.json();
            if (status.total === 0) return; // nothing to wait for

            hint.textContent = `Rebuilding search index... ${status.done}/${status.total}`;
            if (status.running) {
                setTimeout(tick, 1500);
            } else {
                hint.textContent = `Restore complete - search index rebuilt (${status.done}/${status.total}).`;
            }
        } catch (e) {
            // A lost status request must not strand the spinner text.
            hint.textContent = 'Restore complete.';
        }
    };
    setTimeout(tick, 500);
}

// Clicking (or keyboard-opening) the model dropdown refreshes its options
// from the server first, then opens the list - so users always see fresh
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

    // Keyboard access: Enter / Space / ArrowDown open the picker natively -
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
        // Always sent: a plain boolean, so unchecking must persist too.
        llm_supports_vision: document.getElementById('settings-llm-vision').checked,
        // Prompts are always sent; the server treats a blank value as
        // "restore the built-in default".
        chat_system_prompt: document.getElementById('settings-chat-system-prompt').value.trim(),
        search_tool_description: document.getElementById('settings-search-tool-description').value.trim(),
        calendar_tool_description: document.getElementById('settings-calendar-tool-description').value.trim(),
        device_tool_description: document.getElementById('settings-device-tool-description').value.trim(),
        notify_enabled: document.getElementById('settings-notify-enabled').checked,
        notify_check_interval_minutes:
            clampInt(document.getElementById('settings-notify-interval').value, 1, 1440, 15),
        notify_lead_value:
            clampInt(document.getElementById('settings-notify-lead-value').value, 1, 365, 24),
        notify_lead_unit: document.getElementById('settings-notify-lead-unit').value,
        notify_webhook_enabled: document.getElementById('settings-notify-webhook-enabled').checked,
        notify_webhook_type: document.getElementById('settings-notify-webhook-type').value,
        notify_webhook_verify_ssl:
            document.getElementById('settings-notify-webhook-verify-ssl').checked,
        auth_max_failed_attempts:
            clampInt(document.getElementById('settings-auth-max-attempts').value, 1, 100, 8),
        auth_lockout_minutes:
            clampInt(document.getElementById('settings-auth-lockout-minutes').value, 1, 240, 5),
    };
    // The webhook URL is write-only like the key: only a freshly typed value
    // is sent; blank keeps the stored one (removal goes through clear_secrets).
    const webhookUrl = document.getElementById('settings-notify-webhook-url').value.trim();
    if (webhookUrl) payload.notify_webhook_url = webhookUrl;
    // Blank API key field means "keep the existing key" - omit it entirely.
    // (The obscured placeholder is only a visual hint, never a value.)
    const apiKey = document.getElementById('settings-llm-api-key').value.trim();
    if (apiKey) payload.llm_api_key = apiKey;

    // Same keep-existing semantics for the webhook bearer token.
    const webhookToken = document.getElementById('settings-notify-webhook-token').value.trim();
    if (webhookToken) payload.notify_webhook_token = webhookToken;

    // Secrets marked "Remove" since the page loaded; the server applies
    // these after the updates above.
    if (secretsState.cleared.size) payload.clear_secrets = [...secretsState.cleared];

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
        // Apply the (possibly changed) theme immediately so the new colors are
        // visible without a reload.
        setThemePref(payload.theme);
        showToast('Settings saved');
        // Reflect a vision-toggle change in the chat right away: the attach /
        // camera buttons appear or disappear without waiting for a reload.
        chatVisionEnabled = payload.llm_supports_vision;
        updateChatImageControls();
        // Re-probe the chat in case the LLM settings were fixed here: its
        // warning banner should clear even though it is on another tab.
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

// ---------------------------------------------------------------------------
// Secrets master key management (Settings > Advanced + first-run wizard)
// ---------------------------------------------------------------------------

// Last GET /api/settings payload, kept so the Advanced panel can re-render
// after a create/delete without another round trip.
let lastApiSettings = null;

function keyStatusText(s) {
    if (s.secrets_key_source === 'mounted') {
        return 'Key source: mounted key file (SECRETS_KEY_FILE). HomeStew does not manage this key - remove the container\u2019s secret mount to change it.';
    }
    if (s.secrets_key_source === 'generated') {
        return 'Key source: HomeStew-managed key at /data/.secrets_key. Secrets are encrypted at rest with it.';
    }
    return 'No master key found. Secrets are stored as plaintext until you create one.';
}

// Paint the Advanced panel's status box + buttons from a settings payload.
function renderAdvancedKeyPanel(s) {
    lastApiSettings = s;
    const statusBox = document.getElementById('advanced-key-status');
    const createBtn = document.getElementById('create-key-btn');
    const deleteBtn = document.getElementById('delete-key-btn');
    const hint = document.getElementById('advanced-key-hint');

    statusBox.textContent = keyStatusText(s);
    statusBox.classList.toggle('is-ok', s.secrets_encrypted);
    statusBox.classList.toggle('is-warn', !s.secrets_encrypted);

    createBtn.hidden = !!s.secrets_encrypted;
    deleteBtn.hidden = !s.secrets_key_deletable;
    hint.textContent = s.secrets_encrypted
        ? 'Deleting the key makes stored secrets unreadable until a new key is created and they are re-entered.'
        : 'Create one now, or mount your own key file at SECRETS_KEY_FILE (see README \u201cSecrets\u201d).';
}

// Change the account password (Settings > Advanced). Separate from the
// settings form: it verifies the current password and re-issues this
// browser's cookie, so saving does not sign you out. Other browsers get
// logged out on their next request - the signing key derives from the hash.
async function handleChangePassword() {
    const hint = document.getElementById('change-password-hint');
    const current = document.getElementById('settings-current-password').value;
    const next = document.getElementById('settings-new-password').value;
    const confirm = document.getElementById('settings-confirm-password').value;
    if (!current) { hint.textContent = 'Enter the current password first.'; return; }
    if (next.length < 8) { hint.textContent = 'The new password must be at least 8 characters.'; return; }
    if (next !== confirm) { hint.textContent = 'The new passwords do not match.'; return; }
    const btn = document.getElementById('change-password-btn');
    btn.disabled = true;
    try {
        await authRequest('password', 'PUT', {
            current_password: current, new_password: next, confirm_password: confirm,
        });
        document.getElementById('settings-current-password').value = '';
        document.getElementById('settings-new-password').value = '';
        document.getElementById('settings-confirm-password').value = '';
        hint.textContent = 'Password changed. Other signed-in devices were logged out.';
    } catch (error) {
        hint.textContent = error.message;
    } finally {
        btn.disabled = false;
    }
}

// Shared POST helper for the two key endpoints: returns the JSON body on
// success, or throws with the server's detail message.
async function postSecretsKeyAction(endpoint) {
    const response = await fetch(`/api/settings/${endpoint}`, { method: 'POST' });
    let data = null;
    try { data = await response.json(); } catch (e) { /* no body */ }
    if (!response.ok) {
        throw new Error((data && data.detail) || `HTTP ${response.status}`);
    }
    return data;
}

// Refresh the Settings page's key UI + banner from the server after an action.
async function refreshSettingsKeyState() {
    try {
        const response = await fetch('/api/settings');
        if (!response.ok) return;
        const s = await response.json();
        renderAdvancedKeyPanel(s);
        renderSecretsBanner(s);
        // Creating/deleting the key changes which secrets are readable, so
        // re-paint the write-only fields' placeholder/hint/Remove state.
        // (renderSecretField never touches input values, so typing survives.)
        secretsState.configured = {
            llm_api_key: !!s.llm_api_key_set,
            notify_webhook_url: !!s.notify_webhook_url_set,
            notify_webhook_token: !!s.notify_webhook_token_set,
        };
        for (const name of Object.keys(SECRET_FIELDS)) renderSecretField(name);
    } catch (e) { /* leave the panel as-is on a failed refresh */ }
}

async function handleCreateKeyFromSettings() {
    const btn = document.getElementById('create-key-btn');
    btn.disabled = true;
    try {
        await postSecretsKeyAction('secrets-key/create');
        showToast('Master key created \u2014 secrets are encrypted at rest');
        await refreshSettingsKeyState();
    } catch (error) {
        showToast('Could not create the key', 'error', error.message);
    } finally {
        btn.disabled = false;
    }
}

async function handleDeleteKeyFromSettings() {
    if (!confirm('Delete the secrets master key?\n\nStored secrets will become unreadable (treated as unset) until you create a new key and re-enter them. This cannot be undone.')) {
        return;
    }
    const btn = document.getElementById('delete-key-btn');
    btn.disabled = true;
    try {
        await postSecretsKeyAction('secrets-key/delete');
        showToast('Master key deleted \u2014 re-enter your secrets after creating a new one', 'error');
        await refreshSettingsKeyState();
    } catch (error) {
        showToast('Could not delete the key', 'error', error.message);
    } finally {
        btn.disabled = false;
    }
}

// ---------------------------------------------------------------------------
// First-run setup wizard (encryption key -> AI/LLM -> first device)
// ---------------------------------------------------------------------------

// The steps in offer order. A step is shown only while its condition still
// applies AND it has no persisted resolution; skipping or saving a step
// records the outcome on the server (setup_steps), so resolved steps never
// come back - even after a container restart.
const WIZARD_STEPS = [
    { id: 'secrets_key', title: 'Encrypt stored credentials', saveLabel: 'Create Key' },
    { id: 'llm', title: 'Connect your AI model', saveLabel: 'Save & Continue' },
    { id: 'device', title: 'Add your first device', saveLabel: 'Add Device' },
];

// Steps pending for this page load + index of the visible one.
let wizardQueue = [];
let wizardIndex = 0;
// Guards both footer buttons against double-clicks during a server call.
let wizardInFlight = false;

function wizardPendingSteps(s) {
    const resolved = s.setup_steps || {};
    return WIZARD_STEPS.filter((step) => {
        if (resolved[step.id]) return false;
        if (step.id === 'secrets_key') return s.secrets_key_source === 'none';
        if (step.id === 'llm') return !s.llm_configured;
        if (step.id === 'device') return devices.length === 0;
        return false;
    }).map((step) => step.id);
}

// Shown after loadDevices()/settings are available: opens the wizard on the
// first pending step, or stays closed when everything is set up or skipped.
async function checkSetupWizard() {
    try {
        const response = await fetch('/api/settings');
        if (!response.ok) return;
        const s = await response.json();
        wizardQueue = wizardPendingSteps(s);
        // Seed the chat image controls + the wizard's vision checkbox from
        // the saved config before any tab renders, so the attach/camera
        // buttons never flash in for a text-only model. (checkChatModelStatus
        // re-applies this whenever the chat tab opens.)
        chatVisionEnabled = !!s.llm_supports_vision;
        updateChatImageControls();
        document.getElementById('wizard-llm-vision').checked = chatVisionEnabled;
        if (!wizardQueue.length) return;
        // Prefill the AI step from what the server has (defaults or env).
        document.getElementById('wizard-llm-base-url').value = s.llm_base_url || '';
        seedWizardModelSelect(s.llm_model || '');
        wizardIndex = 0;
        showWizardStep();
        document.getElementById('setup-wizard-modal').style.display = 'flex';
    } catch (e) { /* offline/server starting up - no wizard this load */ }
}

function currentWizardStepId() {
    return wizardQueue[wizardIndex];
}

// Paint the visible step, its footer label and the progress dots. Every step
// shares the same modal size and Skip/Save controls by construction.
function showWizardStep() {
    const id = currentWizardStepId();
    const meta = WIZARD_STEPS.find((step) => step.id === id);
    for (const step of WIZARD_STEPS) {
        document.getElementById(`wizard-step-${step.id}`).hidden = step.id !== id;
    }
    document.getElementById('wizard-save-btn').textContent = meta.saveLabel;
    renderWizardProgress();
    setWizardError('');
}

function renderWizardProgress() {
    const wrap = document.getElementById('wizard-progress');
    wrap.innerHTML = '';
    wizardQueue.forEach((id, i) => {
        const meta = WIZARD_STEPS.find((step) => step.id === id);
        const dot = document.createElement('div');
        dot.className = 'wizard-dot'
            + (i < wizardIndex ? ' is-done' : '')
            + (i === wizardIndex ? ' is-current' : '');
        dot.setAttribute('role', 'listitem');
        dot.setAttribute('aria-label', `${meta.title} (${i < wizardIndex ? 'done' : i === wizardIndex ? 'current' : 'upcoming'})`);
        dot.title = meta.title;
        wrap.appendChild(dot);
    });
}

function setWizardError(message) {
    const el = document.getElementById('wizard-error');
    el.textContent = message || '';
    el.hidden = !message;
}

// Persist how a step ended. Skipping is stored exactly like saving: the step
// is resolved and will not be re-offered on the next launch.
async function resolveWizardStep(step, resolution) {
    const response = await fetch('/api/settings/setup-steps/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ step, resolution }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
}

// After a skip/save: move to the next pending step or finish the wizard.
function advanceWizard() {
    wizardIndex += 1;
    if (wizardIndex < wizardQueue.length) {
        showWizardStep();
        return;
    }
    document.getElementById('setup-wizard-modal').style.display = 'none';
}

async function handleWizardSkip() {
    if (wizardInFlight) return;
    const id = currentWizardStepId();
    wizardInFlight = true;
    setWizardButtonsDisabled(true);
    try {
        await resolveWizardStep(id, 'skipped');
        advanceWizard();
    } catch (error) {
        // The step stays open; worst case it is offered again next load.
        setWizardError(`Could not record the skip: ${error.message}`);
    } finally {
        wizardInFlight = false;
        setWizardButtonsDisabled(false);
    }
}

function setWizardButtonsDisabled(disabled) {
    document.getElementById('wizard-skip-btn').disabled = disabled;
    document.getElementById('wizard-save-btn').disabled = disabled;
}

// Save button: run the current step's action, record it as saved, advance.
async function handleWizardSave() {
    if (wizardInFlight) return;
    const id = currentWizardStepId();
    wizardInFlight = true;
    setWizardButtonsDisabled(true);
    setWizardError('');
    try {
        if (id === 'secrets_key') await wizardCreateKey();
        else if (id === 'llm') await wizardSaveLlm();
        else if (id === 'device') await wizardCreateDevice();
        await resolveWizardStep(id, 'saved');
        advanceWizard();
    } catch (error) {
        setWizardError(error.message || String(error));
    } finally {
        wizardInFlight = false;
        setWizardButtonsDisabled(false);
    }
}

// --- Step 1: encryption key -------------------------------------------------

async function wizardCreateKey() {
    await postSecretsKeyAction('secrets-key/create');
    showToast('Master key created \u2014 secrets are encrypted at rest', 'success');
}

// --- Step 2: AI / LLM integration --------------------------------------------

// The model field is a plain <select> like in Settings: seed it with the
// saved/default model so something shows before the list is fetched.
function seedWizardModelSelect(model) {
    const select = document.getElementById('wizard-llm-model');
    select.innerHTML = '';
    const option = document.createElement('option');
    option.value = model;
    option.textContent = model || 'No model selected';
    select.appendChild(option);
}

// Mirror of fetchLlmModels() for the wizard's own field ids: probes
// /api/settings/models with whatever URL/key are typed (unsaved is fine).
let wizardModelRefreshInFlight = false;

async function fetchWizardLlmModels() {
    if (wizardModelRefreshInFlight) return;
    const hint = document.getElementById('wizard-model-list-hint');
    const btn = document.getElementById('wizard-fetch-models-btn');
    const select = document.getElementById('wizard-llm-model');

    const baseUrl = document.getElementById('wizard-llm-base-url').value.trim();
    if (!baseUrl) {
        hint.textContent = 'Enter the LLM Base URL first, then pick a model.';
        return;
    }
    const payload = { llm_base_url: baseUrl };
    // Only send a freshly typed key - the server never ships the stored one
    // to a foreign URL anyway (same rule as the Settings page probe).
    const keyInput = document.getElementById('wizard-llm-api-key');
    if (keyInput.value.trim()) payload.llm_api_key = keyInput.value.trim();

    wizardModelRefreshInFlight = true;
    btn.disabled = true;
    hint.textContent = 'Loading models...';
    // Read before the try: the catch below restores the previous option and
    // must still work when the fetch itself throws.
    const current = select.value;
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
        const current = select.value;
        select.innerHTML = '';
        for (const model of data.models) {
            const option = document.createElement('option');
            option.value = model;
            option.textContent = model;
            select.appendChild(option);
        }
        if (!data.models.length) {
            const empty = document.createElement('option');
            empty.value = '';
            empty.textContent = 'No models available';
            select.appendChild(empty);
        } else if (data.models.includes(current)) {
            select.value = current;
        }
        hint.textContent = data.models.length
            ? `Loaded ${data.models.length} model${data.models.length === 1 ? '' : 's'} from the server.`
            : 'Server responded, but no models were listed.';
    } catch (error) {
        select.innerHTML = '';
        const empty = document.createElement('option');
        empty.value = current;
        empty.textContent = current || 'No models available';
        select.appendChild(empty);
        hint.textContent = error.message;
    } finally {
        wizardModelRefreshInFlight = false;
        btn.disabled = false;
    }
}

async function wizardSaveLlm() {
    const baseUrl = document.getElementById('wizard-llm-base-url').value.trim();
    const model = document.getElementById('wizard-llm-model').value.trim();
    if (!baseUrl) throw new Error('Enter the LLM Base URL (e.g. http://localhost:11434/v1).');
    if (!/^https?:\/\//i.test(baseUrl)) {
        throw new Error('The base URL must start with http:// or https://');
    }
    if (!model) throw new Error('Pick a model - click "Models" to load the list from your server.');

    const payload = {
        llm_base_url: baseUrl,
        llm_model: model,
        // Always sent so unchecking in the wizard persists too.
        llm_supports_vision: document.getElementById('wizard-llm-vision').checked,
    };
    // Blank key = none configured (local servers ignore it); omitted so the
    // server keeps any stored key untouched.
    const apiKey = document.getElementById('wizard-llm-api-key').value.trim();
    if (apiKey) payload.llm_api_key = apiKey;

    const response = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        let detail = `HTTP ${response.status}`;
        try {
            const data = await response.json();
            if (data.detail) detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
        } catch (e) { /* non-JSON error body */ }
        throw new Error(detail);
    }
    showToast('AI model saved', 'success');
    // A fixed configuration should clear the chat warning banner right away.
    if (document.getElementById('chat-tab').classList.contains('active')) {
        checkChatModelStatus();
    }
}

// --- Step 3: first device -----------------------------------------------------

async function wizardCreateDevice() {
    const name = document.getElementById('wizard-device-name').value.trim();
    const brand = document.getElementById('wizard-device-brand').value.trim();
    const model = document.getElementById('wizard-device-model').value.trim();
    if (!name) throw new Error('Give the device a name (e.g. "Living Room AC").');
    if (!brand || !model) throw new Error('Brand and Model are required - they drive manual downloads.');

    const response = await fetch('/api/devices', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            name,
            brand,
            model,
            description: document.getElementById('wizard-device-description').value.trim() || null,
        }),
    });
    if (!response.ok) throw new Error('Failed to add the device.');
    showToast('Device added', 'success');
    await loadDevices();
}
