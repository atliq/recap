// RECAP v2 - Content Script
// Content extraction, omnibar injection (Shadow DOM), highlight-to-save, toast notifications.

const pageLoadTime = Date.now();
let lastActivityTime = Date.now();
let omnibarHost = null;
let omnibarShadow = null;
let omnibarVisible = false;

// ──────────────────────────────────────────────────────────────
// Extension Context Guard
// Prevents "Extension context invalidated" errors when the
// extension is reloaded/updated while this content script is
// still running on the page.
// ──────────────────────────────────────────────────────────────

function isExtensionContextValid() {
  try {
    return !!(chrome.runtime && chrome.runtime.id);
  } catch {
    return false;
  }
}

function safeSendMessage(message, callback) {
  if (!isExtensionContextValid()) {
    console.warn('[RECAP] Extension context invalidated - ignoring message:', message.action);
    if (callback) callback(null);
    return;
  }
  try {
    chrome.runtime.sendMessage(message, (response) => {
      if (chrome.runtime.lastError) {
        console.warn('[RECAP] sendMessage error:', chrome.runtime.lastError.message);
      }
      if (callback) callback(response);
    });
  } catch (err) {
    console.warn('[RECAP] Extension context lost during sendMessage:', err.message);
    if (callback) callback(null);
  }
}

// Track user activity
['mousemove', 'keydown', 'scroll', 'click'].forEach(evt =>
  document.addEventListener(evt, () => { lastActivityTime = Date.now(); }, { passive: true })
);

// ──────────────────────────────────────────────────────────────
// Sensitive Page Detection
// Structural signals measured on the LIVE DOM (before extraction
// strips forms) so credential/account/checkout pages are refused
// here and their text never leaves the tab - not even to the
// local backend. URL blocklists can't cover the long tail; the
// page's own structure can.
// ──────────────────────────────────────────────────────────────

// autocomplete tokens the HTML spec reserves for credentials/payment data
const SENSITIVE_AUTOCOMPLETE = new Set([
  'current-password', 'new-password', 'one-time-code',
  'cc-number', 'cc-csc', 'cc-exp', 'cc-exp-month', 'cc-exp-year', 'cc-name'
]);

// Phrases that dominate auth walls but are incidental in real articles.
// Only consulted on short pages, where they are the page's whole purpose.
const AUTH_PHRASES = [
  'sign in to continue', 'log in to your account', 'sign in to your account',
  'forgot password', 'forgot your password', 'remember me',
  'enter the code', 'one-time password', 'verification code',
  'verify your identity', 'session expired', 'session has expired',
  'access denied', 'two-factor authentication', 'create an account'
];

// Visible = has layout size and isn't hidden. Many content sites embed a
// HIDDEN login form in the header (shown on click) - those must not count.
// Covers the common hiding patterns: display/visibility, opacity:0, and
// off-screen positioning (negative offsets, off-right drawers). Below the
// fold is NOT hidden - that's normal document flow.
function isElementVisible(el) {
  const rect = el.getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0) return false;
  if (rect.bottom < 0 || rect.right < 0 || rect.left > window.innerWidth) return false;
  const style = getComputedStyle(el);
  return style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
}

function detectSensitivePage() {
  try {
    // 1. Visible password field → auth page, on any domain, in any language.
    for (const el of document.querySelectorAll('input[type="password"]')) {
      if (isElementVisible(el)) return { sensitive: true, reason: 'password-field' };
    }

    // 2. Fields the site itself labels as credentials or payment data.
    for (const el of document.querySelectorAll('input[autocomplete], select[autocomplete]')) {
      const tokens = (el.getAttribute('autocomplete') || '').toLowerCase().split(/\s+/);
      if (tokens.some(t => SENSITIVE_AUTOCOMPLETE.has(t)) && isElementVisible(el)) {
        return { sensitive: true, reason: 'sensitive-autocomplete' };
      }
    }

    // 3. The site asks search engines not to index this page - honor it.
    //    Transactional/account pages set noindex; articles almost never do.
    const robots = document.querySelector('meta[name="robots"]');
    if (robots && /\bnoindex\b/i.test(robots.getAttribute('content') || '')) {
      return { sensitive: true, reason: 'noindex' };
    }

    const text = (document.body?.innerText || '').trim();
    const words = text ? text.split(/\s+/).length : 0;

    // 4. Form-dominant, text-light → dashboard/settings/checkout, not content.
    //    Buttons are excluded (cookie banners inflate them).
    let visibleFields = 0;
    for (const el of document.querySelectorAll('input:not([type="hidden"]), select, textarea')) {
      if (isElementVisible(el)) visibleFields++;
    }
    if (visibleFields >= 6 && words < 150) {
      return { sensitive: true, reason: 'form-dominant' };
    }

    // 5. Short page dominated by auth/transactional phrasing.
    if (words > 0 && words < 150) {
      const lowered = text.toLowerCase();
      const hits = AUTH_PHRASES.filter(p => lowered.includes(p)).length;
      if (hits >= 2) return { sensitive: true, reason: 'auth-text' };
    }

    return { sensitive: false, reason: '' };
  } catch {
    // Fail open: a detection error must not silently stop all indexing -
    // the URL blocklists and backend gates still stand guard.
    return { sensitive: false, reason: '' };
  }
}

// ──────────────────────────────────────────────────────────────
// Page Content Extraction
// ──────────────────────────────────────────────────────────────

