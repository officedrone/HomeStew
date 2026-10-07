// HomeStew - Search realm: query + client-side pagination.


// ---------------------------------------------------------------------------
// Search results: client-side pagination.
// The API returns every match for a query (no limit), so switching pages or
// per-page size never re-queries. 'searchPerPage' persists the user's choice
// (10 / 25 / 50 / all) across reloads, same localStorage pattern as theme and
// sidebar prefs (wrapped in try/catch for private-browsing mode).
let lastSearchData = null;
let searchPage = 1;

function getSearchPerPage() {
    let saved = null;
    try { saved = localStorage.getItem('searchPerPage'); } catch (e) { /* private mode */ }
    if (saved === 'all') return Infinity;
    const n = parseInt(saved, 10);
    return [10, 25, 50].includes(n) ? n : 10;
}

function saveSearchPerPage(value) {
    try { localStorage.setItem('searchPerPage', value); } catch (e) { /* private mode */ }
}

// Windowed page list with ellipses: 1 … 4 [5] 6 … 20. Returns strings; '…'
// marks a collapsed gap.
function searchPageNumbers(current, total) {
    const pages = [];
    for (let p = 1; p <= total; p++) {
        if (total <= 7 || p === 1 || p === total || Math.abs(p - current) <= 1) {
            pages.push(String(p));
        } else if (pages[pages.length - 1] !== '\u2026') {
            pages.push('\u2026');
        }
    }
    return pages;
}

function goToSearchPage(page) {
    searchPage = page;
    renderSearchResults(lastSearchData);
    // Bring the top of the results back into view after flipping a page.
    document.getElementById('search-results').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function changeSearchPerPage(value) {
    saveSearchPerPage(value);
    searchPage = 1;
    renderSearchResults(lastSearchData);
}

async function performSearch() {
    const query = document.getElementById('search-input').value.trim();
    
    if (!query) {
        showToast('Please enter a search query', 'error');
        return;
    }
    if (!beginOp('search')) return;
    
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
        
        // Cache the full result set and start on page 1; paging from here
        // on is pure re-render (see goToSearchPage / changeSearchPerPage).
        lastSearchData = data;
        searchPage = 1;
        renderSearchResults(data);
    } catch (error) {
        console.error('Search failed:', error);
        container.innerHTML = '<div class="empty-state">Search failed. Please try again.</div>';
    } finally {
        endOp('search');
    }
}

function renderSearchResults(data) {
    const container = document.getElementById('search-results');
    
    if (!data || data.total_results === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <p>No results found for "${escapeHtml(data ? data.query : '')}"</p>
                <p>Try different keywords or download more manuals.</p>
            </div>
        `;
        return;
    }
    
    // Slice the cached result set for the current page. perPage = Infinity
    // ('All') shows everything on a single page.
    const perPage = getSearchPerPage();
    const total = data.results.length;
    const totalPages = perPage === Infinity ? 1 : Math.max(1, Math.ceil(total / perPage));
    searchPage = Math.min(Math.max(searchPage, 1), totalPages);
    const start = perPage === Infinity ? 0 : (searchPage - 1) * perPage;
    const pageResults = perPage === Infinity
        ? data.results
        : data.results.slice(start, start + perPage);
    const rangeEnd = start + pageResults.length;

    // Page numbers / Prev / Next only exist when there is more than one page;
    // the bar itself always renders so the Per-page select stays reachable.
    let nav = '';
    if (totalPages > 1) {
        nav += `<button type="button" class="page-btn" ${searchPage === 1 ? 'disabled' : ''} onclick="goToSearchPage(${searchPage - 1})">&laquo; Prev</button>`;
        for (const p of searchPageNumbers(searchPage, totalPages)) {
            if (p === '\u2026') {
                nav += '<span class="page-ellipsis">\u2026</span>';
            } else {
                const active = parseInt(p, 10) === searchPage ? ' active' : '';
                nav += `<button type="button" class="page-btn${active}" ${active ? 'aria-current="page"' : ''} onclick="goToSearchPage(${p})">${p}</button>`;
            }
        }
        nav += `<button type="button" class="page-btn" ${searchPage === totalPages ? 'disabled' : ''} onclick="goToSearchPage(${searchPage + 1})">Next &raquo;</button>`;
    }
    const perPageValue = perPage === Infinity ? 'all' : String(perPage);
    const perPageSelect = `
        <label class="per-page-label">Per page
            <select class="per-page-select" onchange="changeSearchPerPage(this.value)">
                ${['10', '25', '50', 'all'].map(v =>
                    `<option value="${v}"${v === perPageValue ? ' selected' : ''}>${v === 'all' ? 'All' : v}</option>`
                ).join('')}
            </select>
        </label>`;
    const pagination = `
        <div class="search-pagination">
            ${nav}
            ${perPageSelect}
        </div>`;

    container.innerHTML = `
        <div class="search-results-count">
            Showing ${start + 1}\u2013${rangeEnd} of ${total} result${total !== 1 ? 's' : ''} for "${escapeHtml(data.query)}"
        </div>
        ${pageResults.map(result => {
            // manual_id 0 = a hit in the device's own record (details /
            // custom attributes), not a PDF page - no file link to open.
            const header = result.manual_id === 0
                ? `
                    <span class="result-title">${escapeHtml(result.filename)}</span>
                    <a class="result-meta result-page-link" href="#" onclick="editDevice(${result.device_id}); return false;">Device entry &nearr;</a>
                `
                : `
                    <a class="result-title" href="/api/downloads/manuals/${result.manual_id}/file#page=${result.page_number}" target="_blank" rel="noopener">${escapeHtml(result.filename)}</a>
                    <a class="result-meta result-page-link" href="/api/downloads/manuals/${result.manual_id}/file#page=${result.page_number}" target="_blank" rel="noopener">Page ${result.page_number} &nearr;</a>
                `;
            // Footer naming the device this hit belongs to. The #edit-device-
            // <id> href is intercepted globally (handleInternalLink) and opens
            // that device's Edit modal, same convention as chat citations.
            const deviceLine = result.device_name ? `
                <div class="result-device">Relevant Device URL: <a href="#edit-device-${result.device_id}" title="Open ${escapeHtml(result.device_name)}">${escapeHtml(result.device_name)}</a></div>` : '';
            return `
            <div class="result-item${result.manual_id === 0 ? ' result-device-entry' : ''}">
                <div class="result-header">${header}</div>
                <div class="result-snippet">${result.snippet}</div>${deviceLine}
            </div>`;
        }).join('')}
        ${pagination}
    `;
}

// ---------------------------------------------------------------------------
// Search wiring (was part of setupEventListeners in the single-file app).
// ---------------------------------------------------------------------------

function wireSearchEvents() {
    document.getElementById('search-btn').addEventListener('click', performSearch);
    document.getElementById('search-input').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') performSearch();
    });

    // Device filter: search keeps a selection independent from the Chat tab.
    document.getElementById('device-filter').addEventListener('change', (e) => {
        currentDeviceFilter = e.target.value || null;
    });
}
