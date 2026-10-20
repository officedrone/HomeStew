// HomeStew - Settings realm: page sections, secrets, LLM models, webhook,
// master key, search-index admin and backup/restore.


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
    // When the hint lives behind an info icon, hide the icon while empty.
    const infoWrap = hint.closest('.info-wrap');
    if (infoWrap) infoWrap.hidden = !hint.textContent.trim();
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
        pendingSettingsSection = null;
        return;
    }

    // General: theme radios (server value wins; localStorage mirror covers
    // the brief window before this fetch completes).
    const themePref = THEME_ORDER.includes(settings.theme) ? settings.theme : 'auto';
    setThemePref(themePref);

    document.getElementById('settings-llm-base-url').value = settings.llm_base_url || '';

    // Provider selector (Custom vs OpenAI cloud): unknown/missing values fall
    // back to 'custom' (older servers omit the key). The stored base URL is
    // kept as the backup so switching back from the cloud provider restores
    // it, and updateLlmProviderUi() then hides/prefills the URL field.
    const providerSelect = document.getElementById('settings-llm-provider');
    providerSelect.value = settings.llm_provider === 'openai' ? 'openai' : 'custom';
    llmCustomUrlBackup.settings = settings.llm_base_url || '';
    document.getElementById('settings-llm-base-url-override').checked =
        settings.llm_base_url_override === true;
    updateLlmProviderUi('settings');

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
    // With no saved model it shows the disabled 'Select Model' placeholder -
    // there is no built-in default model anymore.
    const modelSelect = document.getElementById('settings-llm-model');
    modelSelect.innerHTML = '';
    const savedModelOption = document.createElement('option');
    savedModelOption.value = settings.llm_model || '';
    savedModelOption.textContent = settings.llm_model || 'Select Model';
    savedModelOption.disabled = !settings.llm_model;
    modelSelect.appendChild(savedModelOption);

    document.getElementById('model-list-hint').textContent =
        'Click the dropdown to load models from the server.';

    // Vision toggle: only an explicit true checks it (missing key = off).
    document.getElementById('settings-llm-vision').checked =
        settings.llm_supports_vision === true;

    // Reasoning toggle: on by default, so only an explicit false unticks it
    // (a missing key from an older server still means "reasoning allowed").
    document.getElementById('settings-llm-reasoning').checked =
        settings.llm_reasoning_enabled !== false;

    // API surface selector + Responses reasoning effort: unknown/missing
    // values fall back to the defaults (older servers omit both keys).
    const kindSelect = document.getElementById('settings-llm-api-kind');
    kindSelect.value = ['chat_completions', 'responses'].includes(settings.llm_api_kind)
        ? settings.llm_api_kind : 'chat_completions';
    const effortSelect = document.getElementById('settings-llm-reasoning-effort');
    effortSelect.value = ['low', 'medium', 'high'].includes(settings.llm_reasoning_effort)
        ? settings.llm_reasoning_effort : 'medium';
    updateLlmApiKindHints(kindSelect.value);

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
    // Anything unknown (or an old install without the key) lands on "None" -
    // no webhook delivery until the user picks a real type.
    const savedWebhookType = settings.notify_webhook_type;
    document.getElementById('settings-notify-webhook-type').value =
        savedWebhookType === 'generic' || savedWebhookType === 'synology'
            ? savedWebhookType : 'none';
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

    // Search panel: index tuning options + a fresh stats line. The stats come
    // from their own endpoint and must never block the rest of the page, so
    // they load in the background (loadSearchIndexStats paints or blanks it).
    document.getElementById('settings-index-chunk-size').value =
        settings.index_max_chunk_size ?? 4000;
    document.getElementById('settings-index-auto-on-upload').checked =
        settings.index_auto_on_upload !== false;
    document.getElementById('search-index-hint').textContent = '';
    document.getElementById('search-rebuild-confirm').hidden = true;
    loadSearchIndexStats();
    loadSearchManuals();


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

    // Always land on the General tab with the prompt accordion collapsed -
    // unless a deep link (dashboard status strip) asked for another section.
    document.getElementById('advanced-settings-section').open = false;
    showSettingsSection(pendingSettingsSection || 'general');
    pendingSettingsSection = null;
}

// OpenAI's hosted endpoint. Mirrors OPENAI_CLOUD_BASE_URL in
// homestew/config.py; when the cloud provider is selected without a URL
// override, the Base URL input is hidden and prefilled with this so the
// probe / test-connection / save code paths keep reading one input.
const OPENAI_CLOUD_BASE_URL = 'https://api.openai.com/v1';

// The user's custom Base URL stashed while the cloud provider hides the
// field ('settings' / 'wizard' prefixes), so switching back restores it.
const llmCustomUrlBackup = { settings: '', wizard: '' };

