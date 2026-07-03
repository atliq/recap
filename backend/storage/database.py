"""
RECAP v2 - SQLite + FTS5 Database Manager

Handles all metadata storage and BM25 full-text search.
Tables: pages, chunks, entities, entity_relations, fts5 virtual table.
Thread-safe with connection pooling via contextmanager.
"""

from __future__ import annotations

import sqlite3
import hashlib
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

logger = logging.getLogger(__name__)


class Database:
    """SQLite database manager with FTS5 full-text search."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # =========================================================================
    # Connection Management
    # =========================================================================

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Get a database connection with WAL mode and foreign keys enabled."""
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Initialize database schema."""
        with self._get_connection() as conn:
            conn.executescript("""
                -- Pages table: one row per unique URL
                CREATE TABLE IF NOT EXISTS pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE NOT NULL,
                    title TEXT DEFAULT '',
                    domain TEXT DEFAULT '',
                    content_type TEXT DEFAULT 'other',
                    content_hash TEXT DEFAULT '',
                    quality_score REAL DEFAULT 0.0,
                    word_count INTEGER DEFAULT 0,
                    visit_count INTEGER DEFAULT 1,
                    first_visited TEXT NOT NULL,
                    last_visited TEXT NOT NULL,
                    last_indexed TEXT NOT NULL,
                    meta_description TEXT DEFAULT '',
                    meta_author TEXT DEFAULT ''
                );

                -- Chunks table: text segments from pages
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chunk_id TEXT UNIQUE NOT NULL,
                    page_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    chunk_index INTEGER DEFAULT 0,
                    token_count INTEGER DEFAULT 0,
                    FOREIGN KEY (page_id) REFERENCES pages(id) ON DELETE CASCADE
                );

                -- Entities table: named entities extracted from content
                CREATE TABLE IF NOT EXISTS entities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    frequency INTEGER DEFAULT 1,
                    first_seen TEXT NOT NULL,
                    UNIQUE(name, entity_type)
                );

                -- Entity relations: edges in the knowledge graph
                CREATE TABLE IF NOT EXISTS entity_relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_entity_id INTEGER NOT NULL,
                    target_entity_id INTEGER NOT NULL,
                    relation_type TEXT DEFAULT 'related_to',
                    weight REAL DEFAULT 1.0,
                    source_url TEXT DEFAULT '',
                    FOREIGN KEY (source_entity_id) REFERENCES entities(id) ON DELETE CASCADE,
                    FOREIGN KEY (target_entity_id) REFERENCES entities(id) ON DELETE CASCADE,
                    UNIQUE(source_entity_id, target_entity_id, relation_type)
                );

                -- Indexes for fast lookups
                CREATE INDEX IF NOT EXISTS idx_pages_url ON pages(url);
                CREATE INDEX IF NOT EXISTS idx_pages_domain ON pages(domain);
                CREATE INDEX IF NOT EXISTS idx_pages_content_hash ON pages(content_hash);
                CREATE INDEX IF NOT EXISTS idx_chunks_page_id ON chunks(page_id);
                CREATE INDEX IF NOT EXISTS idx_chunks_chunk_id ON chunks(chunk_id);
                CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
                CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
                CREATE INDEX IF NOT EXISTS idx_entity_relations_source ON entity_relations(source_entity_id);
                CREATE INDEX IF NOT EXISTS idx_entity_relations_target ON entity_relations(target_entity_id);

                -- Highlights table: user-saved text passages
                CREATE TABLE IF NOT EXISTS highlights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_highlights_url ON highlights(url);

                -- Annotations table: user notes attached to indexed pages
                CREATE TABLE IF NOT EXISTS annotations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_annotations_url ON annotations(url);

                -- Key/value metadata (e.g. the embedding-model fingerprint that
                -- identifies the vector index's embedding space).
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                -- Skip telemetry: which domains keep getting rejected and why.
                -- Feeds ignore-domain suggestions; stores NO page content.
                CREATE TABLE IF NOT EXISTS skip_stats (
                    domain TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    count INTEGER DEFAULT 0,
                    last_seen TEXT,
                    PRIMARY KEY (domain, reason)
                );
            """)

            # FTS5 keyword index over chunk text - EXTERNAL-CONTENT mode.
            # It stores ONLY the inverted index (no copy of the text) and reads
            # the original text from `chunks` via content_rowid=id. Triggers keep
            # it in sync on insert/update/delete, so chunk text lives exactly once
            # in chunks.text - the single source of truth.
            conn.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    text,
                    content='chunks',
                    content_rowid='id',
                    tokenize='porter unicode61'
                );

                CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
                    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.id, old.text);
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE ON chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.id, old.text);
                    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
                END;
            """)

            logger.info("Database initialized at %s", self.db_path)

    # =========================================================================
    # Page Operations
    # =========================================================================

    def upsert_page(
        self,
        url: str,
        title: str = "",
        domain: str = "",
        content_type: str = "other",
        content_hash: str = "",
        quality_score: float = 0.0,
        word_count: int = 0,
        meta_description: str = "",
        meta_author: str = "",
    ) -> Tuple[int, bool]:
        """
        Insert or update a page. Returns (page_id, is_new_or_changed).

        If the content_hash matches, only updates visit metadata.
        If content changed, returns True so caller can re-index.

        NOTE: For new/changed content the stored content_hash is left EMPTY here.
        The caller must call update_content_hash(url, content_hash) only after
        chunks + vectors have been written successfully. This guarantees a page
        is never marked indexed until its chunks/vectors actually exist, so a
        failure mid-pipeline leaves it re-indexable on the next visit rather than
        permanently skipped.
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            existing = conn.execute(
                "SELECT id, content_hash, visit_count FROM pages WHERE url = ?",
                (url,)
            ).fetchone()

            if existing is None:
                # New page - store hash empty until indexing completes
                cursor = conn.execute(
                    """INSERT INTO pages
                    (url, title, domain, content_type, content_hash, quality_score,
                     word_count, visit_count, first_visited, last_visited, last_indexed,
                     meta_description, meta_author)
                    VALUES (?, ?, ?, ?, '', ?, ?, 1, ?, ?, ?, ?, ?)""",
                    (url, title, domain, content_type, quality_score,
                     word_count, now, now, now, meta_description, meta_author),
                )
                logger.debug("Inserted new page: %s (id=%d)", url, cursor.lastrowid)
                return cursor.lastrowid, True

            page_id = existing["id"]
            old_hash = existing["content_hash"]
            visit_count = existing["visit_count"] + 1

            content_changed = content_hash != old_hash and content_hash != ""

            if content_changed:
                # Content changed - update metadata but leave content_hash empty
                # until re-indexing completes (see update_content_hash).
                conn.execute(
                    """UPDATE pages SET
                        title=?, domain=?, content_type=?, content_hash='',
                        quality_score=?, word_count=?, visit_count=?,
                        last_visited=?, last_indexed=?,
                        meta_description=?, meta_author=?
                    WHERE id=?""",
                    (title, domain, content_type, quality_score,
                     word_count, visit_count, now, now,
                     meta_description, meta_author, page_id),
                )
                logger.debug("Updated page (content changed): %s", url)
            else:
                # Same content - just update visit metadata
                conn.execute(
                    "UPDATE pages SET visit_count=?, last_visited=? WHERE id=?",
                    (visit_count, now, page_id),
                )
                logger.debug("Updated page (visit only): %s", url)

            return page_id, content_changed

    def update_content_hash(self, url: str, content_hash: str) -> None:
        """
        Persist the content_hash for a page AFTER its chunks + vectors have been
        written successfully. Also refreshes last_indexed. This is the final step
        that marks a page as fully indexed for change-detection purposes.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE pages SET content_hash = ?, last_indexed = ? WHERE url = ?",
                (content_hash, now, url),
            )
        logger.debug("Persisted content_hash for %s", url)

    def get_page(self, url: str) -> Optional[Dict[str, Any]]:
        """Get a page by URL."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM pages WHERE url = ?", (url,)).fetchone()
            return dict(row) if row else None

    def get_all_pages(self) -> List[Dict[str, Any]]:
        """Get all indexed pages."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM pages ORDER BY last_visited DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_page(self, url: str) -> bool:
        """Delete a page and all its chunks. FTS stays in sync via triggers."""
        with self._get_connection() as conn:
            page = conn.execute("SELECT id FROM pages WHERE url = ?", (url,)).fetchone()
            if not page:
                return False

            page_id = page["id"]
            # Delete chunks explicitly (fires the FTS delete trigger), then the page.
            n = conn.execute(
                "DELETE FROM chunks WHERE page_id = ?", (page_id,)
            ).rowcount
            conn.execute("DELETE FROM pages WHERE id = ?", (page_id,))
            logger.info("Deleted page and %d chunks: %s", n, url)
            return True

    def get_content_hash(self, content: str) -> str:
        """Generate a deterministic hash of content for change detection."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    # =========================================================================
    # Metadata (key/value)
    # =========================================================================

    def get_meta(self, key: str) -> Optional[str]:
        """Read a metadata value, or None if unset."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a metadata value."""
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # =========================================================================
    # Chunk Operations
    # =========================================================================

    def insert_chunks(self, page_id: int, page_title: str, chunks: List[Dict[str, Any]]) -> int:
        """
        Insert chunks for a page, replacing any existing chunks.
        The FTS5 index is synced automatically by triggers on the chunks table.
        Returns the number of chunks inserted.
        """
        with self._get_connection() as conn:
            # Replace any existing chunks for this page. The FTS index is kept in
            # sync automatically by triggers, so we only touch the chunks table.
            conn.execute("DELETE FROM chunks WHERE page_id = ?", (page_id,))

            for chunk in chunks:
                conn.execute(
                    """INSERT INTO chunks (chunk_id, page_id, text, chunk_index, token_count)
                    VALUES (?, ?, ?, ?, ?)""",
                    (chunk["chunk_id"], page_id, chunk["text"],
                     chunk.get("chunk_index", 0), chunk.get("token_count", 0)),
                )

            logger.debug("Inserted %d chunks for page_id=%d", len(chunks), page_id)
            return len(chunks)

    def get_chunks_by_page(self, page_id: int) -> List[Dict[str, Any]]:
        """Get all chunks for a page."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM chunks WHERE page_id = ? ORDER BY chunk_index",
                (page_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def get_chunk_by_id(self, chunk_id: str) -> Optional[Dict[str, Any]]:
        """Get a chunk by its unique ID."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT c.*, p.url, p.title as page_title FROM chunks c "
                "JOIN pages p ON c.page_id = p.id WHERE c.chunk_id = ?",
                (chunk_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_chunks_metadata(
        self,
        chunk_ids: List[str],
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Hydrate a set of chunk_ids with their canonical text + page metadata in a
        single query, optionally dropping chunks whose page falls outside a
        visit-date range. This is the ONE place retrieval reads chunk text from,
        so the BM25/dense/KG legs only need to carry chunk_ids and scores.
        Returns {chunk_id: {text, page_url, page_title, timestamp}}.
        """
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" * len(chunk_ids))
        conditions = [f"c.chunk_id IN ({placeholders})"]
        params: list = list(chunk_ids)
        if date_from:
            conditions.append("p.last_visited >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("p.last_visited <= ?")
            params.append(date_to)
        sql = f"""
            SELECT c.chunk_id, c.text, p.url AS page_url,
                   p.title AS page_title, p.last_visited
            FROM chunks c
            JOIN pages p ON c.page_id = p.id
            WHERE {" AND ".join(conditions)}
        """
        with self._get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return {
            r["chunk_id"]: {
                "text": r["text"],
                "page_url": r["page_url"],
                "page_title": r["page_title"],
                "timestamp": r["last_visited"],
            }
            for r in rows
        }

    def bm25_search(
        self,
        query: str,
        limit: int = 20,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        BM25 keyword search using FTS5.
        Returns chunks ranked by relevance with page metadata.
        Optionally filters to pages visited within a date range.
        """
        conditions = ["chunks_fts MATCH ?"]
        params: list = [query]
        if date_from:
            conditions.append("p.last_visited >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("p.last_visited <= ?")
            params.append(date_to)
        params.append(limit)

        where_clause = " AND ".join(conditions)
        # chunks_fts is external-content: its rowid == chunks.id. Join on rowid
        # and read the canonical text/metadata from chunks + pages.
        sql = f"""
            SELECT
                c.chunk_id,
                c.text AS chunk_text,
                p.title AS page_title,
                rank AS bm25_score,
                p.url,
                p.last_visited
            FROM chunks_fts f
            JOIN chunks c ON c.id = f.rowid
            JOIN pages p ON c.page_id = p.id
            WHERE {where_clause}
            ORDER BY rank
            LIMIT ?
        """
        with self._get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    # =========================================================================
    # Entity Operations
    # =========================================================================

    def upsert_entity(self, name: str, entity_type: str) -> int:
        """Insert or update an entity. Returns entity_id."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            existing = conn.execute(
                "SELECT id, frequency FROM entities WHERE name = ? AND entity_type = ?",
                (name, entity_type),
            ).fetchone()

            if existing:
                conn.execute(
                    "UPDATE entities SET frequency = ? WHERE id = ?",
                    (existing["frequency"] + 1, existing["id"]),
                )
                return existing["id"]

            cursor = conn.execute(
                "INSERT INTO entities (name, entity_type, frequency, first_seen) VALUES (?, ?, 1, ?)",
                (name, entity_type, now),
            )
            return cursor.lastrowid

    def upsert_relation(
        self,
        source_id: int,
        target_id: int,
        relation_type: str = "related_to",
        weight: float = 1.0,
        source_url: str = "",
    ) -> int:
        """Insert or update an entity relation. Returns relation_id."""
        with self._get_connection() as conn:
            existing = conn.execute(
                """SELECT id, weight FROM entity_relations
                WHERE source_entity_id = ? AND target_entity_id = ? AND relation_type = ?""",
                (source_id, target_id, relation_type),
            ).fetchone()

            if existing:
                new_weight = existing["weight"] + weight
                conn.execute(
                    "UPDATE entity_relations SET weight = ? WHERE id = ?",
                    (new_weight, existing["id"]),
                )
                return existing["id"]

            cursor = conn.execute(
                """INSERT INTO entity_relations
                (source_entity_id, target_entity_id, relation_type, weight, source_url)
                VALUES (?, ?, ?, ?, ?)""",
                (source_id, target_id, relation_type, weight, source_url),
            )
            return cursor.lastrowid

    def get_entity_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Get entity by name (case-insensitive)."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM entities WHERE LOWER(name) = LOWER(?)", (name,)
            ).fetchone()
            return dict(row) if row else None

    # =========================================================================
    # Statistics
    # =========================================================================

    def record_skip(self, domain: str, reason: str) -> None:
        """Increment the skip counter for a (domain, reason) pair."""
        if not domain:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """INSERT INTO skip_stats (domain, reason, count, last_seen)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(domain, reason) DO UPDATE SET
                       count = count + 1, last_seen = excluded.last_seen""",
                (domain, reason, now),
            )

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive database statistics."""
        with self._get_connection() as conn:
            total_pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            total_entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            total_relations = conn.execute("SELECT COUNT(*) FROM entity_relations").fetchone()[0]

            last_indexed = conn.execute(
                "SELECT last_indexed FROM pages ORDER BY last_indexed DESC LIMIT 1"
            ).fetchone()

            # Content type distribution
            type_dist = conn.execute(
                "SELECT content_type, COUNT(*) as count FROM pages GROUP BY content_type"
            ).fetchall()

            # Top domains
            top_domains = conn.execute(
                """SELECT domain, COUNT(*) as page_count, SUM(visit_count) as total_visits
                FROM pages GROUP BY domain ORDER BY page_count DESC LIMIT 10"""
            ).fetchall()

            # Most-skipped domains (Layer E telemetry - ignore-domain candidates).
            # Reasons are already distinct per domain: (domain, reason) is the PK.
            top_skipped = conn.execute(
                """SELECT domain, SUM(count) as skip_count,
                       GROUP_CONCAT(reason) as reasons
                FROM skip_stats GROUP BY domain ORDER BY skip_count DESC LIMIT 10"""
            ).fetchall()

            # Database file size
            db_size_mb = self.db_path.stat().st_size / (1024 * 1024) if self.db_path.exists() else 0

            return {
                "total_pages": total_pages,
                "total_chunks": total_chunks,
                "total_entities": total_entities,
                "total_relations": total_relations,
                "last_indexed": last_indexed["last_indexed"] if last_indexed else None,
                "content_type_distribution": {row["content_type"]: row["count"] for row in type_dist},
                "top_domains": [dict(row) for row in top_domains],
                "top_skipped_domains": [dict(row) for row in top_skipped],
                "index_size_mb": round(db_size_mb, 2),
            }

    def clear_knowledge_graph(self) -> None:
        """Delete all entities and relations (pages/chunks untouched). Used to
        rebuild the KG from stored chunk text after enabling/upgrading the NER
        model - the text is the source of truth; the graph is derived."""
        with self._get_connection() as conn:
            conn.executescript("""
                DELETE FROM entity_relations;
                DELETE FROM entities;
            """)
            logger.info("Cleared knowledge graph (entities + relations)")

    def clear_all(self) -> None:
        """Delete all data from all tables."""
        with self._get_connection() as conn:
            conn.executescript("""
                DELETE FROM entity_relations;
                DELETE FROM entities;
                DELETE FROM chunks;
                DELETE FROM pages;
                DELETE FROM highlights;
                DELETE FROM annotations;
            """)
            # Rebuild the external-content FTS index from the (now empty) chunks.
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
            logger.info("All data cleared from database")

    # =========================================================================
    # Retention
    # =========================================================================

    def evict_pages_older_than(self, cutoff_iso: str) -> Dict[str, list]:
        """
        Delete pages whose last visit is older than cutoff_iso, cascading to their
        chunks (and the FTS index, via triggers). Returns the chunk_ids and page
        URLs removed so the caller can clean up the vector store and KG relations.
        """
        with self._get_connection() as conn:
            pages = conn.execute(
                "SELECT id, url FROM pages WHERE last_visited < ?", (cutoff_iso,)
            ).fetchall()
            if not pages:
                return {"chunk_ids": [], "urls": []}

            page_ids = [p["id"] for p in pages]
            urls = [p["url"] for p in pages]
            ph = ",".join("?" * len(page_ids))
            chunk_ids = [
                r["chunk_id"] for r in conn.execute(
                    f"SELECT chunk_id FROM chunks WHERE page_id IN ({ph})", page_ids
                ).fetchall()
            ]
            conn.execute(f"DELETE FROM chunks WHERE page_id IN ({ph})", page_ids)
            conn.execute(f"DELETE FROM pages WHERE id IN ({ph})", page_ids)
            logger.info(
                "Retention: evicted %d pages (%d chunks) older than %s",
                len(page_ids), len(chunk_ids), cutoff_iso,
            )
            return {"chunk_ids": chunk_ids, "urls": urls}

    def delete_relations_by_urls(self, urls: List[str]) -> int:
        """Delete KG relations sourced from the given page URLs (retention cleanup)."""
        if not urls:
            return 0
        ph = ",".join("?" * len(urls))
        with self._get_connection() as conn:
            cur = conn.execute(
                f"DELETE FROM entity_relations WHERE source_url IN ({ph})", urls
            )
            return cur.rowcount

    # =========================================================================
    # Highlight Operations
    # =========================================================================

    def save_highlight(self, text: str, url: str, title: str = "") -> int:
        """Save a user highlight. Returns the new highlight id."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO highlights (text, url, title, created_at) VALUES (?, ?, ?, ?)",
                (text, url, title, now),
            )
            return cursor.lastrowid

    def get_highlights(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Return highlights ordered newest-first."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM highlights ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    # =========================================================================
    # Annotations
    # =========================================================================

    def save_annotation(self, url: str, note: str) -> int:
        """Save or update a user note for a page. Returns annotation id."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            existing = conn.execute(
                "SELECT id FROM annotations WHERE url = ?", (url,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE annotations SET note = ?, updated_at = ? WHERE url = ?",
                    (note, now, url),
                )
                return existing["id"]
            cursor = conn.execute(
                "INSERT INTO annotations (url, note, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (url, note, now, now),
            )
            return cursor.lastrowid

    def get_annotation(self, url: str) -> Optional[Dict[str, Any]]:
        """Return annotation for a specific URL, or None."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM annotations WHERE url = ?", (url,)
            ).fetchone()
            return dict(row) if row else None

    def get_all_annotations(self) -> List[Dict[str, Any]]:
        """Return all annotations ordered newest-first."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM annotations ORDER BY updated_at DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_annotation(self, url: str) -> bool:
        """Delete annotation for a URL. Returns True if deleted."""
        with self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM annotations WHERE url = ?", (url,))
            return cursor.rowcount > 0

    def export_all(self) -> Dict[str, Any]:
        """Export all pages, highlights, and stats as a dict."""
        pages = self.get_all_pages()
        highlights = self.get_highlights(limit=10000)
        stats = self.get_stats()
        return {"pages": pages, "highlights": highlights, "stats": stats}