// Primary extractor: Mozilla Readability - the Firefox Reader View algorithm,
// which positively SCORES DOM nodes (text density, link ratio, tag/class hints)
// and returns only the main-content subtree. Runs on a *clone* because
// Readability mutates the document. Returns null on pages it can't confidently
// parse (apps, forums, search results) so the subtractive fallback can run.
function extractReadable() {
  if (typeof Readability !== 'function') return null;
  try {
    const docClone = document.cloneNode(true);
    const article = new Readability(docClone, { charThreshold: 200 }).parse();
    if (!article || !article.textContent) return null;
    // Prefer structured Markdown (headings/lists/tables survive → better
    // chunking); fall back to Readability's plain text if conversion is thin.
    const markdown = htmlToMarkdown(article.content);
    const content = markdown.length >= 200 ? markdown : (article.textContent || '').trim();
    if (content.length < 200) return null;
    return {
      content,
      title: (article.title || document.title || '').trim(),
      description: (article.excerpt || '').trim(),
      author: (article.byline || '').trim(),
    };
  } catch (err) {
    console.warn('[RECAP] Readability failed, using fallback:', err.message);
    return null;
  }
}

// Fallback extractor: subtractive boilerplate removal + innerText. Used when
// Readability declines the page. Less structure-aware but robust everywhere.
function extractFallback() {
  const bodyClone = document.body.cloneNode(true);
  const remove = 'script, style, nav, footer, header, aside, iframe, noscript, ' +
    'svg, canvas, video, audio, form, [role="navigation"], [role="banner"], ' +
    '[role="complementary"], .sidebar, .nav, .menu, .footer, .header, .ad, ' +
    '.advertisement, .social-share, .cookie-banner, .popup, .modal';
  bodyClone.querySelectorAll(remove).forEach(el => el.remove());
  const content = (bodyClone.innerText || bodyClone.textContent || '').trim();
  const metaDesc = document.querySelector('meta[name="description"]');
  const metaAuthor = document.querySelector('meta[name="author"]');
  return {
    content,
    title: document.title || '',
    description: metaDesc ? metaDesc.getAttribute('content') || '' : '',
    author: metaAuthor ? metaAuthor.getAttribute('content') || '' : '',
  };
}

function getPageContent() {
  try {
    const extracted = extractReadable() || extractFallback();
    const content = extracted.content || '';
    // Density signal for the backend quality gate: chars of kept text per DOM
    // element on the live page. Computed identically for both extractors so the
    // classifier threshold behaves the same regardless of which path ran.
    const tagCount = document.body.querySelectorAll('*').length || 1;
    const textToTagRatio = content.length / tagCount;

    return {
      content,
      title: extracted.title || document.title || '',
      description: extracted.description || '',
      author: extracted.author || '',
      url: window.location.href,
      textToTagRatio: Math.round(textToTagRatio * 100) / 100,
      loadTime: pageLoadTime,
      lastActivity: lastActivityTime
    };
  } catch (error) {
    return {
      content: '', title: document.title || '', url: window.location.href,
      textToTagRatio: 0, loadTime: pageLoadTime, lastActivity: lastActivityTime
    };
  }
}

// ──────────────────────────────────────────────────────────────
// HTML → lightweight Markdown
// Converts Readability's cleaned HTML into Markdown that preserves the
// structure that matters for chunking + retrieval: headings, lists, tables,
// blockquotes, code. Inline emphasis is dropped (noise for retrieval). Parsing
// via DOMParser yields an inert document (no script exec, no resource loads),
// and the output is plain TEXT - never assigned to innerHTML - so there is no
// XSS or remote-fetch surface.
// ──────────────────────────────────────────────────────────────

function htmlToMarkdown(html) {
  const doc = new DOMParser().parseFromString(html || '', 'text/html');
  const md = serializeMarkdown(doc.body);
  return md.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
}

function serializeMarkdown(node, depth = 0) {
  // Bound recursion: content.js runs on every page (untrusted DOM), so a
  // pathologically deep subtree could overflow the stack. Past the cap, fall
  // back to collapsed text for this subtree.
  if (depth > 400) return node.textContent.replace(/\s+/g, ' ');
  let out = '';
  for (const child of node.childNodes) {
    if (child.nodeType === Node.TEXT_NODE) {
      out += child.textContent.replace(/\s+/g, ' ');
      continue;
    }
    if (child.nodeType !== Node.ELEMENT_NODE) continue;

    const tag = child.tagName.toLowerCase();
    switch (tag) {
      case 'h1': case 'h2': case 'h3':
      case 'h4': case 'h5': case 'h6': {
        const level = Number(tag[1]);
        out += `\n\n${'#'.repeat(level)} ${child.textContent.trim()}\n\n`;
        break;
      }
      case 'p':
        out += `\n\n${serializeMarkdown(child, depth + 1).trim()}\n\n`;
        break;
      case 'br':
        out += '\n';
        break;
      case 'hr':
        out += '\n\n---\n\n';
        break;
      case 'ul': case 'ol':
        out += `\n${serializeList(child, tag === 'ol', depth + 1)}\n`;
        break;
      case 'blockquote': {
        // Prefix quoted lines with "> ", but leave fenced-code lines intact so a
        // <pre> inside a quote isn't corrupted into "> ```".
        let inFence = false;
        const quoted = serializeMarkdown(child, depth + 1).trim()
          .split('\n').map((l) => {
            if (l.startsWith('```')) { inFence = !inFence; return l; }
            return inFence ? l : `> ${l}`;
          }).join('\n');
        out += `\n\n${quoted}\n\n`;
        break;
      }
      case 'pre':
        out += `\n\n\`\`\`\n${child.textContent.replace(/\n+$/, '')}\n\`\`\`\n\n`;
        break;
      case 'code': {
        // Inline code: collapse newlines; widen the fence if the text itself
        // contains a backtick so the span stays well-formed.
        const codeText = child.textContent.replace(/\s+/g, ' ').trim();
        out += codeText.includes('`') ? `\`\` ${codeText} \`\`` : `\`${codeText}\``;
        break;
      }
      case 'table':
        out += `\n\n${serializeTable(child)}\n\n`;
        break;
      case 'img': {
        const alt = (child.getAttribute('alt') || '').trim();
        if (alt) out += ` ${alt} `;
        break;
      }
      case 'script': case 'style': case 'noscript':
        break;
      default:
        out += serializeMarkdown(child, depth + 1);
    }
  }
  return out;
}

