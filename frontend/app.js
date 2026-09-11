// HomeBrain Frontend Application

document.addEventListener('DOMContentLoaded', () => {
    initializeApp();
});

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
    // and strip referrer info from outbound links.
    DOMPurify.addHook('afterSanitizeAttributes', (node) => {
        if (node.tagName === 'A') {
            node.target = '_blank';
            node.rel = 'noopener noreferrer';
        }
    });
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
// Selected device filter for the AI Chat tab (null = all devices). Mirrors
// currentDeviceFilter, but scoped to chat so the two tabs stay independent.
let currentChatDeviceFilter = null;
// Device id currently targeted by the hidden manual-upload input.
let uploadTargetDeviceId = null;

// Result of the last chat model availability check (see checkChatModelStatus).
// null = unknown/not yet checked; true/false = whether chatting is allowed.
let chatModelAvailable = null;

async function initializeApp() {
    await loadDevices();
    setupEventListeners();
}

function setupEventListeners() {
    // Tab switching
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', (e) => switchTab(e.target.dataset.tab));
    });
    
    // Add device form
    document.getElementById('add-device-form').addEventListener('submit', handleAddDevice);
    
    // Edit device form
    document.getElementById('edit-device-form').addEventListener('submit', handleEditDevice);
    
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
    
    // Chat functionality
    document.getElementById('send-btn').addEventListener('click', sendChatMessage);
    document.getElementById('chat-input').addEventListener('keypress', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendChatMessage();
        }
    });

    // Manual upload: the hidden input is shared by every device's "Upload
    // Manual" button; the target device id is stashed before opening it.
    document.getElementById('manual-upload-input').addEventListener('change', handleManualFileSelected);

    // Sidebar (docked, expanded / collapsed modes)
    applySidebarState();
    document.getElementById('sidebar-toggle-btn').addEventListener('click', toggleSidebar);
    document.getElementById('rail-add-device').addEventListener('click', openAddDeviceSection);

    // Settings modal
    document.getElementById('settings-btn').addEventListener('click', openSettingsModal);
    document.getElementById('settings-form').addEventListener('submit', handleSettingsSave);
    document.getElementById('fetch-models-btn').addEventListener('click', () => fetchLlmModels());
    setupModelDropdownRefresh();

    // Chat model warning banner: takes the user straight to Settings.
    document.getElementById('chat-open-settings-btn').addEventListener('click', openSettingsModal);

    // "Restore Default" buttons in Advanced Settings repopulate the built-in
    // prompt text (fetched from the API) into the matching textarea.
    document.querySelectorAll('[data-restore-default]').forEach((btn) => {
        btn.addEventListener('click', () => restorePromptDefault(btn.dataset.restoreDefault));
    });

    // Escape closes the settings modal
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        const settingsModal = document.getElementById('settings-modal');
        if (settingsModal.style.display === 'flex') {
            closeSettingsModal();
        }
    });
}

async function loadDevices() {
    try {
        const response = await fetch('/api/devices');
        devices = await response.json();
        
        renderDeviceList();
        updateDeviceFilter();
    } catch (error) {
        console.error('Failed to load devices:', error);
        showToast('Failed to load devices', 'error');
    }
}

