"""
RECAP v2 - Semantic Chunker

Splits content into semantically coherent chunks by detecting
natural topic boundaries. Uses sentence embeddings to find
breakpoints where adjacent sentences diverge in meaning.

Algorithm:
1. Split text into sentences (spaCy or regex fallback)
2. Group sentences into windows
3. Compute embeddings for consecutive windows
4. Measure cosine similarity between adjacent windows
5. Identify breakpoints where similarity drops
6. Merge small chunks, split oversized chunks
7. Prepend document context (title + URL) to each chunk
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import List, Optional, Tuple

import numpy as np

from backend.models import ChunkData

logger = logging.getLogger(__name__)


# =============================================================================
# Sentence Splitting
# =============================================================================

# Regex-based sentence splitter (fallback when spaCy is not available)
SENTENCE_SPLIT_RE = re.compile(
    r'(?<=[.!?])\s+(?=[A-Z])'  # Split after punctuation followed by capital letter
    r'|(?<=\n)\s*(?=\S)'       # Split on newlines
)


def _split_sentences_regex(text: str) -> List[str]:
    """Split text into sentences using regex (fallback)."""
    sentences = SENTENCE_SPLIT_RE.split(text)
    return [s.strip() for s in sentences if s.strip() and len(s.strip()) > 10]


def _split_sentences_spacy(text: str, nlp) -> List[str]:
    """Split text into sentences using spaCy."""
    # Process in chunks to handle large texts
    max_length = 100_000
    if len(text) > max_length:
        # Split into processable chunks at newlines
        parts = []
        remaining = text
        while remaining:
            if len(remaining) <= max_length:
                parts.append(remaining)
                break
            # Find a good split point
            split_at = remaining.rfind('\n', 0, max_length)
            if split_at == -1:
                split_at = remaining.rfind('. ', 0, max_length)
            if split_at == -1:
                split_at = max_length
            parts.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip()

        sentences = []
        for part in parts:
            doc = nlp(part)
            sentences.extend([sent.text.strip() for sent in doc.sents
                            if sent.text.strip() and len(sent.text.strip()) > 10])
        return sentences

    doc = nlp(text)
    return [sent.text.strip() for sent in doc.sents
            if sent.text.strip() and len(sent.text.strip()) > 10]


def split_sentences(text: str, nlp=None) -> List[str]:
    """
    Split text into sentences.
    Uses spaCy if available, falls back to regex.
    """
    if nlp:
        try:
            return _split_sentences_spacy(text, nlp)
        except Exception as e:
            logger.warning("spaCy sentence splitting failed: %s, falling back to regex", e)

    return _split_sentences_regex(text)


# =============================================================================
# Semantic Chunking
# =============================================================================


class SemanticChunker:
    """
    Chunks text by detecting semantic boundaries between sentences.

    Uses embedding similarity between consecutive sentence windows
    to find natural topic transitions.
    """

    def __init__(
        self,
        embedding_fn,
        max_chunk_tokens: int = 512,
        min_chunk_tokens: int = 50,
        similarity_threshold: float = 0.5,
        window_size: int = 3,
        nlp=None,
    ):
        """
        Args:
            embedding_fn: Function that takes a list of strings and returns embeddings.
            max_chunk_tokens: Maximum tokens per chunk.
            min_chunk_tokens: Minimum tokens per chunk.
            similarity_threshold: Cosine similarity threshold for breakpoints.
                                 Lower = more splits, higher = fewer splits.
            window_size: Number of sentences to combine per window for comparison.
            nlp: spaCy language model (optional, for sentence splitting).
        """
        self.embedding_fn = embedding_fn
        self.max_chunk_tokens = max_chunk_tokens
        self.min_chunk_tokens = min_chunk_tokens
        self.similarity_threshold = similarity_threshold
        self.window_size = window_size
        self.nlp = nlp

        # We count "tokens" by whitespace words, but real subword tokenizers
        # (used by the embedding model) emit MORE tokens than words. To keep a
        # chunk within the model's token budget we cap the effective word count
        # at a conservative fraction of max_chunk_tokens (~0.75 words/token).
        self._words_per_token = 0.75
        self._max_chunk_words = max(1, int(max_chunk_tokens * self._words_per_token))

    def chunk(
        self,
        text: str,
        page_url: str = "",
        page_title: str = "",
    ) -> List[ChunkData]:
        """
        Split text into semantically coherent chunks.

        Args:
            text: The full text to chunk.
            page_url: Source URL for metadata.
            page_title: Source title for context prefix.

        Returns:
            List of ChunkData objects.
        """
        if not text or not text.strip():
            return []

        # Step 1: Split into sentences
        sentences = split_sentences(text, self.nlp)
        if not sentences:
            return []

        # If very short text, return as a single chunk
        total_words = sum(len(s.split()) for s in sentences)
        if total_words <= self._max_chunk_words or len(sentences) <= 3:
            return self._create_single_chunk(
                " ".join(sentences), page_url, page_title, 0
            )

        # Step 2: Find semantic breakpoints
        breakpoints = self._find_breakpoints(sentences)

        # Step 3: Create chunks from breakpoint segments
        chunks = self._segments_to_chunks(
            sentences, breakpoints, page_url, page_title
        )

        logger.debug(
            "Chunked %d sentences into %d chunks (from %s)",
            len(sentences), len(chunks), page_url,
        )
        return chunks

    def _find_breakpoints(self, sentences: List[str]) -> List[int]:
        """
        Find sentence indices where topic transitions occur.

        Creates sliding windows of sentences, embeds them,
        and detects drops in similarity.
        """
        if len(sentences) <= self.window_size * 2:
            # Not enough sentences for meaningful windowed comparison
            return []

        # Create text windows
        windows = []
        for i in range(len(sentences) - self.window_size + 1):
            window_text = " ".join(sentences[i:i + self.window_size])
            windows.append(window_text)

        if len(windows) < 2:
            return []

        # Embed all windows
        try:
            embeddings = self.embedding_fn(windows)
            if isinstance(embeddings, list):
                embeddings = np.array(embeddings)
        except Exception as e:
            logger.warning("Embedding failed during chunking: %s", e)
            return self._fallback_breakpoints(sentences)

        # Compute cosine similarities between consecutive windows
        similarities = []
        for i in range(len(embeddings) - 1):
            sim = self._cosine_similarity(embeddings[i], embeddings[i + 1])
            similarities.append(sim)

        if not similarities:
            return []

        # Find breakpoints where similarity drops below threshold
        # Use adaptive threshold: percentile of the distribution
        mean_sim = np.mean(similarities)
        std_sim = np.std(similarities)
        adaptive_threshold = max(
            self.similarity_threshold,
            mean_sim - std_sim  # One std below mean
        )

        breakpoints = []
        for i, sim in enumerate(similarities):
            if sim < adaptive_threshold:
                # The breakpoint is at the end of the current window
                bp_idx = i + self.window_size
                if bp_idx < len(sentences):
                    breakpoints.append(bp_idx)

        return breakpoints

    def _fallback_breakpoints(self, sentences: List[str]) -> List[int]:
        """
        Fallback: create breakpoints at regular intervals when embedding fails.
        """
        target_size = self._max_chunk_words
        breakpoints = []
        current_words = 0

        for i, sent in enumerate(sentences):
            current_words += len(sent.split())
            if current_words >= target_size:
                breakpoints.append(i + 1)
                current_words = 0

        return breakpoints

    def _segments_to_chunks(
        self,
        sentences: List[str],
        breakpoints: List[int],
        page_url: str,
        page_title: str,
    ) -> List[ChunkData]:
        """Convert sentence segments (between breakpoints) into ChunkData objects."""
        # Create segments from breakpoints
        all_points = [0] + sorted(set(breakpoints)) + [len(sentences)]
        segments = []
        for i in range(len(all_points) - 1):
            start, end = all_points[i], all_points[i + 1]
            segment = sentences[start:end]
            if segment:
                segments.append(segment)

        # Merge very small segments with neighbors
        merged = self._merge_small_segments(segments)

        # Split oversized segments
        final = []
        for seg in merged:
            text = " ".join(seg)
            word_count = len(text.split())
            if word_count > self._max_chunk_words * 1.5:
                # Split oversized chunk
                final.extend(self._split_oversized(seg))
            else:
                final.append(seg)

        # Create ChunkData objects
        context_prefix = ""
        if page_title:
            context_prefix = f"[{page_title}]"
            if page_url:
                context_prefix += f" ({page_url})"

        chunks = []
        for i, segment in enumerate(final):
            text = " ".join(segment)
            if len(text.split()) < self.min_chunk_tokens and len(final) > 1:
                continue  # Skip very short chunks (unless it's the only one)

            chunk_id = self._make_chunk_id(page_url, i)
            chunks.append(ChunkData(
                chunk_id=chunk_id,
                page_url=page_url,
                page_title=page_title,
                text=text,
                chunk_index=i,
                token_count=len(text.split()),
                context_prefix=context_prefix,
            ))

        return chunks if chunks else self._create_single_chunk(
            " ".join(sentences), page_url, page_title, 0
        )

    def _merge_small_segments(
        self, segments: List[List[str]]
    ) -> List[List[str]]:
        """Merge segments that are too small with their neighbors."""
        if len(segments) <= 1:
            return segments

        merged = []
        buffer = []

        for seg in segments:
            buffer.extend(seg)
            word_count = sum(len(s.split()) for s in buffer)
            if word_count >= self.min_chunk_tokens:
                merged.append(buffer)
                buffer = []

        # Append remaining buffer
        if buffer:
            if merged:
                merged[-1].extend(buffer)
            else:
                merged.append(buffer)

        return merged

    def _split_oversized(self, sentences: List[str]) -> List[List[str]]:
        """Split an oversized segment into smaller pieces."""
        result = []
        current = []
        current_words = 0

        for sent in sentences:
            sent_words = len(sent.split())
            if current_words + sent_words > self._max_chunk_words and current:
                result.append(current)
                current = []
                current_words = 0
            current.append(sent)
            current_words += sent_words

        if current:
            result.append(current)

        return result

    def _create_single_chunk(
        self, text: str, page_url: str, page_title: str, index: int
    ) -> List[ChunkData]:
        """Create a single chunk from the entire text."""
        context_prefix = ""
        if page_title:
            context_prefix = f"[{page_title}]"
            if page_url:
                context_prefix += f" ({page_url})"

        chunk_id = self._make_chunk_id(page_url, index)
        return [ChunkData(
            chunk_id=chunk_id,
            page_url=page_url,
            page_title=page_title,
            text=text.strip(),
            chunk_index=index,
            token_count=len(text.split()),
            context_prefix=context_prefix,
        )]

    @staticmethod
    def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        """Compute cosine similarity between two vectors."""
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    @staticmethod
    def _make_chunk_id(page_url: str, index: int) -> str:
        """Generate a deterministic chunk ID."""
        raw = f"{page_url}::chunk_{index}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]
