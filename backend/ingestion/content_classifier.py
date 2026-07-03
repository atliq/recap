"""
RECAP v2 - Content Classifier

Multi-signal classifier that determines whether a page contains
valuable content worth indexing. Replaces the 250+ regex patterns
in the old background.js with a score-based approach.

Signals:
1. URL structure analysis (path patterns, depth, TLD)
2. Content heuristics (word count, text-to-tag ratio, paragraph density)
3. Domain blocklist (skip auth, banking, email, etc.)
"""

from __future__ import annotations

import logging
import re
from typing import Optional, Tuple
from urllib.parse import urlparse

from backend.models import ContentType

logger = logging.getLogger(__name__)


# =============================================================================
# Domain Classification
# =============================================================================

# Domains that should NEVER be indexed (defense-in-depth, mirrors extension blocklist)
BLOCKED_DOMAINS = frozenset([
    # ── AI Chat / LLM Playgrounds ─────────────────────────────
    "chat.openai.com", "chatgpt.com", "platform.openai.com",
    "claude.ai", "console.anthropic.com",
    "console.groq.com", "groq.com",
    "gemini.google.com", "aistudio.google.com", "bard.google.com",
    "copilot.microsoft.com", "copilot.github.com",
    "poe.com", "perplexity.ai", "you.com",
    "labs.perplexity.ai", "chat.mistral.ai", "coral.cohere.com",
    "lmsys.org", "together.ai",

    # ── Auth & SSO ────────────────────────────────────────────
    "accounts.google.com", "login.microsoftonline.com",
    "appleid.apple.com", "id.apple.com",
    "login.yahoo.com", "auth0.com",
    "okta.com", "onelogin.com",
    # Auth subdomain patterns (substring matched in _is_blocked)
    "sso.", "login.", "signin.", "oauth.", "auth.",

    # ── Banking & Finance (Major Global) ──────────────────────
    # US banks
    "chase.com", "bankofamerica.com", "wellsfargo.com",
    "citi.com", "citibank.com", "usbank.com",
    "capitalone.com", "ally.com", "discover.com", "tdbank.com",
    "pnc.com", "fidelity.com", "schwab.com",
    "vanguard.com", "etrade.com", "robinhood.com",
    "sofi.com", "marcus.com", "americanexpress.com",
    # UK banks
    "hsbc.com", "barclays.co.uk", "natwest.com",
    "lloydsbank.com", "halifax.co.uk", "nationwide.co.uk",
    "tsb.co.uk", "monzo.com", "revolut.com", "starlingbank.com",
    # EU / Global
    "db.com", "ing.com", "bnpparibas.com",
    "credit-suisse.com", "ubs.com",
    # India
    "hdfcbank.com", "icicibank.com", "sbi.co.in",
    "onlinesbi.sbi", "kotak.com", "axisbank.com",
    "yesbank.in", "idfcfirstbank.com",
    # Banking subdomain patterns
    "onlinebanking.", "netbanking.", "ibanking.",
    "ebanking.", "mobilebanking.", "secure.",

    # ── Payment Processors & Fintech ──────────────────────────
    "paypal.com", "venmo.com", "stripe.com",
    "square.com", "wise.com", "razorpay.com",
    "paytm.com", "phonepe.com", "gpay.com",
    "crypto.com", "coinbase.com", "binance.com", "kraken.com",

    # ── Email & Messaging ─────────────────────────────────────
    "mail.google.com", "outlook.live.com", "outlook.office.com",
    "outlook.office365.com", "mail.yahoo.com",
    "protonmail.com", "mail.proton.me",
    "web.whatsapp.com", "web.telegram.org", "discord.com",
    "slack.com", "teams.microsoft.com",
    "messenger.com", "messages.google.com",

    # ── Healthcare Portals ────────────────────────────────────
    "mychart.com", "mychartsso.com", "healthvault.com",

    # ── Password Managers ─────────────────────────────────────
    "vault.bitwarden.com", "my.1password.com", "lastpass.com",
    "dashlane.com", "keeper.io",

    # ── Government / Tax ──────────────────────────────────────
    "irs.gov", "ssa.gov", "turbotax.intuit.com",

    # ── Browser internals ─────────────────────────────────────
    "chrome://", "about:", "edge://", "brave://",
    "chrome-extension://", "moz-extension://",

    # ── Social Media Feeds ────────────────────────────────────
    "facebook.com", "instagram.com", "tiktok.com",
    "twitter.com/home", "x.com/home",
    "snapchat.com", "pinterest.com",

    # ── Streaming (not textual) ───────────────────────────────
    "netflix.com", "disneyplus.com", "primevideo.com",
    "hulu.com", "hbomax.com", "peacocktv.com",
    "spotify.com", "music.youtube.com", "music.apple.com",

    # ── Checkout ──────────────────────────────────────────────
    "checkout.", "cart.",
])

