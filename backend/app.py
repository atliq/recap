"""
RECAP v2 - FastAPI Application

Clean API layer with no business logic. All work delegated
to the ingestion processor and retrieval pipeline.
Startup/shutdown lifecycle manages DB connections and model loading.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from backend import __version__
from backend.bootstrap import ensure_schema_version
from backend.config import get_settings, Settings
from backend.models import (
    AnnotationRequest,
    ChatRequest,
    ChatResult,
    DeleteDomainRequest,
    DeleteURLRequest,
    ExportData,
    HealthResponse,
    KGToggleRequest,
    PageData,
    ProcessingResult,
    QueryRequest,
    QueryResult,
    ReferencesResponse,
    ReferenceItem,
    RelatedRequest,
    SaveHighlightRequest,
    SearchResult,
    StatsResponse,
    TestLLMRequest,
    UpdateAPIKeysRequest,
)
from backend.storage.database import Database
from backend.storage.vector_store import VectorStore
from backend.storage.knowledge_graph import KnowledgeGraph
from backend.ingestion.processor import IngestionProcessor
from backend.retrieval.hybrid_retriever import HybridRetriever
from backend.retrieval.reranker import ReRanker
from backend.retrieval.answer_generator import AnswerGenerator
from backend.prompts import (
    DIGEST_SYSTEM_PROMPT,
    DIGEST_USER_TEMPLATE,
    FLASHCARD_SYSTEM_PROMPT,
    FLASHCARD_USER_TEMPLATE,
    sanitize_untrusted,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Request Models (must be module-level for FastAPI body parsing)
# =============================================================================


# =============================================================================
# Application State (initialized at startup)
# =============================================================================


class AppState:
    """Holds all initialized components. Set during app lifespan."""
    settings: Settings
    db: Database
    vector_store: VectorStore
    knowledge_graph: KnowledgeGraph
    ingestion_processor: IngestionProcessor
    hybrid_retriever: HybridRetriever
    reranker: ReRanker
    answer_generator: AnswerGenerator
    embedding_fn = None
    nlp = None
    start_time: float = 0.0


state = AppState()


# =============================================================================
# Embedding Function
# =============================================================================


# Embedding function construction lives in backend/embeddings.py
# (pluggable local sentence-transformers or OpenAI-compatible /v1/embeddings).


def _load_spacy(model_name: str):
    """Load spaCy language model for NLP tasks."""
    try:
        import spacy
        logger.info("Loading spaCy model: %s", model_name)
        nlp = spacy.load(model_name)
        logger.info("spaCy model loaded successfully")
        return nlp
    except OSError:
        logger.warning(
            "spaCy model '%s' not found. Install with: python -m spacy download %s",
            model_name, model_name,
        )
        return None
    except ImportError:
        logger.warning("spaCy not installed. Entity extraction disabled.")
        return None


# =============================================================================
# Application Lifespan
# =============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize all components on startup, cleanup on shutdown."""
    state.start_time = time.time()

    # Load settings
    settings = get_settings()
    state.settings = settings
    settings.ensure_directories()

    # Clean-rebuild local stores if the storage schema version changed.
    ensure_schema_version(settings)

    logger.info("Initializing RECAP v2...")

    # Build the embedding function (local or OpenAI-compatible). The true vector
    # dimension is derived from the model, not the config int.
    from backend.embeddings import build_embedding_fn
    state.embedding_fn, embed_dim, embed_fingerprint = build_embedding_fn(settings)

    # Load spaCy (optional)
    state.nlp = _load_spacy(settings.spacy_model)

    # Initialize storage
    state.db = Database(settings.db_path)
    state.vector_store = VectorStore(
        settings.vector_store_path,
        embedding_dim=embed_dim,
    )
    state.knowledge_graph = KnowledgeGraph(state.db)

    # The KG toggle set from the extension UI (POST /settings/kg) is persisted
    # in DB meta; once set, it overrides the .env default across restarts.
    stored_kg = state.db.get_meta("enable_kg")
    if stored_kg is not None:
        settings.enable_kg = stored_kg == "1"

    # Initialize ingestion processor
    state.ingestion_processor = IngestionProcessor(
        settings=settings,
        db=state.db,
        vector_store=state.vector_store,
        knowledge_graph=state.knowledge_graph,
        embedding_fn=state.embedding_fn,
        nlp=state.nlp,
    )

    # Initialize retrieval pipeline
    state.hybrid_retriever = HybridRetriever(
        db=state.db,
        vector_store=state.vector_store,
        knowledge_graph=state.knowledge_graph,
        embedding_fn=state.embedding_fn,
        recency_decay=settings.recency_decay,
        enable_kg=settings.enable_kg,
    )
    state.reranker = ReRanker(settings.rerank_model)
    state.answer_generator = AnswerGenerator(settings)

    # Embedding fingerprint: if the model changed, the vector index is in a
    # different space and must be rebuilt from SQLite (the source of truth).
    stored_fp = state.db.get_meta("embedding_fingerprint")
    if stored_fp is None:
        state.db.set_meta("embedding_fingerprint", embed_fingerprint)
    elif stored_fp != embed_fingerprint:
        if state.db.get_stats()["total_chunks"] > 0:
            logger.warning(
                "Embedding model changed (%s -> %s): rebuilding vector index "
                "from stored text (no pages lost)...", stored_fp, embed_fingerprint,
            )
            await asyncio.to_thread(state.ingestion_processor.reindex_all)
        state.db.set_meta("embedding_fingerprint", embed_fingerprint)

    startup_time = time.time() - state.start_time
    logger.info("RECAP v2 initialized in %.2fs", startup_time)

    # Retention: sweep once on startup (off the event loop), then daily.
    def _run_retention() -> None:
        try:
            summary = state.ingestion_processor.evict_expired(settings.retention_days)
            if summary.get("pages_evicted"):
                logger.info("Retention: %s", summary)
        except Exception as e:  # pragma: no cover - best effort
            logger.warning("Retention sweep failed: %s", e)

    await asyncio.to_thread(_run_retention)

    async def _retention_loop() -> None:
        while True:
            await asyncio.sleep(24 * 60 * 60)  # daily
            await asyncio.to_thread(_run_retention)

    retention_task = asyncio.create_task(_retention_loop())

    yield  # App is running

    # Shutdown
    logger.info("RECAP v2 shutting down...")
    retention_task.cancel()
    try:
        await retention_task
    except (asyncio.CancelledError, Exception):
        pass


