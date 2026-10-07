// HomeStew - First-run setup wizard (encryption key -> AI/LLM -> first device).


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
        // Seed the wizard's reasoning checkbox from the saved config too
        // (default on: only an explicit false unticks it).
        document.getElementById('wizard-llm-reasoning').checked =
            s.llm_reasoning_enabled !== false;
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
    // Hard refresh so the freshly loaded page reflects everything the wizard
    // just configured (e.g. a master key created in step 1 would otherwise
    // still show as "no key configured" on the stale original page).
    window.location.reload();
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
// saved model so it shows before the list is fetched; with none saved it
// displays the disabled 'Select Model' placeholder (no built-in default).
function seedWizardModelSelect(model) {
    const select = document.getElementById('wizard-llm-model');
    select.innerHTML = '';
    const option = document.createElement('option');
    option.value = model || '';
    option.textContent = model || 'Select Model';
    option.disabled = !model;
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
        fillModelSelectOptions(select, data.models || []);
        hint.textContent = data.models.length
            ? `Loaded ${data.models.length} model${data.models.length === 1 ? '' : 's'} from the server.`
            : 'Server responded, but no models were listed.';
    } catch (error) {
        // Keep whatever was selected visible (marked stale by the helper)
        // rather than wiping the user's pick on a failed probe.
        fillModelSelectOptions(select, current ? [current] : []);
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
    if (!model) throw new Error('Pick a model - click "Refresh Models" to load the list from your server.');

    const payload = {
        llm_base_url: baseUrl,
        llm_model: model,
        // Always sent so unchecking in the wizard persists too.
        llm_supports_vision: document.getElementById('wizard-llm-vision').checked,
        llm_reasoning_enabled: document.getElementById('wizard-llm-reasoning').checked,
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

// ---------------------------------------------------------------------------
// Wizard wiring (was part of setupEventListeners in the single-file app).
// The wizard is deliberately not closable via Escape: every step must end
// with Skip or Save so its resolution gets persisted.
// ---------------------------------------------------------------------------

function wireWizardEvents() {
    document.getElementById('wizard-skip-btn').addEventListener('click', handleWizardSkip);
    document.getElementById('wizard-save-btn').addEventListener('click', handleWizardSave);
    document.getElementById('wizard-fetch-models-btn').addEventListener('click', fetchWizardLlmModels);
    document.getElementById('wizard-test-connection-btn').addEventListener('click', () => testLlmConnection({
        urlId: 'wizard-llm-base-url', keyId: 'wizard-llm-api-key',
        btnId: 'wizard-test-connection-btn', resultId: 'wizard-conn-test-result',
        modelSelectId: 'wizard-llm-model', hintId: 'wizard-model-list-hint',
    }));
    resetConnTestOnEdit('wizard-llm-base-url', 'wizard-llm-api-key', 'wizard-conn-test-result');
}
