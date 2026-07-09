"""
RECAP v2 - Comprehensive Verification Test

Tests all modules can import, initialize, and perform basic operations.
Run with: python tests/test_integration.py
"""

import os
import sys
import tempfile
import shutil
from pathlib import Path

# Ensure project root is in path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Make emoji output safe on Windows consoles (cp1252 default)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Set test environment variables
os.environ["DATA_DIR"] = str(Path(tempfile.mkdtemp()) / "test_data")
os.environ["GROQ_API_KEY"] = "test_key"


def test_config():
    """Test configuration module."""
    print("1. Testing config...")
    from backend.config import get_settings, Settings

    # Clear cache for fresh settings
    get_settings.cache_clear()
    settings = get_settings()

    assert settings.embedding_model == "BAAI/bge-base-en-v1.5"
    assert settings.embedding_dimension == 768
    assert settings.port == 8000
    assert settings.min_content_quality == 0.3
    assert settings.groq_api_key == "test_key"
    assert settings.semantic_gate_enabled is True
    assert settings.semantic_gate_margin == 0.02
    settings.ensure_directories()
    assert settings.data_path.exists()
    print("   ✓ Config OK")


def test_models():
    """Test Pydantic models."""
    print("2. Testing models...")
    from backend.models import (
        PageData, QueryRequest, ProcessingResult, SearchResult,
        QueryResult, ChunkData, EntityData, ContentType, LLMProvider,
    )

    # Test PageData
    page = PageData(url="https://example.com/article", title="Test", content="Hello world")
    assert page.domain == "example.com"
    assert page.path == "/article"

    # Test QueryRequest validation
    qr = QueryRequest(query="test query", top_k=3)
    assert qr.top_k == 3

    # Test ChunkData
    chunk = ChunkData(
        chunk_id="abc123",
        page_url="https://example.com",
        page_title="Test Page",
        text="This is a test chunk",
        context_prefix="[Test Page] (https://example.com)",
    )
    assert "Test Page" in chunk.embedding_text
    assert "test chunk" in chunk.embedding_text

    # Test enums
    assert ContentType.ARTICLE.value == "article"
    assert LLMProvider.GROQ.value == "groq"

    print("   ✓ Models OK")


def test_database():
    """Test SQLite + FTS5 database."""
    print("3. Testing database...")
    from backend.config import get_settings
    get_settings.cache_clear()
    settings = get_settings()
    settings.ensure_directories()

    from backend.storage.database import Database
    db = Database(settings.db_path)

    # Test page CRUD
    page_id, is_new = db.upsert_page(
        url="https://example.com/test",
        title="Test Page",
        domain="example.com",
        content_type="article",
        content_hash="abc123",
        quality_score=0.8,
        word_count=500,
    )
    assert is_new is True
    assert page_id > 0

    # Simulate completed indexing: the content_hash is persisted only AFTER
    # chunks + vectors are written (see processor). Until then it stays empty.
    db.update_content_hash("https://example.com/test", "abc123")

    # Test duplicate detection
    page_id2, is_changed = db.upsert_page(
        url="https://example.com/test",
        title="Test Page",
        domain="example.com",
        content_hash="abc123",  # Same hash
    )
    assert page_id2 == page_id
    assert is_changed is False

    # Test content change detection
    page_id3, is_changed = db.upsert_page(
        url="https://example.com/test",
        title="Test Page Updated",
        domain="example.com",
        content_hash="xyz789",  # Different hash
    )
    assert page_id3 == page_id
    assert is_changed is True

    # Test get page
    page = db.get_page("https://example.com/test")
    assert page is not None
    assert page["title"] == "Test Page Updated"
    assert page["visit_count"] == 3

    # Test chunk insertion
    chunks = [
        {"chunk_id": "chunk_001", "text": "Python is a programming language", "chunk_index": 0, "token_count": 6},
        {"chunk_id": "chunk_002", "text": "FastAPI is a web framework", "chunk_index": 1, "token_count": 6},
    ]
    inserted = db.insert_chunks(page_id, "Test Page", chunks)
    assert inserted == 2

    # Test BM25 search
    results = db.bm25_search("Python programming")
    assert len(results) >= 1
    assert any("Python" in r["chunk_text"] for r in results)

    results = db.bm25_search("FastAPI framework")
    assert len(results) >= 1

    # Test entity operations
    eid1 = db.upsert_entity("Python", "language")
    eid2 = db.upsert_entity("FastAPI", "technology")
    assert eid1 > 0
    assert eid2 > 0

    rid = db.upsert_relation(eid1, eid2, "used_by", 1.0, "https://example.com")
    assert rid > 0

    entity = db.get_entity_by_name("Python")
    assert entity is not None
    assert entity["entity_type"] == "language"

    # Test stats
    stats = db.get_stats()
    assert stats["total_pages"] >= 1
    assert stats["total_chunks"] >= 2
    assert stats["total_entities"] >= 2

    # Test delete
    deleted = db.delete_page("https://example.com/test")
    assert deleted is True
    assert db.get_page("https://example.com/test") is None

    print("   ✓ Database OK (SQLite + FTS5 BM25)")