// Apply the Provider dropdown + "Override URL" checkbox to the form:
//   custom            -> Base URL visible/editable (exactly as before)
//   openai, no override -> Base URL hidden, input forced to the cloud URL
//   openai, override  -> Base URL visible again for the custom endpoint
// Shared by Settings > AI and the wizard step (prefix 'settings'/'wizard').
function updateLlmProviderUi(prefix) {
    const providerEl = document.getElementById(`${prefix}-llm-provider`);
    if (!providerEl) return;
    const openai = providerEl.value === 'openai';
    const overrideEl = document.getElementById(`${prefix}-llm-base-url-override`);
    const override = !!(overrideEl && overrideEl.checked);
    const urlField = document.getElementById(`${prefix}-llm-base-url-field`);
    const overrideField = document.getElementById(`${prefix}-llm-base-url-override-field`);
    const input = document.getElementById(`${prefix}-llm-base-url`);
    if (overrideField) overrideField.hidden = !openai;
    if (urlField) urlField.hidden = openai && !override;
    if (!input) return;
    if (openai && !override) {
        // Stash a typed custom URL before replacing it with the cloud one.
        const current = input.value.trim();
        if (current && current !== OPENAI_CLOUD_BASE_URL) {
            llmCustomUrlBackup[prefix] = current;
        }
        input.value = OPENAI_CLOUD_BASE_URL;
    } else if (!openai && input.value === OPENAI_CLOUD_BASE_URL && llmCustomUrlBackup[prefix]) {
        // Coming back from the hidden cloud state to Custom: restore the
        // custom URL. With Override ticked the field keeps the cloud URL as
        // the starting point for editing instead.
        input.value = llmCustomUrlBackup[prefix];
    }
    // The URL just changed without an 'input' event, so a previous
    // "Connection Successful" no longer describes the target - clear it.
    const connResult = document.getElementById(`${prefix}-conn-test-result`);
    if (connResult) {
        connResult.className = 'conn-test-result';
        connResult.textContent = '';
    }
}

// Show/hide the reasoning controls for the selected API surface: the
// Responses API takes a reasoning effort object instead of the completions
// enable/disable switches, so its dropdown replaces the checkbox there.
// Applied to both the Settings AI panel and the wizard step (only one is
// ever visible; each re-syncs from its own select on load/change).
function updateLlmApiKindHints(apiKind) {
    const responses = apiKind === 'responses';
    const reasoningField = document.getElementById('settings-llm-reasoning')?.closest('.settings-field');
    if (reasoningField) reasoningField.hidden = responses;
    const effortField = document.getElementById('settings-llm-reasoning-effort-field');
    if (effortField) effortField.hidden = !responses;
    const wizReasoningField = document.getElementById('wizard-llm-reasoning')?.closest('.settings-field');
    if (wizReasoningField) wizReasoningField.hidden = responses;
    const wizEffortField = document.getElementById('wizard-llm-reasoning-effort-field');
    if (wizEffortField) wizEffortField.hidden = !responses;
}

// Per-type help text for the webhook fields. Synology Chat incoming webhooks
// take their token in the URL and want form-encoded payload={text} bodies,
// so the generic bearer-token field is hidden for them. The URL hint also
// carries the configured/cleared status of the write-only URL field.
function updateWebhookTypeHints() {
    const type = document.getElementById('settings-notify-webhook-type').value;
    // "None" disables webhook delivery entirely, so the URL / SSL / test
    // controls are hidden until a real type is chosen (a stored URL is kept
    // server-side and reappears when Generic/Synology is selected again).
    const none = type === 'none';
    const typeHint = document.getElementById('webhook-type-hint');
    if (typeHint) {
        typeHint.textContent = none
            ? 'No webhook delivery. Choose Generic or Synology Chat to send notifications.'
            : '';
    }
    for (const id of ['settings-notify-webhook-url', 'settings-notify-webhook-verify-ssl', 'test-webhook-btn']) {
        const field = document.getElementById(id)?.closest('.settings-field');
        if (field) field.style.display = none ? 'none' : '';
    }
    const urlHint = document.getElementById('webhook-url-hint');
    urlHint.textContent = webhookUrlHintText();
    // The URL help now lives in the field's info popover: keep the icon
    // visible only while there is text to show.
    const urlInfo = urlHint.closest('.info-wrap');
    if (urlInfo) urlInfo.hidden = !urlHint.textContent.trim();
    const tokenField = document.getElementById(
        'settings-notify-webhook-token').closest('.settings-field');
    tokenField.style.display = (type === 'synology' || none) ? 'none' : '';
}

// Fill a model <select> from a model id list. The currently selected value is
// preserved; if the server no longer offers it, it stays visible (marked) so
// saving does not silently drop the configured model. A leading disabled
// 'Select Model' option acts as the placeholder while nothing is picked -
// there is no built-in default model, and its '' value can never be saved.
function fillModelSelectOptions(select, modelIds) {
    const current = select.value;

    select.innerHTML = '';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'Select Model';
    placeholder.disabled = true;
    select.appendChild(placeholder);

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

    // Empty selection keeps showing the placeholder; a saved pick is restored.
    if (current) {
        select.value = current;
    }
}

