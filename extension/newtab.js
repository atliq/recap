document.addEventListener('DOMContentLoaded', () => {
    const statusIndicator = document.getElementById('status-indicator');
    const searchInput = document.getElementById('query-input');
    const searchBtn = document.getElementById('search-button');
    const settingsBtn = document.getElementById('settings-btn');
    const recentContainer = document.getElementById('recent-container');
    const statPages = document.getElementById('stat-pages');
    const statEntities = document.getElementById('stat-entities');
    const statSize = document.getElementById('stat-size');

    // Chat elements
    const chatContainer = document.getElementById('chat-container');
    const chatThread = document.getElementById('chat-thread');
    const chatInput = document.getElementById('chat-input');
    const chatSendBtn = document.getElementById('chat-send-btn');
    const newChatBtn = document.getElementById('new-chat-btn');
    const dashboardContent = document.getElementById('dashboard-content');
    const spinner = document.getElementById('loading-spinner');

    // ── Chat State ────────────────────────────────────────────────────────
    // history stores only the text content (no context injections), clean for display
    let chatHistory = []; // [{role, content}]
    let currentApiBase = 'http://localhost:8000';
    let currentLlm = 'groq';
    let currentModel = null;

    // ── Bootstrap ─────────────────────────────────────────────────────────
    chrome.storage.sync.get(['apiUrl'], (r) => {
        currentApiBase = r.apiUrl || currentApiBase;
        checkBackend(currentApiBase);
        loadStats(currentApiBase);
        loadRecent(currentApiBase);
        loadResurface(currentApiBase);
    });

    chrome.storage.sync.get(['preferredLlm', 'llmModel'], (r) => {
        currentLlm = r.preferredLlm || 'groq';
        currentModel = (r.llmModel || '').trim() || null;
    });

    settingsBtn.addEventListener('click', () => chrome.runtime.openOptionsPage());

    const flashcardBtn = document.getElementById('flashcard-btn');
    if (flashcardBtn) flashcardBtn.addEventListener('click', () => {
        chrome.tabs.create({ url: chrome.runtime.getURL('flashcards.html') });
    });

    const digestBtn = document.getElementById('digest-btn');
    if (digestBtn) digestBtn.addEventListener('click', () => {
        chrome.tabs.create({ url: chrome.runtime.getURL('digest.html') });
    });

    document.getElementById('graph-teaser-btn').addEventListener('click', () => {
        window.location.href = 'graph.html';
    });

    // ── First Search → Starts Chat ────────────────────────────────────────
    const doSearch = () => {
        const q = searchInput.value.trim();
        if (!q) return;
        searchInput.value = '';
        startChat(q);
    };

    searchBtn.addEventListener('click', doSearch);
    searchInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });

    // ── Chat Send ─────────────────────────────────────────────────────────
    chatSendBtn.addEventListener('click', sendChatMessage);
    chatInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') sendChatMessage(); });

    // ── New Chat ──────────────────────────────────────────────────────────
    newChatBtn.addEventListener('click', () => {
        chatHistory = [];
        chatThread.innerHTML = '';
        chatContainer.style.display = 'none';
        dashboardContent.style.display = 'flex';
        searchInput.value = '';
        searchInput.focus();
    });

    // ── Core Chat Logic ───────────────────────────────────────────────────
    function startChat(firstMessage) {
        // Transition UI: hide dashboard, show chat
        dashboardContent.style.display = 'none';
        spinner.style.display = 'none';
        chatThread.innerHTML = '';
        chatContainer.style.display = 'flex';
        chatInput.focus();

        sendTurn(firstMessage);
    }

    function sendChatMessage() {
        const msg = chatInput.value.trim();
        if (!msg) return;
        chatInput.value = '';
        sendTurn(msg);
    }

    async function sendTurn(message) {
        // Append user bubble
        appendBubble('user', message);

        // Append thinking indicator
        const thinkingEl = appendThinking();
        chatThread.scrollTop = chatThread.scrollHeight;

        try {
            const res = await fetch(`${currentApiBase}/chat`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message,
                    history: chatHistory.slice(-40),
                    top_k: 5,
                    llm: currentLlm,
                    model: currentModel,
                }),
            });

            thinkingEl.remove();

            if (!res.ok) throw new Error('Backend error');
            const data = await res.json();

            // Save to history (store clean messages, not context-injected)
            chatHistory.push({ role: 'user', content: message });
            chatHistory.push({ role: 'assistant', content: data.message });

            appendBubble('assistant', data.message, data.sources, {
                retrieval_ms: data.retrieval_time_ms,
                gen_ms: data.generation_time_ms,
            });
        } catch (err) {
            thinkingEl.remove();
            appendError('Search failed. Is the backend running?');
        }

        chatThread.scrollTop = chatThread.scrollHeight;
    }

    function appendBubble(role, content, sources = [], meta = {}) {
        const msgEl = document.createElement('div');
        msgEl.className = `chat-msg ${role}`;

        const bubble = document.createElement('div');
        bubble.className = 'chat-bubble';
        bubble.innerHTML = formatMarkdown(content);
        msgEl.appendChild(bubble);

        if (role === 'assistant') {
            // Meta line: timing
            if (meta.retrieval_ms != null || meta.gen_ms != null) {
                const metaEl = document.createElement('div');
                metaEl.className = 'chat-meta';
                const parts = [];
                if (meta.retrieval_ms != null) parts.push(`⚡ ${Math.round(meta.retrieval_ms)}ms retrieval`);
                if (meta.gen_ms != null) parts.push(`🤖 ${(meta.gen_ms / 1000).toFixed(1)}s gen`);
                metaEl.textContent = parts.join(' · ');
                msgEl.appendChild(metaEl);
            }

            // Sources toggle
            if (sources && sources.length > 0) {
                const toggleBtn = document.createElement('button');
                toggleBtn.className = 'chat-sources-toggle';
                toggleBtn.innerHTML = `<svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7"/></svg> ${sources.length} source${sources.length !== 1 ? 's' : ''}`;

                const sourcesList = document.createElement('div');
                sourcesList.className = 'chat-sources-list';

                sources.forEach(src => {
                    const item = document.createElement('div');
                    item.className = 'chat-source-item';
                    item.innerHTML = `
                        <div class="chat-source-title">${esc(src.title || 'Untitled')}</div>
                        <div class="chat-source-url">${esc(shortUrl(src.url))}</div>
                    `;
                    item.addEventListener('click', () => window.open(safeUrl(src.url), '_blank'));
                    sourcesList.appendChild(item);
                });

                toggleBtn.addEventListener('click', () => {
                    sourcesList.classList.toggle('open');
                    const isOpen = sourcesList.classList.contains('open');
                    toggleBtn.innerHTML = `<svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="${isOpen ? 'M5 15l7-7 7 7' : 'M19 9l-7 7-7-7'}"/></svg> ${sources.length} source${sources.length !== 1 ? 's' : ''}`;
                });

                msgEl.appendChild(toggleBtn);
                msgEl.appendChild(sourcesList);
            }
        }

        chatThread.appendChild(msgEl);
        return msgEl;
    }

    function appendThinking() {
        const msgEl = document.createElement('div');
        msgEl.className = 'chat-msg assistant';
        msgEl.innerHTML = `
            <div class="chat-thinking">
                <div class="thinking-dots">
                    <span></span><span></span><span></span>
                </div>
                Searching memory...
            </div>`;
        chatThread.appendChild(msgEl);
        return msgEl;
    }

    function appendError(msg) {
        const el = document.createElement('div');
        el.className = 'error-msg';
        el.style.cssText = 'margin: 0 auto; max-width: 600px;';
        el.textContent = msg;
        chatThread.appendChild(el);
    }

    // ── Daily Resurface ───────────────────────────────────────────────────
    async function loadResurface(apiBase) {
        const section = document.getElementById('resurface-section');
        const container = document.getElementById('resurface-container');
        // Hide + clear up front so a later empty refresh doesn't leave stale cards.
        section.style.display = 'none';
        container.innerHTML = '';
        try {
            const res = await fetch(`${apiBase}/resurface/daily`);
            if (!res.ok) return;
            const data = await res.json();
            const items = data.resurfaces || [];
            if (items.length === 0) return;

            section.style.display = 'block';
            items.forEach(item => {
                const card = document.createElement('div');
                card.className = 'resurface-card';

                const d = Number(item.days_ago);
                const daysLabel = d === 1
                    ? '1 day ago'
                    : d > 1
                        ? `${d} days ago`
                        : 'recently';

                card.innerHTML = `
                    <div class="resurface-age">${esc(daysLabel)}</div>
                    <div class="resurface-title">${esc(item.title || 'Untitled')}</div>
                    ${item.snippet ? `<div class="resurface-snippet">${esc(item.snippet)}</div>` : ''}
                    <div class="resurface-actions">
                        <button class="btn-resurface-open">Read again</button>
                        <button class="btn-resurface-chat">Ask RECAP</button>
                    </div>
                `;

                card.querySelector('.btn-resurface-open').addEventListener('click', () => {
                    window.open(safeUrl(item.url), '_blank');
                });
                card.querySelector('.btn-resurface-chat').addEventListener('click', () => {
                    const title = item.title || item.url;
                    dashboardContent.style.display = 'none';
                    chatThread.innerHTML = '';
                    chatHistory = [];
                    chatContainer.style.display = 'flex';
                    chatInput.focus();
                    sendTurn(`Tell me what I read about: ${title}`);
                });

                container.appendChild(card);
            });
        } catch (e) {
            // Resurface is non-critical - fail silently
        }
    }

    // ── Recent Pages + Annotations ────────────────────────────────────────
    async function loadRecent(apiBase) {
        try {
            const [refsRes, annotationsRes] = await Promise.all([
                fetch(`${apiBase}/references`),
                fetch(`${apiBase}/annotations`).catch(() => null),
            ]);

            if (!refsRes.ok) throw new Error('Failed to fetch references');
            const response = await refsRes.json();
            const data = (response.references || []).slice(0, 6);

            // Build annotation map for quick lookup
            const annotationMap = {};
            if (annotationsRes && annotationsRes.ok) {
                const annData = await annotationsRes.json();
                (annData.annotations || []).forEach(a => { annotationMap[a.url] = a.note; });
            }

            if (!data || data.length === 0) {
                recentContainer.innerHTML = `<div class="empty-recent">No pages indexed yet. Start browsing!</div>`;
                return;
            }

            recentContainer.innerHTML = '';
            data.forEach(ref => {
                const card = document.createElement('div');
                card.className = 'recent-card';
                const summary = ref.summary || ref.meta_description || '';
                const existingNote = annotationMap[ref.url] || '';

                let ignoreDomain = '';
                try { ignoreDomain = new URL(ref.url).hostname; } catch (_) { ignoreDomain = ''; }
                const ignoreBtnHtml = `
                    <button class="ignore-btn" data-domain="${esc(ignoreDomain)}" title="Stop tracking ${esc(ignoreDomain)} permanently and delete everything already indexed from it">
                        <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                            <path stroke-linecap="round" stroke-linejoin="round" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" />
                        </svg>
                        Ignore
                    </button>
                `;
                const deleteBtnHtml = `
                    <button class="ignore-btn page-delete-btn" data-url="${esc(ref.url)}" title="Delete just this page from your index (its text, search entries and vectors)">
                        <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                            <path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                        </svg>
                        Delete
                    </button>
                `;

                card.innerHTML = `
                  <a href="${safeUrl(ref.url)}" class="rc-link">
                    <div class="rc-title" title="${esc(ref.title || 'Untitled')}">${esc(ref.title || 'Untitled')}</div>
                    <div class="rc-url">${esc(shortUrl(ref.url))}</div>
                    ${summary ? `<div class="rc-summary">${esc(summary)}</div>` : ''}
                  </a>
                  <div class="rc-meta">
                    ${ref.content_type ? `<span class="rc-badge">${esc(ref.content_type)}</span>` : ''}
                    <div style="flex:1"></div>
                    ${deleteBtnHtml}
                    ${ignoreBtnHtml}
                  </div>
                  <div class="annotation-area">
                    <button class="annotation-toggle" data-url="${esc(ref.url)}">
                      <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M15.232 5.232l3.536 3.536M9 13l6.586-6.586a2 2 0 112.828 2.828L11.828 15.828a2 2 0 01-1.414.586H9v-2a2 2 0 01.586-1.414z"/>
                      </svg>
                      ${existingNote ? 'Edit note' : 'Add note'}
                    </button>
                    <div class="annotation-body" data-url="${esc(ref.url)}">
                      ${existingNote ? `<div class="annotation-existing">${esc(existingNote)}</div>` : ''}
                      <textarea class="annotation-input" placeholder="Your thoughts on this page..." rows="2">${esc(existingNote)}</textarea>
                      <button class="annotation-save-btn" data-url="${esc(ref.url)}">Save note</button>
                    </div>
                  </div>
                `;

                recentContainer.appendChild(card);
            });

            // Annotation toggle listeners
            document.querySelectorAll('.annotation-toggle').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.preventDefault();
                    const card = btn.closest('.recent-card');
                    const body = card && card.querySelector('.annotation-body');
                    if (body) body.classList.toggle('open');
                });
            });

            // Annotation save listeners
            document.querySelectorAll('.annotation-save-btn').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    e.preventDefault();
                    const url = btn.getAttribute('data-url');
                    const body = btn.closest('.annotation-body');
                    const textarea = body.querySelector('.annotation-input');
                    const note = textarea.value.trim();
                    if (!note) return;

                    try {
                        await fetch(`${apiBase}/annotate`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ url, note }),
                        });
                        // Update display
                        const existingDiv = body.querySelector('.annotation-existing');
                        if (existingDiv) {
                            existingDiv.textContent = note;
                        } else {
                            const div = document.createElement('div');
                            div.className = 'annotation-existing';
                            div.textContent = note;
                            body.insertBefore(div, textarea);
                        }
                        // Update toggle label
                        const card = body.closest('.recent-card');
                        const toggle = card && card.querySelector('.annotation-toggle');
                        if (toggle) {
                            toggle.innerHTML = `<svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M15.232 5.232l3.536 3.536M9 13l6.586-6.586a2 2 0 112.828 2.828L11.828 15.828a2 2 0 01-1.414.586H9v-2a2 2 0 01.586-1.414z"/></svg> Edit note`;
                        }
                        body.classList.remove('open');
                    } catch (err) {
                        console.warn('Failed to save annotation:', err);
                    }
                });
            });

            // Ignore domain listeners (data-domain excludes the per-page delete
            // button, which shares .ignore-btn only for styling)
            document.querySelectorAll('.ignore-btn[data-domain]').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    const domain = btn.getAttribute('data-domain');
                    if (confirm(`Stop tracking ${domain} permanently and delete ALL its indexed pages?`)) {
                        chrome.runtime.sendMessage({ action: 'ignoreDomain', domain }, (res) => {
                            if (res && res.success) {
                                alert(`${domain} is now ignored: ${res.deleted ?? 0} indexed page(s) deleted, and it won't be tracked again.`);
                                loadRecent(apiBase);
                            } else {
                                alert('Failed to ignore domain.');
                            }
                        });
                    }
                });
            });

            // Per-page delete listeners
            document.querySelectorAll('.page-delete-btn').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    const url = btn.getAttribute('data-url');
                    if (!url) return;
                    if (confirm('Delete this page from your index?')) {
                        chrome.runtime.sendMessage({ action: 'deleteUrl', url }, (res) => {
                            if (res && res.success) {
                                loadRecent(apiBase);
                            } else {
                                alert('Failed to delete page.');
                            }
                        });
                    }
                });
            });

        } catch (e) {
            recentContainer.innerHTML = `<div class="empty-recent">Backend disconnected. Unable to load recent pages.</div>`;
        }
    }

    // ── Stats ─────────────────────────────────────────────────────────────
    async function loadStats(apiBase) {
        try {
            const res = await fetch(`${apiBase}/stats`);
            if (!res.ok) return;
            const data = await res.json();
            statPages.textContent = data.total_pages || 0;
            statEntities.textContent = data.total_entities || 0;
            statSize.textContent = data.index_size_mb != null ? `${data.index_size_mb} MB` : '0 MB';
        } catch (e) {
            console.warn('Failed to load stats:', e);
        }
    }

    async function checkBackend(apiBase) {
        try {
            const res = await fetch(`${apiBase}/health`);
            if (res.ok) {
                statusIndicator.classList.add('connected');
                statusIndicator.querySelector('.status-text').textContent = 'Connected';
            }
        } catch {
            statusIndicator.classList.remove('connected');
            statusIndicator.querySelector('.status-text').textContent = 'Disconnected';
        }
    }

    // ── Utilities ─────────────────────────────────────────────────────────
    function formatMarkdown(text) {
        if (!text) return '';
        let html = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');
        html = html.replace(/`(.*?)`/g, '<code>$1</code>');
        html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m, txt, url) => `<a href="${safeUrl(url)}" target="_blank" rel="noopener">${txt}</a>`);
        html = html.replace(/^#{1,3}\s+(.+)/gm, '<strong>$1</strong>');
        html = html.replace(/^[-*]\s+(.+)/gm, '• $1<br>');
        html = html.split(/\n\n+/).map(p => `<p>${p.replace(/\n/g, '<br>')}</p>`).join('');
        return html;
    }

    function esc(str) {
        return String(str ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function safeUrl(u) {
        return /^https?:\/\//i.test(String(u)) ? String(u) : '#';
    }

    function shortUrl(url) {
        try { return new URL(url).hostname; } catch { return url; }
    }
});