"""
RECAP v2 - Hybrid Retriever

Three-stage retrieval with Reciprocal Rank Fusion (RRF):
1. BM25 keyword search (SQLite FTS5) - exact term matches
2. Dense semantic search (LanceDB) - meaning-based similarity
3. Knowledge Graph traversal - entity-linked context

Results are fused using RRF: score = Σ 1/(k + rank)
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.storage.database import Database
from backend.storage.vector_store import VectorStore
from backend.storage.knowledge_graph import KnowledgeGraph

logger = logging.getLogger(__name__)

# RRF constant (standard value from literature)
RRF_K = 60


def _days_since(ts: str) -> float:
    """Whole/fractional days between an ISO timestamp and now (0 if unparseable)."""
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
    except Exception:
        return 0.0


class HybridRetriever:
    """
    Performs hybrid retrieval using three complementary methods
    and fuses results with Reciprocal Rank Fusion.
    """

    def __init__(
        self,
        db: Database,
        vector_store: VectorStore,
        knowledge_graph: KnowledgeGraph,
        embedding_fn,
        recency_decay: float = 1.0,
        enable_kg: bool = True,
    ):
        """
        Args:
            db: Database for BM25 search.
            vector_store: LanceDB for dense search.
            knowledge_graph: NetworkX graph for entity traversal.
            embedding_fn: Function to embed query text.
            recency_decay: Per-day multiplicative decay applied to fused scores
                (1.0 disables). Boosts recently-visited pages for time-oriented
                browsing-history queries.
            enable_kg: Master switch for the KG retrieval leg (mirrors
                Settings.enable_kg). When False, retrieval runs BM25 + dense
                only, regardless of per-request use_kg flags.
        """
        self.db = db
        self.vector_store = vector_store
        self.knowledge_graph = knowledge_graph
        self.embedding_fn = embedding_fn
        self.recency_decay = recency_decay
        self.enable_kg = enable_kg
        self._query_vec_cache: Dict[str, List[float]] = {}  # small LRU-ish cache

    def retrieve(
        self,
        query: str,
        top_k: int = 20,
        use_kg: bool = True,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Perform hybrid retrieval and return fused results.

        Args:
            query: User's search query.
            top_k: Maximum number of results to return.
            use_kg: Whether to include knowledge graph context.
            date_from: Filter to pages visited on/after this ISO timestamp.
            date_to: Filter to pages visited on/before this ISO timestamp.

        Returns:
            List of result dicts sorted by fused score, each containing:
            - chunk_id, text, score, page_url, page_title, source, timestamp
        """
        start_time = time.time()

        # The instance-level master switch (Settings.enable_kg) trumps the
        # per-request flag: a request can opt out of KG, never opt in past it.
        use_kg = use_kg and self.enable_kg

        # Fetch a WIDE candidate pool per leg so fusion has enough to work with,
        # then fuse and truncate to top_k. A small per-leg pool starves RRF.
        candidate_k = max(top_k * 5, 50)

        # -----------------------------------------------------------------
        # Stages 1-3: BM25 + Dense + KG run in parallel. Each leg returns only
        # chunk_ids (+ its own score). Text/metadata and the date filter are
        # applied once, centrally, during hydration below - so all three legs
        # are filtered consistently on pages.last_visited.
        # -----------------------------------------------------------------
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_bm25 = executor.submit(self._bm25_search, query, candidate_k)
            future_dense = executor.submit(self._dense_search, query, candidate_k)
            future_kg = executor.submit(self._kg_search, query, candidate_k) if use_kg else None

            bm25_results = future_bm25.result()
            dense_results = future_dense.result()

            kg_context = ""
            kg_results = []
            if future_kg is not None:
                kg_context, kg_results = future_kg.result()

        # -----------------------------------------------------------------
        # Reciprocal Rank Fusion (operates on chunk_ids only)
        # -----------------------------------------------------------------
        fused = self._rrf_fusion(
            bm25_results=bm25_results,
            dense_results=dense_results,
            kg_results=kg_results,
            top_k=candidate_k,
        )

        # -----------------------------------------------------------------
        # Hydrate the winners from SQLite (single source of truth) and apply the
        # date filter here, once, for every leg.
        # -----------------------------------------------------------------
        meta = self.db.get_chunks_metadata(
            [f["chunk_id"] for f in fused], date_from=date_from, date_to=date_to
        )

        items: List[Dict[str, Any]] = []
        for f in fused:
            m = meta.get(f["chunk_id"])
            if not m:
                continue  # dropped by the date filter or no longer present
            item = {
                "chunk_id": f["chunk_id"],
                "text": m["text"],
                "page_url": m["page_url"],
                "page_title": m["page_title"],
                "timestamp": m["timestamp"],
                "score": f["score"],
                "source": f["source"],
            }
            if kg_context:
                item["kg_context"] = kg_context
            items.append(item)

        # Recency: gently boost recently-visited pages. Browsing-history queries
        # are often time-oriented ("that article last week"), so a slightly older
        # exact match should not outrank what the user actually read recently.
        if self.recency_decay < 1.0:
            for it in items:
                it["score"] = it["score"] * (self.recency_decay ** _days_since(it["timestamp"]))
            items.sort(key=lambda x: -x["score"])

        results = items[:top_k]

        elapsed = time.time() - start_time
        logger.info(
            "Hybrid retrieval: %d results in %.2fs (bm25=%d, dense=%d, kg=%d)",
            len(results), elapsed,
            len(bm25_results), len(dense_results), len(kg_results),
        )

        return results

    # =========================================================================
    # Individual Search Methods
    # =========================================================================

    def _bm25_search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """BM25 keyword search using FTS5."""
        try:
            # FTS5 requires a proper query format
            # Escape special characters and create an OR query
            terms = query.strip().split()
            if not terms:
                return []

            # Build FTS5 query: each term with OR.
            # Strip embedded double quotes so they can't break FTS5 MATCH syntax.
            fts_query = " OR ".join(
                f'"{term.replace(chr(34), "")}"'
                for term in terms if len(term) > 1
            )
            if not fts_query:
                return []

            results = self.db.bm25_search(fts_query, limit=limit)
            return [
                {
                    "chunk_id": r["chunk_id"],
                    "text": r["chunk_text"],
                    "score": abs(r.get("bm25_score", 0)),  # FTS5 rank is negative
                    "page_url": r["url"],
                    "page_title": r.get("title", ""),
                    "timestamp": r.get("last_visited", ""),
                    "source": "bm25",
                }
                for r in results
            ]
        except Exception as e:
            logger.warning("BM25 search failed: %s", e)
            return []

    def _dense_search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Dense semantic search using LanceDB."""
        try:
            # Embed the query (cached - browsing-history searches repeat a lot)
            query_vector = self._query_vec_cache.get(query)
            if query_vector is None:
                emb = self.embedding_fn([query])
                query_vector = emb[0] if isinstance(emb, list) and emb else emb
                if len(self._query_vec_cache) > 256:  # simple bound
                    self._query_vec_cache.clear()
                self._query_vec_cache[query] = query_vector

            results = self.vector_store.search(
                query_vector=query_vector,
                limit=limit,
            )
            for r in results:
                r["source"] = "dense"
            return results

        except Exception as e:
            logger.warning("Dense search failed: %s", e)
            return []

    def _kg_search(
        self, query: str, limit: int = 10
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Find entities mentioned in the query and retrieve their context.

        Returns:
            Tuple of (kg_context_string, kg_augmented_results).
        """
        try:
            # Find entities mentioned in the query
            entity_names = self.knowledge_graph.find_entities_in_query(query)
            if not entity_names:
                return "", []

            # Generate context from knowledge graph
            kg_context = self.knowledge_graph.get_context_for_entities(
                entity_names[:5],  # Top 5 entity matches
                max_hops=2,
                max_per_entity=10,
            )

            # Find chunk IDs associated with these entities via the database
            kg_results = []
            for entity_name in entity_names[:3]:  # Top 3 entities
                entity = self.db.get_entity_by_name(entity_name)
                if entity:
                    # Search for chunks mentioning this entity
                    try:
                        safe_entity = entity_name.replace('"', '')
                        entity_chunks = self.db.bm25_search(
                            f'"{safe_entity}"', limit=limit
                        )
                        for r in entity_chunks:
                            kg_results.append({
                                "chunk_id": r["chunk_id"],
                                "text": r["chunk_text"],
                                "score": 0.5,  # Base score for KG matches
                                "page_url": r["url"],
                                "page_title": r.get("title", ""),
                                "timestamp": r.get("last_visited", ""),
                                "source": "knowledge_graph",
                            })
                    except Exception:
                        pass

            return kg_context, kg_results

        except Exception as e:
            logger.warning("KG search failed: %s", e)
            return "", []

    # =========================================================================
    # Reciprocal Rank Fusion
    # =========================================================================

    def _rrf_fusion(
        self,
        bm25_results: List[Dict[str, Any]],
        dense_results: List[Dict[str, Any]],
        kg_results: List[Dict[str, Any]],
        top_k: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Fuse results from multiple retrieval methods using RRF.

        RRF score = Σ (1 / (k + rank_i))
        where k is a constant (60) and rank_i is the 1-based rank
        in each result list.
        """
        # RRF operates purely on chunk_ids: accumulate weight/(k+rank) per chunk
        # and track which legs contributed. Metadata is hydrated afterwards.
        chunk_scores: Dict[str, float] = defaultdict(float)
        chunk_sources: Dict[str, set] = defaultdict(set)

        for results, weight in [
            (bm25_results, 1.0),     # BM25 weight
            (dense_results, 1.0),    # Dense weight
            (kg_results, 0.8),       # KG weight (slightly lower)
        ]:
            for rank, result in enumerate(results, start=1):
                chunk_id = result["chunk_id"]
                chunk_scores[chunk_id] += weight * (1.0 / (RRF_K + rank))
                chunk_sources[chunk_id].add(result.get("source", "unknown"))

        sorted_chunks = sorted(chunk_scores.items(), key=lambda x: -x[1])[:top_k]

        return [
            {
                "chunk_id": chunk_id,
                "score": round(fused_score, 6),
                "source": "+".join(sorted(chunk_sources[chunk_id])),
            }
            for chunk_id, fused_score in sorted_chunks
        ]
