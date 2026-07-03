"""
RECAP v2 - Semantic Junk Gate

Embedding-prototype classifier that catches sensitive/transactional pages the
URL and regex layers miss - login walls, account dashboards, checkouts, error
pages on domains nobody thought to blocklist. The page's opening text is
compared against fixed prototype sentences in embedding space using the same
pluggable embedding function as indexing: no new dependencies, no network
beyond the embedder the user already configured.

The decision rule is RELATIVE - the best junk-prototype similarity must beat
the best content-prototype similarity by a margin - rather than an absolute
threshold, so it transfers across embedding models whose cosine distributions
differ.

The gate FAILS OPEN: any error means "not junk" so a broken embedder can never
silently stop indexing (the URL/DOM/PII layers still guard sensitive pages).
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Prototype sentences describing page archetypes. Junk prototypes are page
# types that should never be indexed; content prototypes anchor the other side
# of the relative margin.
JUNK_PROTOTYPES: List[Tuple[str, str]] = [
    ("login", "A login page asking for a username, email address, and password to sign in to an account."),
    ("signup", "A registration page asking the user to create an account with a password and personal details."),
    ("2fa", "A verification page asking the user to enter a one-time security code sent to their phone or email."),
    ("account", "An account settings dashboard showing profile, security, notification, and billing preferences."),
    ("checkout", "A shopping cart checkout page asking for shipping address and credit card payment details."),
    ("banking", "An online banking portal showing account balances, recent transactions, and transfer options."),
    ("error", "An error page saying access is denied, the page was not found, or the session has expired."),
    ("cookiewall", "A consent page asking the visitor to accept cookies and privacy terms before continuing."),
]

CONTENT_PROTOTYPES: List[Tuple[str, str]] = [
    ("article", "An informative article or blog post explaining a topic in depth with paragraphs of prose."),
    ("docs", "Technical documentation describing how to install, configure, and use a software library."),
    ("news", "A news report describing a recent event with quotes, background, and analysis."),
    ("forum", "A question and answer discussion thread where people explain a problem and its solutions."),
]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity; safe for unnormalized vectors and zero vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class SemanticGate:
    """
    Zero-shot junk-page detector via nearest-prototype matching.

    Prototype embeddings are computed lazily on first use (one batched call)
    so instantiating the gate never forces the embedding model to load.
    """

    def __init__(
        self,
        embedding_fn: Callable[[List[str]], list],
        margin: float = 0.02,
        head_chars: int = 1500,
    ):
        """
        Args:
            embedding_fn: Function taking List[str] and returning embeddings.
            margin: How much closer to junk than content a page must be to be
                flagged. Relative, so it is robust to the embedding model.
            head_chars: How much of the page head to embed for the decision.
        """
        self.embedding_fn = embedding_fn
        self.margin = margin
        self.head_chars = head_chars
        self._junk_vecs: Optional[list] = None
        self._content_vecs: Optional[list] = None

    def _ensure_prototypes(self) -> None:
        """Embed all prototype sentences once, in a single batch."""
        if self._junk_vecs is not None:
            return
        texts = [t for _, t in JUNK_PROTOTYPES] + [t for _, t in CONTENT_PROTOTYPES]
        vectors = self.embedding_fn(texts)
        self._junk_vecs = vectors[: len(JUNK_PROTOTYPES)]
        self._content_vecs = vectors[len(JUNK_PROTOTYPES):]

    def assess(self, text: str) -> Tuple[bool, str, float]:
        """
        Decide whether page text looks like a junk/sensitive page archetype.

        Args:
            text: Page text; only the first `head_chars` characters are used.

        Returns:
            Tuple of (is_junk, best_junk_label, margin) where margin is the
            best junk similarity minus the best content similarity. On any
            failure returns (False, "gate_error", 0.0) - fail open.
        """
        try:
            head = (text or "")[: self.head_chars].strip()
            if not head:
                return False, "empty", 0.0

            self._ensure_prototypes()
            page_vec = self.embedding_fn([head])[0]

            junk_label, junk_sim = "", -1.0
            for (label, _), vec in zip(JUNK_PROTOTYPES, self._junk_vecs):
                sim = _cosine(page_vec, vec)
                if sim > junk_sim:
                    junk_label, junk_sim = label, sim

            content_sim = max(_cosine(page_vec, vec) for vec in self._content_vecs)

            margin = junk_sim - content_sim
            return margin > self.margin, junk_label, margin
        except Exception as e:
            logger.warning("Semantic gate degraded (treating page as content): %s", e)
            return False, "gate_error", 0.0
