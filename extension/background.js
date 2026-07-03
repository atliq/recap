// RECAP v2 - Background Service Worker
// Delegates classification to the backend. Tracks tab visits, extracts
// content, sends to backend. Handles keyboard commands and first-run onboarding.

const config = {
  apiUrl: 'http://127.0.0.1:8000',
  minVisitDuration: 30,    // seconds
  checkInterval: 15,       // seconds
  maxContentLength: 150000,
  enabled: true,
  userBlockedDomains: []
};

// Minimal protocol blocklist
const BLOCKED_PROTOCOLS = ['chrome://', 'chrome-extension://', 'about:', 'edge://', 'brave://', 'file:///', 'data:', 'blob:'];

const BLOCKED_DOMAINS_EXACT = new Set([
  'chat.openai.com', 'chatgpt.com', 'platform.openai.com',
  'claude.ai', 'console.anthropic.com',
  'console.groq.com', 'groq.com',
  'gemini.google.com', 'aistudio.google.com', 'bard.google.com',
  'copilot.microsoft.com', 'copilot.github.com',
  'poe.com', 'perplexity.ai', 'you.com',
  'huggingface.co/chat', 'labs.perplexity.ai',
  'chat.mistral.ai', 'coral.cohere.com',
  'lmsys.org', 'together.ai',
  'chase.com', 'secure.chase.com',
  'bankofamerica.com', 'secure.bankofamerica.com',
  'wellsfargo.com', 'citi.com', 'usbank.com', 'capitalone.com', 'ally.com',
  'discover.com', 'tdbank.com', 'pnc.com', 'fidelity.com', 'schwab.com',
  'vanguard.com', 'etrade.com', 'robinhood.com', 'sofi.com', 'marcus.com',
  'americanexpress.com', 'hsbc.com', 'barclays.co.uk', 'natwest.com',
  'lloydsbank.com', 'halifax.co.uk', 'nationwide.co.uk', 'tsb.co.uk',
  'monzo.com', 'revolut.com', 'starlingbank.com', 'db.com', 'ing.com',
  'bnpparibas.com', 'credit-suisse.com', 'ubs.com',
  'hdfcbank.com', 'icicibank.com', 'sbi.co.in', 'onlinesbi.sbi',
  'kotak.com', 'axisbank.com', 'yesbank.in', 'idfcfirstbank.com',
  'paypal.com', 'venmo.com', 'stripe.com', 'square.com', 'wise.com',
  'razorpay.com', 'paytm.com', 'phonepe.com', 'gpay.com',
  'crypto.com', 'coinbase.com', 'binance.com', 'kraken.com',
  'mail.google.com', 'outlook.live.com', 'outlook.office.com',
  'outlook.office365.com', 'mail.yahoo.com', 'protonmail.com', 'mail.proton.me',
  'web.whatsapp.com', 'web.telegram.org', 'discord.com', 'slack.com',
  'teams.microsoft.com', 'messenger.com', 'messages.google.com',
  'accounts.google.com', 'login.microsoftonline.com', 'appleid.apple.com',
  'id.apple.com', 'login.yahoo.com', 'auth0.com', 'okta.com', 'onelogin.com',
  'mychart.com', 'mychartsso.com', 'patient.portal', 'healthvault.com',
  'vault.bitwarden.com', 'my.1password.com', 'lastpass.com', 'dashlane.com', 'keeper.io',
  'irs.gov', 'ssa.gov', 'turbotax.intuit.com',
  'facebook.com', 'instagram.com', 'tiktok.com', 'snapchat.com', 'reddit.com',
  'pinterest.com', 'linkedin.com/feed',
  'netflix.com', 'disneyplus.com', 'primevideo.com', 'hulu.com',
  'hbomax.com', 'peacocktv.com', 'spotify.com', 'music.youtube.com', 'music.apple.com',
]);

const BLOCKED_DOMAIN_SUBSTRINGS = [
  'onlinebanking.', 'netbanking.', 'ibanking.', 'ebanking.', 'mobilebanking.',
  'secure.bank', 'login.', 'signin.', 'sso.', 'oauth.', 'auth.', 'accounts.',
  'checkout.', 'pay.', 'payment.',
];

const BLOCKED_PATH_PATTERNS = [
  /\/log-?in(\/|$)/i,
  /\/sign-?(in|up|out)(\/|$)/i,
  /\/(account|profile|settings|preferences|dashboard)(\/|$)/i,
  /\/(cart|checkout|payment|billing|receipt|order)(\/|$)/i,
  /\/(auth|oauth|callback|sso|token|verify|confirm)(\/|$)/i,
  /\/(password|reset|forgot|2fa|mfa)(\/|$)/i,
];