function renderDeviceList() {
    const container = document.getElementById('device-list');
    
    if (devices.length === 0) {
        container.innerHTML = '<div class="empty-state">No devices yet. Add your first device!</div>';
        return;
    }
    
    container.innerHTML = devices.map(device => `
        <div class="device-item" data-id="${device.id}" onclick="editDevice(${device.id})">
            <div class="device-name">${escapeHtml(device.name)}</div>
            <div class="device-meta">${escapeHtml(device.brand)} ${escapeHtml(device.model)}</div>
            ${device.serial_number ? `<div class="device-meta">Serial: ${escapeHtml(device.serial_number)}</div>` : ''}
            ${device.product_number ? `<div class="device-meta">Product: ${escapeHtml(device.product_number)}</div>` : ''}
            <div class="device-meta">${device.manual_count} manual${device.manual_count !== 1 ? 's' : ''}</div>
            <div class="device-actions">
                <button class="btn btn-secondary btn-small" onclick="event.stopPropagation(); editDevice(${device.id})">
                    Edit
                </button>
                <button class="btn btn-danger btn-small" onclick="event.stopPropagation(); deleteDevice(${device.id})">
                    Delete
                </button>
            </div>
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
}

async function handleAddDevice(e) {
    e.preventDefault();
    
    const deviceData = {
        name: document.getElementById('device-name').value,
        brand: document.getElementById('device-brand').value,
        model: document.getElementById('device-model').value,
        description: document.getElementById('device-description').value,
        serial_number: document.getElementById('device-serial-number').value || null,
        product_number: document.getElementById('device-product-number').value || null
    };
    
    try {
        const response = await fetch('/api/devices', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(deviceData)
        });
        
        if (!response.ok) throw new Error('Failed to add device');
        
        showToast('Device added successfully!', 'success');
        e.target.reset();
        await loadDevices();
    } catch (error) {
        console.error('Failed to add device:', error);
        showToast('Failed to add device', 'error');
    }
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
    
    // Load and render attributes
    await loadDeviceAttributes(deviceId);

    // Load and render manuals
    await loadManuals(deviceId);
    
    // Show edit modal
    document.getElementById('edit-device-modal').style.display = 'flex';
}

async function handleEditDevice(e) {
    e.preventDefault();
    
    const deviceId = parseInt(document.getElementById('edit-device-id').value);
    const deviceData = {
        name: document.getElementById('edit-device-name').value,
        brand: document.getElementById('edit-device-brand').value,
        model: document.getElementById('edit-device-model').value,
        description: document.getElementById('edit-device-description').value,
        serial_number: document.getElementById('edit-device-serial-number').value || null,
        product_number: document.getElementById('edit-device-product-number').value || null
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
    }
}

async function loadDeviceAttributes(deviceId) {
    const container = document.getElementById('device-attributes-list');

    // The devices array comes from GET /api/devices, which does not include
    // attributes — fetch the single-device endpoint so newly added/removed
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
            <button class="btn btn-danger btn-small" onclick="removeAttribute(${deviceId}, ${attr.id})">
                ×
            </button>
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
    
    try {
        const response = await fetch(`/api/devices/${deviceId}/attributes?attribute_name=${encodeURIComponent(attributeName)}&attribute_value=${encodeURIComponent(attributeValue)}`, {
            method: 'POST'
        });
        
        if (!response.ok) throw new Error('Failed to add attribute');
        
        showToast('Attribute added successfully!', 'success');
        nameInput.value = '';
        valueInput.value = '';
        await loadDeviceAttributes(deviceId);
    } catch (error) {
        console.error('Failed to add attribute:', error);
        showToast('Failed to add attribute', 'error');
    }
}

async function removeAttribute(deviceId, attributeId) {
    try {
        const response = await fetch(`/api/devices/${deviceId}/attributes/${attributeId}`, {
            method: 'DELETE'
        });
        
        if (!response.ok) throw new Error('Failed to remove attribute');
        
        showToast('Attribute removed', 'success');
        await loadDeviceAttributes(deviceId);
    } catch (error) {
        console.error('Failed to remove attribute:', error);
        showToast('Failed to remove attribute', 'error');
    }
}

function closeEditModal() {
    document.getElementById('edit-device-modal').style.display = 'none';
}

async function downloadManuals(deviceId) {
    // Show progress modal
    const modal = document.getElementById('download-progress-modal');
    const stepsContainer = document.getElementById('download-progress-steps');
    const statusBox = document.getElementById('download-progress-status');
    const spinner = document.getElementById('download-spinner');
    const statusText = document.getElementById('download-current-step');
    const closeBtn = document.getElementById('close-download-modal');

    modal.style.display = 'flex';
    stepsContainer.innerHTML = '';
    closeBtn.style.display = 'none';
    // Reset the status row to its "in progress" look (spinner visible, neutral).
    statusBox.classList.remove('done-success', 'done-error');
    if (spinner) spinner.style.display = '';
    statusText.textContent = 'Initializing...';

    // Guard so a terminal event is only handled once and late messages are ignored.
    let finished = false;

    // Stop the spinner and turn the status row into a clear success/error state.
    function finishDownload({ ok, headline, detail }) {
        if (spinner) spinner.style.display = 'none';
        statusBox.classList.add(ok ? 'done-success' : 'done-error');
        statusText.textContent = headline;
        closeBtn.style.display = 'inline-block';
        // Reuse the same expandable-detail toast as the device-added popup so the
        // real reason (e.g. a DuckDuckGo rate-limit) is visible here too.
        showToast(headline, ok ? 'success' : 'error', detail || null);
    }

    try {
        // Use EventSource for Server-Sent Events streaming
        const eventSource = new EventSource(`/api/downloads/stream/${deviceId}`);

        eventSource.onmessage = async function(event) {
            let data;
            try {
                data = JSON.parse(event.data);
            } catch (e) {
                console.error('Failed to parse progress message:', event.data, e);
                return;
            }

            switch(data.type) {
                case 'step':
                    // Intermediate progress only — ignore once finished.
                    if (finished) break;
                    addProgressStep(stepsContainer, data.message, data.status || 'searching');
                    statusText.textContent = data.message;
                    break;

                case 'complete':
                    if (finished) break;
                    finished = true;
                    eventSource.close();
                    if (data.success) {
                        const count = data.downloaded_count ?? 0;
                        addProgressStep(stepsContainer, `Successfully downloaded ${count} manual(s)`, 'success');
                        finishDownload({ ok: true, headline: `Download complete! ${count} manual(s) downloaded` });
                        // Refresh device counts and the modal's manual list if it is open.
                        await loadDevices();
                        const editModal = document.getElementById('edit-device-modal');
                        if (editModal.style.display === 'flex' &&
                            parseInt(document.getElementById('edit-device-id').value) === deviceId) {
                            await loadManuals(deviceId);
                        }
                    } else {
                        // Backend sends a single terminal event with the real reason.
                        const reason = data.error_detail || 'No manuals were downloaded.';
                        addProgressStep(stepsContainer, reason, 'error');
                        finishDownload({ ok: false, headline: `Download failed: ${reason}`, detail: reason });
                    }
                    break;

                case 'error':
                    if (finished) break;
                    finished = true;
                    eventSource.close();
                    const message = data.message || 'An unexpected error occurred.';
                    addProgressStep(stepsContainer, message, 'error');
                    finishDownload({ ok: false, headline: `Error: ${message}`, detail: message });
                    break;
            }
        };

        eventSource.onerror = function(error) {
            if (finished) return;
            finished = true;
            console.error('EventSource failed:', error);
            eventSource.close();
            const reason = 'Connection to the server was lost while downloading.';
            addProgressStep(stepsContainer, reason, 'error');
            finishDownload({ ok: false, headline: `Connection error: ${reason}`, detail: reason });
        };

    } catch (error) {
        if (finished) return;
        finished = true;
        console.error('Failed to start download:', error);
        const reason = error.message || 'Could not start the download.';
        addProgressStep(stepsContainer, `Failed to start: ${reason}`, 'error');
        finishDownload({ ok: false, headline: `Error: ${reason}`, detail: reason });
    }
}

function addProgressStep(container, message, type = 'searching') {
    const step = document.createElement('div');
    step.className = `progress-step ${type}`;
    
    let icon = '🔄';
    if (type === 'searching') icon = '🔍';
    else if (type === 'found') icon = '✅';
    else if (type === 'downloading') icon = '📥';
    else if (type === 'success') icon = '✨';
    else if (type === 'error') icon = '❌';
    
    step.innerHTML = `
        <div class="step-icon">${icon}</div>
        <div class="step-content">
            <div class="step-title">${escapeHtml(message)}</div>
        </div>
    `;
    
    container.appendChild(step);
    container.scrollTop = container.scrollHeight;
}

function closeDownloadModal() {
    document.getElementById('download-progress-modal').style.display = 'none';
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
                <span class="manual-filename" title="${escapeHtml(m.filepath)}">${escapeHtml(m.filename)}</span>
                <button class="btn btn-danger btn-small" onclick="deleteManual(${deviceId}, ${m.id})">
                    ×
                </button>
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
    }
}

async function deleteDevice(deviceId) {
    if (!confirm('Are you sure you want to delete this device and all its manuals?')) {
        return;
    }
    
    try {
        const response = await fetch(`/api/devices/${deviceId}`, {
            method: 'DELETE'
        });
        
        if (!response.ok) throw new Error('Failed to delete device');
        
        showToast('Device deleted', 'success');
        await loadDevices();
    } catch (error) {
        console.error('Failed to delete device:', error);
        showToast('Failed to delete device', 'error');
    }
}

async function performSearch() {
    const query = document.getElementById('search-input').value.trim();
    
    if (!query) {
        showToast('Please enter a search query', 'error');
        return;
    }
    
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
        
        renderSearchResults(data);
    } catch (error) {
        console.error('Search failed:', error);
        container.innerHTML = '<div class="empty-state">Search failed. Please try again.</div>';
    }
}

function renderSearchResults(data) {
    const container = document.getElementById('search-results');
    
    if (data.total_results === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <p>No results found for "${escapeHtml(data.query)}"</p>
                <p>Try different keywords or download more manuals.</p>
            </div>
        `;
        return;
    }
    
    container.innerHTML = `
        <div style="margin-bottom: 15px; color: var(--text-secondary);">
            Found ${data.total_results} result${data.total_results !== 1 ? 's' : ''} for "${escapeHtml(data.query)}"
        </div>
        ${data.results.map(result => `
            <div class="result-item">
                <div class="result-header">
                    <a class="result-title" href="/api/downloads/manuals/${result.manual_id}/file#page=${result.page_number}" target="_blank" rel="noopener">${escapeHtml(result.filename)}</a>
                    <a class="result-meta result-page-link" href="/api/downloads/manuals/${result.manual_id}/file#page=${result.page_number}" target="_blank" rel="noopener">Page ${result.page_number} &nearr;</a>
                </div>
                <div class="result-snippet">${result.snippet}</div>
            </div>
        `).join('')}
    `;
}

