// RECAP Flashcards - spaced-repetition-style quiz UI

let deck = [];
let currentIndex = 0;
let goodCount = 0;
let againQueue = [];
let isFlipped = false;

async function loadFlashcards(url) {
  const urlInput = document.getElementById('url-input');
  const targetUrl = url || urlInput?.value.trim() || '';

  document.getElementById('loading').style.display = 'block';
  document.getElementById('error').style.display = 'none';
  document.getElementById('quiz-area').style.display = 'none';
  document.getElementById('done').style.display = 'none';

  try {
    const apiBase = await getApiBase();
    const params = targetUrl ? `?url=${encodeURIComponent(targetUrl)}` : '';
    const res = await fetch(`${apiBase}/flashcards${params}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    deck = data.flashcards || [];
    if (deck.length === 0) {
      document.getElementById('loading').style.display = 'none';
      document.getElementById('error').textContent = 'No flashcards generated. Try a different page.';
      document.getElementById('error').style.display = 'block';
      return;
    }

    const label = document.getElementById('source-label');
    if (data.source_title) {
      label.innerHTML = `From: <a href="${safeUrl(data.source_url)}" target="_blank">${esc(data.source_title)}</a>`;
    } else {
      label.textContent = 'Random page from your memory';
    }

    currentIndex = 0;
    goodCount = 0;
    againQueue = [];
    document.getElementById('loading').style.display = 'none';
    document.getElementById('quiz-area').style.display = 'flex';
    showCard();
  } catch (e) {
    document.getElementById('loading').style.display = 'none';
    document.getElementById('error').textContent = 'Could not load flashcards. Is the backend running?';
    document.getElementById('error').style.display = 'block';
  }
}

function showCard() {
  const allCards = [...deck, ...againQueue];
  if (currentIndex >= allCards.length) {
    showDone();
    return;
  }

  const card = allCards[currentIndex];
  document.getElementById('question-text').textContent = card.question;
  document.getElementById('answer-text').textContent = card.answer;

  // Reset flip state
  isFlipped = false;
  document.getElementById('card').classList.remove('flipped');
  document.getElementById('controls').style.display = 'none';
  document.getElementById('flip-btn').style.display = 'inline-block';

  // Update progress
  const total = deck.length;
  const pct = Math.round((Math.min(currentIndex, total) / total) * 100);
  document.getElementById('progress-bar').style.width = pct + '%';
  document.getElementById('counter').textContent = `Card ${Math.min(currentIndex + 1, total)} of ${total}`;
}

function flipCard() {
  if (isFlipped) return;
  isFlipped = true;
  document.getElementById('card').classList.add('flipped');
  document.getElementById('flip-btn').style.display = 'none';
  document.getElementById('controls').style.display = 'flex';
}

function markCard(good) {
  if (good) {
    goodCount++;
  } else {
    // Put card at back of queue to review again
    const allCards = [...deck, ...againQueue];
    againQueue.push(allCards[currentIndex]);
  }
  currentIndex++;
  showCard();
}

function showDone() {
  document.getElementById('quiz-area').style.display = 'none';
  document.getElementById('done').style.display = 'block';
  document.getElementById('progress-bar').style.width = '100%';
  const total = deck.length;
  document.getElementById('done-msg').textContent =
    `You got ${goodCount} out of ${total} cards right. ${goodCount === total ? 'Perfect score!' : 'Keep practising!'}`;
}

function restartDeck() {
  currentIndex = 0;
  goodCount = 0;
  againQueue = [];
  document.getElementById('done').style.display = 'none';
  document.getElementById('quiz-area').style.display = 'flex';
  showCard();
}

function getApiBase() {
  return new Promise((resolve) => {
    if (typeof chrome !== 'undefined' && chrome.storage) {
      chrome.storage.sync.get(['apiUrl'], (r) => resolve(r.apiUrl || 'http://localhost:8000'));
    } else {
      resolve('http://localhost:8000');
    }
  });
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

// Auto-load on page open - check URL param first
const urlParams = new URLSearchParams(window.location.search);
const pageUrl = urlParams.get('url');
loadFlashcards(pageUrl || '');

// Wire up buttons (inline onclick is blocked by MV3 CSP)
document.getElementById('back-btn').addEventListener('click', () => {
  if (history.length > 1) history.back();
  else window.close();
});
document.getElementById('load-btn').addEventListener('click', () => loadFlashcards());
document.getElementById('card').addEventListener('click', flipCard);
document.getElementById('flip-btn').addEventListener('click', flipCard);
document.getElementById('btn-again').addEventListener('click', () => markCard(false));
document.getElementById('btn-good').addEventListener('click', () => markCard(true));
document.getElementById('restart-btn').addEventListener('click', restartDeck);