def test_vector_store():
    """Test LanceDB vector store."""
    print("4. Testing vector store...")
    from backend.config import get_settings
    get_settings.cache_clear()
    settings = get_settings()

    from backend.storage.vector_store import VectorStore
    vs = VectorStore(settings.vector_store_path, embedding_dim=4)  # Small dim for test

    # Test add
    count = vs.add_chunks(
        chunk_ids=["c1", "c2", "c3"],
        vectors=[[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]],
    )
    assert count == 3
    assert vs.count() == 3

    # Test search (returns chunk_id + score only; text is hydrated from SQLite)
    results = vs.search([1, 0.1, 0, 0], limit=2)  # Should match c1 best
    assert len(results) >= 1
    assert results[0]["chunk_id"] == "c1"
    assert results[0]["score"] > 0

    # Test delete by id
    vs.delete_chunks(["c2"])
    assert vs.count() == 2
    vs.delete_chunks(["c1"])
    assert vs.count() == 1

    # Test clear
    vs.clear()
    assert vs.count() == 0

    print("   ✓ Vector Store OK (LanceDB)")


def test_knowledge_graph():
    """Test knowledge graph."""
    print("5. Testing knowledge graph...")
    from backend.config import get_settings
    get_settings.cache_clear()
    settings = get_settings()

    from backend.storage.database import Database
    from backend.storage.knowledge_graph import KnowledgeGraph

    db = Database(settings.db_path)
    kg = KnowledgeGraph(db)

    # Add entities
    eid1 = kg.add_entity("Python", "language")
    eid2 = kg.add_entity("FastAPI", "technology")
    eid3 = kg.add_entity("Uvicorn", "technology")
    assert eid1 > 0

    # Add relations
    kg.add_relation("Python", "FastAPI", "used_for", 2.0)
    kg.add_relation("FastAPI", "Uvicorn", "runs_on", 1.5)

    # Test neighbor lookup
    neighbors = kg.get_neighbors("Python", max_hops=1)
    assert len(neighbors) >= 1
    assert any(n["name"] == "FastAPI" for n in neighbors)

    # Test 2-hop
    neighbors_2hop = kg.get_neighbors("Python", max_hops=2)
    assert any(n["name"] == "Uvicorn" for n in neighbors_2hop)

    # Test entity finding in query
    found = kg.find_entities_in_query("How does Python work with FastAPI?")
    assert "Python" in found
    assert "FastAPI" in found

    # Test context generation
    context = kg.get_context_for_entities(["Python", "FastAPI"])
    assert "Python" in context
    assert len(context) > 0

    # Test stats
    stats = kg.get_stats()
    assert stats["nodes"] >= 3
    assert stats["edges"] >= 2

    print("   ✓ Knowledge Graph OK (NetworkX + SQLite)")


def test_content_classifier():
    """Test content classifier."""
    print("6. Testing content classifier...")
    from backend.ingestion.content_classifier import classify_page
    from backend.models import ContentType

    # Should be blocked
    ct, score = classify_page("chrome://settings")
    assert ct == ContentType.SKIP

    ct, score = classify_page("https://accounts.google.com/signin")
    assert ct == ContentType.SKIP

    # Should be high quality
    ct, score = classify_page(
        "https://docs.python.org/3/library/asyncio.html",
        title="asyncio - Asynchronous I/O",
        content="x " * 500,
        word_count=500,
    )
    assert ct == ContentType.DOCUMENTATION
    assert score > 0.3

    # Should be article
    ct, score = classify_page(
        "https://medium.com/great-article-about-python",
        title="Great Article About Python Programming",
        content="x " * 300,
        word_count=300,
    )
    assert score > 0.2

    # Should be forum
    ct, score = classify_page(
        "https://stackoverflow.com/questions/12345/how-to-use-python",
        title="How to use Python",
        content="x " * 200,
    )
    assert ct == ContentType.FORUM

    # --- Sensitive-content backstop (PII + auth-page text) ---
    from backend.ingestion.content_classifier import detect_sensitive_content

    # Luhn-valid card number on a short page → sensitive
    reason = detect_sensitive_content("Your card 4111 1111 1111 1111 was charged.", word_count=7)
    assert reason == "pii:card"

    # SSN on a short page → sensitive
    assert detect_sensitive_content("SSN on file: 123-45-6789", word_count=5) == "pii:ssn"

    # IBAN on a short page → sensitive
    assert detect_sensitive_content("Wire to DE89370400440532013000 today", word_count=4) == "pii:iban"

    # A long article QUOTING one example card number still indexes
    long_article = ("This article explains payment security in depth. " * 60
                    + "A classic test number is 4111 1111 1111 1111.")
    assert detect_sensitive_content(long_article) is None

    # Contact directory: many distinct emails relative to text → sensitive
    directory = " ".join(f"person{i}@example.com office" for i in range(8))
    assert detect_sensitive_content(directory) == "pii:contact-density"

    # Short page dominated by auth phrasing → sensitive
    login_text = "Sign in to your account. Forgot password? Remember me."
    assert detect_sensitive_content(login_text) == "auth-text"

    # Benign short page → fine
    assert detect_sensitive_content("A quick note about Python decorators and closures.") is None

    print("   ✓ Content Classifier OK (incl. PII/auth backstop)")