// Fill the Settings > AI LLM Model <select> from a model id list.
function populateModelSelect(modelIds) {
    fillModelSelectOptions(document.getElementById('settings-llm-model'), modelIds);
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
// reusing the same /api/settings/models call as the "Refresh Models" button.
// A 200 means the server answered, so we report success even when it lists no
// models - reachability is what this checks. The probe response already
// carries the model list, so a successful test also refreshes the model
// dropdown (modelSelectId) instead of making the user fetch again.
// Shared by Settings and the wizard.
let connTestInFlight = false;

async function testLlmConnection({ urlId, keyId, btnId, resultId, modelSelectId, hintId }) {
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
        // The probe doubles as a model list: refresh the dropdown with it so
        // a successful test leaves the picker ready to use.
        if (modelSelectId) {
            try {
                const data = await response.json();
                const select = document.getElementById(modelSelectId);
                fillModelSelectOptions(select, data.models || []);
                if (hintId) {
                    document.getElementById(hintId).textContent = (data.models || []).length
                        ? `Loaded ${data.models.length} model${data.models.length === 1 ? '' : 's'} from the server.`
                        : 'Server responded, but no models were listed.';
                }
            } catch (e) { /* list refresh is a bonus; success stands without it */ }
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

    // Type "None" means no webhook at all - nothing to test against.
    if (document.getElementById('settings-notify-webhook-type').value === 'none') {
        hint.textContent = 'Choose a Webhook Type other than None first.';
        return;
    }

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

// The archive chosen through the hidden input, waiting for the Restore button.
// Picking a file no longer starts the restore - it only stages the file and
// enables the button, so the destructive step is always an explicit click.
let pendingRestoreFile = null;

// File picker result: stage the archive, show its name/size, enable Restore.
function handleRestoreFileSelected(event) {
    const file = event.target.files[0];
    if (!file) return; // picker cancelled - keep any previously staged file

    pendingRestoreFile = file;
    const mb = file.size / (1024 * 1024);
    const size = mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.max(1, Math.round(file.size / 1024))} KB`;
    document.getElementById('restore-file-name').textContent =
        `Selected: "${file.name}" (${size})`;
    updateRestoreRunButton();
}

// Restore is enabled only with a staged file and no restore already running.
function updateRestoreRunButton() {
    const runBtn = document.getElementById('restore-run-btn');
    if (runBtn) runBtn.disabled = !pendingRestoreFile || _opsInFlight.has('backup-restore');
}

// Run the staged restore. Replace mode is destructive, so it asks for
// confirmation first. Progress is shown in the fixed bottom status bar (not
// the panel body): a poller reads /api/backup/restore-progress while the POST
// runs and while the background re-index continues after it.
async function runRestore() {
    const file = pendingRestoreFile;
    if (!file) return;

    const mode = document.querySelector('input[name="restore-mode"]:checked')?.value || 'merge';
    if (mode === 'replace' && !confirm(
        'Replace mode deletes ALL current devices, manuals and calendar events before restoring. Continue?'
    )) {
        return;
    }
    if (!beginOp('backup-restore')) return;

    updateRestoreRunButton(); // disabled while the op lock is held
    showRestoreBar(file.name, mode);
    startRestorePoll();
    await postRestoreFile(file, mode);
}

// POST the archive. The status bar is already up and polling; this only sends
// the request, refreshes the app on success, and records the summary the bar
// shows when it finishes. Errors fail the bar directly. The op lock is released
// here (it guards against a duplicate POST), independent of the bar's lifetime.
// Returns whether the restore succeeded so the wizard's restore step can tell
// success from failure (runRestore in this file ignores the value).
async function postRestoreFile(file, mode) {
    try {
        const formData = new FormData();
        formData.append('file', file);
        formData.append('mode', mode);

        const response = await fetch('/api/backup/restore', { method: 'POST', body: formData });
        if (!response.ok) throw new Error(await errorDetailFrom(response, 'Restore failed'));

        const result = await response.json();
        restoreSummaryText = `Restored ${result.devices_created} device(s), ` +
            `${result.manuals_restored} manual(s), ${result.events_restored} event(s)` +
            (result.skipped ? ` (${result.skipped} already present, skipped)` : '') + '.';

        // Refresh everything the restore may have changed. The bar keeps
        // following the background re-index until the server reports done.
        await loadDevices();
        if (document.getElementById('calendar-tab').classList.contains('active')) {
            loadCalendarEvents();
        }
        return true;
    } catch (error) {
        console.error('Restore failed:', error);
        stopRestorePoll();
        failRestoreBar(error.message || 'Restore failed');
        return false;
    } finally {
        restorePostDone = true;
        endOp('backup-restore');
    }
}

// ---------------------------------------------------------------------------
// Restore status bar (fixed bottom overlay) + progress poller
// ---------------------------------------------------------------------------
//
// The bar is driven entirely by GET /api/backup/restore-progress, whose phase
// counters are folded from the shared indexer state server-side. Polling uses
// the same non-overlapping recursive setTimeout pattern as monitorIndexJob():
// one request in flight at a time, and it stops by simply not scheduling again.

const RESTORE_POLL_MS = 800;
let restorePollTimer = null;
let restorePostDone = false;      // POST settled (success or handled error)
let restoreServerConfirmed = false; // saw running=true at least once
let restoreSummaryText = '';      // summary shown when the bar finishes

// Human label + count noun for each server phase.
const RESTORE_PHASE_LABELS = {
    upload: ['Uploading archive\u2026', 'MB'],
    read_archive: ['Reading archive\u2026', ''],
    prepare: ['Preparing database\u2026', ''],
    rows: ['Restoring data\u2026', 'rows'],
    files: ['Writing manual files\u2026', 'files'],
    index: ['Re-indexing manuals\u2026', 'manuals'],
};

function formatElapsed(sec) {
    const s = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    const mm = h > 0 ? String(m).padStart(2, '0') : String(m);
    const ss = String(r).padStart(2, '0');
    return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

// Enable/disable the restore/backup buttons and mode radios as a group so a
// running restore can't be double-triggered from the panel. The Restore button
// additionally needs a staged file, so it re-arms itself after each run.
function setRestoreControls(disabled) {
    const restoreBtn = document.getElementById('restore-btn');
    const backupBtn = document.getElementById('backup-create-btn');
    if (restoreBtn) restoreBtn.disabled = disabled;
    if (backupBtn) backupBtn.disabled = disabled;
    document.querySelectorAll('input[name="restore-mode"]').forEach((r) => { r.disabled = disabled; });
    updateRestoreRunButton();
}

// Reveal the bar in its running look. filename/mode are known from the picker;
// on a page-load resume they're blank, so fall back to a generic label.
function showRestoreBar(filename, mode) {
    const bar = document.getElementById('restore-status-bar');
    if (!bar) return;
    bar.hidden = false;
    bar.classList.remove('done', 'failed', 'is-indeterminate');
    document.body.classList.add('restore-active');
    setRestoreControls(true);

    restorePostDone = false;
    restoreServerConfirmed = false;
    restoreSummaryText = '';

    const label = filename
        ? `Restoring "${filename}" (${mode})\u2026`
        : 'Restoring backup\u2026';
    document.getElementById('restore-status-label').textContent = label;
    document.getElementById('restore-status-detail').textContent = '';
    document.getElementById('restore-status-elapsed').textContent = '0:00';
    document.getElementById('restore-status-steps').textContent = '';
    setRestoreFill(0);
}

function setRestoreFill(percent) {
    const fill = document.getElementById('restore-status-fill');
    const track = document.getElementById('restore-status-track');
    if (fill) fill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
    if (track) track.setAttribute('aria-valuenow', String(percent));
}

// Paint one poll snapshot: label/detail text, meta column, fill width, and the
// indeterminate sweep when a phase has no known total.
function renderRestoreProgress(p) {
    const bar = document.getElementById('restore-status-bar');
    if (!bar || bar.hidden) return;

    const [label] = RESTORE_PHASE_LABELS[p.phase] || ['Restoring\u2026', ''];
    document.getElementById('restore-status-label').textContent = label;

    const detail = p.step_total > 0 ? `${p.step_done} / ${p.step_total}` : '';
    document.getElementById('restore-status-detail').textContent = detail;

    document.getElementById('restore-status-elapsed').textContent = formatElapsed(p.elapsed);
    document.getElementById('restore-status-steps').textContent =
        p.steps_remaining > 0 ? `${p.steps_remaining} step${p.steps_remaining === 1 ? '' : 's'} remaining` : '';
    document.getElementById('restore-status-pct').textContent = `${p.percent}%`;

    bar.classList.toggle('is-indeterminate', p.step_total === 0);
    setRestoreFill(p.percent);
}

// Poll while the restore runs. A transient fetch failure retries without
// killing the bar. Finalization waits until we know the server actually started
// (saw running=true) or the POST settled, so a leftover "done" state from a
// previous restore can't finish this one prematurely.
function startRestorePoll() {
    if (restorePollTimer) return;

    const tick = async () => {
        restorePollTimer = null;
        let p;
        try {
            const response = await fetch('/api/backup/restore-progress');
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            p = await response.json();
        } catch (e) {
            restorePollTimer = setTimeout(tick, 2000);
            return;
        }

        if (p.running) {
            restoreServerConfirmed = true;
            renderRestoreProgress(p);
            restorePollTimer = setTimeout(tick, RESTORE_POLL_MS);
            return;
        }

        // Not running: finalize only once the run is confirmed to have started
        // or the POST has settled; otherwise keep waiting for the server.
        if (restoreServerConfirmed || restorePostDone) {
            if (p.error) failRestoreBar(p.error);
            else finishRestoreBar(restoreSummaryText);
        } else {
            restorePollTimer = setTimeout(tick, RESTORE_POLL_MS);
        }
    };
    tick();
}

function stopRestorePoll() {
    if (restorePollTimer) {
        clearTimeout(restorePollTimer);
        restorePollTimer = null;
    }
}

// Success: 100%, green, summary line, then auto-hide. Controls re-enabled here
// (not in the POST finally) so they stay locked through the background index.
function finishRestoreBar(summary) {
    stopRestorePoll();
    const bar = document.getElementById('restore-status-bar');
    if (!bar) return;
    bar.classList.remove('is-indeterminate');
    bar.classList.add('done');
    setRestoreFill(100);
    document.getElementById('restore-status-label').textContent = 'Restore complete';
    document.getElementById('restore-status-detail').textContent = summary || '';
    document.getElementById('restore-status-pct').textContent = '100%';
    document.getElementById('restore-status-steps').textContent = '';

    const hint = document.getElementById('restore-hint');
    if (hint) hint.textContent = summary || 'Restore complete.';
    setRestoreControls(false);

    setTimeout(hideRestoreBar, 6000);
}

// Failure: red, message visible, stays until dismissed. Controls re-enabled so
// the user can try again.
function failRestoreBar(message) {
    stopRestorePoll();
    const bar = document.getElementById('restore-status-bar');
    if (!bar) return;
    bar.classList.remove('is-indeterminate');
    bar.classList.add('failed');
    setRestoreFill(100);
    document.getElementById('restore-status-label').textContent = 'Restore failed';
    document.getElementById('restore-status-detail').textContent = message || '';
    document.getElementById('restore-status-steps').textContent = '';

    const hint = document.getElementById('restore-hint');
    if (hint) hint.textContent = '';
    showToast(message || 'Restore failed', 'error');
    setRestoreControls(false);
}

// Dismiss the bar (only reachable via ✕ in a terminal state). Does not cancel
// any server work - it only clears the overlay.
function hideRestoreBar() {
    stopRestorePoll();
    const bar = document.getElementById('restore-status-bar');
    if (bar) bar.hidden = true;
    document.body.classList.remove('restore-active');
}

// Page load / second tab while a restore is still running server-side: reattach
// the bar so progress never vanishes until the job actually ends. Silent when
// nothing is running (a finished restore's summary is not re-shown on load).
async function resumeRestoreMonitorIfRunning() {
    try {
        const response = await fetch('/api/backup/restore-progress');
        if (!response.ok) return;
        const p = await response.json();
        if (p.running) {
            showRestoreBar(null, '');
            startRestorePoll();
        }
    } catch (e) { /* offline or no session yet - nothing to resume */ }
}


// ---------------------------------------------------------------------------
// Search index progress monitor (Settings > Search, restore re-index)
// ---------------------------------------------------------------------------

// Every long-running index job (Re-index, Rebuild, post-restore re-index)
// reports through GET /api/search/index-status; a single persistent toast
// follows it with "done/total (N%) - current file" plus a progress bar. The
// recursive setTimeout(1000ms) pattern matches the old restore poller: no
// request overlaps, and polling stops by simply not scheduling the next tick.
const INDEX_TOAST_ID = 'index-progress';
let indexMonitorTimer = null;

function indexToastEl() {
    return document.querySelector(`[data-toast-id="${CSS.escape(INDEX_TOAST_ID)}"]`);
}

// Poll once per second while a job runs; on completion swap the toast to a
// final summary that stays until dismissed. Safe to call repeatedly - a second
// caller joins the existing poller instead of starting a parallel one.
function monitorIndexJob() {
    if (indexMonitorTimer) return;

    const tick = async () => {
        indexMonitorTimer = null;
        let status;
        try {
            const response = await fetch('/api/search/index-status');
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            status = await response.json();
        } catch (e) {
            // A transient status failure must not kill the monitor - retry.
            indexMonitorTimer = setTimeout(tick, 2000);
            return;
        }

        if (status.running) {
            const pct = status.total > 0 ? Math.round((status.done / status.total) * 100) : 0;
            const file = status.current_file ? ` - ${status.current_file}` : '';
            const message = `Indexing... ${status.done}/${status.total} (${pct}%)${file}`;
            let toast = indexToastEl();
            if (!toast) {
                showToast(message, 'info', null,
                    { persistent: true, progress: true, id: INDEX_TOAST_ID });
            } else {
                // Reusing a finished summary toast (new job started without
                // dismissing the old one): reset it to the in-progress look.
                if (!toast.classList.contains('info')) toast.className = 'toast info';
                updateToast(toast, message, pct);
            }
            indexMonitorTimer = setTimeout(tick, 1000);
        } else {
            // Job over: turn the monitor's own toast into a final summary.
            // (No toast => this poller was only joining to check; nothing to do.)
            const toast = indexToastEl();
            if (toast) {
                const okCount = Math.max(0, status.done - status.failed);
                const failedNote = status.failed ? ` (${status.failed} failed)` : '';
                updateToast(toast,
                    `Indexing complete: ${okCount}/${status.total} manual(s) indexed${failedNote}.`, 100);
                toast.className = `toast ${status.failed ? 'error' : 'success'}`;
            }
            // Index counts changed - repaint the Settings > Search stats line
            // and manual table (also clears any "Queued..." row buttons).
            loadSearchIndexStats();
            loadSearchManuals();
        }
    };
    tick();
}

// Page load / tab open while a job is still running server-side (the user hit
// F5 mid-index, or opened HomeStew in a second tab): resume the toast so
// progress never disappears until the job actually ends.
async function resumeIndexMonitorIfRunning() {
    try {
        const response = await fetch('/api/search/index-status');
        if (!response.ok) return;
        const status = await response.json();
        if (status.running) monitorIndexJob();
    } catch (e) { /* offline or no session yet - nothing to resume */ }
}

// Read-only stats line for Settings > Search ("N manuals, M indexed pages").
// Fired on every visit to the panel; a failure only blanks the line.
async function loadSearchIndexStats() {
    const el = document.getElementById('search-index-stats');
    try {
        const response = await fetch('/api/search/stats');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const stats = await response.json();
        const manuals = Number(stats.manuals ?? 0);
        const pages = Number(stats.indexed_documents ?? 0);
        el.textContent = `${manuals} manual${manuals === 1 ? '' : 's'}, ` +
            `${pages.toLocaleString()} indexed page${pages === 1 ? '' : 's'}.`;
    } catch (e) {
        console.error('Failed to load search stats:', e);
        el.textContent = '';
    }
}

// Settings > Search manual table: one row per stored manual showing filename,
// owning device and how many pages it currently has in the search index
// (0 = never indexed or a failed extraction). Fired alongside the stats line
// on every visit to the panel; failures keep the previous rows on screen.
async function loadSearchManuals() {
    const tbody = document.getElementById('search-manuals-tbody');
    const table = document.getElementById('search-manuals-table');
    const empty = document.getElementById('search-manuals-empty');
    let data;
    try {
        const response = await fetch('/api/search/manuals');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        data = await response.json();
    } catch (e) {
        console.error('Failed to load manuals index list:', e);
        return;
    }
    const rows = data.manuals || [];
    tbody.innerHTML = rows.map((m) => {
        const status = m.pages > 0
            ? `<span class="idx-chip idx-ok">Indexed · ${Number(m.pages).toLocaleString()} page${m.pages === 1 ? '' : 's'}</span>`
            : '<span class="idx-chip idx-missing">Not indexed</span>';
        // The filename itself is the link: the same inline FileResponse
        // endpoint search citations use, so it opens the PDF in a new tab.
        const fileUrl = `/api/downloads/manuals/${m.manual_id}/file`;
        // device_name is always a usable label server-side (friendly name,
        // else "Brand Model", else '#<id>'). Manuals whose device row is gone
        // are labelled as orphans and get a trashcan - they can never be
        // re-indexed into anything useful, only cleaned up.
        const deviceLabel = m.orphaned ? `Orphaned, Device #${m.device_id}` : m.device_name;
        const deleteBtn = m.orphaned
            ? `<button type="button" class="card-icon-btn card-icon-danger delete-orphan-btn"
                    title="Delete this orphaned manual" aria-label="Delete this orphaned manual"
                    data-manual-id="${m.manual_id}" data-device-id="${m.device_id}"
                    data-filename="${escapeHtml(m.filename)}" data-dedupe>${CARD_ACTION_ICONS.trash}</button>`
            : '';
        return `<tr data-manual-id="${m.manual_id}">
            <td class="col-file">
                <a class="manual-index-name" href="${fileUrl}" target="_blank"
                    rel="noopener noreferrer" title="Open ${escapeHtml(m.filename)}">${escapeHtml(m.filename)}</a>
            </td>
            <td class="col-device"><span class="${m.orphaned ? 'manual-device-orphan' : 'manual-device-name'}"
                title="${escapeHtml(deviceLabel)}">${escapeHtml(deviceLabel)}</span></td>
            <td class="col-status">${status}</td>
            <td class="col-action">
                ${deleteBtn}
                <button type="button" class="card-icon-btn reindex-one-btn"
                    title="Re-index this manual" aria-label="Re-index this manual"
                    data-reindex-id="${m.manual_id}" data-dedupe>${CARD_ACTION_ICONS.refresh}</button>
            </td>
        </tr>`;
    }).join('');
    table.hidden = rows.length === 0;
    empty.hidden = rows.length !== 0;
}

