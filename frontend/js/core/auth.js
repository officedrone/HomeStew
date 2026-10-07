// HomeStew - Authentication gate (single-user account, services/auth.py on the server).


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

// Sidebar sign-out. The auth gate's own listeners live in setupAuthGate(),
// which runs before initializeApp() so the login screen works standalone.
function wireAuthEvents() {
    document.getElementById('logout-btn').addEventListener('click', handleLogout);
}
