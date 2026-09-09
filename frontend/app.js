// HomeBrain Frontend Application

document.addEventListener('DOMContentLoaded', () => {
    initializeApp();
});

let devices = [];
let currentDeviceFilter = null;
// Device id currently targeted by the hidden manual-upload input.
let uploadTargetDeviceId = null;

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
    const select = document.getElementById('device-filter');
    select.innerHTML = '<option value="">All Devices</option>' + 
        devices.map(device => 
            `<option value="${device.id}">${escapeHtml(device.name)}</option>`
        ).join('');
    
    if (currentDeviceFilter) {
        select.value = currentDeviceFilter;
    }
    
    select.addEventListener('change', (e) => {
        currentDeviceFilter = e.target.value || null;
    });
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
    
    // Add user message to chat
    addMessageToChat('user', message);
    input.value = '';
    
    // Show loading indicator
    const loadingId = addLoadingIndicator();
    
    try {
        // Get conversation history
        const messages = getChatMessages();
        
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                messages: messages.map(msg => ({
                    role: msg.role,
                    content: msg.content
                }))
            })
        });
        
        const data = await response.json();
        
        // Remove loading indicator
        removeMessage(loadingId);
        
        // Add assistant response
        addMessageToChat('assistant', data.message);
        
    } catch (error) {
        console.error('Chat failed:', error);
        removeMessage(loadingId);
        addMessageToChat('assistant', 'Sorry, I encountered an error. Please try again.');
    }
}

function getChatMessages() {
    const container = document.getElementById('chat-messages');
    const messages = [];
    
    container.querySelectorAll('.message').forEach(msgEl => {
        const role = msgEl.classList.contains('user') ? 'user' : 'assistant';
        const content = msgEl.querySelector('.message-content').textContent;
        
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
    messageDiv.innerHTML = `
        <div class="message-content">${escapeHtml(content)}</div>
    `;
    
    container.appendChild(messageDiv);
    container.scrollTop = container.scrollHeight;
}

function addLoadingIndicator() {
    const container = document.getElementById('chat-messages');
    const id = 'loading-' + Date.now();
    
    const messageDiv = document.createElement('div');
    messageDiv.className = 'message assistant';
    messageDiv.id = id;
    messageDiv.innerHTML = `
        <div class="message-content">Thinking...</div>
    `;
    
    container.appendChild(messageDiv);
    container.scrollTop = container.scrollHeight;
    
    return id;
}

function removeMessage(id) {
    const element = document.getElementById(id);
    if (element) {
        element.remove();
    }
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
    btn.textContent = collapsed ? '>' : '<';
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
    document.getElementById('settings-llm-model').value = settings.llm_model || '';
    const keyInput = document.getElementById('settings-llm-api-key');
    keyInput.value = '';
    document.getElementById('settings-api-key-hint').textContent = settings.llm_api_key_set
        ? 'An API key is configured. Leave blank to keep it, or type a new one to replace it.'
        : 'No API key is set (not required for Ollama). Enter one to configure it.';

    document.getElementById('settings-modal').style.display = 'flex';
}

function closeSettingsModal() {
    document.getElementById('settings-modal').style.display = 'none';
}

async function handleSettingsSave(event) {
    event.preventDefault();

    const payload = {
        llm_base_url: document.getElementById('settings-llm-base-url').value.trim(),
        llm_model: document.getElementById('settings-llm-model').value.trim(),
    };
    // Blank API key field means "keep the existing key" — omit it entirely.
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
    } catch (error) {
        console.error('Failed to save settings:', error);
        showToast('Failed to save settings', 'error', error.message);
    } finally {
        saveBtn.disabled = false;
    }
}