function serializeList(listEl, ordered, depth = 0) {
  let out = '';
  let i = 1;
  // Direct <li> children only; nested sub-lists are recursed into and indented so
  // the hierarchy survives instead of being flattened onto one line.
  for (const li of listEl.children) {
    if (li.tagName.toLowerCase() !== 'li') continue;
    const marker = ordered ? `${i++}.` : '-';

    // Separate the item's own inline content from nested sub-lists.
    let nested = '';
    const liClone = li.cloneNode(true);
    liClone.querySelectorAll(':scope > ul, :scope > ol').forEach((sub) => {
      nested += serializeList(sub, sub.tagName.toLowerCase() === 'ol', depth + 1);
      sub.remove();
    });
    const text = serializeMarkdown(liClone, depth + 1).trim().replace(/\s*\n\s*/g, ' ');
    out += `${marker} ${text}\n`;
    if (nested) {
      out += nested.split('\n').filter(Boolean).map((l) => `  ${l}`).join('\n') + '\n';
    }
  }
  return out;
}

function serializeTable(tableEl) {
  // Direct rows only - descendant selectors would pull a nested table's rows into
  // this one. Cover <tr> directly under <table> and under thead/tbody/tfoot.
  const rows = Array.from(tableEl.querySelectorAll(
    ':scope > tr, :scope > thead > tr, :scope > tbody > tr, :scope > tfoot > tr'
  ));
  if (!rows.length) return '';
  const toCells = (tr) =>
    Array.from(tr.querySelectorAll(':scope > th, :scope > td'))
      .map((c) => c.textContent.trim().replace(/\|/g, '\\|').replace(/\s+/g, ' '));
  const header = toCells(rows[0]);
  if (!header.length) return '';
  const lines = [
    `| ${header.join(' | ')} |`,
    `| ${header.map(() => '---').join(' | ')} |`,
  ];
  for (let r = 1; r < rows.length; r++) {
    const cells = toCells(rows[r]);
    if (cells.length) lines.push(`| ${cells.join(' | ')} |`);
  }
  return lines.join('\n');
}

// ──────────────────────────────────────────────────────────────
// Omnibar - Shadow DOM isolated floating search UI
// ──────────────────────────────────────────────────────────────