// Delete an orphaned manual (its device row is gone, so nothing links to it
// any more). Reuses the same DELETE /api/devices/{device_id}/manuals/{id}
// endpoint as the Edit Device modal - it matches on the manual's stored
// device_id and never joins devices, so it deletes orphans fine. Afterwards
// only the Settings > Search table/stats need a repaint (no device views to
// refresh - there is no device).
async function deleteOrphanManual(btn) {
    const manualId = btn.dataset.manualId;
    const deviceId = btn.dataset.deviceId;
    const label = btn.dataset.filename || `manual ${manualId}`;
    if (!confirm(`Delete "${label}"? It belongs to a device that no longer exists.`)) return;
    if (!beginOp(`delete-orphan-${manualId}`)) return;
    btn.disabled = true;
    try {
        const response = await fetch(`/api/devices/${deviceId}/manuals/${manualId}`,
            { method: 'DELETE' });
        if (!response.ok) throw new Error(await errorDetailFrom(response, 'Failed to delete manual'));
        showToast('Orphaned manual deleted', 'success');
        loadSearchIndexStats();
        await loadSearchManuals(); // repaints the table without the row
    } catch (error) {
        console.error('Failed to delete orphaned manual:', error);
        showToast(error.message || 'Failed to delete manual', 'error');
        btn.disabled = false;
    } finally {
        endOp(`delete-orphan-${manualId}`);
    }
}

