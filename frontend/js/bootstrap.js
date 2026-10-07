// HomeStew - App bootstrap: startup entry point, per-realm wiring order,
// and the shared Escape-key modal handling.

document.addEventListener('DOMContentLoaded', () => {
    // Auth gate listeners must exist before anything else can run: the rest
    // of the wiring only happens once a session is confirmed (see wireAll()).
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

// Runs every realm's own wiring function, then the shared Escape handler.
// This replaces the single setupEventListeners() from the old app.js: each
// realm now owns its listeners in its own file, and this is the one place
// that fixes their order. Order only matters where a wire function reads DOM
// another has prepared; today they are independent, so this list is stable.
function wireAll() {
    wireChromeEvents();
    wireDevicesEvents();
    wireCalendarEvents();
    wireChatEvents();
    wireSearchEvents();
    wireSettingsEvents();
    wireAuthEvents();
    wireWizardEvents();
    wireMarkdownLinks();
    wireEscape();
}

async function initializeApp() {
    // Nothing below may run without a session: the loaders would only 401
    // (and several swallow errors), so gate first and stop. On successful
    // create/login the page reloads and this runs again, authenticated.
    if (!(await checkAuth())) return;
    await loadDevices();
    wireAll();
    // Restore the tab from before a page refresh (URL hash first, then the
    // last tab persisted to localStorage) so F5 no longer dumps the user on
    // the Search tab.
    restoreActiveTab();
    // A background index job may still be running from before a refresh (or
    // started in another tab): resume its progress toast if so.
    resumeIndexMonitorIfRunning();
    // One-time setup wizard (encryption key / AI model / first device).
    // Runs after loadDevices() so the "first device" step can see whether
    // any devices exist yet; skipped/saved steps are persisted server-side.
    checkSetupWizard();
}

// ---------------------------------------------------------------------------
// Shared Escape handling. Kept here (not in a realm) because it is cross-modal
// and ORDER-SENSITIVE: an open info popover dismisses first, then the Fetch
// Manuals dialog (which needs its close function to release the op lock, and
// cancels an in-progress search-terms edit before closing), then the stacked
// modals top-down. The setup wizard is deliberately absent - skipping/saving a
// step must persist its resolution, so it can't be dismissed with Escape.
// ---------------------------------------------------------------------------

function wireEscape() {
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        // An open settings info popover dismisses first.
        const openPop = document.querySelector('.info-wrap.open');
        if (openPop) {
            closeInfoPopover(openPop);
            openPop.querySelector('.info-btn')?.focus();
            return;
        }
        // The fetch dialog needs its close function (releases the op lock),
        // so it is handled before the generic display:none loop below. While
        // its search-terms box is being edited, Escape cancels the edit first.
        const fetchModal = document.getElementById('fetch-manuals-modal');
        if (fetchModal.style.display === 'flex') {
            if (!document.getElementById('fetch-search-input').readOnly) {
                cancelChangeSearchTerms();
                return;
            }
            closeFetchManualsModal();
            return;
        }
        // The icon picker comes first: Escape closes it before the editor
        // dialog it is stacked on top of (closeIconPicker also clears the
        // picker state so a stale query never survives a dismissal).
        for (const id of ['icon-picker-modal', 'new-device-choice-modal', 'add-device-modal', 'edit-device-modal', 'event-modal']) {
            const modal = document.getElementById(id);
            if (modal.style.display === 'flex') {
                if (id === 'icon-picker-modal') closeIconPicker();
                else modal.style.display = 'none';
                return;
            }
        }
        closeDrawer();
    });
}