const OMNIBAR_CSS = `
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :host { all: initial; }

  .recap-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
    z-index: 2147483646;
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding-top: 15vh;
    animation: recap-fade-in 0.15s ease;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  }

  @keyframes recap-fade-in {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  @keyframes recap-slide-down {
    from { opacity: 0; transform: translateY(-12px) scale(0.98); }
    to { opacity: 1; transform: translateY(0) scale(1); }
  }

  @keyframes recap-slide-up {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }

  @keyframes recap-shimmer {
    0% { background-position: 200% 0; }
    100% { background-position: -200% 0; }
  }

  .recap-container {
    width: 640px;
    max-width: calc(100vw - 40px);
    background: #0e1520;
    border: 1px solid rgba(62, 207, 191, 0.25);
    border-radius: 16px;
    box-shadow: 0 24px 80px rgba(0, 0, 0, 0.6), 0 0 0 1px rgba(62, 207, 191, 0.08);
    overflow: hidden;
    animation: recap-slide-down 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
    max-height: 70vh;
    display: flex;
    flex-direction: column;
  }

  .recap-header {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 14px 18px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    flex-shrink: 0;
  }

  .recap-brand {
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 1px;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .recap-brand .re { color: #3ecfbf; }
  .recap-brand .cap { color: #ffffff; }

  .recap-input {
    flex: 1;
    background: none;
    border: none;
    outline: none;
    color: #e8e8ed;
    font-family: inherit;
    font-size: 15px;
    font-weight: 400;
    caret-color: #3ecfbf;
    min-width: 0;
  }

  .recap-input::placeholder { color: #5a5a6e; }

  .recap-esc {
    font-size: 10px;
    font-weight: 500;
    color: #5a5a6e;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 5px;
    padding: 3px 7px;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .recap-body {
    overflow-y: auto;
    flex: 1;
    padding: 0;
  }

  .recap-body::-webkit-scrollbar { width: 4px; }
  .recap-body::-webkit-scrollbar-track { background: transparent; }
  .recap-body::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 4px; }

  .recap-empty {
    padding: 40px 20px;
    text-align: center;
    color: #5a5a6e;
    font-size: 13px;
    line-height: 1.6;
  }

  .recap-empty-icon { font-size: 32px; margin-bottom: 12px; opacity: 0.5; }
  .recap-empty-title { color: #8b8b9e; font-size: 14px; font-weight: 600; margin-bottom: 6px; }

  .recap-skeleton { padding: 20px 18px; }
  .recap-skel-line {
    height: 11px;
    background: linear-gradient(90deg, rgba(255,255,255,0.04) 25%, rgba(255,255,255,0.08) 50%, rgba(255,255,255,0.04) 75%);
    background-size: 200% 100%;
    border-radius: 6px;
    margin-bottom: 10px;
    animation: recap-shimmer 1.5s infinite;
  }
  .recap-skel-line:nth-child(1) { width: 80%; }
  .recap-skel-line:nth-child(2) { width: 65%; }
  .recap-skel-line:nth-child(3) { width: 90%; margin-top: 16px; }
  .recap-skel-line:nth-child(4) { width: 55%; }

  .recap-answer {
    padding: 16px 18px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    animation: recap-slide-up 0.3s ease;
  }

  .recap-answer-label {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #3ecfbf;
    margin-bottom: 10px;
  }

  .recap-answer-dot {
    width: 6px;
    height: 6px;
    background: #3ecfbf;
    border-radius: 50%;
    animation: recap-pulse 2s infinite;
  }

  @keyframes recap-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }

  .recap-answer-text {
    font-size: 14px;
    line-height: 1.7;
    color: #e8e8ed;
    /* Long unbroken strings (URLs, tokens) must wrap inside the panel */
    overflow-wrap: anywhere;
    word-break: break-word;
  }

  .recap-answer-text strong { color: #fff; }
  .recap-answer-text em { color: #8b8b9e; font-style: italic; }

  .recap-answer-meta {
    display: flex;
    gap: 12px;
    margin-top: 10px;
    font-size: 11px;
    color: #5a5a6e;
  }

  .recap-sources { padding: 0; }

  .recap-sources-label {
    padding: 10px 18px 6px;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: #5a5a6e;
  }

  .recap-source-item {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 10px 18px;
    cursor: pointer;
    transition: background 0.15s;
    border-left: 2px solid transparent;
    animation: recap-slide-up 0.3s ease backwards;
  }

  .recap-source-item:hover {
    background: rgba(62, 207, 191, 0.05);
    border-left-color: #3ecfbf;
  }

  .recap-source-num {
    font-size: 11px;
    font-weight: 700;
    color: #3ecfbf;
    width: 16px;
    flex-shrink: 0;
    margin-top: 2px;
  }

  .recap-source-info { flex: 1; min-width: 0; }

  .recap-source-title {
    font-size: 13px;
    font-weight: 600;
    color: #e8e8ed;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-bottom: 2px;
  }

  .recap-source-url {
    font-size: 11px;
    color: #3ecfbf;
    opacity: 0.6;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .recap-source-snippet {
    font-size: 12px;
    color: #8b8b9e;
    margin-top: 3px;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .recap-footer {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 8px 18px;
    border-top: 1px solid rgba(255, 255, 255, 0.05);
    flex-shrink: 0;
  }

  .recap-hint {
    font-size: 11px;
    color: #5a5a6e;
    display: flex;
    align-items: center;
    gap: 5px;
  }

  .recap-hint kbd {
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 4px;
    padding: 1px 5px;
    font-size: 10px;
    font-family: inherit;
  }

  .recap-error {
    margin: 12px 18px;
    padding: 10px 14px;
    background: rgba(248, 113, 113, 0.08);
    border: 1px solid rgba(248, 113, 113, 0.2);
    border-radius: 8px;
    color: #f87171;
    font-size: 12px;
    animation: recap-slide-up 0.2s ease;
  }
`;

function buildOmnibarHTML() {
  const div = document.createElement('div');
  div.className = 'recap-overlay';
  div.id = 'recap-overlay';
  div.innerHTML = `
    <div class="recap-container" id="recap-container">
      <div class="recap-header">
        <div class="recap-brand"><span class="re">RE</span><span class="cap">CAP</span></div>
        <input class="recap-input" id="recap-input" type="text"
          placeholder="Ask anything about pages you've read..." autocomplete="off" spellcheck="false">
        <span class="recap-esc">ESC</span>
      </div>
      <div class="recap-body" id="recap-body">
        <div class="recap-empty">
          <div class="recap-empty-icon"><img src="${chrome.runtime.getURL('icons/RECAPL.png')}" alt="Recap" style="height: 48px; width: auto;"></div>
          <div class="recap-empty-title">Search your browsing memory</div>
          <div>Type a question and press Enter</div>
        </div>
      </div>
      <div class="recap-footer">
        <span class="recap-hint"><kbd>↵</kbd> Search</span>
        <span class="recap-hint"><kbd>ESC</kbd> Close</span>
        <span class="recap-hint"><kbd>↑↓</kbd> Navigate</span>
      </div>
    </div>
  `;
  return div;
}

function showOmnibar() {
  if (omnibarVisible) return;
  omnibarVisible = true;

  omnibarHost = document.createElement('div');
  omnibarHost.id = 'recap-omnibar-host';
  omnibarHost.style.cssText = 'position:fixed;top:0;left:0;width:0;height:0;z-index:2147483647;';

  omnibarShadow = omnibarHost.attachShadow({ mode: 'open' });

  const style = document.createElement('style');
  style.textContent = OMNIBAR_CSS;
  omnibarShadow.appendChild(style);

  const overlay = buildOmnibarHTML();
  omnibarShadow.appendChild(overlay);
  document.documentElement.appendChild(omnibarHost);

  // Focus input
  setTimeout(() => {
    const input = omnibarShadow.getElementById('recap-input');
    if (input) input.focus();
  }, 50);

  // Global Escape is handled by the capture-phase document keydown listener
  // below (which calls destroyOmnibar), so no separate one-shot handler is
  // registered here - that leaked and could swallow an unrelated later Escape.

  // Let local handler catch Enter keys
  overlay.addEventListener('keydown', handleOmnibarKey);

  // Click outside to close
  overlay.addEventListener('mousedown', (e) => {
    if (e.target === overlay) destroyOmnibar();
  });

  // Click ESC button to close
  const escBtn = omnibarShadow.querySelector('.recap-esc');
  if (escBtn) escBtn.addEventListener('click', destroyOmnibar);
}