# =============================================================================
# Create FastAPI App
# =============================================================================


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="RECAP v2",
        description="Browser History RAG System",
        version=__version__,
        lifespan=lifespan,
    )

    # CORS: only the Chrome extension origin may call this local API (blocks ordinary websites from reaching localhost:8000)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"chrome-extension://.*",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routes
    _register_routes(app)

    return app


def _dedup_results(results: list) -> list:
    """Keep only the top-scored chunk per unique page URL.

    Retrieval returns multiple chunks from the same page.
    We want one source card per page, not one per chunk.
    Results must be in descending score order (reranker guarantees this).
    """
    from urllib.parse import urlparse, urlunparse

    seen: dict = {}
    for r in results:
        url = r.get("page_url", "")
        if not url:
            continue
        # Normalize: strip query params, fragments, and trailing slash
        parsed = urlparse(url)
        base = urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))
        if base not in seen:
            seen[base] = r
    return list(seen.values())



def _register_routes(app: FastAPI) -> None:
    """Register all API endpoints."""

    # -----------------------------------------------------------------
    # Health Check
    # -----------------------------------------------------------------
    @app.get("/health", response_model=HealthResponse)
    async def health_check():
        """Health check endpoint."""
        uptime = time.time() - state.start_time if state.start_time else 0
        stats = state.db.get_stats()
        return HealthResponse(
            status="ok",
            version=__version__,
            uptime_seconds=round(uptime, 1),
            pages_indexed=stats["total_pages"],
            vector_store_ready=state.vector_store is not None,
            database_ready=state.db is not None,
            default_provider=state.settings.get_default_provider(),
        )

    # -----------------------------------------------------------------
    # Page Processing
    # -----------------------------------------------------------------
    @app.post("/process_page", response_model=ProcessingResult)
    async def process_page(page_data: PageData):
        """Process and index a page from the Chrome extension."""
        # Blocking pipeline (SQLite, embedding - incl. the semantic gate's
        # per-page embed - LanceDB) must not stall the event loop.
        result = await asyncio.to_thread(
            state.ingestion_processor.process_page, page_data
        )
        return result

    # -----------------------------------------------------------------
    # Delete API
    # -----------------------------------------------------------------

    @app.post("/delete_domain")
    def delete_domain(req: DeleteDomainRequest):
        from urllib.parse import urlparse
        # Normalize "www.example.com" -> "example.com" so the apex form matches
        # the whole site; the suffix check below then covers every subdomain.
        domain = req.domain.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        pages = state.db.get_all_pages()
        deleted = 0
        for p in pages:
            try:
                hostname = urlparse(p["url"]).hostname
            except Exception:
                continue
            if hostname and (hostname == domain or hostname.endswith('.' + domain)):
                # Delegate to the processor so vectors (LanceDB) and KG relations
                # are removed too - a bare db.delete_page would orphan them.
                if state.ingestion_processor.delete_page(p["url"]):
                    deleted += 1
        logger.info("Deleted %d pages for domain: %s", deleted, req.domain)
        return {"status": "success", "deleted": deleted}

    # -----------------------------------------------------------------
    # Query / RAG
    # -----------------------------------------------------------------
    @app.post("/query", response_model=QueryResult)
    def query(request: QueryRequest):
        """Search browsing history and generate an answer."""
        # Auto-detect provider if the requested one has no key
        provider = request.llm
        if not state.settings.get_api_key(provider):
            provider = state.settings.get_default_provider()
            logger.info("No key for '%s', using '%s' instead", request.llm, provider)
        model_id = state.answer_generator.resolve_model(provider, request.model)

        # Step 1: Hybrid retrieval
        retrieval_start = time.time()
        raw_results = state.hybrid_retriever.retrieve(
            query=request.query,
            top_k=state.settings.retrieval_top_k,
            use_kg=request.use_kg,
            date_from=request.date_from,
            date_to=request.date_to,
        )
        retrieval_time = (time.time() - retrieval_start) * 1000

        if not raw_results:
            return QueryResult(
                query=request.query,
                answer="No pages have been indexed yet. Browse some websites first!",
                retrieval_time_ms=retrieval_time,
                provider=provider,
                model=model_id,
            )

        # Extract KG context if available
        kg_context = raw_results[0].get("kg_context", "") if raw_results else ""

        # Step 2: Re-rank then deduplicate (one source card per page, not per chunk)
        reranked = _dedup_results(state.reranker.rerank(
            query=request.query,
            results=raw_results,
            top_k=request.top_k * 3,  # over-fetch before dedup so we have enough unique pages
        ))[:request.top_k]

        # Step 3: Generate answer
        gen_start = time.time()
        answer = state.answer_generator.generate(
            query=request.query,
            context_results=reranked,
            provider=provider,
            model=model_id,
            kg_context=kg_context,
        )
        generation_time = (time.time() - gen_start) * 1000

        # Format response
        search_results = [
            SearchResult(
                url=r.get("page_url", ""),
                title=r.get("page_title", ""),
                snippet=r.get("text", "")[:300],
                score=round(r.get("score", 0.0), 4),
                source=r.get("source", "hybrid"),
                timestamp=r.get("timestamp", ""),
            )
            for r in reranked
        ]

        return QueryResult(
            query=request.query,
            answer=answer,
            results=search_results,
            sources_used=len(reranked),
            retrieval_time_ms=round(retrieval_time, 1),
            generation_time_ms=round(generation_time, 1),
            provider=provider,
            model=model_id,
        )

    # -----------------------------------------------------------------
    # Statistics
    # -----------------------------------------------------------------
    @app.get("/stats", response_model=StatsResponse)
    def get_stats():
        """Get system statistics."""
        db_stats = state.db.get_stats()
        kg_stats = state.knowledge_graph.get_stats()

        return StatsResponse(
            total_pages=db_stats["total_pages"],
            total_chunks=db_stats["total_chunks"],
            total_entities=kg_stats["nodes"],
            total_relations=kg_stats["edges"],
            index_size_mb=db_stats["index_size_mb"],
            last_indexed=db_stats["last_indexed"],
            content_type_distribution=db_stats["content_type_distribution"],
            top_domains=db_stats["top_domains"],
            top_skipped_domains=db_stats["top_skipped_domains"],
        )

    # -----------------------------------------------------------------
    # References
    # -----------------------------------------------------------------
    @app.get("/references", response_model=ReferencesResponse)
    def get_references():
        """Get all indexed page references with auto-summaries."""
        from urllib.parse import urlparse, urlunparse
        
        pages = state.db.get_all_pages()
        
        # Deduplicate by base URL (ignore query params and fragments)
        unique_pages = {}
        for p in pages:
            # Parse URL and strip query/fragment
            parsed = urlparse(p["url"])
            base_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
            
            # Keep the most recently visited variant
            if base_url not in unique_pages or p.get("last_visited", "") > unique_pages[base_url].get("last_visited", ""):
                # We intentionally don't rewrite p["url"] so the delete action still has the exact DB key,
                # but we could also group chunks. For UI purposes, keeping the most recent exact URL is fine.
                unique_pages[base_url] = p
                
        references = [
            ReferenceItem(
                url=p["url"],
                title=p.get("title", ""),
                content_type=p.get("content_type", ""),
                indexed_at=p.get("last_indexed", ""),
                visit_count=p.get("visit_count", 1),
                chunk_count=len(state.db.get_chunks_by_page(p["id"])),
                summary=p.get("meta_description", "") or "",
            )
            for p in unique_pages.values()
        ]
        return ReferencesResponse(
            references=references,
            total=len(references),
        )

    # -----------------------------------------------------------------
    # Delete URL
    # -----------------------------------------------------------------
    @app.post("/delete_url")
    def delete_url(request: DeleteURLRequest):
        """Delete a URL and all its indexed data."""
        deleted = state.ingestion_processor.delete_page(request.url)
        if not deleted:
            # Try with/without trailing slash
            alt_url = request.url[:-1] if request.url.endswith('/') else request.url + '/'
            deleted = state.ingestion_processor.delete_page(alt_url)
            
        if not deleted:
            raise HTTPException(status_code=404, detail="URL not found")
        return {"status": "deleted", "url": request.url}

    # -----------------------------------------------------------------
    # Update API Keys
    # -----------------------------------------------------------------
    @app.post("/update_api_keys")
    async def update_api_keys(request: UpdateAPIKeysRequest):
        """Update LLM provider API keys at runtime."""
        settings = state.settings
        if request.groq_api_key:
            settings.groq_api_key = request.groq_api_key
        if request.openai_api_key:
            settings.openai_api_key = request.openai_api_key
        if request.anthropic_api_key:
            settings.anthropic_api_key = request.anthropic_api_key
        if request.google_api_key:
            settings.google_api_key = request.google_api_key
        if request.openrouter_api_key:
            settings.openrouter_api_key = request.openrouter_api_key
        # Custom / self-hosted OpenAI-compatible endpoint
        if request.llm_base_url is not None:
            settings.llm_base_url = request.llm_base_url or None
        if request.llm_api_key is not None:
            settings.llm_api_key = request.llm_api_key or None
        if request.llm_model is not None:
            settings.llm_model = request.llm_model or None
        if request.default_provider:
            settings.default_provider = request.default_provider
        return {"status": "updated"}

    # -----------------------------------------------------------------
    # Knowledge graph toggle
    # -----------------------------------------------------------------
    @app.post("/settings/kg")
    async def update_kg_setting(request: KGToggleRequest):
        """Flip the knowledge-graph master switch at runtime (extension Options).

        Takes effect immediately for both ingestion NER and the KG retrieval
        leg, and is persisted in DB meta so it survives backend restarts
        (overriding the .env default). After enabling, already-indexed pages
        can be backfilled via POST /maintenance/rebuild_kg.
        """
        state.settings.enable_kg = request.enabled
        state.hybrid_retriever.enable_kg = request.enabled
        await asyncio.to_thread(
            state.db.set_meta, "enable_kg", "1" if request.enabled else "0"
        )
        return {"status": "updated", "enable_kg": request.enabled}

    # -----------------------------------------------------------------
    # Test LLM connection
    # -----------------------------------------------------------------
    @app.post("/test_llm")
    async def test_llm(request: TestLLMRequest):
        """Ping the configured LLM with a trivial prompt so the user can confirm it works.

        Returns 200 with {ok: false, error} on an LLM-side failure (bad key, unknown
        model, unreachable endpoint) so the extension can show the provider's own
        message. Credentials must already be set via /update_api_keys or .env.
        """
        provider = request.provider or state.settings.get_default_provider()
        model = request.model or state.settings.llm_model
        try:
            result = await asyncio.to_thread(state.answer_generator.ping, provider, model)
            return {"ok": True, "provider": provider, **result}
        except Exception as e:  # surface any provider error verbatim to the user
            logger.info("LLM test failed (%s): %s", provider, e)
            return {"ok": False, "provider": provider, "error": str(e)}

    # -----------------------------------------------------------------
    # Save Highlight
    # -----------------------------------------------------------------
    @app.post("/save_highlight")
    def save_highlight(request: SaveHighlightRequest):
        """Save a user-highlighted text passage for later search."""
        highlight_id = state.db.save_highlight(
            text=request.text,
            url=request.url,
            title=request.title,
        )
        return {"status": "saved", "id": highlight_id}

    # -----------------------------------------------------------------
    # Get Highlights
    # -----------------------------------------------------------------
    @app.get("/highlights")
    def get_highlights():
        """Return all saved highlights."""
        highlights = state.db.get_highlights()
        return {"highlights": highlights, "total": len(highlights)}

    # -----------------------------------------------------------------
    # Export All Data
    # -----------------------------------------------------------------
    @app.get("/export", response_model=ExportData)
    def export_data():
        """Export all indexed data as JSON."""
        from datetime import datetime, timezone
        raw = state.db.export_all()
        return ExportData(
            pages=raw["pages"],
            highlights=raw["highlights"],
            stats=raw["stats"],
            exported_at=datetime.now(timezone.utc).isoformat(),
        )

    # -----------------------------------------------------------------
    # Proactive Resurfacing
    # -----------------------------------------------------------------

    @app.post("/related")
    def get_related(request: RelatedRequest):
        """Find related pages to a currently viewed page for Resurface."""
        query_text = request.content[:800] if request.content else request.url
        results = state.hybrid_retriever.retrieve(
            query=query_text,
            top_k=8
        )
        
        # Filter out same URL and domain for a serendipitous discovery
        from urllib.parse import urlparse
        req_domain = urlparse(request.url).netloc
        
        filtered = []
        for r in results:
            r_url = r.get("page_url", "")
            if r_url == request.url:
                continue
            res_domain = urlparse(r_url).netloc
            if res_domain == req_domain:
                continue
            # Must be reasonably relevant (RRF scores are small, ~0.005-0.03)
            if r.get("score", 0) > 0.005:
                filtered.append(r)
                
        if not filtered:
            return {"related": []}
            
        # Deduplicate by URL
        seen_urls = set()
        unique_filtered = []
        for f in filtered:
            f_url = f.get("page_url", "")
            if f_url not in seen_urls:
                seen_urls.add(f_url)
                unique_filtered.append(f)
        
        # Return top 1 related page
        best_match = unique_filtered[0] if unique_filtered else None
        if best_match:
            return {"related": [{
                "url": best_match.get("page_url", ""),
                "title": best_match.get("page_title", ""),
                "snippet": (best_match.get("text", "") or "")[:300],
                "score": best_match.get("score", 0)
            }]}
        return {"related": []}

    # -----------------------------------------------------------------
    # Knowledge Graph
    # -----------------------------------------------------------------
    @app.get("/graph")
    def get_graph():
        """Get the full knowledge graph for visualization.

        Always includes pages and domain clusters as nodes so the graph is
        populated even when spaCy entity extraction is not available.
        Graph groups:
          1 = person / org entities
          2 = other entities
          3 = domain cluster hub
          4 = individual page
        """
        with state.db._get_connection() as conn:
            nodes = []
            links = []

            # ── Pages & domain clusters ─────────────────────────────────────
            pages = conn.execute(
                "SELECT id, url, title, domain, word_count, visit_count "
                "FROM pages ORDER BY last_visited DESC LIMIT 200"
            ).fetchall()

            domain_seen: set = set()
            for p in pages:
                domain = p["domain"] or "unknown"
                # Domain hub node (one per domain)
                if domain not in domain_seen:
                    domain_seen.add(domain)
                    nodes.append({
                        "id": f"d_{domain}",
                        "name": domain,
                        "group": 3,
                        "val": 6,
                    })

                # Page node
                page_label = (p["title"] or p["url"])[:60]
                nodes.append({
                    "id": f"p_{p['id']}",
                    "name": page_label,
                    "group": 4,
                    "val": max(1, min(5, (p["visit_count"] or 1))),
                    "url": p["url"],
                })
                # Page → domain edge
                links.append({
                    "source": f"p_{p['id']}",
                    "target": f"d_{domain}",
                    "value": 1,
                })

            # ── Entity nodes (when spaCy is available) ──────────────────────
            entities = conn.execute(
                "SELECT id, name, entity_type, frequency FROM entities "
                "ORDER BY frequency DESC LIMIT 300"
            ).fetchall()
            valid_ids = {e["id"] for e in entities}

            for e in entities:
                nodes.append({
                    "id": f"e_{e['id']}",
                    "name": e["name"],
                    "group": 1 if e["entity_type"] in ["person", "organization"] else 2,
                    "val": max(1, e["frequency"]),
                })

            # ── Entity-entity relations ─────────────────────────────────────
            relations = conn.execute(
                "SELECT source_entity_id, target_entity_id, weight "
                "FROM entity_relations ORDER BY weight DESC LIMIT 2000"
            ).fetchall()
            for r in relations:
                if r["source_entity_id"] in valid_ids and r["target_entity_id"] in valid_ids:
                    links.append({
                        "source": f"e_{r['source_entity_id']}",
                        "target": f"e_{r['target_entity_id']}",
                        "value": r["weight"],
                    })

            return {"nodes": nodes, "links": links}

    # -----------------------------------------------------------------
    # Flashcards
    # -----------------------------------------------------------------
    @app.get("/flashcards")
    async def get_flashcards(url: Optional[str] = None):
        """Generate Q&A flashcard pairs from an indexed page (or a random recent page)."""
        pages = state.db.get_all_pages()
        if not pages:
            return {"flashcards": [], "source_url": None, "source_title": None}

        # Pick target page
        target_page = None
        if url:
            for p in pages:
                if p["url"] == url:
                    target_page = p
                    break
        if not target_page:
            import random
            target_page = random.choice(pages)

        # Get chunks for this page
        chunks = state.db.get_chunks_by_page(target_page["id"])
        if not chunks:
            return {"flashcards": [], "source_url": target_page["url"], "source_title": target_page.get("title", "")}

        # Build content from top chunks
        content_parts = [c["text"] for c in chunks[:6] if c.get("text")]
        content = "\n\n".join(content_parts)[:3000]

        # Use LLM to generate flashcards
        provider = state.settings.get_default_provider()
        # Resolve the model from the provider preset (covers openrouter/ollama/
        # custom, which a hardcoded map here would silently break with a Groq id).
        model_id = None

        # Templates live in answer_generator.py; title/content are page-derived
        # (untrusted) so they are sanitized and fenced in <page_content>.
        system = FLASHCARD_SYSTEM_PROMPT
        user = FLASHCARD_USER_TEMPLATE.format(
            title=sanitize_untrusted(target_page.get("title", "Unknown")),
            content=sanitize_untrusted(content),
        )

        import json as _json
        try:
            import asyncio
            raw = await asyncio.wait_for(
                asyncio.to_thread(
                    state.answer_generator._call_llm, provider, model_id, system, user
                ),
                timeout=30,
            )
            # Extract JSON even if LLM wraps it in markdown
            start = raw.find('{')
            end = raw.rfind('}') + 1
            parsed = _json.loads(raw[start:end])
            cards = parsed.get("flashcards", [])[:5]
        except asyncio.TimeoutError:
            logger.warning("Flashcard generation timed out after 30s")
            cards = []
        except Exception as e:
            logger.warning("Flashcard generation failed: %s", e)
            cards = []

        return {
            "flashcards": cards,
            "source_url": target_page["url"],
            "source_title": target_page.get("title", ""),
        }

    # -----------------------------------------------------------------
    # Weekly Digest
    # -----------------------------------------------------------------
    @app.get("/digest")
    async def get_digest():
        """Generate a weekly digest summarising the past 7 days of browsing."""
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        pages = state.db.get_all_pages()
        recent = [p for p in pages if (p.get("last_indexed") or "") >= cutoff]

        if not recent:
            recent = pages[:20]  # fallback: last 20 pages

        if not recent:
            return {"digest": "No browsing history found yet. Browse some websites first!", "page_count": 0, "generated_at": datetime.now(timezone.utc).isoformat()}

        # Build summary list for LLM
        summaries = []
        for p in recent[:25]:
            title = p.get("title", "Unknown")
            url = p.get("url", "")
            desc = p.get("meta_description", "") or ""
            summaries.append(f"- {title} ({url})\n  {desc[:120]}" if desc else f"- {title} ({url})")

        content = "\n".join(summaries)
        provider = state.settings.get_default_provider()
        # Resolve the model from the provider preset (covers openrouter/ollama/
        # custom, which a hardcoded map here would silently break with a Groq id).
        model_id = None

        # Templates live in answer_generator.py; titles/URLs/descriptions are
        # page-derived (untrusted) so they are sanitized and fenced.
        system = DIGEST_SYSTEM_PROMPT
        user = DIGEST_USER_TEMPLATE.format(
            content=sanitize_untrusted(content), page_count=len(recent)
        )

        try:
            import asyncio
            digest_text = await asyncio.wait_for(
                asyncio.to_thread(
                    state.answer_generator._call_llm, provider, model_id, system, user
                ),
                timeout=30,
            )
        except asyncio.TimeoutError:
            logger.warning("Digest generation timed out after 30s")
            digest_text = f"You visited {len(recent)} pages this week. Keep exploring!"
        except Exception as e:
            logger.warning("Digest generation failed: %s", e)
            digest_text = f"You visited {len(recent)} pages this week. Keep exploring!"

        return {
            "digest": digest_text,
            "page_count": len(recent),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    # -----------------------------------------------------------------
    # Mobile PWA
    # -----------------------------------------------------------------
    @app.get("/mobile")
    def mobile_app():
        """Serve the mobile companion web app."""
        from fastapi.responses import HTMLResponse
        import html as _html
        pages = state.db.get_all_pages()
        stats = state.db.get_stats()
        total_pages = stats.get("total_pages", 0)

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
  <meta name="theme-color" content="#0a0f1a">
  <title>RECAP Mobile</title>
  <link rel="manifest" href="/mobile/manifest.json">
  <style>
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#0a0f1a;color:#e8e8ed;font-family:'Inter',system-ui,sans-serif;min-height:100vh;padding:0 0 80px}}
    header{{background:rgba(14,21,32,0.95);border-bottom:1px solid rgba(255,255,255,0.07);padding:16px 20px;position:sticky;top:0;z-index:10;backdrop-filter:blur(10px);display:flex;align-items:center;justify-content:space-between}}
    .brand{{font-size:18px;font-weight:800;letter-spacing:1px}}.brand .re{{color:#3ecfbf}}.brand .cap{{color:#fff}}
    .stats-pill{{font-size:11px;background:rgba(62,207,191,0.12);border:1px solid rgba(62,207,191,0.2);color:#3ecfbf;padding:4px 10px;border-radius:20px}}
    .search-section{{padding:20px 20px 8px}}
    .search-box{{display:flex;gap:8px}}
    .search-box input{{flex:1;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:10px;padding:12px 16px;color:#e8e8ed;font-size:15px;outline:none}}
    .search-box input::placeholder{{color:#5a5a6e}}
    .search-box input:focus{{border-color:rgba(62,207,191,0.4);background:rgba(62,207,191,0.04)}}
    .search-box button{{background:#3ecfbf;color:#0a0f1a;border:none;border-radius:10px;padding:12px 18px;font-weight:700;font-size:14px;cursor:pointer}}
    #results{{padding:8px 20px 0}}
    .result-card{{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);border-radius:10px;padding:14px;margin-bottom:10px;cursor:pointer;transition:border-color 0.15s,background 0.15s}}
    .result-card:active{{background:rgba(62,207,191,0.06);border-color:rgba(62,207,191,0.3)}}
    .result-title{{font-weight:600;font-size:14px;color:#e8e8ed;margin-bottom:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
    .result-url{{font-size:11px;color:#3ecfbf;opacity:0.7;margin-bottom:6px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}}
    .result-snippet{{font-size:12px;color:#8b8b9e;line-height:1.5;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}}
    .answer-box{{background:rgba(62,207,191,0.06);border:1px solid rgba(62,207,191,0.18);border-radius:10px;padding:14px;margin-bottom:12px}}
    .answer-label{{font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#3ecfbf;margin-bottom:8px}}
    .answer-text{{font-size:14px;line-height:1.6;color:#e8e8ed}}
    .section-title{{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.8px;color:#5a5a6e;padding:16px 20px 8px}}
    .page-card{{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);border-radius:10px;margin:0 20px 8px;padding:12px;cursor:pointer;transition:border-color 0.15s}}
    .page-card:active{{border-color:rgba(62,207,191,0.35)}}
    .page-title{{font-weight:600;font-size:13px;color:#e8e8ed;margin-bottom:4px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}}
    .page-url{{font-size:11px;color:#5a5a6e;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}}
    .empty{{text-align:center;color:#5a5a6e;padding:40px 20px;font-size:14px}}
    .spinner{{text-align:center;padding:24px;color:#3ecfbf;font-size:13px}}
  </style>
</head>
<body>
  <header>
    <div class="brand"><span class="re">RE</span><span class="cap">CAP</span></div>
    <div class="stats-pill">{total_pages} pages</div>
  </header>
  <div class="search-section">
    <div class="search-box">
      <input type="search" id="q" placeholder="Search your memory..." autocomplete="off">
      <button id="go-btn">Go</button>
    </div>
  </div>
  <div id="results"></div>
  <div class="section-title">Recently Read</div>
  <div id="recent">
    {''.join(f'<div class="page-card" data-url="{_html.escape(p["url"], quote=True)}"><div class="page-title">{_html.escape(p.get("title","Untitled"))}</div><div class="page-url">{_html.escape(p["url"][:60])}...</div></div>' for p in pages[:15]) or '<div class="empty">No pages indexed yet.</div>'}
  </div>
  <script>
    function esc(s){{ return String(s==null?'':s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])); }}
    function safeUrl(u){{ return new RegExp('^https?://','i').test(String(u)) ? String(u) : '#'; }}
    async function doSearch() {{
      const q = document.getElementById('q').value.trim();
      if (!q) return;
      const out = document.getElementById('results');
      out.innerHTML = '<div class="spinner">Searching...</div>';
      try {{
        const r = await fetch('/query', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{query:q,top_k:5,llm:'groq'}})}});
        const d = await r.json();
        let html = '';
        if (d.answer) html += `<div class="answer-box"><div class="answer-label">AI Answer</div><div class="answer-text">${{esc(d.answer)}}</div></div>`;
        (d.results||[]).forEach(r => {{
          html += `<div class="result-card" data-url="${{esc(safeUrl(r.url))}}">
            <div class="result-title">${{esc(r.title||'Untitled')}}</div>
            <div class="result-url">${{esc(r.url)}}</div>
            ${{r.snippet?`<div class="result-snippet">${{esc(r.snippet)}}</div>`:''}}
          </div>`;
        }});
        out.innerHTML = html || '<div class="empty">No results found.</div>';
      }} catch(e) {{
        out.innerHTML = '<div class="empty">Search failed. Is the backend running?</div>';
      }}
    }}
    document.getElementById('go-btn').addEventListener('click', doSearch);
    document.getElementById('q').addEventListener('keydown', e => {{ if (e.key==='Enter') doSearch(); }});
    document.body.addEventListener('click', e => {{
      const card = e.target.closest('[data-url]');
      if (card && card.dataset.url && card.dataset.url !== '#') window.open(card.dataset.url, '_blank');
    }});
  </script>
</body>
</html>"""
        return HTMLResponse(content=html)

    @app.get("/mobile/manifest.json")
    async def mobile_manifest():
        """PWA manifest for the mobile companion app."""
        from fastapi.responses import JSONResponse
        return JSONResponse({
            "name": "RECAP",
            "short_name": "RECAP",
            "description": "Your AI-powered browsing memory",
            "start_url": "/mobile",
            "display": "standalone",
            "background_color": "#0a0f1a",
            "theme_color": "#3ecfbf",
            "icons": [
                {"src": "/mobile/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/mobile/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ]
        })

    # -----------------------------------------------------------------
    # Conversational Chat
    # -----------------------------------------------------------------
    @app.post("/chat", response_model=ChatResult)
    def chat(request: ChatRequest):
        """Multi-turn conversational interface over browsing history."""
        provider = request.llm
        if not state.settings.get_api_key(provider):
            provider = state.settings.get_default_provider()
        model_id = state.answer_generator.resolve_model(provider, request.model)

        # Build retrieval query: combine current message with last user turn for context
        retrieval_query = request.message
        prior_user_turns = [t for t in request.history if t.role == "user"]
        if prior_user_turns:
            last_user = prior_user_turns[-1].content
            # Prepend last user context so retrieval stays on topic
            retrieval_query = f"{last_user} {request.message}"[:400]

        # Retrieve
        retrieval_start = time.time()
        raw_results = state.hybrid_retriever.retrieve(
            query=retrieval_query,
            top_k=state.settings.retrieval_top_k,
            date_from=request.date_from,
            date_to=request.date_to,
        )
        retrieval_time = (time.time() - retrieval_start) * 1000

        reranked = _dedup_results(state.reranker.rerank(
            query=request.message,
            results=raw_results,
            top_k=request.top_k * 3,
        ))[:request.top_k] if raw_results else []

        kg_context = raw_results[0].get("kg_context", "") if raw_results else ""

        # Convert history to plain dicts for generate_chat
        history_dicts = [{"role": t.role, "content": t.content} for t in request.history]

        gen_start = time.time()
        answer = state.answer_generator.generate_chat(
            message=request.message,
            history=history_dicts,
            context_results=reranked,
            provider=provider,
            model=model_id,
            kg_context=kg_context,
        )
        generation_time = (time.time() - gen_start) * 1000

        sources = [
            SearchResult(
                url=r.get("page_url", ""),
                title=r.get("page_title", ""),
                snippet=r.get("text", "")[:300],
                score=round(r.get("score", 0.0), 4),
                source=r.get("source", "hybrid"),
                timestamp=r.get("timestamp", ""),
            )
            for r in reranked
        ]

        return ChatResult(
            message=answer,
            sources=sources,
            retrieval_time_ms=round(retrieval_time, 1),
            generation_time_ms=round(generation_time, 1),
            provider=provider,
            model=model_id,
        )

    # -----------------------------------------------------------------
    # Smart Daily Resurface
    # -----------------------------------------------------------------
    @app.get("/resurface/daily")
    def daily_resurface():
        """Return 3 pages from the 'forgetting zone' (3-30 days ago) relevant to recent browsing."""
        from datetime import datetime, timedelta, timezone
        import random

        now = datetime.now(timezone.utc)
        cutoff_near = (now - timedelta(days=3)).isoformat()
        cutoff_far = (now - timedelta(days=30)).isoformat()
        cutoff_recent = (now - timedelta(hours=48)).isoformat()

        pages = state.db.get_all_pages()
        if not pages:
            return {"resurfaces": []}

        def days_since(page):
            lv = page.get("last_visited") or page.get("last_indexed", "")
            if not lv:
                return 999
            try:
                dt = datetime.fromisoformat(lv.replace("Z", "+00:00"))
                return max(0, (now - dt).days)
            except Exception:
                return 999

        # Pages in the forgetting zone
        forgotten = [p for p in pages if cutoff_far <= (p.get("last_visited") or "") <= cutoff_near]
        if not forgotten:
            # Fallback: any page older than 1 day
            forgotten = [p for p in pages if (p.get("last_visited") or "") <= (now - timedelta(days=1)).isoformat()]

        if not forgotten:
            return {"resurfaces": []}

        # Try to find semantically related old pages using recent browsing as anchor
        recent_pages = [p for p in pages if (p.get("last_visited") or "") >= cutoff_recent]
        anchor_text = " ".join(p.get("title", "") for p in recent_pages[:5]) if recent_pages else ""

        picked = []
        if anchor_text.strip():
            try:
                results = state.hybrid_retriever.retrieve(query=anchor_text, top_k=30)
                forgotten_urls = {p["url"] for p in forgotten}
                seen_urls: set = set()
                for r in results:
                    url = r.get("page_url", "")
                    if url in forgotten_urls and url not in seen_urls:
                        seen_urls.add(url)
                        # Find the matching page for metadata
                        page_meta = next((p for p in forgotten if p["url"] == url), {})
                        picked.append({
                            "url": url,
                            "title": r.get("page_title", page_meta.get("title", "")),
                            "snippet": (r.get("text") or page_meta.get("meta_description", ""))[:220],
                            "days_ago": days_since(page_meta),
                        })
                        if len(picked) >= 3:
                            break
            except Exception:
                pass

        # If semantic search didn't find enough, fill with random forgotten pages
        if len(picked) < 3:
            already_picked_urls = {p["url"] for p in picked}
            remaining = [p for p in forgotten if p["url"] not in already_picked_urls]
            for p in random.sample(remaining, min(3 - len(picked), len(remaining))):
                picked.append({
                    "url": p["url"],
                    "title": p.get("title", ""),
                    "snippet": p.get("meta_description", "")[:220],
                    "days_ago": days_since(p),
                })

        return {"resurfaces": picked}

    # -----------------------------------------------------------------
    # Page Annotations
    # -----------------------------------------------------------------
    @app.post("/annotate")
    def save_annotation(request: AnnotationRequest):
        """Save or update a user note on an indexed page."""
        annotation_id = state.db.save_annotation(url=request.url, note=request.note)
        return {"status": "saved", "id": annotation_id}

    @app.get("/annotations")
    def get_annotations(url: Optional[str] = None):
        """Get annotation(s). Pass ?url=... for a specific page, or omit for all."""
        if url:
            annotation = state.db.get_annotation(url)
            return {"annotation": annotation}
        all_annotations = state.db.get_all_annotations()
        return {"annotations": all_annotations, "total": len(all_annotations)}

    @app.delete("/annotate")
    def delete_annotation(url: str):
        """Delete a user note for a page."""
        deleted = state.db.delete_annotation(url)
        if not deleted:
            raise HTTPException(status_code=404, detail="No annotation found for this URL")
        return {"status": "deleted"}

    # -----------------------------------------------------------------
    # Clear All Data
    # -----------------------------------------------------------------
    @app.post("/clear_data")
    def clear_data():
        """Clear all indexed data."""
        state.ingestion_processor.clear_all()
        return {"status": "cleared"}

    # -----------------------------------------------------------------
    # Retention Maintenance
    # -----------------------------------------------------------------
    @app.post("/maintenance/evict")
    async def maintenance_evict():
        """Run the retention sweep now (deletes pages past retention_days)."""
        summary = await asyncio.to_thread(
            state.ingestion_processor.evict_expired, state.settings.retention_days
        )
        return {"status": "ok", "retention_days": state.settings.retention_days, **summary}

    @app.post("/maintenance/reindex")
    async def maintenance_reindex():
        """Rebuild the vector index from stored text (use after an embedding change)."""
        summary = await asyncio.to_thread(state.ingestion_processor.reindex_all)
        return {"status": "reindexed", **summary}

    @app.post("/maintenance/rebuild_kg")
    async def maintenance_rebuild_kg():
        """Re-extract entities from stored text and rebuild the knowledge graph.
        Use after installing spaCy so already-indexed pages gain entities."""
        return await asyncio.to_thread(state.ingestion_processor.rebuild_knowledge_graph)

# Create the app instance
app = create_app()