async function sendChatMessage() {
    const input = document.getElementById('chat-input');
    const message = input.value.trim();
    
    if (!message) return;

    // If the last model probe failed, don't waste a round-trip: keep the
    // warning banner visible and point the user at Settings instead.
    if (chatModelAvailable === false) {
        showToast('No model is available — update the connection in Settings', 'error');
        return;
    }

    // Add user message to chat
    addMessageToChat('user', message);
    input.value = '';
    
    // Build history BEFORE adding the streaming bubble so its transient
    // content never leaks into the conversation sent to the backend.
    const messages = getChatMessages();

    // Streaming assistant bubble: collapsible thinking / tool-call trace plus
    // the live answer text (see createStreamRenderer).
    const renderer = createStreamRenderer();

    try {
        // EventSource can't POST, so consume the SSE stream with fetch + reader.
        const chatPayload = {
            messages: messages.map(msg => ({
                role: msg.role,
                content: msg.content
            }))
        };
        // The chat device filter scopes manual searches to one device.
        if (currentChatDeviceFilter) {
            chatPayload.device_id = parseInt(currentChatDeviceFilter, 10);
        }

        const response = await fetch('/api/chat/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(chatPayload)
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
                    renderer.fail('Sorry, I encountered an error. Please try again.');
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
        console.error('Chat failed:', error);
        renderer.fail('Sorry, I encountered an error. Please try again.');
    }
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
    container.scrollTop = container.scrollHeight;

    let thinkingDetails = null;
    let thinkingPre = null;
    // Tool-call <details> elements keyed by tool call id.
    const toolCallEls = new Map();
    let contentText = '';
    let settled = false;

    function scroll() {
        container.scrollTop = container.scrollHeight;
    }

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
            scroll();
        },

        appendThinking(delta) {
            if (!thinkingPre) return;
            thinkingPre.textContent += delta;
            // Keep the newest reasoning line in view inside the block.
            thinkingPre.scrollTop = thinkingPre.scrollHeight;
            scroll();
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
                else if (Object.keys(parsed).length) argPreview = `: ${event.arguments}`;
            } catch (e) {
                /* no preview */
            }
            summary.textContent = `🔧 ${event.name || 'tool'}${argPreview}`;

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
            scroll();
        },

        finishToolCall(event) {
            const entry = toolCallEls.get(event.id);
            if (!entry) return;
            if (event.ok) {
                const n = event.result_count ?? 0;
                entry.resultLine.textContent = n > 0
                    ? `Found ${n} result${n !== 1 ? 's' : ''}`
                    : 'No results found';
                entry.resultLine.classList.remove('pending');
            } else {
                entry.resultLine.textContent = 'Search failed';
                entry.resultLine.classList.add('failed');
            }
            scroll();
        },

        appendContent(delta) {
            removePlaceholder();
            contentText += delta;
            // Live markdown preview, throttled to one re-render per animation
            // frame so fast token streams don't thrash the DOM.
            scheduleRender();
            scroll();
        },

        finalize(finalContent) {
            if (settled) return;
            settled = true;
            removePlaceholder();
            this.closeThinking();
            contentText = finalContent;
            renderNow();
            messageDiv.classList.remove('streaming');
            scroll();
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
            scroll();
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
        if (content.includes('Hello! I\'m HomeBrain')) return;
        
        messages.push({ role, content });
    });
    
    return messages;
}

