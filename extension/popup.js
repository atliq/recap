/**
 * RECAP v2 - Popup Controller
 *
 * Handles search, result display, references, highlights, stats, and preferences.
 * Communicates with background.js via chrome.runtime messages.
 */

document.addEventListener('DOMContentLoaded', () => {
  // ── DOM refs ──────────────────────────────────────────────────
  const queryInput = document.getElementById('query-input');
  const searchButton = document.getElementById('search-button');
  const activeModelEl = document.getElementById('active-model');
  const toggleInput = document.getElementById('recap-toggle');
  const settingsBtn = document.getElementById('settings-btn');
  const omnibarBtn = document.getElementById('omnibar-btn');
  const viewRefsBtn = document.getElementById('view-references');
  const viewHlBtn = document.getElementById('view-highlights');
  const errorContainer = document.getElementById('error-container');
  const resultsContainer = document.getElementById('results-container');
  const statPages = document.getElementById('stat-pages');
  const statChunks = document.getElementById('stat-chunks');
  const statEntities = document.getElementById('stat-entities');
  const backendStatus = document.getElementById('backend-status');

  // view: 'search' | 'refs' | 'highlights'
  let activeView = 'search';
  let activeDateDays = 0; // 0 = all time

  // Active LLM config (source of truth = Options/onboarding, stored in chrome.storage).
  // The popup only DISPLAYS it and sends it with each query - it does not edit it.
  let activeProvider = '';
  let activeModel = null;
  const PROVIDER_LABELS = {
    groq: 'Groq', openai: 'OpenAI', anthropic: 'Anthropic',
    google: 'Google', openrouter: 'OpenRouter', ollama: 'Ollama', custom: 'Custom',
  };
  function renderActiveModel(provider, model) {
    const p = PROVIDER_LABELS[provider] || provider || 'Model';
    activeModelEl.textContent = model ? `${p} · ${model}` : p;
  }

  // ── Date Filter ───────────────────────────────────────────────
  document.querySelectorAll('.date-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.date-tab').forEach(b => b.classList.remove('active-date'));
      btn.classList.add('active-date');
      activeDateDays = parseInt(btn.dataset.days, 10);
    });
  });

  function getDateFrom() {
    if (!activeDateDays) return null;
    const d = new Date();
    d.setDate(d.getDate() - activeDateDays);
    d.setHours(0, 0, 0, 0);
    return d.toISOString();
  }

  // ── Chat State ────────────────────────────────────────────────
  let chatHistory = [];
  let apiBase = 'http://localhost:8000';
  chrome.storage.sync.get(['apiUrl'], (r) => { apiBase = r.apiUrl || apiBase; });

  const chatFollowup = document.getElementById('chat-followup');
  const followupInput = document.getElementById('followup-input');
  const followupSend = document.getElementById('followup-send');
  const newChatPopup = document.getElementById('new-chat-popup');

  // ── Init ──────────────────────────────────────────────────────
  loadPreferences();
  loadStats();

  // ── Events ────────────────────────────────────────────────────
  searchButton.addEventListener('click', performSearch);
  queryInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') performSearch();
  });

  followupSend.addEventListener('click', sendFollowup);
  followupInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') sendFollowup(); });

  newChatPopup.addEventListener('click', () => {
    chatHistory = [];
    chatFollowup.style.display = 'none';
    queryInput.value = '';
    showEmptyState();
    queryInput.focus();
  });

  viewRefsBtn.addEventListener('click', () => {
    if (activeView === 'refs') {
      setView('search');
    } else {
      setView('refs');
      loadReferences();
    }
  });

  viewHlBtn.addEventListener('click', () => {
    if (activeView === 'highlights') {
      setView('search');
    } else {
      setView('highlights');
      loadHighlights();
    }
  });

  toggleInput.addEventListener('change', () => {
    const enabled = toggleInput.checked;
    chrome.storage.sync.set({ recapEnabled: enabled });
    chrome.runtime.sendMessage({ action: 'setEnabled', enabled });
  });

  settingsBtn.addEventListener('click', () => {
    if (chrome.runtime.openOptionsPage) chrome.runtime.openOptionsPage();
  });

  omnibarBtn.addEventListener('click', async () => {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab?.id) {
      chrome.tabs.sendMessage(tab.id, { action: 'toggleOmnibar' }).catch(() => { });
    }
    window.close();
  });

  activeModelEl.addEventListener('click', () => {
    if (chrome.runtime.openOptionsPage) chrome.runtime.openOptionsPage();
  });

  // ── View state ────────────────────────────────────────────────
  function setView(view) {
    activeView = view;
    viewRefsBtn.classList.toggle('active-tab', view === 'refs');
    viewHlBtn.classList.toggle('active-tab', view === 'highlights');
    if (view === 'search') {
      // Only show empty state if no chat is active
      if (chatHistory.length === 0) showEmptyState();
    }
  }

  // ── Search → First chat turn ──────────────────────────────────
  function performSearch() {
    const query = queryInput.value.trim();
    if (!query) return;
    queryInput.value = '';

    setView('search');
    hideError();

    // If starting fresh, clear thread
    if (chatHistory.length === 0) {
      resultsContainer.innerHTML = '<div class="popup-chat-thread" id="popup-thread"></div>';
      chatFollowup.style.display = 'block';
    }

    sendChatTurn(query);
  }

  function sendFollowup() {
    const msg = followupInput.value.trim();
    if (!msg) return;
    followupInput.value = '';
    sendChatTurn(msg);
  }

  async function sendChatTurn(message) {
    const thread = document.getElementById('popup-thread') || (() => {
      const t = document.createElement('div');
      t.className = 'popup-chat-thread';
      t.id = 'popup-thread';
      resultsContainer.innerHTML = '';
      resultsContainer.appendChild(t);
      return t;
    })();

    // User bubble
    appendPopupBubble(thread, 'user', message);

    // Thinking indicator
    const thinkingEl = document.createElement('div');
    thinkingEl.className = 'popup-msg assistant';
    thinkingEl.innerHTML = `<div class="popup-thinking"><div class="popup-dots"><span></span><span></span><span></span></div> Searching...</div>`;
    thread.appendChild(thinkingEl);
    thread.scrollTop = thread.scrollHeight;

    try {
      const res = await fetch(`${apiBase}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message,
          history: chatHistory.slice(-40),
          top_k: 5,
          llm: activeProvider || 'groq',
          model: activeModel,
          date_from: getDateFrom(),
        }),
      });

      thinkingEl.remove();

      if (!res.ok) throw new Error('Backend error');
      const data = await res.json();

      // Reflect the provider + model the backend actually used (truthful display).
      if (data.provider) {
        activeProvider = data.provider;
        renderActiveModel(data.provider, data.model);
      }

      chatHistory.push({ role: 'user', content: message });
      chatHistory.push({ role: 'assistant', content: data.message });

      appendPopupBubble(thread, 'assistant', data.message, data.sources || []);
      chatFollowup.style.display = 'block';
      followupInput.focus();

    } catch (err) {
      thinkingEl.remove();
      showError('Chat failed. Is the backend running?');
    }

    thread.scrollTop = thread.scrollHeight;
  }

  function appendPopupBubble(thread, role, content, sources = []) {
    const msgEl = document.createElement('div');
    msgEl.className = `popup-msg ${role}`;

    const bubble = document.createElement('div');
    bubble.className = 'popup-bubble';
    bubble.innerHTML = formatMarkdown(content);
    msgEl.appendChild(bubble);

    if (role === 'assistant' && sources.length > 0) {
      const toggle = document.createElement('button');
      toggle.className = 'popup-sources-toggle';
      toggle.textContent = `▶ ${sources.length} source${sources.length !== 1 ? 's' : ''}`;

      const list = document.createElement('div');
      list.className = 'popup-sources-list';
      sources.forEach(src => {
        const item = document.createElement('div');
        item.className = 'popup-source-item';
        item.innerHTML = `<div class="popup-source-title">${escapeHtml(src.title || 'Untitled')}</div><div class="popup-source-url">${escapeHtml(truncateUrl(src.url))}</div>`;
        item.addEventListener('click', () => { const u = safeUrl(src.url); if (u !== '#') chrome.tabs.create({ url: u }); });
        list.appendChild(item);
      });

      toggle.addEventListener('click', () => {
        list.classList.toggle('open');
        toggle.textContent = `${list.classList.contains('open') ? '▼' : '▶'} ${sources.length} source${sources.length !== 1 ? 's' : ''}`;
      });

      msgEl.appendChild(toggle);
      msgEl.appendChild(list);
    }

    thread.appendChild(msgEl);
    return msgEl;
  }

  // ── References ────────────────────────────────────────────────
  function loadReferences() {
    showLoading();
    chrome.runtime.sendMessage({ action: 'getReferences' }, (response) => {
      if (chrome.runtime.lastError || !response?.success) {
        showError('Could not load references.');
        showEmptyState();
        return;
      }
      displayReferences(response.references || []);
    });
  }

  function displayReferences(refs) {
    resultsContainer.innerHTML = '';

    // Deduplicate by URL - keep first occurrence
    const seen = new Set();
    refs = refs.filter(r => {
      if (seen.has(r.url)) return false;
      seen.add(r.url);
      return true;
    });

    const header = document.createElement('div');
    header.className = 'refs-header';
    header.innerHTML = `
      <h3>Indexed Pages</h3>
      <span class="refs-count">${refs.length}</span>`;
    resultsContainer.appendChild(header);

    if (refs.length === 0) {
      resultsContainer.innerHTML += `
        <div class="empty-state">
          <div class="empty-state-icon"><svg style="width:32px;height:32px;color:#5a5a6e;" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M5 19a2 2 0 01-2-2V7a2 2 0 012-2h4l2 2h4a2 2 0 012 2v1M5 19h14a2 2 0 002-2v-5a2 2 0 00-2-2H9a2 2 0 00-2 2v5a2 2 0 01-2 2z" /></svg></div>
          <div class="empty-state-title">No pages indexed yet</div>
          <div class="empty-state-sub">Browse some websites and RECAP will start remembering.</div>
        </div>`;
      return;
    }

    refs.forEach((ref, i) => {
      const card = document.createElement('div');
      card.className = 'ref-card';
      card.style.animationDelay = `${i * 0.03}s`;

      const summary = ref.summary || ref.meta_description || '';
      let host = '';
      try { host = new URL(ref.url).hostname; } catch { }

      card.innerHTML = `
        <div class="ref-title" data-url="${escapeHtml(ref.url)}">${escapeHtml(ref.title || 'Untitled')}</div>
        <div class="ref-url">${escapeHtml(truncateUrl(ref.url))}</div>
        ${summary ? `<div class="ref-summary">${escapeHtml(summary)}</div>` : ''}
        <div class="ref-meta">
          ${ref.content_type ? `<span class="badge">${escapeHtml(ref.content_type)}</span>` : ''}
          ${ref.visit_count && ref.visit_count > 1 ? `<span class="badge">${ref.visit_count} visits</span>` : ''}
          <div style="margin-left:auto; display:flex; gap:6px;">
            <button class="ref-ignore" data-domain="${escapeHtml(host)}">Ignore</button>
            <button class="ref-delete" data-url="${escapeHtml(ref.url)}">Delete</button>
          </div>
        </div>`;

      card.querySelector('.ref-title').addEventListener('click', () => {
        const u = safeUrl(ref.url); if (u !== '#') chrome.tabs.create({ url: u });
      });

      card.querySelector('.ref-delete').addEventListener('click', (e) => {
        e.stopPropagation();
        deleteReference(ref.url, card);
      });

      const ignoreBtn = card.querySelector('.ref-ignore');
      if (ignoreBtn) {
        ignoreBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          const domain = host;
          if (!domain) return;
          if (confirm(`Are you sure you want to ignore all future tracking for ${domain}?`)) {
            chrome.runtime.sendMessage({ action: 'ignoreDomain', domain: domain }, (res) => {
              if (res?.success) {
                // Delete the current card visually, backend handles the rest
                card.style.opacity = '0';
                card.style.transform = 'translateX(30px)';
                card.style.transition = '0.25s ease';
                setTimeout(() => { card.remove(); loadStats(); }, 250);
              }
            });
          }
        });
      }

      resultsContainer.appendChild(card);
    });
  }

  function deleteReference(url, cardEl) {
    chrome.runtime.sendMessage({ action: 'deleteUrl', url }, (response) => {
      if (response?.success) {
        cardEl.style.opacity = '0';
        cardEl.style.transform = 'translateX(30px)';
        cardEl.style.transition = '0.25s ease';
        setTimeout(() => { cardEl.remove(); loadStats(); }, 250);
      } else {
        showError('Failed to delete.');
      }
    });
  }

  // ── Highlights ────────────────────────────────────────────────
  function loadHighlights() {
    showLoading();
    chrome.runtime.sendMessage({ action: 'getHighlights' }, (response) => {
      if (chrome.runtime.lastError || !response?.success) {
        showEmptyState();
        return;
      }
      displayHighlights(response.highlights || []);
    });
  }

  function displayHighlights(highlights) {
    resultsContainer.innerHTML = '';

    const header = document.createElement('div');
    header.className = 'refs-header';
    header.innerHTML = `
      <h3>Saved Highlights</h3>
      <div style="display:flex;align-items:center;gap:8px;">
        <span class="refs-count">${highlights.length}</span>
        ${highlights.length > 0 ? '<button class="ref-delete" id="clear-highlights-btn">Clear All</button>' : ''}
      </div>`;
    resultsContainer.appendChild(header);

    const clearBtn = header.querySelector('#clear-highlights-btn');
    if (clearBtn) {
      clearBtn.addEventListener('click', () => {
        chrome.runtime.sendMessage({ action: 'clearHighlights' }, () => loadHighlights());
      });
    }

    if (highlights.length === 0) {
      resultsContainer.innerHTML += `
        <div class="empty-state">
          <div class="empty-state-icon"><svg style="width:32px;height:32px;color:#3ecfbf;" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z" /></svg></div>
          <div class="empty-state-title">No highlights yet</div>
          <div class="empty-state-sub">Select text on any page - a <strong>Save</strong> button will appear above your selection.</div>
        </div>`;
      return;
    }

    highlights.forEach((hl, i) => {
      const card = document.createElement('div');
      card.className = 'highlight-card';
      card.style.animationDelay = `${i * 0.03}s`;

      const date = hl.timestamp
        ? new Date(hl.timestamp).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
        : '';

      card.innerHTML = `
        <div class="highlight-text">${escapeHtml(hl.text)}</div>
        <div class="highlight-source" data-url="${escapeHtml(hl.url)}">${escapeHtml(truncateUrl(hl.url))}</div>
        <div style="display:flex;align-items:center;justify-content:space-between;margin-top:4px;">
          <div class="highlight-date">${date}${hl.title ? ` · ${escapeHtml(hl.title)}` : ''}</div>
          <button class="ref-delete hl-delete-btn">Delete</button>
        </div>`;

      card.querySelector('.highlight-source').addEventListener('click', () => {
        const u = safeUrl(hl.url); if (u !== '#') chrome.tabs.create({ url: u });
      });

      card.querySelector('.hl-delete-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        chrome.runtime.sendMessage({ action: 'deleteHighlight', id: hl.id }, (res) => {
          if (res?.success) {
            card.style.opacity = '0';
            card.style.transform = 'translateX(30px)';
            card.style.transition = '0.25s ease';
            setTimeout(() => card.remove(), 250);
          }
        });
      });

      resultsContainer.appendChild(card);
    });
  }

  // ── Stats ─────────────────────────────────────────────────────
  function loadStats() {
    chrome.runtime.sendMessage({ action: 'getStats' }, (response) => {
      if (chrome.runtime.lastError || !response?.success) {
        setBackendStatus(false);
        return;
      }

      setBackendStatus(true);

      // If the user hasn't explicitly chosen a provider, reflect the backend's
      // active default so the badge is never blank.
      if (!activeProvider && response.defaultProvider) {
        activeProvider = response.defaultProvider;
        renderActiveModel(activeProvider, activeModel);
      }

      const s = response.stats;
      animateStat(statPages, s.total_pages || 0);
      animateStat(statChunks, s.total_chunks || 0);
      animateStat(statEntities, s.total_entities || 0);
    });
  }

  function animateStat(el, value) {
    const current = parseInt(el.textContent) || 0;
    if (current === value) { el.textContent = value; return; }
    const steps = 15;
    const diff = value - current;
    let step = 0;
    const timer = setInterval(() => {
      step++;
      el.textContent = Math.round(current + diff * (step / steps));
      if (step >= steps) { clearInterval(timer); el.textContent = value; }
    }, 20);
  }

  function setBackendStatus(online) {
    backendStatus.innerHTML = online
      ? '<span class="status-dot"></span><span>Backend online</span>'
      : '<span class="status-dot offline"></span><span>Backend offline</span>';
  }

  // ── Preferences ───────────────────────────────────────────────
  function loadPreferences() {
    chrome.storage.sync.get(['recapEnabled', 'preferredLlm', 'llmModel'], (data) => {
      toggleInput.checked = data.recapEnabled !== false;
      if (data.preferredLlm) activeProvider = data.preferredLlm;
      activeModel = (data.llmModel || '').trim() || null;
      renderActiveModel(activeProvider, activeModel);
    });
  }

  // ── UI helpers ────────────────────────────────────────────────
  function showLoading() {
    resultsContainer.innerHTML = `
      <div class="loading-skeleton">
        <div class="skeleton-line"></div>
        <div class="skeleton-line"></div>
        <div class="skeleton-line"></div>
        <div class="skeleton-line"></div>
        <div class="skeleton-line"></div>
      </div>`;
  }

  function showEmptyState() {
    resultsContainer.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon"><img src="icons/RECAPL.png" alt="Recap" style="height: 48px; width: auto; opacity: 0.5;"></div>
        <div class="empty-state-title">Your browsing memory</div>
        <div class="empty-state-sub">Ask anything about pages you've visited.<br>RECAP remembers so you don't have to.</div>
      </div>`;
  }

  function showError(msg) {
    errorContainer.textContent = msg;
    errorContainer.style.display = 'block';
  }

  function hideError() {
    errorContainer.style.display = 'none';
  }

  function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function truncateUrl(url) {
    try {
      const u = new URL(url);
      const path = u.pathname.length > 30 ? u.pathname.substring(0, 30) + '…' : u.pathname;
      return u.hostname + path;
    } catch { return url; }
  }

  function safeUrl(u) {
    return /^https?:\/\//i.test(String(u)) ? String(u) : '#';
  }

  function formatMarkdown(text) {
    if (!text) return '';
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;')
      .replace(/^#{1,6}\s+(.+)$/gm, '<span class="popup-md-h">$1</span>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.+?)\*/g, '<em>$1</em>')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m, txt, url) => `<a href="${safeUrl(url)}" target="_blank" rel="noopener">${txt}</a>`)
      .replace(/\n/g, '<br>');
  }
});