function shouldTrack(url) {
  if (!url || !config.enabled) return false;
  if (BLOCKED_PROTOCOLS.some(p => url.startsWith(p))) return false;
  if (!url.startsWith('http://') && !url.startsWith('https://')) return false;
  try {
    const parsed = new URL(url);
    const hostname = parsed.hostname.toLowerCase();
    const pathname = parsed.pathname.toLowerCase();

    // Check built-in exact domains
    if (BLOCKED_DOMAINS_EXACT.has(hostname)) return false;
    for (const blocked of BLOCKED_DOMAINS_EXACT) {
      if (hostname.endsWith('.' + blocked)) return false;
    }

    // Check user-configured blocked domains
    if (config.userBlockedDomains.some(domain => hostname === domain || hostname.endsWith('.' + domain))) {
      return false;
    }

    if (BLOCKED_DOMAIN_SUBSTRINGS.some(sub => hostname.includes(sub))) return false;
    if (BLOCKED_PATH_PATTERNS.some(re => re.test(pathname))) return false;
    return true;
  } catch { return false; }
}

// ──────────────────────────────────────────────────────────────
// Tab Tracking
// ──────────────────────────────────────────────────────────────

const tabVisits = {};

chrome.tabs.onActivated.addListener(({ tabId }) => {
  if (!tabVisits[tabId]) {
    tabVisits[tabId] = { startTime: Date.now(), url: null, title: null, processed: false };
  }
  chrome.tabs.get(tabId, (tab) => {
    if (chrome.runtime.lastError || !tab?.url) return;
    if (shouldTrack(tab.url)) {
      tabVisits[tabId].url = tab.url;
      tabVisits[tabId].title = tab.title || '';
    }
  });
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && tab.url && shouldTrack(tab.url)) {
    if (tabVisits[tabId]?.url && !tabVisits[tabId].processed) {
      const duration = (Date.now() - tabVisits[tabId].startTime) / 1000;
      if (duration >= config.minVisitDuration) {
        processTab(tabId, tabVisits[tabId]);
        tabVisits[tabId].processed = true;
      }
    }
    tabVisits[tabId] = { startTime: Date.now(), url: tab.url, title: tab.title || '', processed: false };
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabVisits[tabId]?.url && !tabVisits[tabId].processed) {
    const duration = (Date.now() - tabVisits[tabId].startTime) / 1000;
    if (duration >= config.minVisitDuration) {
      processTab(tabId, tabVisits[tabId]);
    }
  }
  delete tabVisits[tabId];
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'checkTabs') {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (!tabs?.[0]) return;
      const tabId = tabs[0].id;
      const visit = tabVisits[tabId];
      if (visit?.url && !visit.processed) {
        const duration = (Date.now() - visit.startTime) / 1000;
        if (duration >= config.minVisitDuration) {
          processTab(tabId, visit);
          visit.processed = true;
        }
      }
    });
  } else if (alarm.name === 'weeklyDigest') {
    generateWeeklyDigest();
  }
});

// ──────────────────────────────────────────────────────────────
// Lifecycle
// ──────────────────────────────────────────────────────────────

chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === 'install') {
    // First install → open onboarding
    chrome.tabs.create({ url: chrome.runtime.getURL('onboarding.html') });
  }
  // Create alarms only after config (checkInterval) is populated from storage.
  loadConfig(ensureAlarms);
  // Re-sync the user's provider/model/keys to the backend (it holds them in memory only).
  pushConfigToBackend();

  // Right-click context menu for saving selections
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: 'recap-save-highlight',
      title: 'Save to RECAP',
      contexts: ['selection']
    });
    chrome.contextMenus.create({
      id: 'recap-search',
      title: 'Search in RECAP: "%s"',
      contexts: ['selection']
    });
  });
});

chrome.runtime.onStartup.addListener(() => {
  // Create alarms only after config (checkInterval) is populated from storage.
  loadConfig(ensureAlarms);
  // Re-sync the user's provider/model/keys to the backend (it holds them in memory only).
  pushConfigToBackend();
});

// Create the periodic alarms. Called after loadConfig resolves so that
// config.checkInterval reflects the stored value, not the default.
function ensureAlarms() {
  // Chrome clamps periodInMinutes to a 0.5-minute (30s) floor; passing less
  // triggers a console warning. Respect larger user-configured intervals.
  chrome.alarms.create('checkTabs', { periodInMinutes: Math.max(0.5, config.checkInterval / 60) });
  // Weekly digest: fires every 7 days
  chrome.alarms.create('weeklyDigest', { periodInMinutes: 7 * 24 * 60 });
}

