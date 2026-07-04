/**
 * RECAP v2 - Options Controller
 *
 * Settings persistence, API key management, and backend sync.
 */

const defaultConfig = {
  apiUrl: 'http://localhost:8000',
  minVisitDuration: 30,
  checkInterval: 15,
  defaultLlm: 'openai',
};

// Provider metadata. `keyStore` is the chrome.storage.local slot for the secret
// (null = provider needs no key, e.g. local Ollama); `backendKey` is the field
// the backend expects; `model` is the default-model hint shown as a placeholder.
const PROVIDERS = {
  openai:     { keyStore: 'openaiApiKey',     backendKey: 'openai_api_key',     model: 'gpt-4o-mini' },
  groq:       { keyStore: 'groqApiKey',       backendKey: 'groq_api_key',       model: 'llama-3.3-70b-versatile' },
  openrouter: { keyStore: 'openrouterApiKey', backendKey: 'openrouter_api_key', model: 'openai/gpt-4o-mini' },
  google:     { keyStore: 'googleApiKey',     backendKey: 'google_api_key',     model: 'gemini-2.0-flash' },
  anthropic:  { keyStore: 'anthropicApiKey',  backendKey: 'anthropic_api_key',  model: 'claude-3-5-haiku-20241022' },
  ollama:     { keyStore: null,               backendKey: null,                 model: 'gemma3:4b' },
  custom:     { keyStore: 'customApiKey',     backendKey: 'llm_api_key',        model: '', needsBaseUrl: true },
};
const KEY_STORES = ['groqApiKey', 'openaiApiKey', 'anthropicApiKey', 'googleApiKey', 'openrouterApiKey', 'customApiKey'];

// Per-provider values remembered across switches (hydrated from storage on load).
const providerKeys = {};    // { openai: 'sk-...', groq: 'gsk_...', ... }
const providerModels = {};  // { openai: 'gpt-4o', ... }
let llmBaseUrl = '';
let activeProvider = defaultConfig.defaultLlm;

const els = {
  apiUrl: document.getElementById('api-url'),
  minVisitDuration: document.getElementById('min-visit-duration'),
  checkInterval: document.getElementById('check-interval'),
  kgEnabled: document.getElementById('kg-enabled'),
  provider: document.getElementById('default-llm'),
  model: document.getElementById('llm-model'),
  apiKey: document.getElementById('provider-api-key'),
  baseUrl: document.getElementById('llm-base-url'),
  keyGroup: document.getElementById('key-group'),
  baseUrlGroup: document.getElementById('baseurl-group'),
  modelKeyRow: document.getElementById('model-key-row'),
};

const saveBtn = document.getElementById('save-options');
const resetBtn = document.getElementById('reset-options');
const clearBtn = document.getElementById('clear-data');
const exportBtn = document.getElementById('export-data');
const refreshStatsBtn = document.getElementById('refresh-stats');
const testBtn = document.getElementById('test-llm');
const testResultEl = document.getElementById('test-llm-result');
const statusEl = document.getElementById('status');

// ── Init ────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadOptions();
  loadPrivacyStats();
});
saveBtn.addEventListener('click', saveOptions);
resetBtn.addEventListener('click', resetOptions);
clearBtn.addEventListener('click', clearData);
exportBtn.addEventListener('click', exportData);
refreshStatsBtn.addEventListener('click', loadPrivacyStats);
testBtn.addEventListener('click', testLlm);

// Switching provider: stash the current fields, then paint the target provider's.
els.provider.addEventListener('change', () => {
  captureActiveProvider();
  applyProvider(els.provider.value);
});

// Key visibility toggles
document.querySelectorAll('.key-toggle').forEach((btn) => {
  btn.addEventListener('click', () => {
    const input = document.getElementById(btn.dataset.target);
    if (!input) return;
    if (input.type === 'password') {
      input.type = 'text';
      btn.textContent = '🔒';
    } else {
      input.type = 'password';
      btn.textContent = '👁';
    }
  });
});

