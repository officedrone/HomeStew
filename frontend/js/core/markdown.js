// HomeStew - Markdown rendering for chat answers + internal device links.


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
    // and strip referrer info from outbound links. Internal app links
    // (href="#...", e.g. the #edit-device-<id> links the device tool hands
    // back) must stay in-page - they are handled by a click listener.
    DOMPurify.addHook('afterSanitizeAttributes', (node) => {
        if (node.tagName !== 'A') return;
        // Internal app links stay in-page: they are routed by handleInternalLink
        // rather than followed. Checked on the RESOLVED hash because a model may
        // emit an absolute URL for them (http://host/#fetch-manuals-3).
        if (internalDeviceLink(node.getAttribute('href'))) return;
        node.target = '_blank';
        node.rel = 'noopener noreferrer';
    });
}

// ---------------------------------------------------------------------------
// Internal device links handed out by the manage_devices tool.
// ---------------------------------------------------------------------------

// Maps an anchor href to { action, deviceId }, or null when it is not one of
// our internal links. Deliberately resolves the href first: small models often
// write the absolute form ('http://localhost:8000/#fetch-manuals-8') instead of
// a bare '#fetch-manuals-8', and an attribute-prefix selector would miss it.
function internalDeviceLink(href) {
    const raw = String(href || '');
    if (!raw) return null;
    let hash;
    try {
        const url = new URL(raw, window.location.origin);
        // Only our own origin counts - never route a link to another host.
        if (url.origin !== window.location.origin) return null;
        hash = url.hash;
    } catch (e) {
        return null;
    }
    const m = /^#(edit-device|fetch-manuals)-(\d+)$/.exec(hash);
    if (!m) return null;
    return { action: m[1], deviceId: parseInt(m[2], 10) };
}

// Click handler for those links, whatever container rendered them (chat bubbles,
// tool traces, ...). Returns true when the click was consumed.
function handleInternalLink(e) {
    const link = e.target.closest && e.target.closest('a');
    if (!link) return false;
    const target = internalDeviceLink(link.getAttribute('href'));
    if (!target) return false;
    // Never let the browser touch the URL hash: restoreActiveTab() reads it on
    // the next load and would otherwise reopen this tab/modal state.
    e.preventDefault();
    if (target.action === 'fetch-manuals') {
        openDeviceEditorAndFetchById(target.deviceId);
    } else {
        openDeviceEditorById(target.deviceId);
    }
    return true;
}

function renderMarkdown(text) {
    const source = text || '';
    // Graceful fallback when the vendor scripts failed to load: plain text.
    if (!window.marked) return escapeHtml(source).replace(/\n/g, '<br>');
    const html = marked.parse(source);
    return window.DOMPurify ? DOMPurify.sanitize(html) : html;
}

// Internal links produced by the device tool:
//   #edit-device-<id>   - the LLM refuses a delete and hands over the editor
//   #fetch-manuals-<id> - a freshly created device: open its editor AND start
//                         the manual search so the user only has to approve.
// Bound on document (capture) rather than on #chat-messages: answers are
// rendered dynamically, and links can also appear in tool traces.
function wireMarkdownLinks() {
    document.addEventListener('click', handleInternalLink, true);
}
