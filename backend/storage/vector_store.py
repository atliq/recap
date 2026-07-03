"""
RECAP v2 - LanceDB Vector Store

Manages dense vector storage for semantic search.
Uses LanceDB (serverless, disk-backed) with sentence-transformers embeddings.
Supports incremental upsert and batch operations.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

import lancedb
import pyarrow as pa

logger = logging.getLogger(__name__)


class VectorStore:
    """LanceDB vector store for semantic search over content chunks."""

    TABLE_NAME = "chunks"

    # Schema: chunk_id (join key) + vector only. Text and all page metadata live
    # in SQLite (the single source of truth); this table is a pure ANN index over
    # chunk_ids, so no content is duplicated here.
    SCHEMA = pa.schema([
        pa.field("chunk_id", pa.string()),
    ])

    def __init__(self, db_path: Path, embedding_dim: int = 768):
        """
        Initialize the vector store.

        Args:
            db_path: Directory path for LanceDB storage.
            embedding_dim: Dimension of embedding vectors (must match model).
        """
        self.db_path = db_path
        self.embedding_dim = embedding_dim
        self.db_path.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(self.db_path))
        self._table = None
        self._ensure_table()

    def _get_full_schema(self) -> pa.Schema:
        """Build schema with the vector field included."""
        fields = list(self.SCHEMA)
        fields.append(
            pa.field("vector", pa.list_(pa.float32(), self.embedding_dim))
        )
        return pa.schema(fields)

    def _ensure_table(self) -> None:
        """Open the existing table, or create it only when genuinely absent.

        We check table_names() rather than catching open errors so that a
        transient open failure propagates instead of silently overwriting
        (and wiping) an existing index.
        """
        if self.TABLE_NAME in self._db.table_names():
            self._table = self._db.open_table(self.TABLE_NAME)
            logger.info(
                "Opened existing vector table '%s' with %d rows",
                self.TABLE_NAME,
                self._table.count_rows(),
            )
        else:
            # Table genuinely absent - create it fresh
            schema = self._get_full_schema()
            self._table = self._db.create_table(
                self.TABLE_NAME, schema=schema, mode="create"
            )
            logger.info("Created new vector table '%s'", self.TABLE_NAME)

    # =========================================================================
    # Core Operations
    # =========================================================================

    def add_chunks(
        self,
        chunk_ids: List[str],
        vectors: List[List[float]],
    ) -> int:
        """
        Add chunk vectors to the store, keyed by chunk_id. If a chunk_id already
        exists it is replaced. Text and page metadata are NOT stored here - they
        live in SQLite and are joined back by chunk_id at query time.

        Args:
            chunk_ids: Unique identifiers for each chunk.
            vectors: Embedding vectors (must match embedding_dim).

        Returns:
            Number of vectors added.
        """
        if not chunk_ids:
            return 0

        assert len(chunk_ids) == len(vectors), (
            f"Length mismatch: {len(chunk_ids)} ids, {len(vectors)} vectors"
        )

        for i, vec in enumerate(vectors):
            if len(vec) != self.embedding_dim:
                raise ValueError(
                    f"Vector {i} has dimension {len(vec)}, expected {self.embedding_dim}"
                )

        # Replace any existing rows with the same IDs (idempotent upsert)
        self.delete_chunks(chunk_ids)

        data = [
            {"chunk_id": chunk_ids[i], "vector": vectors[i]}
            for i in range(len(chunk_ids))
        ]
        self._table.add(data)
        logger.debug("Added %d vectors to store", len(data))
        return len(data)

    def search(
        self,
        query_vector: List[float],
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        ANN search. Returns [{chunk_id, score}] ordered by similarity (desc).
        Text and metadata are hydrated from SQLite by the retriever using chunk_id.
        """
        if self._table.count_rows() == 0:
            return []

        try:
            results = (
                self._table.search(query_vector)
                .limit(limit)
                .metric("cosine")
                .to_list()
            )
        except Exception as e:
            logger.error("Vector search failed: %s", e)
            return []

        # LanceDB returns _distance (lower = better for cosine) → similarity.
        formatted = []
        for row in results:
            distance = row.get("_distance", 0.0)
            similarity = max(0.0, 1.0 - distance)
            formatted.append({"chunk_id": row["chunk_id"], "score": similarity})
        return formatted

    def delete_chunks(self, chunk_ids: List[str]) -> int:
        """Delete chunks by their IDs in a single predicate. Returns number removed.

        Deletion by URL is intentionally not offered here: the vector store no
        longer holds URLs. Callers resolve a page's chunk_ids from SQLite (the
        source of truth) and pass them in.
        """
        if not chunk_ids or self._table.count_rows() == 0:
            return 0

        before = self._table.count_rows()
        # chunk_ids are md5-hex (safe); quote-escape defensively regardless.
        id_list = ",".join('"' + c.replace('"', '""') + '"' for c in chunk_ids)
        try:
            self._table.delete(f"chunk_id IN ({id_list})")
        except Exception as e:
            logger.error("Failed to delete chunks: %s", e)
            return 0
        removed = before - self._table.count_rows()
        if removed:
            logger.debug("Deleted %d vectors from store", removed)
        return removed

    def count(self) -> int:
        """Get total number of chunks in the store."""
        return self._table.count_rows()

    def clear(self) -> None:
        """Delete all data from the vector store."""
        self._db.drop_table(self.TABLE_NAME, ignore_missing=True)
        self._ensure_table()
        logger.info("Vector store cleared")