function destroyOmnibar() {
  if (!omnibarVisible) return;
  omnibarVisible = false;
  if (omnibarHost) {
    const hostToRemove = omnibarHost;
    const shadow = omnibarShadow;
    omnibarHost = null;
    omnibarShadow = null;
    const overlay = shadow.getElementById('recap-overlay');
    if (overlay) {
      overlay.style.animation = 'recap-fade-in 0.15s ease reverse';
      setTimeout(() => { if (hostToRemove) hostToRemove.remove(); }, 140);
    } else {
      hostToRemove.remove();
    }
  }
}

function toggleOmnibar() {
  if (omnibarVisible) {
    destroyOmnibar();
  } else {
    showOmnibar();
  }
}

function handleOmnibarKey(e) {
  if (e.key === 'Escape') {
    destroyOmnibar();
    return;
  }
  if (e.key === 'Enter') {
    const input = omnibarShadow.getElementById('recap-input');
    if (input && input.value.trim()) {
      runOmnibarSearch(input.value.trim());
    }
  }
}

function runOmnibarSearch(query) {
  const body = omnibarShadow.getElementById('recap-body');
  if (!body) return;

  // Show skeleton
  body.innerHTML = `
    <div class="recap-skeleton">
      <div class="recap-skel-line"></div>
      <div class="recap-skel-line"></div>
      <div class="recap-skel-line"></div>
      <div class="recap-skel-line"></div>
    </div>`;

  safeSendMessage(
    { action: 'omnibarQuery', query, top_k: 3 },
    (response) => {
      if (!omnibarShadow) return;
      const b = omnibarShadow.getElementById('recap-body');
      if (!b) return;

      if (!response?.success) {
        b.innerHTML = `<div class="recap-error">
          Could not reach RECAP backend. Make sure the server is running.
        </div>`;
        return;
      }

      b.innerHTML = '';

      if (response.answer) {
        const ans = document.createElement('div');
        ans.className = 'recap-answer';
        ans.innerHTML = `
          <div class="recap-answer-label">
            <span class="recap-answer-dot"></span> AI Answer
          </div>
          <div class="recap-answer-text">${formatMd(esc(response.answer))}</div>
          <div class="recap-answer-meta">
            ${response.retrieval_time_ms != null ? `<span>⚡ ${Math.round(response.retrieval_time_ms)}ms</span>` : ''}
            ${response.sources_used != null ? `<span>📄 ${response.sources_used} source${response.sources_used !== 1 ? 's' : ''}</span>` : ''}
          </div>`;
        b.appendChild(ans);
      }

      if (response.results?.length > 0) {
        const sourceWrap = document.createElement('div');
        sourceWrap.className = 'recap-sources';
        const label = document.createElement('div');
        label.className = 'recap-sources-label';
        label.textContent = `Sources`;
        sourceWrap.appendChild(label);

        response.results.forEach((r, i) => {
          const item = document.createElement('div');
          item.className = 'recap-source-item';
          item.style.animationDelay = `${i * 0.05}s`;
          item.innerHTML = `
            <div class="recap-source-num">${i + 1}</div>
            <div class="recap-source-info">
              <div class="recap-source-title">${esc(r.title || 'Untitled')}</div>
              <div class="recap-source-url">${esc(shortUrl(r.url))}</div>
              ${r.snippet ? `<div class="recap-source-snippet">${esc(r.snippet)}</div>` : ''}
            </div>`;
          item.addEventListener('click', () => {
            window.open(safeUrl(r.url), '_blank');
            destroyOmnibar();
          });
          sourceWrap.appendChild(item);
        });
        b.appendChild(sourceWrap);
      }
    }
  );
}

// ──────────────────────────────────────────────────────────────
// Highlight-to-Save
// ──────────────────────────────────────────────────────────────

async function saveHighlight() {
  const selected = window.getSelection()?.toString()?.trim();

  if (!selected || selected.length < 5) {
    showToast('Select some text first, then press Ctrl+Shift+S', 'warning');
    return;
  }

  if (selected.length > 2000) {
    showToast('Selection too long. Select a shorter passage.', 'warning');
    return;
  }

  // Do not send page content to the backend on sensitive/blocked domains.
  const r = await chrome.runtime.sendMessage({ action: 'shouldTrack', url: location.href });
  if (!r || !r.track) {
    showToast('Saving is disabled on this site for privacy.', 'warning');
    return;
  }
  // Same DOM-level gate as indexing: no saving from login/payment pages.
  if (detectSensitivePage().sensitive) {
    showToast('Saving is disabled on this site for privacy.', 'warning');
    return;
  }

  safeSendMessage(
    {
      action: 'saveHighlight',
      text: selected,
      url: window.location.href,
      title: document.title
    },
    (response) => {
      if (!response?.success) {
        showToast('Could not save - is the backend running?', 'error');
        return;
      }
      const snippet = selected.length > 60 ? selected.substring(0, 60) + '…' : selected;
      showToast(`Saved: "${snippet}"`, 'success');
    }
  );
}

// ──────────────────────────────────────────────────────────────
// Selection Toolbar - floating bubble on text highlight
// ──────────────────────────────────────────────────────────────

