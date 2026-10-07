// HomeStew - Shared utilities: toasts, escaping, error extraction, click dedupe
// and in-flight operation locks.


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

// Feather-style icons for the square icon-only buttons on device cards.
const CARD_ACTION_ICONS = {
    edit: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"></path></svg>',
    trash: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>',
    check: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>',
    refresh: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>',
    download: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>'
};

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

// Show a transient notification. With options.persistent the toast gets a ✕
// button and stays until dismissed (used by the index progress monitor); all
// other call sites keep the auto-remove timer. options.id reuses an existing
// toast with that id instead of stacking a new one - the progress monitor
// updates its single toast in place via updateToast().
function showToast(message, type = 'success', detailedError = null, options = {}) {
    const container = document.getElementById('toast-container');

    // Same-id persistent toasts are updated rather than duplicated.
    if (options.id) {
        const existing = container.querySelector(`[data-toast-id="${CSS.escape(options.id)}"]`);
        if (existing) return updateToast(existing, message);
    }
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    if (options.id) toast.dataset.toastId = options.id;

    // Create message content
    const messageDiv = document.createElement('div');
    messageDiv.className = 'toast-message';
    messageDiv.textContent = message;
    toast.appendChild(messageDiv);

    // Optional progress bar (index jobs): a thin track whose fill width is the
    // done/total percentage. Created up-front so updateToast() only nudges it.
    if (options.progress) {
        const track = document.createElement('div');
        track.className = 'toast-progress';
        const fill = document.createElement('div');
        fill.className = 'toast-progress-fill';
        fill.style.width = '0%';
        track.appendChild(fill);
        toast.appendChild(track);
    }

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

    // Persistent toasts never time out; they get a close button instead.
    if (options.persistent) {
        const closeBtn = document.createElement('button');
        closeBtn.className = 'toast-close';
        closeBtn.innerHTML = '&times;';
        closeBtn.setAttribute('aria-label', 'Dismiss');
        closeBtn.addEventListener('click', () => toast.remove());
        toast.appendChild(closeBtn);
    }

    container.appendChild(toast);

    if (!options.persistent) {
        // Auto-remove after 5 seconds for errors with details, 3 seconds otherwise
        const timeout = (type === 'error' && detailedError) ? 5000 : 3000;
        setTimeout(() => {
            toast.remove();
        }, timeout);
    }

    return toast;
}

// Replace the text (and optional progress-bar width) of an existing toast,
// used by the index monitor to keep one persistent toast current. Returns the
// element so callers can chain further updates.
function updateToast(toast, message, percent = null) {
    const msgEl = toast.querySelector('.toast-message');
    if (msgEl) msgEl.textContent = message;
    if (percent !== null) {
        const fill = toast.querySelector('.toast-progress-fill');
        if (fill) fill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
    }
    return toast;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Parse an <input type=number> into an integer clamped to [min, max]; a
// blank/invalid value falls back to `fallback` so saving never sends NaN.
function clampInt(raw, min, max, fallback) {
    const n = parseInt(String(raw).trim(), 10);
    if (Number.isNaN(n)) return fallback;
    return Math.min(max, Math.max(min, n));
}