// ── Provider switching ──────────────────────────────────────────
// Map a storage slot back to its provider id (customApiKey -> custom, ...).
function keyToProvider(store) {
  return Object.keys(PROVIDERS).find((p) => PROVIDERS[p].keyStore === store);
}

// Read the visible Model / Key / Base-URL fields into the in-memory maps for
// whichever provider is currently active.
function captureActiveProvider() {
  const meta = PROVIDERS[activeProvider];
  if (!meta) return;
  providerModels[activeProvider] = els.model.value.trim();
  if (meta.keyStore) providerKeys[activeProvider] = els.apiKey.value.trim();
  if (meta.needsBaseUrl) llmBaseUrl = els.baseUrl.value.trim();
}

// Paint the visible fields from the maps for `provider`, and toggle which fields
// are relevant (Ollama needs no key; only Custom needs a base URL).
function applyProvider(provider) {
  const meta = PROVIDERS[provider] || PROVIDERS.openai;
  activeProvider = provider;

  els.model.value = providerModels[provider] || '';
  els.model.placeholder = meta.model ? `default: ${meta.model}` : 'e.g. your-model-id';

  const needsKey = !!meta.keyStore;
  els.keyGroup.style.display = needsKey ? '' : 'none';
  els.modelKeyRow.classList.toggle('single', !needsKey);
  els.apiKey.value = needsKey ? (providerKeys[provider] || '') : '';

  els.baseUrlGroup.style.display = meta.needsBaseUrl ? '' : 'none';
  els.baseUrl.value = llmBaseUrl || '';
}

// ── Load ────────────────────────────────────────────────────────
function loadOptions() {
  // Load sync prefs first, then the secret keys, then paint - so all three maps
  // (models, keys, base URL) are populated before applyProvider() runs.
  chrome.storage.sync.get(
    ['apiUrl', 'minVisitDuration', 'checkInterval', 'defaultLlm', 'preferredLlm', 'llmModels', 'llmBaseUrl', 'kgEnabled'],
    (sync) => {
      els.apiUrl.value = sync.apiUrl || defaultConfig.apiUrl;
      els.minVisitDuration.value = sync.minVisitDuration || defaultConfig.minVisitDuration;
      els.checkInterval.value = sync.checkInterval || defaultConfig.checkInterval;
      els.kgEnabled.checked = sync.kgEnabled === true;  // off unless explicitly enabled
      Object.assign(providerModels, sync.llmModels || {});
      llmBaseUrl = sync.llmBaseUrl || '';
      const provider = sync.defaultLlm || sync.preferredLlm || defaultConfig.defaultLlm;
      if (PROVIDERS[provider]) els.provider.value = provider;

      chrome.storage.local.get(KEY_STORES, (local) => {
        KEY_STORES.forEach((store) => {
          if (local[store]) providerKeys[keyToProvider(store)] = local[store];
        });
        applyProvider(els.provider.value || defaultConfig.defaultLlm);
      });
    }
  );
}