const SELECTION_TOOLBAR_CSS = `
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :host { all: initial; }

  .recap-sel-toolbar {
    position: fixed;
    display: flex;
    align-items: center;
    background: #0e1520;
    border: 1px solid rgba(62, 207, 191, 0.35);
    border-radius: 8px;
    padding: 3px;
    gap: 2px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.65), 0 0 0 1px rgba(62,207,191,0.08);
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    z-index: 2147483646;
    animation: sel-in 0.14s cubic-bezier(0.34, 1.56, 0.64, 1);
    transform: translateX(-50%) translateY(calc(-100% - 10px));
    white-space: nowrap;
  }

  @keyframes sel-in {
    from { opacity: 0; transform: translateX(-50%) translateY(calc(-100% - 4px)) scale(0.9); }
    to   { opacity: 1; transform: translateX(-50%) translateY(calc(-100% - 10px)) scale(1); }
  }

  .recap-sel-btn {
    display: flex;
    align-items: center;
    gap: 5px;
    background: none;
    border: none;
    color: #e8e8ed;
    font-family: inherit;
    font-size: 12px;
    font-weight: 600;
    padding: 6px 11px;
    border-radius: 5px;
    cursor: pointer;
    transition: background 0.12s, color 0.12s;
    user-select: none;
    -webkit-user-select: none;
    letter-spacing: 0.2px;
  }

  .recap-sel-btn:hover { background: rgba(62, 207, 191, 0.12); color: #3ecfbf; }

  .recap-sel-icon-save { color: #3ecfbf; font-size: 11px; }
  .recap-sel-icon-search { color: #8b8b9e; }

  .recap-sel-divider {
    width: 1px; height: 18px;
    background: rgba(255,255,255,0.08);
    flex-shrink: 0;
  }
`;

let selectionToolbarHost = null;
let selectionToolbarShadow = null;

function showSelectionToolbar(text, cx, ty) {
  destroySelectionToolbar();

  selectionToolbarHost = document.createElement('div');
  selectionToolbarHost.id = 'recap-sel-host';
  selectionToolbarHost.style.cssText = 'position:fixed;top:0;left:0;width:0;height:0;z-index:2147483646;';
  selectionToolbarShadow = selectionToolbarHost.attachShadow({ mode: 'open' });

  const style = document.createElement('style');
  style.textContent = SELECTION_TOOLBAR_CSS;
  selectionToolbarShadow.appendChild(style);

  const toolbar = document.createElement('div');
  toolbar.className = 'recap-sel-toolbar';
  toolbar.style.left = cx + 'px';
  toolbar.style.top = ty + 'px';
  toolbar.innerHTML = `
    <button class="recap-sel-btn" id="recap-sel-save">
      <span class="recap-sel-icon-save">✦</span> Save
    </button>
    <div class="recap-sel-divider"></div>
    <button class="recap-sel-btn" id="recap-sel-search">
      <span class="recap-sel-icon-search">🔍</span> Search
    </button>`;
  selectionToolbarShadow.appendChild(toolbar);
  document.documentElement.appendChild(selectionToolbarHost);

  selectionToolbarShadow.getElementById('recap-sel-save').addEventListener('mousedown', async (e) => {
    e.preventDefault();
    destroySelectionToolbar();
    // Do not send page content to the backend on sensitive/blocked domains.
    const r = await chrome.runtime.sendMessage({ action: 'shouldTrack', url: location.href });
    if (!r || !r.track) {
      showToast('Saving is disabled on this site for privacy.', 'warning');
      return;
    }
    // Same DOM-level gate as indexing: no saving from login/payment pages.
    if (detectSensitivePage().sensitive) {
      showToast('Saving is disabled on this site for privacy.', 'warning');
      return;
    }
    safeSendMessage(
      { action: 'saveHighlight', text, url: window.location.href, title: document.title },
      (response) => {
        if (!response?.success) {
          showToast('Could not save - is the backend running?', 'error');
          return;
        }
        const snippet = text.length > 60 ? text.substring(0, 60) + '…' : text;
        showToast(`Saved: "${snippet}"`, 'success');
      }
    );
  });

  selectionToolbarShadow.getElementById('recap-sel-search').addEventListener('mousedown', (e) => {
    e.preventDefault();
    destroySelectionToolbar();
    showOmnibar();
    setTimeout(() => {
      const input = omnibarShadow && omnibarShadow.getElementById('recap-input');
      if (input) { input.value = text; runOmnibarSearch(text); }
    }, 80);
  });
}

function destroySelectionToolbar() {
  if (selectionToolbarHost) {
    try { selectionToolbarHost.remove(); } catch (e) { }
    selectionToolbarHost = null;
    selectionToolbarShadow = null;
  }
}

document.addEventListener('mouseup', () => {
  setTimeout(() => {
    const sel = window.getSelection();
    const text = sel?.toString()?.trim();
    if (!text || text.length < 5 || text.length > 2000) {
      destroySelectionToolbar();
      return;
    }
    try {
      const range = sel.getRangeAt(0);
      const rect = range.getBoundingClientRect();
      if (!rect.width && !rect.height) { destroySelectionToolbar(); return; }
      showSelectionToolbar(text, rect.left + rect.width / 2, rect.top);
    } catch (_) { destroySelectionToolbar(); }
  }, 10);
});

document.addEventListener('mousedown', (e) => {
  // Clicks inside the toolbar (shadow host is the target from doc's view)
  if (selectionToolbarHost && e.target === selectionToolbarHost) return;
  destroySelectionToolbar();
});

document.addEventListener('scroll', () => destroySelectionToolbar(), { passive: true });
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    destroySelectionToolbar();
    destroyOmnibar();
  }
}, true);

// ──────────────────────────────────────────────────────────────
// In-page Toast Notification (Shadow DOM)
// ──────────────────────────────────────────────────────────────