// Re-send the user's stored provider/model/keys to the backend. The backend keeps
// these in memory only, so after its own restart it would otherwise forget them and
// fall back to key-order (groq). Re-pushing on service-worker startup keeps the
// backend in sync with the extension - the durable source of truth. Missing values
// are sent as null, which the backend treats as "leave unchanged".
function pushConfigToBackend() {
  chrome.storage.sync.get(['apiUrl', 'preferredLlm', 'llmModel', 'llmBaseUrl'], (sync) => {
    const apiUrl = sync.apiUrl || config.apiUrl;
    chrome.storage.local.get(
      ['groqApiKey', 'openaiApiKey', 'anthropicApiKey', 'googleApiKey', 'openrouterApiKey', 'customApiKey'],
      (loc) => {
        const body = {
          groq_api_key: loc.groqApiKey || null,
          openai_api_key: loc.openaiApiKey || null,
          anthropic_api_key: loc.anthropicApiKey || null,
          google_api_key: loc.googleApiKey || null,
          openrouter_api_key: loc.openrouterApiKey || null,
          llm_base_url: sync.llmBaseUrl || null,
          llm_api_key: loc.customApiKey || null,
          llm_model: (sync.llmModel || '').trim() || null,
          default_provider: sync.preferredLlm || null,
        };
        fetch(`${apiUrl}/update_api_keys`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }).catch(() => {});
      }
    );
  });
}

function loadConfig(callback) {
  chrome.storage.sync.get(
    ['apiUrl', 'minVisitDuration', 'checkInterval', 'recapEnabled', 'userBlockedDomains'],
    (result) => {
      if (result.apiUrl) config.apiUrl = result.apiUrl;
      if (result.minVisitDuration) config.minVisitDuration = Number(result.minVisitDuration);
      if (result.checkInterval) config.checkInterval = Number(result.checkInterval);
      if (result.recapEnabled !== undefined) config.enabled = result.recapEnabled;
      if (result.userBlockedDomains) config.userBlockedDomains = result.userBlockedDomains;
      if (typeof callback === 'function') callback();
    }
  );
}

// ──────────────────────────────────────────────────────────────
// Keyboard Commands
// ──────────────────────────────────────────────────────────────

chrome.commands.onCommand.addListener(async (command) => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return;

  if (command === 'toggle-omnibar') {
    chrome.tabs.sendMessage(tab.id, { action: 'toggleOmnibar' }).catch(() => { });
  }

  if (command === 'save-highlight') {
    chrome.tabs.sendMessage(tab.id, { action: 'triggerSaveHighlight' }).catch(() => { });
  }
});

// ──────────────────────────────────────────────────────────────
// Context Menu
// ──────────────────────────────────────────────────────────────

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (!tab?.id) return;

  if (info.menuItemId === 'recap-save-highlight' && info.selectionText) {
    const text = info.selectionText.trim();
    if (!text) return;

    await handleSaveHighlight({ text, url: tab.url, title: tab.title });

    const snippet = text.length > 60 ? text.substring(0, 60) + '…' : text;
    chrome.tabs.sendMessage(tab.id, {
      action: 'showToast',
      message: `"${snippet}"`,
      type: 'success'
    }).catch(() => { });
  }

  if (info.menuItemId === 'recap-search' && info.selectionText) {
    chrome.tabs.sendMessage(tab.id, {
      action: 'launchOmnibarWithQuery',
      query: info.selectionText.trim()
    }).catch(() => { });
  }
});

// ──────────────────────────────────────────────────────────────
// Content Extraction & Backend
// ──────────────────────────────────────────────────────────────