function addMessageToChat(role, content) {
    const container = document.getElementById('chat-messages');
    
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${role}`;
    // Assistant answers are markdown; user messages stay plain escaped text.
    const contentHtml = role === 'assistant' ? renderMarkdown(content) : escapeHtml(content);
    messageDiv.innerHTML = `
        <div class="message-content${role === 'assistant' ? ' markdown-body' : ''}">${contentHtml}</div>
    `;
    messageDiv.querySelector('.message-content')._raw = content;

    container.appendChild(messageDiv);
    container.scrollTop = container.scrollHeight;
}

function switchTab(tabName) {
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
    } catch (error) {
        // The status probe itself failed — treat as unavailable but keep the
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
// Sidebar (docked, pushes content; expanded / collapsed modes)
// ---------------------------------------------------------------------------

// The sidebar is always docked inline and behaves the same at every screen
// size. It defaults to expanded; the collapsed/expanded choice persists.
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

function updateSidebarToggle() {
    const collapsed = document.body.classList.contains('sidebar-collapsed');
    const btn = document.getElementById('sidebar-toggle-btn');
    // Chevron points in the direction the toggle action takes the sidebar.
    // Chevron Left (collapse) / Chevron Right (expand), Feather-style icons.
    const points = collapsed ? '9 18 15 12 9 6' : '15 18 9 12 15 6';
    btn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="${points}"></polyline></svg>`;
    btn.title = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
}