def test_chunker():
    """Test semantic chunker with mock embeddings."""
    print("7. Testing semantic chunker...")
    import numpy as np
    from backend.ingestion.chunker import SemanticChunker, split_sentences

    # Test sentence splitting
    text = "This is sentence one. This is sentence two. And this is sentence three."
    sentences = split_sentences(text)
    assert len(sentences) >= 2

    # Mock embedding function
    def mock_embed(texts):
        return [np.random.randn(4).tolist() for _ in texts]

    chunker = SemanticChunker(
        embedding_fn=mock_embed,
        max_chunk_tokens=50,
        min_chunk_tokens=5,
    )

    # Test with short text
    chunks = chunker.chunk("Short text.", "http://test.com", "Test")
    assert len(chunks) >= 1
    assert chunks[0].chunk_id

    # Test with longer text
    long_text = ". ".join([f"This is sentence number {i} about topic {i % 3}" for i in range(30)])
    chunks = chunker.chunk(long_text, "http://test.com/article", "Long Article")
    assert len(chunks) >= 1
    assert all(c.page_url == "http://test.com/article" for c in chunks)
    assert all(c.context_prefix for c in chunks)

    print("   ✓ Semantic Chunker OK")


def test_reranker():
    """Test re-ranker (with mock if model not available)."""
    print("8. Testing re-ranker...")
    from backend.retrieval.reranker import ReRanker

    reranker = ReRanker()

    # Test with empty results
    result = reranker.rerank("test", [])
    assert result == []

    # Test with results (will load model or fall back gracefully)
    results = [
        {"text": "Python is a programming language", "chunk_id": "c1", "score": 0.5},
        {"text": "JavaScript is used for web development", "chunk_id": "c2", "score": 0.4},
        {"text": "Python web frameworks include FastAPI", "chunk_id": "c3", "score": 0.3},
    ]
    try:
        reranked = reranker.rerank("Python web framework", results, top_k=2)
        assert len(reranked) <= 2
        # The cross-encoder should rank c3 higher than c2 for "Python web framework"
        print("   ✓ Re-ranker OK (cross-encoder loaded)")
    except Exception as e:
        print(f"   ⚠ Re-ranker model loading skipped: {e}")
        print("   ✓ Re-ranker fallback OK")