const TOAST_CSS = `
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :host { all: initial; }

  .recap-toast {
    position: fixed;
    bottom: 24px;
    right: 24px;
    z-index: 2147483647;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 18px;
    border-radius: 10px;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    font-size: 13px;
    font-weight: 500;
    max-width: 340px;
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    animation: toast-in 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
    line-height: 1.4;
  }

  @keyframes toast-in {
    from { opacity: 0; transform: translateY(16px) scale(0.95); }
    to { opacity: 1; transform: translateY(0) scale(1); }
  }

  @keyframes toast-out {
    from { opacity: 1; transform: translateY(0); }
    to { opacity: 0; transform: translateY(8px); }
  }

  .recap-toast.leaving { animation: toast-out 0.25s ease forwards; }

  .recap-toast.success {
    background: rgba(9, 13, 20, 0.95);
    border: 1px solid rgba(62, 207, 191, 0.35);
    color: #e8e8ed;
  }

  .recap-toast.error {
    background: rgba(9, 13, 20, 0.95);
    border: 1px solid rgba(248, 113, 113, 0.35);
    color: #f87171;
  }

  .recap-toast.warning {
    background: rgba(9, 13, 20, 0.95);
    border: 1px solid rgba(251, 191, 36, 0.35);
    color: #fbbf24;
  }

  .recap-toast-icon { font-size: 16px; flex-shrink: 0; }

  .recap-toast-body { flex: 1; min-width: 0; }

  .recap-toast-title {
    font-weight: 700;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 2px;
  }

  .recap-toast.success .recap-toast-title { color: #3ecfbf; }
  .recap-toast.error .recap-toast-title { color: #f87171; }
  .recap-toast.warning .recap-toast-title { color: #fbbf24; }

  .recap-toast-msg { font-size: 12px; color: #8b8b9e; }
`;

let toastHost = null;

function showToast(message, type = 'success') {
  // Remove existing toast
  if (toastHost) {
    try { toastHost.remove(); } catch (e) { }
    toastHost = null;
  }

  toastHost = document.createElement('div');
  toastHost.id = 'recap-toast-host';
  toastHost.style.cssText = 'position:fixed;bottom:0;right:0;width:0;height:0;z-index:2147483647;';

  const shadow = toastHost.attachShadow({ mode: 'open' });

  const style = document.createElement('style');
  style.textContent = TOAST_CSS;
  shadow.appendChild(style);

  const icons = { success: '✦', error: '✕', warning: '⚠' };
  const titles = { success: 'Saved to RECAP', error: 'Error', warning: 'Notice' };

  const toast = document.createElement('div');
  toast.className = `recap-toast ${type}`;
  toast.innerHTML = `
    <div class="recap-toast-icon">${icons[type] || '✦'}</div>
    <div class="recap-toast-body">
      <div class="recap-toast-title">${titles[type] || 'RECAP'}</div>
      <div class="recap-toast-msg">${esc(message)}</div>
    </div>`;
  shadow.appendChild(toast);
  document.documentElement.appendChild(toastHost);

  // Auto-dismiss
  setTimeout(() => {
    toast.classList.add('leaving');
    setTimeout(() => {
      if (toastHost) {
        try { toastHost.remove(); } catch (e) { }
        toastHost = null;
      }
    }, 260);
  }, 3200);
}

// ──────────────────────────────────────────────────────────────
// Resurface Card - subtle related-page reminder (Shadow DOM)
// ──────────────────────────────────────────────────────────────

const RESURFACE_CSS = `
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :host { all: initial; }

  .recap-resurface {
    position: fixed;
    bottom: 24px;
    left: 24px;
    z-index: 2147483646;
    width: 300px;
    background: #0e1520;
    border: 1px solid rgba(62, 207, 191, 0.28);
    border-radius: 12px;
    box-shadow: 0 12px 40px rgba(0,0,0,0.55), 0 0 0 1px rgba(62,207,191,0.06);
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    animation: rs-in 0.35s cubic-bezier(0.34, 1.56, 0.64, 1);
    overflow: hidden;
  }

  @keyframes rs-in {
    from { opacity: 0; transform: translateY(20px) scale(0.95); }
    to   { opacity: 1; transform: translateY(0) scale(1); }
  }

  @keyframes rs-out {
    from { opacity: 1; transform: translateY(0); }
    to   { opacity: 0; transform: translateY(12px); }
  }

  .recap-resurface.leaving { animation: rs-out 0.22s ease forwards; }

  .rs-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px 8px;
    border-bottom: 1px solid rgba(255,255,255,0.05);
  }

  .rs-label {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.8px;
    text-transform: uppercase;
    color: #3ecfbf;
  }

  .rs-dot {
    width: 5px; height: 5px;
    background: #3ecfbf;
    border-radius: 50%;
    animation: rs-pulse 2.5s infinite;
  }

  @keyframes rs-pulse {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.3; }
  }

  .rs-close {
    background: none;
    border: none;
    color: #5a5a6e;
    font-size: 16px;
    cursor: pointer;
    line-height: 1;
    padding: 2px 4px;
    border-radius: 4px;
    transition: color 0.12s;
  }

  .rs-close:hover { color: #e8e8ed; }

  .rs-body {
    padding: 12px 14px 14px;
  }

  .rs-title {
    font-size: 13px;
    font-weight: 600;
    color: #e8e8ed;
    line-height: 1.4;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    margin-bottom: 4px;
    cursor: pointer;
    transition: color 0.12s;
  }

  .rs-title:hover { color: #3ecfbf; }

  .rs-url {
    font-size: 11px;
    color: #3ecfbf;
    opacity: 0.6;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-bottom: 8px;
  }

  .rs-snippet {
    font-size: 12px;
    color: #8b8b9e;
    line-height: 1.5;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    margin-bottom: 10px;
  }

  .rs-open {
    display: inline-block;
    padding: 6px 12px;
    background: rgba(62,207,191,0.12);
    border: 1px solid rgba(62,207,191,0.25);
    border-radius: 6px;
    color: #3ecfbf;
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    text-decoration: none;
    transition: background 0.12s;
    font-family: inherit;
  }

  .rs-open:hover { background: rgba(62,207,191,0.22); }
`;