function openAddDeviceSection() {
    // The rail's "+" lives in collapsed mode: expand first, then focus.
    if (document.body.classList.contains('sidebar-collapsed')) {
        toggleSidebar();
    }
    setTimeout(() => {
        const form = document.getElementById('add-device-form');
        form.scrollIntoView({ behavior: 'smooth', block: 'start' });
        document.getElementById('device-name').focus();
    }, 260);
}

// ---------------------------------------------------------------------------
// Settings modal (LLM configuration)
// ---------------------------------------------------------------------------

// Shown inside the API key field when a key is already configured. The real
// value is never sent to the browser — an obscured placeholder just signals
// "a key exists"; leaving the field blank keeps it, typing replaces it.
const API_KEY_PLACEHOLDER = '********';

// Built-in prompt defaults fetched from the API, used by the "Restore
// Default" buttons in Advanced Settings. Keyed by setting name.
let promptDefaults = {};

function restorePromptDefault(settingName) {
    const fieldId = settingName === 'chat_system_prompt'
        ? 'settings-chat-system-prompt'
        : 'settings-search-tool-description';
    document.getElementById(fieldId).value = promptDefaults[settingName] || '';
}

async function openSettingsModal() {
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

    document.getElementById('settings-llm-base-url').value = settings.llm_base_url || '';

    const keyInput = document.getElementById('settings-llm-api-key');
    keyInput.value = '';
    if (settings.llm_api_key_set) {
        keyInput.placeholder = API_KEY_PLACEHOLDER;
        document.getElementById('settings-api-key-hint').textContent =
            'An API key is configured. Leave blank to keep it, or type a new one to replace it.';
    } else {
        keyInput.placeholder = '';
        // No hint when nothing is configured — the empty field says it all.
        document.getElementById('settings-api-key-hint').textContent = '';
    }

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

    // Advanced settings: current prompts + built-in defaults (for Restore).
    promptDefaults = {
        chat_system_prompt: settings.chat_system_prompt_default || '',
        search_tool_description: settings.search_tool_description_default || '',
    };
    document.getElementById('settings-chat-system-prompt').value =
        settings.chat_system_prompt || '';
    document.getElementById('settings-search-tool-description').value =
        settings.search_tool_description || '';
    // Always open the modal with Advanced Settings collapsed.
    document.getElementById('advanced-settings-section').open = false;

    document.getElementById('settings-modal').style.display = 'flex';
}