async function processTab(tabId, visit) {
  try {
    const response = await chrome.tabs.sendMessage(tabId, { action: 'getPageContent' });

    // Content script refused: page is structurally sensitive (login/payment/
    // account). Its text was never extracted, so there is nothing to send.
    if (response?.skip) {
      console.log(`RECAP skipped (${response.reason}): ${visit.url}`);
      return;
    }
    if (!response?.content) return;

    const content = response.content.substring(0, config.maxContentLength);
    // Count only tokens with an actual word char so Markdown structural tokens
    // (#, |, -, >, ```) from the extractor don't inflate the count / quality score.
    const wordCount = content.split(/\s+/).filter(w => /[\p{L}\p{N}]/u.test(w)).length;

    // Near-empty pages (redirect shells, loading screens, error stubs) are
    // noise in the index - don't ship them to the backend at all.
    if (wordCount < 50) {
      console.log(`RECAP skipped (only ${wordCount} words): ${visit.url}`);
      return;
    }
    const duration = (Date.now() - visit.startTime) / 1000;

    const result = await fetch(`${config.apiUrl}/process_page`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url: visit.url,
        title: visit.title || response.title || '',
        content: content,
        meta_description: response.description || '',
        visit_duration: duration,
        word_count: wordCount,
        text_to_tag_ratio: response.textToTagRatio || 0,
        timestamp: new Date().toISOString()
      })
    });

    if (!result.ok) return;
    const data = await result.json();
    console.log(`RECAP indexed: ${visit.url} → ${data.status} (${data.chunks_created} chunks)`);

  } catch (err) {
    if (!err.message?.includes('Receiving end does not exist')) {
      console.warn('RECAP process error:', err.message);
    }
  }
}

// ──────────────────────────────────────────────────────────────
// Message Handler
// ──────────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  // Only accept messages from this extension's own contexts (content scripts,
  // popup, options, newtab). Web pages can't reach here without
  // externally_connectable, but validate defensively against privileged actions.
  if (sender.id !== chrome.runtime.id) return false;

  if (request.action === 'shouldTrack') {
    sendResponse({ track: shouldTrack(request.url) });
    return false;
  }

  if (request.action === 'omnibarQuery') {
    handleQuery(request).then(sendResponse).catch(e => sendResponse({ success: false, error: e.message }));
    return true;
  }

  if (request.action === 'getStats') {
    handleGetStats().then(sendResponse).catch(e => sendResponse({ success: false, error: e.message }));
    return true;
  }

  if (request.action === 'getReferences') {
    handleGetReferences().then(sendResponse).catch(e => sendResponse({ success: false, error: e.message }));
    return true;
  }

  if (request.action === 'deleteUrl') {
    handleDeleteUrl(request.url).then(sendResponse).catch(e => sendResponse({ success: false, error: e.message }));
    return true;
  }

  if (request.action === 'saveHighlight') {
    handleSaveHighlight(request).then(sendResponse).catch(e => sendResponse({ success: false, error: e.message }));
    return true;
  }

  if (request.action === 'getHighlights') {
    handleGetHighlights().then(sendResponse).catch(e => sendResponse({ success: false, error: e.message }));
    return true;
  }

  if (request.action === 'deleteHighlight') {
    handleDeleteHighlight(request.id).then(sendResponse).catch(e => sendResponse({ success: false, error: e.message }));
    return true;
  }

  if (request.action === 'clearHighlights') {
    handleClearHighlights().then(sendResponse).catch(e => sendResponse({ success: false, error: e.message }));
    return true;
  }

  if (request.action === 'exportData') {
    handleExport().then(sendResponse).catch(e => sendResponse({ success: false, error: e.message }));
    return true;
  }

  if (request.action === 'updateConfig') {
    if (request.config) Object.assign(config, request.config);
    sendResponse({ success: true });
    return false;
  }

  if (request.action === 'setEnabled') {
    config.enabled = request.enabled;
    sendResponse({ success: true });
    return false;
  }

  if (request.action === 'ignoreDomain') {
    handleIgnoreDomain(request.domain).then(sendResponse).catch(e => sendResponse({ success: false, error: e.message }));
    return true;
  }

  if (request.action === 'getRelated') {
    handleGetRelated(request.url, request.content).then(sendResponse).catch(e => sendResponse({ success: false, error: e.message }));
    return true;
  }
});

// ──────────────────────────────────────────────────────────────
// API Handlers
// ──────────────────────────────────────────────────────────────

// Resolve the user's chosen provider + model from storage so every query path
// (popup, newtab, omnibar) uses the same active configuration.
function getActiveLlm() {
  return new Promise((resolve) => {
    chrome.storage.sync.get(['preferredLlm', 'llmModel'], (r) => {
      resolve({
        provider: r.preferredLlm || 'groq',
        model: (r.llmModel || '').trim() || null,
      });
    });
  });
}

async function handleQuery(request) {
  const active = await getActiveLlm();
  const body = {
    query: request.query,
    top_k: request.top_k || 5,
    llm: request.llm || active.provider,
    model: request.model || active.model,
    use_kg: true,
  };
  if (request.date_from) body.date_from = request.date_from;

  const res = await fetch(`${config.apiUrl}/query`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });

  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  const data = await res.json();

  return {
    success: true,
    query: data.query,
    answer: data.answer,
    results: data.results || [],
    sources_used: data.sources_used,
    retrieval_time_ms: data.retrieval_time_ms,
    generation_time_ms: data.generation_time_ms,
    provider: data.provider,
    model: data.model
  };
}