// Per-row Re-index: queue a single-manual job. The shared job slot means a
// click while any index job runs gets 409 - we join that job's toast instead
// of erroring. On success the icon button stays disabled (dimmed) while the
// job runs; monitorIndexJob repaints the whole table when it finishes.
async function reindexOneManual(btn) {
    const id = btn.dataset.reindexId;
    if (!beginOp(`search-reindex-${id}`)) return;
    btn.disabled = true;
    try {
        const response = await fetch(`/api/search/manuals/${id}/reindex`, { method: 'POST' });
        if (response.status === 409) {
            showToast('An indexing job is already running', 'error');
            monitorIndexJob();
            btn.disabled = false;
            return;
        }
        if (!response.ok) throw new Error(await errorDetailFrom(response, 'Failed to queue re-index'));
        monitorIndexJob();
    } catch (error) {
        console.error('Failed to queue manual re-index:', error);
        showToast(error.message || 'Failed to start re-index', 'error');
        btn.disabled = false;
    } finally {
        endOp(`search-reindex-${id}`);
    }
}

// Queue POST /api/search/<path> (reindex | rebuild). 202 returns immediately
// with the queued count; a 409 means another job owns the slot, so just join
// its progress toast instead of erroring out.
async function startIndexJob(path, btn, doneHint) {
    if (!beginOp('search-index-job')) return;

    const hint = document.getElementById('search-index-hint');
    btn.disabled = true;
    try {
        const response = await fetch(`/api/search/${path}`, { method: 'POST' });
        if (response.status === 409) {
            showToast('An indexing job is already running', 'error');
            monitorIndexJob();
            return;
        }
        if (!response.ok) throw new Error(await errorDetailFrom(response, 'Failed to queue indexing job'));

        const info = await response.json();
        hint.textContent = `${doneHint} ${info.queued} manual(s) queued.`;
        monitorIndexJob();
    } catch (error) {
        console.error('Failed to queue index job:', error);
        showToast(error.message || 'Failed to start indexing', 'error');
    } finally {
        btn.disabled = false;
        endOp('search-index-job');
    }
}

