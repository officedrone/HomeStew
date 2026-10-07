// HomeStew - App chrome: tab routing, theme, sidebar rail/drawer, info popovers.


// Name of the tab currently rendered. Used to tell a real navigation apart
// from a re-activation of the tab we are already on (which must not push a
// duplicate history entry).
let currentTabName = null;

function switchTab(tabName, pushHistory = true) {
    // On phones the tab was picked from the drawer - dismiss it so the
    // content is visible right away.
    closeDrawer();

    // Keep the URL hash in sync with the active tab: it survives F5 and lets
    // browser back/forward walk through the tabs visited this session. A real
    // navigation PUSHES a new history entry (so Back returns to the previous
    // HomeStew tab instead of leaving the app); re-activating the current tab,
    // or activating one we arrived at via Back/Forward (pushHistory=false),
    // never adds an entry.
    const newHash = `#${tabName}`;
    if (location.hash !== newHash) {
        if (history.pushState && pushHistory) history.pushState(null, '', newHash);
        else if (history.replaceState) history.replaceState(null, '', newHash);
        else location.hash = tabName;
    }
    currentTabName = tabName;

    // Update tab buttons
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tabName);
    });
    
    // Update tab content
    document.querySelectorAll('.tab-content').forEach(content => {
        content.classList.toggle('active', content.id === `${tabName}-tab`);
    });

    // The Dashboard recomputes its stat cards on every visit so the numbers
    // never go stale between device/event changes made elsewhere in the app.
    if (tabName === 'dashboard') {
        refreshDashboard();
    }

    // The "attach a photo" hint from the New Device flow is one-shot: it
    // belongs to the visit it was shown on, so any tab change clears it
    // (chooseNewDeviceChat re-shows it AFTER switching here).
    hideChatAttachTip();

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
const TAB_NAMES = ['dashboard', 'chat', 'devices', 'calendar', 'search', 'settings'];

// Resolve the tab to show from the current URL. The hash is the single source
// of truth: with no (or an unknown) hash we always land on Dashboard, so a
// bare '/' or '/index.html' open never drops the user on some stale tab.
function tabFromLocation() {
    const hash = location.hash.replace('#', '');
    return TAB_NAMES.includes(hash) ? hash : 'dashboard';
}

// Re-activate the tab named by the URL, defaulting to Dashboard when there is
// no usable hash. Always routes through switchTab() so the restored tab runs
// its load hooks; pushHistory=false because we are syncing TO the URL, not
// navigating within it (this must not add a history entry on first paint).
function restoreActiveTab() {
    switchTab(tabFromLocation(), false);
}

// Browser Back / Forward change the hash without running our click handlers,
// so render whichever tab the new URL names. pushHistory=false: we arrived via
// history and must not push another entry (that would corrupt the back-stack).
window.addEventListener('popstate', () => {
    switchTab(tabFromLocation(), false);
});
// Older browsers / manual hash edits fire hashchange instead of popstate.
window.addEventListener('hashchange', () => {
    if (location.hash.replace('#', '') !== currentTabName) {
        switchTab(tabFromLocation(), false);
    }
});

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
// Collapsible sections (Dashboard's Recent Devices / Upcoming feeds)
// ---------------------------------------------------------------------------

// Each .section header is a toggle button; the collapsed state of every
// section is remembered per key in localStorage so the choice survives
// reloads. Independent of the whole-sidebar collapse above. The sections now
// live inside the Dashboard cards, so the selector is no longer scoped to a
// sidebar container - the stored sectionCollapsed:* keys carry over as-is.
function initCollapsibleSections() {
    document.querySelectorAll('.section-toggle').forEach((btn) => {
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
// Settings info popovers (the circled "i" next to a label or subheading).
// The description text lives inside .info-popover; CSS shows it on hover /
// focus for pointer users, while click/tap toggles the .open class here so
// touch devices get the same information. Popovers are position:fixed and
// placed by JS so they never clip against scrolling containers.
// ---------------------------------------------------------------------------
function placeInfoPopover(wrap) {
    const btn = wrap.querySelector('.info-btn');
    const pop = wrap.querySelector('.info-popover');
    if (!btn || !pop) return;
    // The popover may be display:none when measuring (click path); force it
    // visible but invisible just long enough to read its size.
    const wasHidden = getComputedStyle(pop).display === 'none';
    if (wasHidden) {
        pop.style.visibility = 'hidden';
        pop.style.display = 'block';
    }
    const r = btn.getBoundingClientRect();
    const pw = pop.offsetWidth;
    const ph = pop.offsetHeight;
    let left = r.left + r.width / 2 - pw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
    let top = r.bottom + 6;
    if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 6);
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
    // CSS only reveals hover-popovers once they carry this marker, so the box
    // can never flash at its unpositioned static location for one frame.
    pop.dataset.placed = '1';
    if (wasHidden) {
        pop.style.display = '';
        pop.style.visibility = '';
    }
}

function closeInfoPopover(wrap) {
    wrap.classList.remove('open');
}

function closeAllInfoPopovers() {
    document.querySelectorAll('.info-wrap.open').forEach(closeInfoPopover);
}

function initInfoPopovers() {
    const wraps = Array.from(document.querySelectorAll('.info-wrap'));
    if (!wraps.length) return;
    for (const wrap of wraps) {
        const btn = wrap.querySelector('.info-btn');
        if (!btn) continue;
        // Hover/focus display is pure CSS; JS only needs to position the box
        // before it appears.
        wrap.addEventListener('mouseenter', () => placeInfoPopover(wrap));
        btn.addEventListener('focus', () => placeInfoPopover(wrap));
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const willOpen = !wrap.classList.contains('open');
            closeAllInfoPopovers();
            if (willOpen) {
                wrap.classList.add('open');
                placeInfoPopover(wrap);
            }
        });
    }
    // Clicking anywhere outside an icon/popover dismisses a click-opened
    // popover. Clicks inside stay live so the text can be selected/copied.
    document.addEventListener('click', (e) => {
        if (!e.target.closest || e.target.closest('.info-wrap')) return;
        closeAllInfoPopovers();
    });
    // Keep fixed-position popovers glued to their icon while scrolling or
    // after a resize (the settings page scrolls inside the main content).
    const reposition = () => {
        document.querySelectorAll('.info-wrap.open, .info-wrap:hover')
            .forEach(placeInfoPopover);
    };
    window.addEventListener('scroll', reposition, true);
    window.addEventListener('resize', reposition);
}

// ---------------------------------------------------------------------------
// Chrome wiring (was part of setupEventListeners in the single-file app).
// ---------------------------------------------------------------------------

function wireChromeEvents() {
    // Tab switching. The buttons now contain an icon/label, so read the tab
    // name from currentTarget - e.target can be the inner <svg>/<span>.
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', (e) => switchTab(e.currentTarget.dataset.tab));
    });

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

    initInfoPopovers();
}