# URL path patterns that indicate non-informational pages
BLOCKED_PATH_PATTERNS = [
    re.compile(r"/log-?in(/|$)", re.IGNORECASE),
    re.compile(r"/sign-?(in|up|out)(/|$)", re.IGNORECASE),
    re.compile(r"/(account|profile|settings|preferences|dashboard)(/|$)", re.IGNORECASE),
    re.compile(r"/(cart|checkout|payment|billing|receipt)(/|$)", re.IGNORECASE),
    re.compile(r"/(auth|oauth|callback|sso|token)(/|$)", re.IGNORECASE),
    re.compile(r"/(unsubscribe|opt-?out|confirm)(/|$)", re.IGNORECASE),
    re.compile(r"/(password|reset|forgot|2fa|mfa|verify)(/|$)", re.IGNORECASE),
    re.compile(r"/\d+[x×]\d+", re.IGNORECASE),  # Image dimensions in URLs
]

# URL path patterns that indicate HIGH-VALUE content
CONTENT_PATH_PATTERNS = [
    (re.compile(r"/blog(s)?(/|$)", re.IGNORECASE), ContentType.BLOG),
    (re.compile(r"/article(s)?(/|$)", re.IGNORECASE), ContentType.ARTICLE),
    (re.compile(r"/post(s)?(/|$)", re.IGNORECASE), ContentType.BLOG),
    (re.compile(r"/doc(s|umentation)?(/|$)", re.IGNORECASE), ContentType.DOCUMENTATION),
    (re.compile(r"/guide(s)?(/|$)", re.IGNORECASE), ContentType.DOCUMENTATION),
    (re.compile(r"/tutorial(s)?(/|$)", re.IGNORECASE), ContentType.DOCUMENTATION),
    (re.compile(r"/wiki(/|$)", re.IGNORECASE), ContentType.REFERENCE),
    (re.compile(r"/reference(/|$)", re.IGNORECASE), ContentType.REFERENCE),
    (re.compile(r"/learn(ing)?(/|$)", re.IGNORECASE), ContentType.DOCUMENTATION),
    (re.compile(r"/news(/|$)", re.IGNORECASE), ContentType.NEWS),
    (re.compile(r"/paper(s)?(/|$)", re.IGNORECASE), ContentType.ARTICLE),
    (re.compile(r"/research(/|$)", re.IGNORECASE), ContentType.ARTICLE),
    (re.compile(r"/(question|answer|thread|discussion)(/|$)", re.IGNORECASE), ContentType.FORUM),
]

