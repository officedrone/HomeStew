// HomeBrain Frontend Application

document.addEventListener('DOMContentLoaded', () => {
    initializeApp();
});

let devices = [];
let currentDeviceFilter = null;

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
        <div class="device-item" data-id="${device.id}">
            <div class="device-name">${escapeHtml(device.name)}</div>
            <div class="device-meta">${escapeHtml(device.brand)} ${escapeHtml(device.model)}</div>
            ${device.serial_number ? `<div class="device-meta">Serial: ${escapeHtml(device.serial_number)}</div>` : ''}
            ${device.product_number ? `<div class="device-meta">Product: ${escapeHtml(device.product_number)}</div>` : ''}
            <div class="device-meta">${device.manual_count} manual${device.manual_count !== 1 ? 's' : ''}</div>
            <div class="device-actions">
                <button class="btn btn-primary btn-small" onclick="downloadManuals(${device.id})">
                    Download Manuals
                </button>
                <button class="btn btn-secondary btn-small" onclick="editDevice(${device.id})">
                    Edit
                </button>
                <button class="btn btn-danger btn-small" onclick="deleteDevice(${device.id})">
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
    const device = devices.find(d => d.id === deviceId);
    
    if (!device || !device.attributes || device.attributes.length === 0) {
        container.innerHTML = '<div class="empty-state">No custom attributes</div>';
        return;
    }
    
    container.innerHTML = device.attributes.map(attr => `
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
    const statusText = document.getElementById('download-current-step');
    const closeBtn = document.getElementById('close-download-modal');
    
    modal.style.display = 'flex';
    stepsContainer.innerHTML = '';
    closeBtn.style.display = 'none';
    statusText.textContent = 'Initializing...';
    
    try {
        // Use EventSource for Server-Sent Events streaming
        const eventSource = new EventSource(`/api/downloads/stream/${deviceId}`);
        
        eventSource.onmessage = function(event) {
            const data = JSON.parse(event.data);
            
            switch(data.type) {
                case 'step':
                    // Add a progress step
                    addProgressStep(stepsContainer, data.message, data.status || 'searching');
                    statusText.textContent = data.message;
                    break;
                    
                case 'complete':
                    eventSource.close();
                    if (data.success) {
                        addProgressStep(stepsContainer, `✅ Successfully downloaded ${data.downloaded_count} manual(s)`, 'success');
                        statusText.textContent = `Download complete! ${data.downloaded_count} manual(s) downloaded`;
                        showToast(`Downloaded ${data.downloaded_count} manuals!`, 'success');
                    } else {
                        addProgressStep(stepsContainer, `❌ Download failed: ${data.error_detail}`, 'error');
                        statusText.textContent = 'Download failed';
                        showToast(data.error_detail || 'Download failed', 'error');
                    }
                    closeBtn.style.display = 'inline-block';
                    break;
                    
                case 'error':
                    eventSource.close();
                    addProgressStep(stepsContainer, `❌ Error: ${data.message}`, 'error');
                    statusText.textContent = 'Error occurred';
                    showToast(data.message, 'error');
                    closeBtn.style.display = 'inline-block';
                    break;
            }
        };
        
        eventSource.onerror = function(error) {
            console.error('EventSource failed:', error);
            eventSource.close();
            addProgressStep(stepsContainer, '❌ Connection lost', 'error');
            statusText.textContent = 'Connection error';
            showToast('Download connection error', 'error');
            closeBtn.style.display = 'inline-block';
        };
        
    } catch (error) {
        console.error('Failed to start download:', error);
        addProgressStep(stepsContainer, `❌ Failed to start: ${error.message}`, 'error');
        statusText.textContent = 'Error';
        showToast('Failed to start download', 'error');
        closeBtn.style.display = 'inline-block';
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
        const params = new URLSearchParams({ query });
        if (currentDeviceFilter) {
            params.append('device_id', currentDeviceFilter);
        }
        
        const response = await fetch(`/api/search?${params}`);
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
                    <span class="result-title">${escapeHtml(result.filename)}</span>
                    <span class="result-meta">Page ${result.page_number}</span>
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