// ── Save ────────────────────────────────────────────────────────
function saveOptions() {
  const apiUrl = els.apiUrl.value.trim();
  const minVisitDuration = parseInt(els.minVisitDuration.value, 10);
  const checkInterval = parseInt(els.checkInterval.value, 10);
  const kgEnabled = els.kgEnabled.checked;

  if (!apiUrl) { showStatus('Enter a valid API URL.', 'error'); return; }
  if (isNaN(minVisitDuration) || minVisitDuration < 5) {
    showStatus('Min duration must be ≥ 5 seconds.', 'error'); return;
  }
  if (isNaN(checkInterval) || checkInterval < 5) {
    showStatus('Check interval must be ≥ 5 seconds.', 'error'); return;
  }

  // Fold the currently-visible fields into the maps before persisting.
  captureActiveProvider();
  const provider = activeProvider;

  if (provider === 'custom' && !llmBaseUrl) {
    showStatus('Custom provider needs a Base URL.', 'error'); return;
  }

  // Non-secret prefs → cloud-synced storage.
  chrome.storage.sync.set({
    apiUrl,
    minVisitDuration,
    checkInterval,
    kgEnabled,
    defaultLlm: provider,
    preferredLlm: provider,
    llmModels: providerModels,
    llmBaseUrl,
  });

  // API keys → local storage only (never cloud-synced).
  const secretStore = {};
  KEY_STORES.forEach((store) => { secretStore[store] = providerKeys[keyToProvider(store)] || ''; });

  chrome.storage.local.set(secretStore, () => {
    chrome.runtime.sendMessage({
      action: 'updateConfig',
      config: { apiUrl, minVisitDuration, checkInterval },
    });

    fetch(`${apiUrl}/update_api_keys`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildBackendCredentials(provider)),
    })
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then(() => fetch(`${apiUrl}/settings/kg`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: kgEnabled }),
      }))
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then(() => showStatus('Settings saved & synced with backend ✓', 'success'))
      .catch(e => showStatus(`Saved locally. Backend sync failed: ${e.message}`, 'error'));
  });
}

// Build the /update_api_keys payload from the remembered credentials. Empty
// strings (not null) for the custom triple so the backend clears stale values
// (it uses `is not None` there); per-provider keys use null (backend sets on truthy).
function buildBackendCredentials(provider) {
  const body = {
    default_provider: provider,
    llm_model: providerModels[provider] || '',
    llm_base_url: llmBaseUrl || '',
    llm_api_key: providerKeys.custom || '',
  };
  ['groq', 'openai', 'anthropic', 'google', 'openrouter'].forEach((p) => {
    body[PROVIDERS[p].backendKey] = providerKeys[p] || null;
  });
  return body;
}

// ── Test connection ─────────────────────────────────────────────
// Pushes the current form's credentials to the backend, then asks it to ping the
// provider with a trivial "hi". A reply → success; anything else → the error.
function testLlm() {
  const apiUrl = els.apiUrl.value.trim();
  if (!apiUrl) { setTestResult('Enter a valid API URL first.', 'error'); return; }

  captureActiveProvider();
  const provider = activeProvider;
  const meta = PROVIDERS[provider];

  if (provider === 'custom' && !llmBaseUrl) {
    setTestResult('Custom provider needs a Base URL.', 'error'); return;
  }
  if (meta.keyStore && !providerKeys[provider]) {
    setTestResult('Enter an API key first.', 'error'); return;
  }

  setTestBusy(true);
  setTestResult('Testing…', 'muted');

  fetch(`${apiUrl}/update_api_keys`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(buildBackendCredentials(provider)),
  })
    .then(r => { if (!r.ok) throw new Error(`config HTTP ${r.status}`); return r.json(); })
    .then(() => fetch(`${apiUrl}/test_llm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, model: providerModels[provider] || null }),
    }))
    .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
    .then(data => {
      if (data.ok) {
        setTestResult(`✓ ${data.provider} / ${data.model} responded - you're good to go.`, 'success');
      } else {
        setTestResult(`✗ ${truncate(data.error || 'No response from the model.')}`, 'error');
      }
    })
    .catch(e => setTestResult(`✗ Couldn't reach the backend: ${e.message}`, 'error'))
    .finally(() => setTestBusy(false));
}

function setTestBusy(busy) {
  testBtn.disabled = busy;
  testBtn.style.opacity = busy ? '0.6' : '';
}

function setTestResult(message, type) {
  testResultEl.textContent = message;
  testResultEl.className = `test-result ${type || ''}`.trim();
}