let resurfaceHost = null;

function showResurfaceCard(page) {
  if (resurfaceHost) return; // only one at a time

  resurfaceHost = document.createElement('div');
  resurfaceHost.id = 'recap-resurface-host';
  resurfaceHost.style.cssText = 'position:fixed;bottom:0;left:0;width:0;height:0;z-index:2147483646;';

  const shadow = resurfaceHost.attachShadow({ mode: 'open' });

  const style = document.createElement('style');
  style.textContent = RESURFACE_CSS;
  shadow.appendChild(style);

  const card = document.createElement('div');
  card.className = 'recap-resurface';
  card.innerHTML = `
    <div class="rs-header">
      <div class="rs-label"><span class="rs-dot"></span>Resurface</div>
      <button class="rs-close" id="rs-close-btn" title="Dismiss">&#x2715;</button>
    </div>
    <div class="rs-body">
      <div class="rs-title" id="rs-title">${esc(page.title || 'Untitled')}</div>
      <div class="rs-url">${esc(shortUrl(page.url))}</div>
      ${page.snippet ? `<div class="rs-snippet">${esc(page.snippet)}</div>` : ''}
      <button class="rs-open" id="rs-open-btn">Open page</button>
    </div>`;
  shadow.appendChild(card);
  document.documentElement.appendChild(resurfaceHost);

  shadow.getElementById('rs-close-btn').addEventListener('click', destroyResurfaceCard);
  shadow.getElementById('rs-open-btn').addEventListener('click', () => {
    window.open(safeUrl(page.url), '_blank');
    destroyResurfaceCard();
  });
  shadow.getElementById('rs-title').addEventListener('click', () => {
    window.open(safeUrl(page.url), '_blank');
    destroyResurfaceCard();
  });

  // Auto-dismiss after 12 seconds
  setTimeout(() => destroyResurfaceCard(), 12000);
}

function destroyResurfaceCard() {
  if (!resurfaceHost) return;
  const shadow = resurfaceHost.shadowRoot;
  const card = shadow && shadow.querySelector('.recap-resurface');
  if (card) {
    card.classList.add('leaving');
    setTimeout(() => {
      if (resurfaceHost) { try { resurfaceHost.remove(); } catch (_) { } resurfaceHost = null; }
    }, 230);
  } else {
    try { resurfaceHost.remove(); } catch (_) { }
    resurfaceHost = null;
  }
}

// Trigger resurface after page settles
setTimeout(async () => {
  if (!isExtensionContextValid()) return;
  try {
    // Do not exfiltrate page content on sensitive/blocked domains.
    const r = await chrome.runtime.sendMessage({ action: 'shouldTrack', url: location.href });
    if (!r || !r.track) return;
    // Same DOM-level gate as indexing: sensitive pages send nothing anywhere.
    if (detectSensitivePage().sensitive) return;
    const content = getPageContent();
    // Skip when the page has essentially no extractable text.
    if (!content.content || content.content.length < 50) return;
    safeSendMessage(
      { action: 'getRelated', url: window.location.href, title: content.title || document.title || '', content: content.content.substring(0, 600) },
      (response) => {
        if (response?.success && response.related?.length > 0) {
          showResurfaceCard(response.related[0]);
        }
      }
    );
  } catch (_) { }
}, 5000);

// ──────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────

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
  try {
    const u = new URL(url);
    const path = u.pathname.length > 28 ? u.pathname.substring(0, 28) + '…' : u.pathname;
    return u.hostname + path;
  } catch { return url; }
}

function formatMd(text) {
  if (!text) return '';
  return text
    .replace(/^#{1,6}\s+(.+)$/gm, '<strong style="display:block;margin:8px 0 2px;">$1</strong>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/`([^`]+)`/g, '<code style="background:rgba(62,207,191,0.12);color:#5eddd4;padding:1px 5px;border-radius:4px;font-size:12px;">$1</code>')
    .replace(/\n/g, '<br>');
}

// ──────────────────────────────────────────────────────────────
// Message Handler
// ──────────────────────────────────────────────────────────────

if (isExtensionContextValid()) {
  try {
    chrome.runtime.onMessage.addListener((request, _sender, sendResponse) => {
      if (request.action === 'getPageContent') {
        // Refuse BEFORE extraction so sensitive text never leaves the tab.
        const check = detectSensitivePage();
        if (check.sensitive) {
          sendResponse({ skip: true, reason: check.reason, url: window.location.href });
        } else {
          sendResponse(getPageContent());
        }
        return true;
      }

      if (request.action === 'toggleOmnibar') {
        toggleOmnibar();
        sendResponse({ success: true });
        return false;
      }

      if (request.action === 'triggerSaveHighlight') {
        saveHighlight();
        sendResponse({ success: true });
        return false;
      }

      if (request.action === 'showToast') {
        showToast(request.message || '', request.type || 'success');
        sendResponse({ success: true });
        return false;
      }

      if (request.action === 'launchOmnibarWithQuery') {
        showOmnibar();
        setTimeout(() => {
          const input = omnibarShadow && omnibarShadow.getElementById('recap-input');
          if (input && request.query) {
            input.value = request.query;
            runOmnibarSearch(request.query);
          }
        }, 80);
        sendResponse({ success: true });
        return false;
      }

      return false;
    });
  } catch (err) {
    console.warn('[RECAP] Could not register message listener - context invalidated:', err.message);
  }
}
