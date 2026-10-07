// HomeStew - Chat realm: sending, streaming, tool traces, images and model status.


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

    // The message is on its way: the one-shot "attach a photo" hint from the
    // New Device flow has served its purpose.
    hideChatAttachTip();

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
    hideChatAttachTip();
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
                    // The calendar changed: refresh the dashboard feed and
                    // stat cards and, if that tab is open behind the chat,
                    // its list too.
                    loadUpcomingEvents();
                    loadDashboardStats();
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

// Ask the backend to probe the saved LLM connection (the same model-list call
// the Settings "Refresh Models" button uses) and reflect the outcome in the chat:
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

// ---------------------------------------------------------------------------
// Chat wiring (was part of setupEventListeners in the single-file app).
// ---------------------------------------------------------------------------

function wireChatEvents() {
    // Device filter: chat keeps a selection independent from the Search tab.
    document.getElementById('chat-device-filter').addEventListener('change', (e) => {
        currentChatDeviceFilter = e.target.value || null;
    });

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

    // Chat model warning banner: takes the user straight to the Settings page.
    document.getElementById('chat-open-settings-btn').addEventListener('click', () => switchTab('settings'));

    // Model switcher in the chat toolbar: picking a model saves it right away
    // (same settings the Settings page writes) and re-runs the availability probe.
    document.getElementById('chat-model-select').addEventListener('change', handleChatModelChange);
}
