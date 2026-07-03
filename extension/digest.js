    function renderMarkdown(text) {
      // Escape HTML first so LLM/page-derived content can't inject markup.
      return String(text ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/^### (.+)$/gm, '<h3>$1</h3>')
        .replace(/^## (.+)$/gm, '<h2>$1</h2>')
        .replace(/^# (.+)$/gm, '<h1>$1</h1>')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\*(.+?)\*/g, '<em>$1</em>')
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/^---$/gm, '<hr>')
        .replace(/^\- (.+)$/gm, '<li>$1</li>')
        .replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>')
        .replace(/\n\n/g, '</p><p>')
        .replace(/^(?!<[hup])(.+)$/gm, '<p>$1</p>');
    }

    async function loadDigest() {
      document.getElementById('loading').style.display = 'block';
      document.getElementById('digest-card').style.display = 'none';

      try {
        let apiBase = 'http://localhost:8000';
        if (typeof chrome !== 'undefined' && chrome.storage) {
          await new Promise(resolve => {
            chrome.storage.sync.get(['apiUrl'], r => { apiBase = r.apiUrl || apiBase; resolve(); });
          });

          // First check if we have a cached digest
          const cached = await new Promise(resolve => {
            chrome.storage.local.get(['lastDigest'], r => resolve(r.lastDigest));
          });

          if (cached && cached.generated_at) {
            const age = Date.now() - new Date(cached.generated_at).getTime();
            // Use cached if less than 6 hours old
            if (age < 6 * 60 * 60 * 1000) {
              showDigest(cached);
              return;
            }
          }
        }

        const res = await fetch(`${apiBase}/digest`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        if (typeof chrome !== 'undefined' && chrome.storage) {
          chrome.storage.local.set({ lastDigest: data });
        }

        showDigest(data);
      } catch (e) {
        document.getElementById('loading').style.display = 'none';
        document.getElementById('digest-card').style.display = 'block';
        document.getElementById('digest-body').innerHTML =
          '<p style="color:#f87171">Failed to generate digest. Make sure the backend is running.</p>';
      }
    }

    function showDigest(data) {
      document.getElementById('loading').style.display = 'none';

      const card = document.getElementById('digest-card');
      card.style.display = 'block';

      const meta = document.getElementById('digest-meta');
      if (data.generated_at) {
        const d = new Date(data.generated_at);
        meta.textContent = `Generated ${d.toLocaleDateString(undefined, { weekday:'long', month:'long', day:'numeric' })}`;
      }

      const pill = document.getElementById('page-count-pill');
      pill.textContent = `${data.page_count || 0} pages this week`;

      const body = document.getElementById('digest-body');
      body.innerHTML = renderMarkdown(data.digest || 'No digest available.');
    }

    loadDigest();

// Button handlers (moved off inline onclick= for MV3 CSP compliance)
document.getElementById('digest-back-btn')?.addEventListener('click', () => {
  if (history.length > 1) {
    history.back();
  } else {
    window.location.href = chrome.runtime.getURL('newtab.html');
  }
});
document.getElementById('digest-refresh-btn')?.addEventListener('click', () => loadDigest());
document.getElementById('digest-flashcards-btn')?.addEventListener('click', () =>
  window.open(chrome.runtime.getURL('flashcards.html'), '_blank'));