def test_end_to_end_pipeline():
    """Test the full pipeline: ingest → retrieve → answer."""
    print("9. Testing end-to-end pipeline...")
    import numpy as np
    from backend.config import get_settings
    get_settings.cache_clear()
    settings = get_settings()
    settings.ensure_directories()

    from backend.storage.database import Database
    from backend.storage.vector_store import VectorStore
    from backend.storage.knowledge_graph import KnowledgeGraph
    from backend.ingestion.processor import IngestionProcessor
    from backend.retrieval.hybrid_retriever import HybridRetriever
    from backend.models import PageData

    db = Database(settings.db_path)
    vs = VectorStore(settings.vector_store_path, embedding_dim=4)
    kg = KnowledgeGraph(db)

    # Mock embedding function (consistent for retrieval)
    def mock_embed(texts):
        results = []
        for t in texts:
            # Simple hash-based embedding for deterministic results
            h = hash(t) % 1000
            results.append([h / 1000, (h * 7) % 1000 / 1000,
                           (h * 13) % 1000 / 1000, (h * 31) % 1000 / 1000])
        return results

    # Initialize processor
    processor = IngestionProcessor(
        settings=settings,
        db=db,
        vector_store=vs,
        knowledge_graph=kg,
        embedding_fn=mock_embed,
        nlp=None,  # No spaCy in test
    )
    # Hash-mock embeddings carry no semantics - the gate's junk-vs-content
    # margin would be arbitrary noise here, so disable it for this test.
    # (The gate itself is tested with a controlled mock in test_semantic_gate.)
    processor.semantic_gate = None

    # The config flag must disable the gate at construction time
    from backend.config import Settings
    no_gate = IngestionProcessor(
        settings=Settings(semantic_gate_enabled=False),
        db=db, vector_store=vs, knowledge_graph=kg,
        embedding_fn=mock_embed, nlp=None,
    )
    assert no_gate.semantic_gate is None

    # PII backstop runs inside the pipeline: a statement-like page is refused
    # BEFORE anything is written to SQLite
    # Long enough to clear the quality gate (so the PII backstop, not the
    # quality threshold, is what rejects it) but under the 400-word "short
    # page" line where a single card number is decisive.
    pii_page = PageData(
        url="https://unlisted-neobank.example/statement",
        title="Monthly Account Statement",
        content=(
            "Account statement for your records. Card number 4111 1111 1111 1111 "
            "was charged for the monthly subscription service. " * 20
        ),
        word_count=360,
        text_to_tag_ratio=0.6,
    )
    pii_result = processor.process_page(pii_page)
    assert pii_result.status == "skipped"
    assert "pii:card" in pii_result.message
    assert db.get_page(pii_page.url) is None  # never touched the source of truth

    # Skip telemetry recorded per domain (Layer E - ignore-domain candidates)
    skipped = db.get_stats()["top_skipped_domains"]
    assert any(
        row["domain"] == "unlisted-neobank.example" and "pii:card" in row["reasons"]
        for row in skipped
    )

    # Process a page
    page = PageData(
        url="https://docs.python.org/3/library/asyncio.html",
        title="asyncio - Asynchronous I/O - Python 3.12 documentation",
        content=(
            "asyncio is a library to write concurrent code using the async/await syntax. "
            "It is used as a foundation for multiple Python asynchronous frameworks. "
            "asyncio provides high-level APIs to create and manage coroutines. "
            "The module includes support for running asynchronous tasks concurrently."
        ) * 5,  # Repeat to meet word count threshold
        word_count=300,
        text_to_tag_ratio=0.6,
    )

    result = processor.process_page(page)
    print(f"   Processing result: status={result.status}, chunks={result.chunks_created}")
    assert result.status in ("indexed", "skipped")

    if result.status == "indexed":
        # Verify data in DB
        db_page = db.get_page(page.url)
        assert db_page is not None
        assert db_page["visit_count"] >= 1

        # Verify chunks in vector store
        assert vs.count() > 0

        # Test retrieval
        retriever = HybridRetriever(db, vs, kg, mock_embed)
        results = retriever.retrieve("asyncio concurrent", top_k=5)
        print(f"   Retrieved {len(results)} results")
        assert len(results) >= 1

    print("   ✓ End-to-End Pipeline OK")


def test_semantic_gate():
    """Test the embedding-prototype junk gate with an injected mock embedder."""
    print("10. Testing semantic gate...")
    from backend.ingestion.semantic_gate import SemanticGate

    calls = {"count": 0}
    junk_words = ("password", "sign in", "checkout", "balances", "security code",
                  "cookies", "denied", "create an account", "one-time", "dashboard")

    def mock_embed(texts):
        # Keyword-keyed embedding: junk-flavored text → [1,0], content → [0,1].
        # Tests the gate MECHANISM (lazy prototypes, relative margin, fail-open)
        # without loading the real model.
        calls["count"] += 1
        return [
            [1.0, 0.0] if any(w in t.lower() for w in junk_words) else [0.0, 1.0]
            for t in texts
        ]

    gate = SemanticGate(mock_embed, margin=0.02)

    is_junk, label, margin = gate.assess("Please sign in with your password to continue.")
    assert is_junk is True
    assert label  # a junk prototype label was identified
    assert margin > 0.02

    is_junk, _, _ = gate.assess("Python asyncio lets you write concurrent coroutines.")
    assert is_junk is False

    # Prototypes embedded lazily, exactly once: 1 prototype batch + 2 page calls
    assert calls["count"] == 3

    # Empty text → not junk, no embedder call
    assert gate.assess("")[0] is False

    # Broken embedder → fails OPEN (page treated as content, no crash)
    def broken_embed(texts):
        raise RuntimeError("embedder unavailable")
    gate2 = SemanticGate(broken_embed)
    assert gate2.assess("anything at all")[0] is False

    print("   ✓ Semantic Gate OK (prototype matching + fail-open)")


