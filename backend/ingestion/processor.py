"""
RECAP v2 - Ingestion Processor (Orchestrator)

Coordinates the full ingestion pipeline:
1. Receive page content from the Chrome extension
2. Classify content quality
3. Check for content changes (deduplication)
4. Semantically chunk the content
5. Generate embeddings and store in LanceDB
6. Extract entities and update knowledge graph
7. Index chunks in FTS5 for BM25 search
8. Update page metadata in SQLite
"""

from __future__ import annotations

import logging
import time
from typing import List

from backend.config import Settings
from backend.models import (
    ContentType,
    PageData,
    ProcessingResult,
)
from backend.ingestion.content_classifier import classify_page, detect_sensitive_content
from backend.ingestion.chunker import SemanticChunker
from backend.ingestion.entity_extractor import EntityExtractor
from backend.ingestion.semantic_gate import SemanticGate
from backend.storage.database import Database
from backend.storage.vector_store import VectorStore
from backend.storage.knowledge_graph import KnowledgeGraph

logger = logging.getLogger(__name__)


class IngestionProcessor:
    """
    Orchestrates the full page ingestion pipeline.

    Manages the lifecycle from raw page data to indexed, searchable content
    with entity-enriched knowledge graph.
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        vector_store: VectorStore,
        knowledge_graph: KnowledgeGraph,
        embedding_fn,
        nlp=None,
    ):
        """
        Args:
            settings: Application settings.
            db: SQLite database instance.
            vector_store: LanceDB vector store instance.
            knowledge_graph: Knowledge graph instance.
            embedding_fn: Function that takes List[str] and returns embeddings.
            nlp: spaCy language model for chunking and NER.
        """
        self.settings = settings
        self.db = db
        self.vector_store = vector_store
        self.knowledge_graph = knowledge_graph
        self.embedding_fn = embedding_fn

        self.chunker = SemanticChunker(
            embedding_fn=embedding_fn,
            max_chunk_tokens=settings.max_chunk_tokens,
            min_chunk_tokens=settings.min_chunk_tokens,
            nlp=nlp,
        )
        self.entity_extractor = EntityExtractor(nlp=nlp)
        self.semantic_gate = (
            SemanticGate(embedding_fn, margin=settings.semantic_gate_margin)
            if settings.semantic_gate_enabled
            else None
        )

    def process_page(self, page_data: PageData) -> ProcessingResult:
        """
        Process a single page through the full ingestion pipeline.

        Args:
            page_data: Incoming page data from the Chrome extension.

        Returns:
            ProcessingResult with status and metrics.
        """
        start_time = time.time()
        url = page_data.url

        try:
            # -----------------------------------------------------------------
            # Step 1: Classify content
            # -----------------------------------------------------------------
            content_type, quality_score = classify_page(
                url=url,
                title=page_data.title,
                content=page_data.content,
                word_count=page_data.word_count,
                text_to_tag_ratio=page_data.text_to_tag_ratio,
            )

            if content_type == ContentType.SKIP:
                logger.info("Skipped (blocked): %s", url)
                self._record_skip(page_data.domain, "url_blocked")
                return ProcessingResult(
                    url=url,
                    status="skipped",
                    content_type=content_type,
                    quality_score=quality_score,
                    message="Page type is blocked from indexing",
                )

            if quality_score < self.settings.min_content_quality:
                logger.info(
                    "Skipped (low quality %.2f < %.2f): %s",
                    quality_score, self.settings.min_content_quality, url,
                )
                self._record_skip(page_data.domain, "low_quality")
                return ProcessingResult(
                    url=url,
                    status="skipped",
                    content_type=content_type,
                    quality_score=quality_score,
                    message=f"Quality score {quality_score:.2f} below threshold",
                )

            # -----------------------------------------------------------------
            # Step 2: Check for content changes
            # -----------------------------------------------------------------
            content = page_data.content
            if not content or not content.strip():
                logger.info("Skipped (no content): %s", url)
                return ProcessingResult(
                    url=url,
                    status="skipped",
                    content_type=content_type,
                    quality_score=quality_score,
                    message="No extractable text content",
                )

            # -----------------------------------------------------------------
            # Step 2.5: Sensitive-content backstop + semantic junk gate.
            # Both run BEFORE upsert_page so nothing about a sensitive page -
            # not even its URL or title - ever lands in the source of truth.
            # -----------------------------------------------------------------
            pii_reason = detect_sensitive_content(content, page_data.word_count)
            if pii_reason:
                logger.info("Skipped (%s): %s", pii_reason, url)
                self._record_skip(page_data.domain, pii_reason)
                return ProcessingResult(
                    url=url,
                    status="skipped",
                    content_type=ContentType.SKIP,
                    quality_score=0.0,
                    message=f"Sensitive content detected ({pii_reason})",
                )

            if self.semantic_gate is not None:
                is_junk, junk_label, margin = self.semantic_gate.assess(content)
                if is_junk:
                    logger.info(
                        "Skipped (semantic:%s, margin=%.3f): %s", junk_label, margin, url
                    )
                    self._record_skip(page_data.domain, f"semantic:{junk_label}")
                    return ProcessingResult(
                        url=url,
                        status="skipped",
                        content_type=ContentType.SKIP,
                        quality_score=0.0,
                        message=f"Page resembles a {junk_label} page (margin {margin:.3f})",
                    )

            content_hash = self.db.get_content_hash(content)
            page_id, is_new_or_changed = self.db.upsert_page(
                url=url,
                title=page_data.title,
                domain=page_data.domain,
                content_type=content_type.value,
                content_hash=content_hash,
                quality_score=quality_score,
                word_count=page_data.word_count or len(content.split()),
                meta_description=page_data.meta_description,
                meta_author=page_data.meta_author,
            )

            if not is_new_or_changed:
                logger.info("Skipped (unchanged content): %s", url)
                return ProcessingResult(
                    url=url,
                    status="skipped",
                    content_type=content_type,
                    quality_score=quality_score,
                    message="Content unchanged since last index",
                )

            # -----------------------------------------------------------------
            # Step 3: Semantic chunking
            # -----------------------------------------------------------------
            chunks = self.chunker.chunk(
                text=content,
                page_url=url,
                page_title=page_data.title,
            )

            if not chunks:
                logger.warning("No chunks created for: %s", url)
                return ProcessingResult(
                    url=url,
                    status="error",
                    content_type=content_type,
                    quality_score=quality_score,
                    message="Chunking produced no results",
                )

            # -----------------------------------------------------------------
            # Step 4: Store chunks in SQLite - the SINGLE SOURCE OF TRUTH.
            # Written first so the derived indexes (FTS via triggers, vectors)
            # can always be rebuilt from it, and so a later failure never leaves
            # searchable text without a backing row.
            # -----------------------------------------------------------------
            chunk_dicts = [
                {
                    "chunk_id": c.chunk_id,
                    "text": c.text,
                    "chunk_index": c.chunk_index,
                    "token_count": c.token_count,
                }
                for c in chunks
            ]
            # Capture the page's previous chunk_ids BEFORE replacing them, so we can
            # drop vectors for any chunk that no longer exists (a shrunk page would
            # otherwise leave orphaned vectors in the ANN index).
            old_chunk_ids = {c["chunk_id"] for c in self.db.get_chunks_by_page(page_id)}

            self.db.insert_chunks(page_id, page_data.title, chunk_dicts)

            # -----------------------------------------------------------------
            # Step 5: Embed and store vectors (derived ANN index, keyed by id)
            # -----------------------------------------------------------------
            new_chunk_ids = [c.chunk_id for c in chunks]
            embedding_texts = [chunk.embedding_text for chunk in chunks]
            embeddings = self.embedding_fn(embedding_texts)
            self.vector_store.add_chunks(chunk_ids=new_chunk_ids, vectors=embeddings)

            # Remove vectors for chunks that existed before but are gone now.
            orphans = list(old_chunk_ids - set(new_chunk_ids))
            if orphans:
                self.vector_store.delete_chunks(orphans)

            # -----------------------------------------------------------------
            # Step 6: Entity extraction and knowledge graph update
            # -----------------------------------------------------------------
            entities_count = 0
            if self.settings.enable_kg:
                all_entities = []
                for chunk in chunks:
                    entities = self.entity_extractor.extract(
                        text=chunk.text,
                        source_url=url,
                        source_chunk_id=chunk.chunk_id,
                    )
                    all_entities.extend(entities)

                if all_entities:
                    entity_dicts = [
                        {"name": e.name, "entity_type": e.entity_type}
                        for e in all_entities
                    ]
                    entities_count, _ = self.knowledge_graph.add_entities_and_relations(
                        entity_dicts, source_url=url
                    )

            # -----------------------------------------------------------------
            # Step 7: Persist content_hash LAST - only now that chunks + vectors
            # succeeded is the page truly indexed. If anything above failed, the
            # hash stays empty and the page is re-indexed on the next visit.
            # -----------------------------------------------------------------
            self.db.update_content_hash(url, content_hash)

            # -----------------------------------------------------------------
            # Done
            # -----------------------------------------------------------------
            elapsed = time.time() - start_time
            status = "indexed" if is_new_or_changed else "updated"
            logger.info(
                "%s %s: %d chunks, %d entities (%.2fs)",
                status.capitalize(), url, len(chunks), entities_count, elapsed,
            )

            return ProcessingResult(
                url=url,
                status=status,
                content_type=content_type,
                quality_score=quality_score,
                chunks_created=len(chunks),
                entities_extracted=entities_count,
                message=f"Processed in {elapsed:.2f}s",
            )

        except Exception as e:
            logger.error("Failed to process %s: %s", url, e, exc_info=True)
            return ProcessingResult(
                url=url,
                status="error",
                message=str(e),
            )

    def _record_skip(self, domain: str, reason: str) -> None:
        """Record skip telemetry (feeds ignore-domain suggestions); never raises."""
        try:
            self.db.record_skip(domain, reason)
        except Exception as e:
            logger.debug("Skip telemetry failed for %s: %s", domain, e)

    def delete_page(self, url: str) -> bool:
        """
        Delete a page and all associated data.

        Args:
            url: URL to delete.

        Returns:
            True if deleted, False if not found.
        """
        # Resolve the page's chunk_ids from SQLite (source of truth) so we can
        # remove their vectors, then delete from SQLite (FTS syncs via triggers).
        page = self.db.get_page(url)
        if page:
            chunk_ids = [c["chunk_id"] for c in self.db.get_chunks_by_page(page["id"])]
            if chunk_ids:
                self.vector_store.delete_chunks(chunk_ids)
        deleted = self.db.delete_page(url)
        if deleted:
            logger.info("Deleted page and all associated data: %s", url)
        return deleted

    def clear_all(self) -> None:
        """Delete all indexed data."""
        self.vector_store.clear()
        self.db.clear_all()
        # Reload empty knowledge graph
        self.knowledge_graph.graph.clear()
        logger.info("All data cleared")

    def evict_expired(self, retention_days: int) -> dict:
        """
        Retention: delete pages not visited within `retention_days`, cascading to
        their chunks (SQLite + FTS via triggers), vectors (LanceDB), and KG
        relations. retention_days <= 0 disables retention. Returns a summary.
        """
        if not retention_days or retention_days <= 0:
            return {"pages_evicted": 0, "chunks_evicted": 0}

        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()

        removed = self.db.evict_pages_older_than(cutoff)
        chunk_ids = removed["chunk_ids"]
        urls = removed["urls"]
        if chunk_ids:
            self.vector_store.delete_chunks(chunk_ids)
        if urls:
            self.db.delete_relations_by_urls(urls)
            # Keep the in-memory KG in sync with the DB (edges/nodes we just deleted).
            self.knowledge_graph.remove_relations_by_urls(urls)
        return {"pages_evicted": len(urls), "chunks_evicted": len(chunk_ids)}

    def reindex_all(self) -> dict:
        """
        Rebuild the vector index from SQLite (the source of truth) - used when the
        embedding model changes. Text is never lost; only vectors are recomputed.
        """
        self.vector_store.clear()
        pages = self.db.get_all_pages()
        total = 0
        for p in pages:
            chunks = self.db.get_chunks_by_page(p["id"])
            if not chunks:
                continue
            title = p.get("title", "")
            # Approximate the original embedding_text (title prefix + chunk text).
            texts = [f"{title}\n\n{c['text']}" if title else c["text"] for c in chunks]
            vectors = self.embedding_fn(texts)
            self.vector_store.add_chunks(
                chunk_ids=[c["chunk_id"] for c in chunks], vectors=vectors
            )
            total += len(chunks)
        logger.info("Reindexed %d chunks across %d pages", total, len(pages))
        return {"pages": len(pages), "chunks": total}

    def rebuild_knowledge_graph(self) -> dict:
        """
        Re-run entity extraction over all stored chunk text and rebuild the
        knowledge graph. Used after enabling spaCy so pages indexed *before* the
        model was available gain entities without having to be re-browsed
        (SQLite text is the source of truth; the KG is derived and reconstructible).
        """
        if not self.entity_extractor.nlp:
            logger.warning("Cannot rebuild KG: no spaCy model loaded")
            return {"status": "skipped", "reason": "spaCy model not available",
                    "pages": 0, "entities": 0}

        # Clear the derived KG (DB tables + in-memory graph), then repopulate.
        self.db.clear_knowledge_graph()
        self.knowledge_graph.graph.clear()

        pages = self.db.get_all_pages()
        for page in pages:
            url = page.get("url", "")
            all_entities = []
            for chunk in self.db.get_chunks_by_page(page["id"]):
                all_entities.extend(
                    self.entity_extractor.extract(
                        chunk.get("text", ""),
                        source_url=url,
                        source_chunk_id=chunk.get("chunk_id", ""),
                    )
                )
            if all_entities:
                entity_dicts = [
                    {"name": e.name, "entity_type": e.entity_type} for e in all_entities
                ]
                self.knowledge_graph.add_entities_and_relations(entity_dicts, source_url=url)

        # Report the true distinct count (matches the UI stat), not per-page sums.
        entities = self.db.get_stats().get("total_entities", 0)
        logger.info("Rebuilt knowledge graph: %d entities across %d pages", entities, len(pages))
        return {"status": "ok", "pages": len(pages), "entities": entities}