function truncate(s, max = 160) {
  s = String(s);
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

// ── Reset ───────────────────────────────────────────────────────
function resetOptions() {
  els.apiUrl.value = defaultConfig.apiUrl;
  els.minVisitDuration.value = defaultConfig.minVisitDuration;
  els.checkInterval.value = defaultConfig.checkInterval;
  Object.keys(providerKeys).forEach((k) => delete providerKeys[k]);
  Object.keys(providerModels).forEach((k) => delete providerModels[k]);
  llmBaseUrl = '';
  els.provider.value = defaultConfig.defaultLlm;
  applyProvider(defaultConfig.defaultLlm);
  showStatus('Reset to defaults - click Save to apply.', 'success');
}

// ── Clear data ──────────────────────────────────────────────────
function clearData() {
  if (!confirm('Delete ALL indexed data? This cannot be undone.')) return;
  const apiUrl = els.apiUrl.value.trim();
  if (!apiUrl) { showStatus('Enter a valid API URL first.', 'error'); return; }

  fetch(`${apiUrl}/clear_data`, { method: 'POST' })
    .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
    .then(() => showStatus('All indexed data cleared.', 'success'))
    .catch(e => showStatus(`Error: ${e.message}`, 'error'));
}

// ── Privacy Stats ───────────────────────────────────────────────
function loadPrivacyStats() {
  chrome.runtime.sendMessage({ action: 'getStats' }, (resp) => {
    if (chrome.runtime.lastError || !resp?.success) return;
    const s = resp.stats;
    const pv = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val ?? '-';
    };
    pv('priv-pages', s.total_pages ?? '-');
    pv('priv-chunks', s.total_chunks ?? '-');
    pv('priv-entities', s.total_entities ?? '-');
    pv('priv-size', s.index_size_mb != null ? `${s.index_size_mb} MB` : '-');
  });
}

// ── Export ──────────────────────────────────────────────────────
function exportData() {
  exportBtn.textContent = '⏳ Exporting...';
  exportBtn.disabled = true;

  chrome.runtime.sendMessage({ action: 'exportData' }, (resp) => {
    exportBtn.textContent = '⬇ Export All Data (JSON)';
    exportBtn.disabled = false;

    if (chrome.runtime.lastError || !resp?.success) {
      showStatus('Export failed - is the backend running?', 'error');
      return;
    }

    const blob = new Blob([JSON.stringify(resp.data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `recap-export-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
    showStatus('Export complete ✓', 'success');
  });
}

// ── Toast notification ──────────────────────────────────────────
let toastTimer = null;
function showStatus(message, type) {
  if (toastTimer) clearTimeout(toastTimer);
  statusEl.textContent = message;
  statusEl.className = type;

  // Trigger slide-up
  requestAnimationFrame(() => {
    statusEl.classList.add('visible');
  });

  toastTimer = setTimeout(() => {
    statusEl.classList.remove('visible');
  }, 4000);
}

// ── Sidebar scroll-spy ──────────────────────────────────────────
// Highlights the nav link whose section is currently in view. Anchors
// (href="#id") handle the smooth scroll themselves via CSS - no click
// handlers needed. We only observe which section owns the top of the
// viewport and toggle `.active` accordingly.
(() => {
  const navLinks = Array.from(document.querySelectorAll('.side-nav a'));
  const sections = navLinks
    .map((a) => document.querySelector(a.getAttribute('href')))
    .filter(Boolean);

  if (!sections.length || !('IntersectionObserver' in window)) return;

  const visible = new Set();
  const setActive = (id) => {
    navLinks.forEach((a) =>
      a.classList.toggle('active', a.getAttribute('href') === `#${id}`)
    );
  };

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) visible.add(entry.target.id);
        else visible.delete(entry.target.id);
      });
      // Activate the first section (in document order) still in the band.
      // If none qualify (e.g. scrolled past the last one), keep the last.
      const activeId = sections.map((s) => s.id).find((id) => visible.has(id));
      if (activeId) setActive(activeId);
    },
    // Observation band = top 25% of the viewport.
    { rootMargin: '0px 0px -75% 0px', threshold: 0 }
  );

  sections.forEach((s) => observer.observe(s));
})();
