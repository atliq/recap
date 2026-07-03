    let currentStep = 1;
    const totalSteps = 5;

    const progress = document.getElementById('progress');
    const nextBtn = document.getElementById('next-btn');
    const skipBtn = document.getElementById('skip-btn');
    const testBtn = document.getElementById('test-conn-btn');
    const connStatus = document.getElementById('conn-status');
    const apiInput = document.getElementById('api-url-input');

    function goToStep(n) {
      document.getElementById(`step-${currentStep}`).classList.remove('active');
      document.getElementById(`dot-${currentStep}`).classList.remove('active');
      currentStep = n;
      document.getElementById(`step-${currentStep}`).classList.add('active');
      document.getElementById(`dot-${currentStep}`).classList.add('active');
      progress.style.width = `${(currentStep / totalSteps) * 100}%`;

      if (currentStep === totalSteps) {
        nextBtn.textContent = 'Start Browsing →';
        skipBtn.style.display = 'none';
        loadStats();
      } else if (currentStep === totalSteps - 1) {
        nextBtn.textContent = 'Next →';
        skipBtn.style.display = '';
      } else {
        nextBtn.textContent = 'Next →';
        skipBtn.style.display = '';
      }
    }

    nextBtn.addEventListener('click', () => {
      if (currentStep < totalSteps) {
        // On step 2, save the API URL before proceeding
        if (currentStep === 2) {
          const url = apiInput.value.trim();
          if (url) {
            chrome.storage.sync.set({ apiUrl: url });
            chrome.runtime.sendMessage({ action: 'updateConfig', config: { apiUrl: url } });
          }
        }
        if (currentStep === 3) {
          saveModelChoice();
        }
        goToStep(currentStep + 1);
      } else {
        // Done - close onboarding tab
        chrome.runtime.sendMessage({ action: 'updateConfig', config: {} });
        window.close();
      }
    });

    skipBtn.addEventListener('click', () => {
      window.close();
    });

    testBtn.addEventListener('click', async () => {
      const url = apiInput.value.trim() || 'http://localhost:8000';
      testBtn.textContent = 'Testing...';
      testBtn.disabled = true;
      connStatus.className = 'conn-status';

      try {
        const res = await fetch(`${url}/health`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        connStatus.className = 'conn-status success show';
        connStatus.textContent = `✓ Connected to RECAP${data.version ? ` v${data.version}` : ''} - ${data.pages_indexed || 0} pages indexed`;

        chrome.storage.sync.set({ apiUrl: url });
        chrome.runtime.sendMessage({ action: 'updateConfig', config: { apiUrl: url } });

        // Auto-advance after success
        setTimeout(() => goToStep(3), 1200);

      } catch (err) {
        connStatus.className = 'conn-status error show';
        connStatus.textContent = `✕ Could not connect: ${err.message}. Make sure the backend is running.`;
      }

      testBtn.textContent = 'Test Connection';
      testBtn.disabled = false;
    });

    function loadStats() {
      chrome.runtime.sendMessage({ action: 'getStats' }, (resp) => {
        if (resp?.success) {
          const pages = resp.stats?.total_pages || 0;
          const chunks = resp.stats?.total_chunks || 0;
          animateCount('ready-pages', pages);
          animateCount('ready-chunks', chunks);
        }
      });
    }

    function animateCount(id, target) {
      const el = document.getElementById(id);
      if (!el) return;
      let cur = 0;
      const steps = 20;
      const interval = setInterval(() => {
        cur += Math.ceil(target / steps);
        if (cur >= target) { cur = target; clearInterval(interval); }
        el.textContent = cur;
      }, 40);
    }

    // ── AI provider selection (step 3) ──
    const provSelect = document.getElementById('onboard-provider');

    // Mirrors the presets in options.js and backend/retrieval/answer_generator.py.
    const ONBOARD_PROVIDERS = {
      openai:     { keyStore: 'openaiApiKey',     backendKey: 'openai_api_key',     model: 'gpt-4o-mini' },
      groq:       { keyStore: 'groqApiKey',       backendKey: 'groq_api_key',       model: 'llama-3.3-70b-versatile' },
      openrouter: { keyStore: 'openrouterApiKey', backendKey: 'openrouter_api_key', model: 'openai/gpt-4o-mini' },
      google:     { keyStore: 'googleApiKey',     backendKey: 'google_api_key',     model: 'gemini-2.0-flash' },
      anthropic:  { keyStore: 'anthropicApiKey',  backendKey: 'anthropic_api_key',  model: 'claude-3-5-haiku-20241022' },
      ollama:     { keyStore: null,               backendKey: null,                 model: 'gemma3:4b' },
      custom:     { keyStore: 'customApiKey',     backendKey: 'llm_api_key',        model: '' },
    };

    function syncProviderFields() {
      if (!provSelect) return;
      const p = provSelect.value;
      const meta = ONBOARD_PROVIDERS[p] || {};
      const localHint = document.getElementById('onboard-local-hint');
      const keyGroup = document.getElementById('onboard-key-group');
      const urlGroup = document.getElementById('onboard-baseurl-group');
      const modelInput = document.getElementById('onboard-model');
      if (localHint) localHint.style.display = (p === 'ollama') ? '' : 'none';
      if (keyGroup) keyGroup.style.display = (p === 'ollama') ? 'none' : '';
      if (urlGroup) urlGroup.style.display = (p === 'custom') ? '' : 'none';
      if (modelInput) modelInput.placeholder = meta.model ? `default: ${meta.model}` : 'e.g. your-model-id';
    }
    if (provSelect) provSelect.addEventListener('change', syncProviderFields);

    function saveModelChoice() {
      if (!provSelect) return;
      const provider = provSelect.value;
      const meta = ONBOARD_PROVIDERS[provider] || {};
      const key = (document.getElementById('onboard-key')?.value || '').trim();
      const baseUrl = (document.getElementById('onboard-baseurl')?.value || '').trim();
      const model = (document.getElementById('onboard-model')?.value || '').trim();

      // Persist locally so the Settings page reflects what was chosen here.
      chrome.storage.sync.get(['llmModels'], (d) => {
        const llmModels = Object.assign({}, d.llmModels || {});
        llmModels[provider] = model;
        const prefs = { preferredLlm: provider, defaultLlm: provider, llmModels };
        if (provider === 'custom') prefs.llmBaseUrl = baseUrl;
        chrome.storage.sync.set(prefs);
      });
      if (meta.keyStore && key) chrome.storage.local.set({ [meta.keyStore]: key });

      const apiUrl = apiInput.value.trim() || 'http://localhost:8000';
      const body = { default_provider: provider, llm_model: model || '' };
      if (provider === 'custom') { body.llm_base_url = baseUrl; body.llm_api_key = key; }
      else if (meta.backendKey) body[meta.backendKey] = key;
      // 'ollama' needs no key; the backend preset points at http://localhost:11434/v1
      fetch(`${apiUrl}/update_api_keys`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
      }).catch(() => {});
    }

    // Load saved settings
    chrome.storage.sync.get(['apiUrl', 'preferredLlm', 'defaultLlm', 'llmModels', 'llmBaseUrl'], (data) => {
      if (data.apiUrl) apiInput.value = data.apiUrl;
      const p = data.defaultLlm || data.preferredLlm;
      if (p && provSelect) provSelect.value = p;
      const cur = provSelect ? provSelect.value : '';
      const modelInput = document.getElementById('onboard-model');
      if (modelInput && data.llmModels && data.llmModels[cur]) modelInput.value = data.llmModels[cur];
      const urlInput = document.getElementById('onboard-baseurl');
      if (urlInput && data.llmBaseUrl) urlInput.value = data.llmBaseUrl;
      syncProviderFields();
      // Prefill the saved key for the selected provider, if any.
      const meta = ONBOARD_PROVIDERS[cur];
      if (meta && meta.keyStore) {
        chrome.storage.local.get([meta.keyStore], (k) => {
          const keyInput = document.getElementById('onboard-key');
          if (keyInput && k[meta.keyStore]) keyInput.value = k[meta.keyStore];
        });
      }
    });