async function handleGetStats() {
  const res = await fetch(`${config.apiUrl}/health`);
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  const health = await res.json();
  const defaultProvider = health.default_provider || 'groq';

  const statsRes = await fetch(`${config.apiUrl}/stats`);
  if (!statsRes.ok) throw new Error(`Backend error: ${statsRes.status}`);
  const data = await statsRes.json();

  return {
    success: true,
    defaultProvider: defaultProvider,
    stats: {
      total_pages: data.total_pages,
      total_chunks: data.total_chunks,
      total_entities: data.total_entities,
      index_size_mb: data.index_size_mb,
      last_indexed: data.last_indexed,
      content_type_distribution: data.content_type_distribution,
      top_domains: data.top_domains || []
    }
  };
}

async function handleGetReferences() {
  const res = await fetch(`${config.apiUrl}/references`);
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  const data = await res.json();
  return { success: true, references: data.references || [] };
}

async function handleDeleteUrl(url) {
  const res = await fetch(`${config.apiUrl}/delete_url`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url })
  });
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  return { success: true };
}

async function handleIgnoreDomain(domain) {
  return new Promise((resolve, reject) => {
    chrome.storage.sync.get(['userBlockedDomains'], async (data) => {
      const blocked = data.userBlockedDomains || [];
      if (!blocked.includes(domain)) {
        blocked.push(domain);
        chrome.storage.sync.set({ userBlockedDomains: blocked }, async () => {
          config.userBlockedDomains = blocked;

          // Optionally, also tell the backend to delete all existing history for this domain
          try {
            await fetch(`${config.apiUrl}/delete_domain`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ domain })
            });
          } catch (e) { console.warn("Could not delete domain from backend:", e); }

          resolve({ success: true });
        });
      } else {
        resolve({ success: true });
      }
    });
  });
}

async function handleSaveHighlight(request) {
  // Save locally first (works offline)
  const key = `highlight_${Date.now()}`;
  const highlight = {
    id: key,
    text: request.text,
    url: request.url,
    title: request.title,
    timestamp: new Date().toISOString()
  };

  const data = await chrome.storage.local.get(['highlights']);
  const highlights = data.highlights || [];
  highlights.unshift(highlight);
  // Keep last 500 highlights
  if (highlights.length > 500) highlights.splice(500);
  await chrome.storage.local.set({ highlights });

  // Also sync to backend if available
  try {
    await fetch(`${config.apiUrl}/save_highlight`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: request.text,
        url: request.url,
        title: request.title,
        timestamp: highlight.timestamp
      })
    });
  } catch (_) { /* backend optional */ }

  return { success: true };
}

async function handleGetHighlights() {
  return new Promise((resolve) => {
    chrome.storage.local.get(['highlights'], (data) => {
      resolve({ success: true, highlights: data.highlights || [] });
    });
  });
}

async function handleDeleteHighlight(id) {
  return new Promise((resolve) => {
    chrome.storage.local.get(['highlights'], (data) => {
      const highlights = (data.highlights || []).filter(h => h.id !== id);
      chrome.storage.local.set({ highlights }, () => resolve({ success: true }));
    });
  });
}

async function handleClearHighlights() {
  return new Promise((resolve) => {
    chrome.storage.local.set({ highlights: [] }, () => resolve({ success: true }));
  });
}

async function handleExport() {
  const res = await fetch(`${config.apiUrl}/export`);
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  const data = await res.json();
  return { success: true, data };
}

async function handleGetRelated(url, content) {
  const res = await fetch(`${config.apiUrl}/related`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url: url || '', content: content || '' })
  });
  if (!res.ok) return { success: false };
  const data = await res.json();
  return { success: true, related: data.related || [] };
}

// ──────────────────────────────────────────────────────────────
// Weekly Digest
// ──────────────────────────────────────────────────────────────

async function generateWeeklyDigest() {
  try {
    const res = await fetch(`${config.apiUrl}/digest`);
    if (!res.ok) return;
    const data = await res.json();
    if (data.digest) {
      // Store the digest for the digest page to display
      chrome.storage.local.set({ lastDigest: { digest: data.digest, generated_at: data.generated_at, page_count: data.page_count } });
      chrome.notifications.create('weeklyDigest', {
        type: 'basic',
        iconUrl: 'icons/icon48.png',
        title: 'Your RECAP Weekly Digest is ready',
        message: 'See what you learned this week. Click to open.'
      });
    }
  } catch (_) { /* silent */ }
}