# Known high-quality content domains
CONTENT_DOMAINS = {
    # Technical documentation
    "docs.python.org": ContentType.DOCUMENTATION,
    "developer.mozilla.org": ContentType.DOCUMENTATION,
    "docs.microsoft.com": ContentType.DOCUMENTATION,
    "learn.microsoft.com": ContentType.DOCUMENTATION,
    "docs.aws.amazon.com": ContentType.DOCUMENTATION,
    "cloud.google.com": ContentType.DOCUMENTATION,
    "pytorch.org": ContentType.DOCUMENTATION,
    "huggingface.co": ContentType.DOCUMENTATION,
    # Knowledge bases
    "en.wikipedia.org": ContentType.REFERENCE,
    "stackoverflow.com": ContentType.FORUM,
    "stackexchange.com": ContentType.FORUM,
    "github.com": ContentType.REFERENCE,
    # News & articles
    "arxiv.org": ContentType.ARTICLE,
    "medium.com": ContentType.ARTICLE,
    "dev.to": ContentType.ARTICLE,
    "hackernews.com": ContentType.NEWS,
    "news.ycombinator.com": ContentType.NEWS,
    "techcrunch.com": ContentType.NEWS,
    "arstechnica.com": ContentType.NEWS,
    "theverge.com": ContentType.NEWS,
    "wired.com": ContentType.NEWS,
}


# =============================================================================
# Sensitive-Content Backstop (PII + auth-page text)
# =============================================================================
# Catches sensitive pages that pass every URL/DOM layer: account statements,
# order confirmations, contact directories, and login walls on unlisted
# domains. Called by the processor BEFORE anything is written to SQLite.