// Rebuild drops the whole FTS table first, so it arms a confirm row instead of
// firing straight away (same two-step spirit as Replace-mode restore).
function handleSearchRebuildClick() {
    document.getElementById('search-rebuild-confirm').hidden = false;
}

async function handleSearchRebuildConfirm() {
    document.getElementById('search-rebuild-confirm').hidden = true;
    await startIndexJob('rebuild',
        document.getElementById('search-rebuild-btn'), 'Index rebuild queued:');
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
    // Provider state decides whether the URL field's content is sent: with
    // the cloud provider and no override, the input only holds the built-in
    // cloud URL, so omit it and keep the stored custom URL for later.
    const providerValue = document.getElementById('settings-llm-provider').value;
    const urlOverridden = document.getElementById('settings-llm-base-url-override').checked;
    const sendBaseUrl = !(providerValue === 'openai' && !urlOverridden);
    const payload = {
        theme: themeInput ? themeInput.value : 'auto',
        llm_provider: providerValue,
        llm_base_url_override: urlOverridden,
        ...(sendBaseUrl
            ? { llm_base_url: document.getElementById('settings-llm-base-url').value.trim() }
            : {}),
        // Blank = the 'Select Model' placeholder (nothing picked yet): omit it
        // so saving other tabs never sends an empty model the server would
        // reject, and the saved selection is kept unchanged.
        ...(document.getElementById('settings-llm-model').value.trim()
            ? { llm_model: document.getElementById('settings-llm-model').value.trim() }
            : {}),
        // Always sent: a plain boolean, so unchecking must persist too.
        llm_supports_vision: document.getElementById('settings-llm-vision').checked,
        llm_reasoning_enabled: document.getElementById('settings-llm-reasoning').checked,
        // Always sent: independent persisted fields, each read only by its
        // own code path (kind selects the endpoint, effort the Responses
        // reasoning body), so a hidden control still stores its preference.
        llm_api_kind: document.getElementById('settings-llm-api-kind').value,
        llm_reasoning_effort: document.getElementById('settings-llm-reasoning-effort').value,
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
        // Search index tuning (Settings > Search). Both always sent: the chunk
        // size is clamped client-side to the same range the server enforces.
        index_max_chunk_size:
            clampInt(document.getElementById('settings-index-chunk-size').value, 200, 20000, 4000),
        index_auto_on_upload: document.getElementById('settings-index-auto-on-upload').checked,
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
// Settings wiring (was part of setupEventListeners in the single-file app).
// ---------------------------------------------------------------------------

function wireSettingsEvents() {
    // Settings page section tabs (General / AI / Notifications / Advanced):
    // show one panel at a time. The gear itself is a .tab-btn wired by chrome.
    document.querySelectorAll('.settings-tab-btn').forEach((btn) => {
        btn.addEventListener('click', () => showSettingsSection(btn.dataset.settingsSection));
    });
    document.getElementById('settings-form').addEventListener('submit', handleSettingsSave);
    document.getElementById('fetch-models-btn').addEventListener('click', () => fetchLlmModels());
    document.getElementById('settings-llm-api-kind').addEventListener('change', (event) => {
        updateLlmApiKindHints(event.target.value);
    });
    document.getElementById('settings-llm-provider').addEventListener('change', () => {
        updateLlmProviderUi('settings');
    });
    document.getElementById('settings-llm-base-url-override').addEventListener('change', () => {
        updateLlmProviderUi('settings');
    });
    document.getElementById('test-connection-btn').addEventListener('click', () => testLlmConnection({
        urlId: 'settings-llm-base-url', keyId: 'settings-llm-api-key',
        btnId: 'test-connection-btn', resultId: 'settings-conn-test-result',
        modelSelectId: 'settings-llm-model', hintId: 'model-list-hint',
    }));
    resetConnTestOnEdit('settings-llm-base-url', 'settings-llm-api-key', 'settings-conn-test-result');
    document.getElementById('test-webhook-btn').addEventListener('click', () => sendTestNotification());

    // Backup & Restore (Settings > General). Buttons are type="button" so the
    // surrounding settings form never submits when they're clicked. Choosing a
    // file only stages it; the dedicated Restore button starts the restore.
    document.getElementById('backup-create-btn').addEventListener('click', createBackup);
    document.getElementById('restore-btn').addEventListener('click', triggerRestoreFilePick);
    document.getElementById('restore-run-btn').addEventListener('click', runRestore);
    document.getElementById('restore-file-input').addEventListener('change', handleRestoreFileSelected);
    // The status bar's ✕ only dismisses the finished/failed overlay; it never
    // cancels server work (there is no cancel endpoint).
    document.getElementById('restore-status-close').addEventListener('click', hideRestoreBar);

    // Search index (Settings > Search). Re-index fires straight away; Rebuild
    // arms a confirm row first because it drops the FTS table. Progress for
    // both shows in the shared persistent toast (monitorIndexJob).
    document.getElementById('search-reindex-btn').addEventListener(
        'click', () => startIndexJob('reindex',
            document.getElementById('search-reindex-btn'), 'Re-index queued:'));
    document.getElementById('search-rebuild-btn').addEventListener('click', handleSearchRebuildClick);
    document.getElementById('search-rebuild-confirm-btn').addEventListener(
        'click', handleSearchRebuildConfirm);
    document.getElementById('search-rebuild-cancel-btn').addEventListener(
        'click', () => { document.getElementById('search-rebuild-confirm').hidden = true; });

    // Per-manual Re-index / orphan-delete buttons are rendered dynamically,
    // so one delegated listener covers every row and survives table re-renders.
    document.getElementById('search-manuals-tbody').addEventListener('click', (e) => {
        const btn = e.target.closest('.reindex-one-btn');
        if (btn && !btn.disabled) { reindexOneManual(btn); return; }
        const del = e.target.closest('.delete-orphan-btn');
        if (del && !del.disabled) deleteOrphanManual(del);
    });

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
            const hint = document.getElementById(field.hintId);
            hint.textContent = secretHintText(name);
            const infoWrap = hint.closest('.info-wrap');
            if (infoWrap) infoWrap.hidden = !hint.textContent.trim();
        });
    }
    // Switching webhook type updates the URL hint and hides the bearer-token
    // field (Synology Chat authenticates via the token= param in its URL).
    document.getElementById('settings-notify-webhook-type').addEventListener(
        'change', updateWebhookTypeHints);
    setupModelDropdownRefresh();

    // "Restore Default" buttons in Advanced Settings repopulate the built-in
    // prompt text (fetched from the API) into the matching textarea.
    document.querySelectorAll('[data-restore-default]').forEach((btn) => {
        btn.addEventListener('click', () => restorePromptDefault(btn.dataset.restoreDefault));
    });

    // Secrets master key management: Settings > Advanced create/delete.
    document.getElementById('create-key-btn').addEventListener('click', handleCreateKeyFromSettings);
    document.getElementById('delete-key-btn').addEventListener('click', handleDeleteKeyFromSettings);

    // Account password change (Settings > Advanced).
    document.getElementById('change-password-btn').addEventListener('click', handleChangePassword);
}