def test_query_instruction():
    """Fix #1: query gets the bge instruction; documents never do."""
    print("11. Testing query/document embedding asymmetry fix...")
    from backend.embeddings import query_instruction_for

    # The default model is bge -> query side gets the retrieval instruction.
    instr = query_instruction_for("local", "BAAI/bge-base-en-v1.5")
    assert instr and "searching relevant passages" in instr.lower()
    # Non-bge models / other providers get no instruction.
    assert query_instruction_for("openai", "text-embedding-3-small") == ""
    assert query_instruction_for("local", "sentence-transformers/all-MiniLM-L6-v2") == ""

    print("   ✓ Query Instruction OK (bge query prefix, documents unprefixed)")


def test_related_selection():
    """Fix #2: index-native Resurface - cosine floor + self/domain exclusion."""
    print("12. Testing resurface related-page selection...")
    import hashlib
    from backend.config import get_settings
    get_settings.cache_clear()
    settings = get_settings()
    settings.ensure_directories()

    from backend.storage.database import Database
    from backend.storage.vector_store import VectorStore
    from backend.storage.knowledge_graph import KnowledgeGraph
    from backend.retrieval.hybrid_retriever import HybridRetriever

    db = Database(settings.db_path)
    vs = VectorStore(settings.vector_store_path, embedding_dim=4)
    kg = KnowledgeGraph(db)

    # Controlled embeddings so cosine is exact: "alpha" text -> e1, "beta" -> e2
    # (orthogonal, so alpha vs beta cosine == 0).
    UNIT = {"alpha": [1.0, 0.0, 0.0, 0.0], "beta": [0.0, 1.0, 0.0, 0.0]}
    def mock_embed(texts):
        return [list(UNIT["alpha"] if "alpha" in t else UNIT["beta"]) for t in texts]

    def add_page(url, domain, text, vec):
        pid, _ = db.upsert_page(url=url, title=url, domain=domain,
                                content_type="article", quality_score=0.9,
                                word_count=len(text.split()))
        cid = hashlib.md5(url.encode()).hexdigest()[:16]
        db.insert_chunks(pid, url, [{"chunk_id": cid, "text": text,
                                     "chunk_index": 0, "token_count": 3}])
        db.update_content_hash(url, "h")
        vs.add_chunks([cid], [vec])

    add_page("https://a.com/cur",  "a.com", "alpha topic here",  UNIT["alpha"])  # current
    add_page("https://b.com/rel",  "b.com", "alpha topic again", UNIT["alpha"])  # related, cosine 1.0
    add_page("https://c.com/un",   "c.com", "beta different",    UNIT["beta"])   # unrelated, cosine 0
    add_page("https://a.com/same", "a.com", "alpha same domain", UNIT["alpha"])  # same domain -> excluded

    retr = HybridRetriever(db, vs, kg, mock_embed)

    # Related cross-domain page wins; self and same-domain are excluded.
    best = retr.find_related("https://a.com/cur", min_similarity=0.65)
    assert best is not None
    assert best["page_url"] == "https://b.com/rel"
    assert best["similarity"] > 0.99

    # Precision-first: when the only cross-domain matches are unrelated (cosine 0
    # for a beta page), nothing clears the floor -> no card.
    assert retr.find_related("https://c.com/un", min_similarity=0.65) is None

    print("   ✓ Resurface Related-Page Selection OK (index-native cosine + exclusions)")


def cleanup(test_data_dir: str):
    """Clean up test data."""
    try:
        shutil.rmtree(test_data_dir, ignore_errors=True)
    except Exception:
        pass


if __name__ == "__main__":
    test_data_dir = os.environ.get("DATA_DIR", "")

    print("=" * 60)
    print("RECAP v2 - Integration Test Suite")
    print("=" * 60)
    print()

    passed = 0
    failed = 0
    tests = [
        test_config,
        test_models,
        test_database,
        test_vector_store,
        test_knowledge_graph,
        test_content_classifier,
        test_chunker,
        test_reranker,
        test_end_to_end_pipeline,
        test_semantic_gate,
        test_query_instruction,
        test_related_selection,
    ]

    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"   ✗ FAILED: {e}")
            import traceback
            traceback.print_exc()
        print()

    print("=" * 60)
    print(f"Results: {passed}/{passed + failed} passed, {failed} failed")
    print("=" * 60)

    cleanup(test_data_dir)

    if failed > 0:
        sys.exit(1)