# Candidate card numbers: digit runs allowing space/hyphen separators. A plain
# character-class scan (no ambiguous quantifiers → no backtracking blow-up on
# digit-heavy pages); length and Luhn are checked after stripping separators.
_CARD_RE = re.compile(r"\b\d[\d -]{11,24}\d\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# Phrases that dominate auth/transactional pages but are incidental in prose.
_AUTH_PHRASES = (
    "sign in to continue", "log in to your account", "sign in to your account",
    "forgot password", "forgot your password", "remember me",
    "enter the code", "one-time password", "verification code",
    "verify your identity", "session expired", "session has expired",
    "access denied", "two-factor authentication", "create an account",
)

# Pages shorter than this are judged strictly: a single PII hit or a couple of
# auth phrases is the page's whole purpose, not an incidental mention.
_SHORT_PAGE_WORDS = 400
_AUTH_PAGE_WORDS = 150


def _luhn_valid(digits: str) -> bool:
    """Luhn checksum - separates real card numbers from arbitrary digit runs."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def detect_sensitive_content(content: str, word_count: int = 0) -> Optional[str]:
    """
    Detect PII-bearing or auth-wall page text that should never be indexed.

    Short pages are judged strictly (one hit = the page's purpose); long pages
    need repeated hits so an article *quoting* an example card still indexes.

    Args:
        content: Extracted page text.
        word_count: Word count if already known; computed from content if 0.

    Returns:
        A short reason code ("pii:card", "pii:ssn", "pii:iban",
        "pii:contact-density", "auth-text") or None if the content is fine.
    """
    if not content:
        return None
    if word_count <= 0:
        word_count = len(content.split())
    short_page = word_count < _SHORT_PAGE_WORDS

    card_hits = set()
    for match in _CARD_RE.findall(content):
        digits = re.sub(r"[ -]", "", match)
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            card_hits.add(digits)
    if card_hits and (short_page or len(card_hits) >= 3):
        return "pii:card"

    ssn_hits = set(_SSN_RE.findall(content))
    if ssn_hits and (short_page or len(ssn_hits) >= 3):
        return "pii:ssn"

    iban_hits = set(_IBAN_RE.findall(content))
    if iban_hits and (short_page or len(iban_hits) >= 3):
        return "pii:iban"

    # Contact directories: many distinct emails relative to total text.
    email_hits = set(_EMAIL_RE.findall(content))
    if len(email_hits) >= 5 and len(email_hits) / max(word_count, 1) > 0.02:
        return "pii:contact-density"

    # Login/verification walls: short pages dominated by auth phrasing.
    if word_count < _AUTH_PAGE_WORDS:
        lowered = content.lower()
        if sum(1 for p in _AUTH_PHRASES if p in lowered) >= 2:
            return "auth-text"

    return None


# =============================================================================
# Content Quality Scoring
# =============================================================================


def classify_page(
    url: str,
    title: str = "",
    content: str = "",
    word_count: int = 0,
    text_to_tag_ratio: float = 0.0,
) -> Tuple[ContentType, float]:
    """
    Classify a page and compute a quality score.

    Args:
        url: Full URL of the page.
        title: Page title.
        content: Extracted text content.
        word_count: Word count from the extension.
        text_to_tag_ratio: Text-to-HTML-tag ratio.

    Returns:
        Tuple of (content_type, quality_score).
        quality_score is 0.0-1.0 where higher = more valuable.
    """
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()

    # ------------------------------------------------------------------
    # Step 1: Hard blocks - instant rejection
    # ------------------------------------------------------------------
    if _is_blocked(domain, path, url):
        return ContentType.SKIP, 0.0

    # ------------------------------------------------------------------
    # Step 2: Domain-based classification (highest confidence)
    # ------------------------------------------------------------------
    content_type = ContentType.OTHER
    domain_score = 0.0

    for known_domain, ct in CONTENT_DOMAINS.items():
        if known_domain in domain:
            content_type = ct
            domain_score = 0.4  # Known content domain bonus
            break

    # ------------------------------------------------------------------
    # Step 3: Path-based classification
    # ------------------------------------------------------------------
    path_score = 0.0
    for pattern, ct in CONTENT_PATH_PATTERNS:
        if pattern.search(path):
            if content_type == ContentType.OTHER:
                content_type = ct
            path_score = 0.3
            break

    # Slug-like paths (e.g., /vector-databases, /my-great-article) are often
    # content. Two-or-more hyphenated words qualify - the old 3-word requirement
    # missed common 2-word slugs like /vector-databases or /mv3-extensions.
    slug_pattern = re.compile(r"/[\w]+(?:-[\w]+)+")
    if slug_pattern.search(path):
        path_score = max(path_score, 0.15)

    # Path depth bonus: deeper paths (3-6 segments) tend to be content
    path_depth = len([s for s in path.split("/") if s])
    if 2 <= path_depth <= 5:
        path_score += 0.05

    # ------------------------------------------------------------------
    # Step 4: Content quality heuristics
    # ------------------------------------------------------------------
    content_score = 0.0

    # Word count scoring
    if word_count == 0 and content:
        word_count = len(content.split())

    # Partial credit for moderate content so a genuinely informative page on an
    # unknown domain (no domain bonus) can still clear the quality threshold.
    if word_count >= 100:
        content_score += 0.10
    if word_count >= 200:
        content_score += 0.15
    if word_count >= 500:
        content_score += 0.10
    if word_count >= 1000:
        content_score += 0.05

    # Text-to-tag ratio (higher = more content, less chrome)
    if text_to_tag_ratio >= 0.3:
        content_score += 0.10
    if text_to_tag_ratio >= 0.5:
        content_score += 0.05

    # Title quality
    if title and len(title.split()) >= 3:
        content_score += 0.05

    # ------------------------------------------------------------------
    # Step 5: Combine scores
    # ------------------------------------------------------------------
    quality_score = min(1.0, domain_score + path_score + content_score)

    # If we still don't have a content_type, guess from word count
    if content_type == ContentType.OTHER and word_count >= 300:
        content_type = ContentType.ARTICLE

    logger.debug(
        "Classified %s: type=%s score=%.2f (domain=%.2f path=%.2f content=%.2f)",
        url, content_type.value, quality_score, domain_score, path_score, content_score,
    )

    return content_type, quality_score


def _is_blocked(domain: str, path: str, url: str) -> bool:
    """Check if a URL should be blocked from indexing."""
    # Check domain blocklist
    for blocked in BLOCKED_DOMAINS:
        if blocked in domain or blocked in url:
            return True

    # Check path blocklist
    for pattern in BLOCKED_PATH_PATTERNS:
        if pattern.search(path):
            return True

    # Block non-HTTP URLs
    if not url.startswith(("http://", "https://")):
        return True

    return False
