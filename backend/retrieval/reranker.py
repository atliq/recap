"""
RECAP v2 - Cross-Encoder Re-Ranker

Re-ranks retrieved results using a cross-encoder model for precision.
Cross-encoders jointly encode query-document pairs, producing more
accurate relevance scores than bi-encoder similarity.

Model: cross-encoder/ms-marco-MiniLM-L-6-v2 (fast, accurate)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ReRanker:
    """
    Cross-encoder re-ranker for improving retrieval precision.

    Loads the model lazily on first use to avoid startup cost
    when re-ranking is not needed.
    """

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        """
        Args:
            model_name: HuggingFace cross-encoder model ID.
        """
        self.model_name = model_name
        self._model = None

    def _ensure_model(self) -> None:
        """Lazy-load the cross-encoder model."""
        if self._model is not None:
            return

        try:
            from sentence_transformers import CrossEncoder
            logger.info("Loading cross-encoder model: %s", self.model_name)
            self._model = CrossEncoder(self.model_name)
            logger.info("Cross-encoder loaded successfully")
        except ImportError:
            logger.error(
                "sentence-transformers not installed. Install with: "
                "pip install sentence-transformers"
            )
            raise
        except Exception as e:
            logger.error("Failed to load cross-encoder: %s", e)
            raise

    def rerank(
        self,
        query: str,
        results: List[Dict[str, Any]],
        top_k: int = 5,
        score_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Re-rank results using the cross-encoder.

        Args:
            query: The user's search query.
            results: List of result dicts (should have a 'text' key).
            top_k: Number of top results to return after re-ranking.
            score_threshold: Optional minimum cross-encoder score. Left as None by
                default because ms-marco cross-encoder scores are unbounded logits
                that are often negative for weakly-but-legitimately relevant
                passages - a fixed 0.0 cutoff would silently empty the results.

        Returns:
            Re-ranked (and optionally threshold-filtered) results with scores.
        """
        if not results:
            return []

        start_time = time.time()

        try:
            self._ensure_model()
        except Exception:
            # If model loading fails, return original results unchanged
            logger.warning("Re-ranker unavailable, returning original ranking")
            return results[:top_k]

        # Cross-encoders take ~512 tokens (~1500 chars). Use .get so a result
        # missing 'text' degrades to empty instead of raising KeyError.
        pairs = [[query, (result.get("text") or "")[:1500]] for result in results]

        # Get cross-encoder scores
        try:
            scores = self._model.predict(pairs)
        except Exception as e:
            logger.error("Cross-encoder prediction failed: %s", e)
            return results[:top_k]

        # Attach scores and sort
        scored_results = []
        for result, score in zip(results, scores):
            result_copy = result.copy()
            result_copy["rerank_score"] = float(score)
            # Keep original score for reference
            result_copy["retrieval_score"] = result_copy.get("score", 0.0)
            # Use rerank score as primary score
            result_copy["score"] = float(score)
            scored_results.append(result_copy)

        # Sort by re-rank score (descending)
        scored_results.sort(key=lambda x: -x["rerank_score"])

        # Filter by threshold (only if one was explicitly provided) and limit
        if score_threshold is not None:
            scored_results = [r for r in scored_results if r["rerank_score"] >= score_threshold]
        filtered = scored_results[:top_k]

        elapsed = time.time() - start_time
        logger.debug(
            "Re-ranked %d → %d results in %.2fs",
            len(results), len(filtered), elapsed,
        )

        return filtered