function closeSettingsModal() {
    document.getElementById('settings-modal').style.display = 'none';
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
    // Only send a freshly typed key; if blank, the server probes with the saved one.
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
            } catch (e) { /* activation expired — list is still populated */ }
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

// Clicking (or keyboard-opening) the model dropdown refreshes its options
// from the server first, then opens the list — so users always see fresh
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

    // Keyboard access: Enter / Space / ArrowDown open the picker natively —
    // intercept them the same way.
    select.addEventListener('keydown', (e) => {
        if (!['Enter', ' ', 'ArrowDown'].includes(e.key)) return;
        e.preventDefault();
        fetchLlmModels({ openPicker: true });
    });
}

async function handleSettingsSave(event) {
    event.preventDefault();

    const payload = {
        llm_base_url: document.getElementById('settings-llm-base-url').value.trim(),
        llm_model: document.getElementById('settings-llm-model').value.trim(),
        // Prompts are always sent; the server treats a blank value as
        // "restore the built-in default".
        chat_system_prompt: document.getElementById('settings-chat-system-prompt').value.trim(),
        search_tool_description: document.getElementById('settings-search-tool-description').value.trim(),
    };
    // Blank API key field means "keep the existing key" — omit it entirely.
    // (The obscured placeholder is only a visual hint, never a value.)
    const apiKey = document.getElementById('settings-llm-api-key').value.trim();
    if (apiKey) payload.llm_api_key = apiKey;

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
        closeSettingsModal();
        showToast('Settings saved');
        // If the chat tab is open behind the modal, re-probe so a fixed
        // configuration clears its warning banner right away.
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
