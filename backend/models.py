"""
RECAP v2 - Pydantic Models

Request/response models for the FastAPI endpoints.
All data contracts defined in one place.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum

from pydantic import BaseModel, Field, field_validator


def _validate_iso8601(v: Optional[str]) -> Optional[str]:
    """Validate an optional ISO-8601 date/datetime string. Allows None/empty."""
    if v is None or v == "":
        return v
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise ValueError("must be an ISO-8601 date/datetime string")
    return v


# =============================================================================
# Enums
# =============================================================================


class ContentType(str, Enum):
    """Classification of page content type."""
    ARTICLE = "article"
    DOCUMENTATION = "documentation"
    BLOG = "blog"
    FORUM = "forum"
    REFERENCE = "reference"
    NEWS = "news"
    SOCIAL = "social"
    COMMERCIAL = "commercial"
    OTHER = "other"
    SKIP = "skip"


class LLMProvider(str, Enum):
    """Known OpenAI-compatible providers (presets). `custom` uses llm_base_url.

    Requests accept `llm` as a free string so any provider name works; this enum
    documents the built-in presets in backend/retrieval/answer_generator.py.
    """
    GROQ = "groq"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    OPENROUTER = "openrouter"
    OLLAMA = "ollama"
    CUSTOM = "custom"


# =============================================================================
# Request Models
# =============================================================================


class PageData(BaseModel):
    """Incoming page data from the Chrome extension."""
    url: str = Field(..., max_length=8192, description="Full URL of the page")
    title: str = Field(default="", max_length=2048, description="Page title")
    content: str = Field(default="", max_length=1_000_000, description="Extracted page text content")
    html: str = Field(default="", max_length=2_000_000, description="Raw HTML for server-side extraction fallback")
    meta_description: str = Field(default="", max_length=8192, description="Meta description tag content")
    meta_author: str = Field(default="", max_length=2048, description="Author from meta tags")
    visit_duration: float = Field(default=0.0, ge=0, description="Time spent on page in seconds")
    timestamp: Optional[str] = Field(default=None, description="ISO timestamp of visit")
    word_count: int = Field(default=0, ge=0, description="Word count from extension")
    text_to_tag_ratio: float = Field(default=0.0, ge=0, description="Text-to-HTML tag ratio")

    @property
    def domain(self) -> str:
        """Extract domain from URL."""
        from urllib.parse import urlparse
        parsed = urlparse(self.url)
        return parsed.netloc

    @property
    def path(self) -> str:
        """Extract path from URL."""
        from urllib.parse import urlparse
        parsed = urlparse(self.url)
        return parsed.path


class QueryRequest(BaseModel):
    """Search query from the user."""
    query: str = Field(..., min_length=1, max_length=2000, description="User's natural language query")
    top_k: int = Field(default=5, ge=1, le=50, description="Number of results to return")
    llm: str = Field(default="groq", description="LLM provider to use for answer generation")
    model: Optional[str] = Field(default=None, description="Specific model ID (overrides provider default)")
    use_kg: bool = Field(default=True, description="Include knowledge graph in retrieval")
    date_from: Optional[str] = Field(default=None, description="Filter to pages visited on/after this ISO timestamp")
    date_to: Optional[str] = Field(default=None, description="Filter to pages visited on/before this ISO timestamp")

    @field_validator("date_from", "date_to")
    @classmethod
    def _check_iso(cls, v: Optional[str]) -> Optional[str]:
        return _validate_iso8601(v)


class UpdateAPIKeysRequest(BaseModel):
    """Request to update LLM provider credentials at runtime."""
    groq_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    google_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    # Custom / self-hosted OpenAI-compatible provider
    llm_base_url: Optional[str] = Field(default=None, max_length=2048)
    llm_api_key: Optional[str] = None
    llm_model: Optional[str] = Field(default=None, max_length=256)
    default_provider: Optional[str] = Field(default=None, max_length=32)


class KGToggleRequest(BaseModel):
    """Request to flip the knowledge-graph master switch at runtime.

    Sent by the extension Options page. Gates both ingestion NER and the KG
    retrieval leg; persisted in DB meta so it survives backend restarts and
    overrides the .env default from then on.
    """
    enabled: bool


class TestLLMRequest(BaseModel):
    """Request to ping the currently-configured LLM with a trivial prompt.

    Both fields are optional; when omitted the backend falls back to the active
    provider and model in Settings. Credentials must already be set (via
    /update_api_keys or .env) - this endpoint only verifies the round-trip.
    """
    provider: Optional[str] = Field(default=None, max_length=32)
    model: Optional[str] = Field(default=None, max_length=256)


class DeleteURLRequest(BaseModel):
    """Request to delete a specific URL from the index."""
    url: str = Field(..., description="URL to delete")


class DeleteDomainRequest(BaseModel):
    """Request to delete every indexed page belonging to a domain."""
    domain: str = Field(..., max_length=253, description="Domain (or parent domain) to delete")


class RelatedRequest(BaseModel):
    """Request for pages related to the one currently being viewed (Resurface)."""
    url: str = Field(..., max_length=8192, description="URL of the current page")
    content: str = Field(default="", max_length=8192, description="Snippet of the current page's text")
    title: str = Field(default="", max_length=2048, description="Current page title (strong topic signal)")


# =============================================================================
# Response Models
# =============================================================================


class ProcessingResult(BaseModel):
    """Result of processing a page."""
    url: str
    status: str = Field(description="'indexed', 'skipped', 'updated', or 'error'")
    content_type: ContentType = ContentType.OTHER
    quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    chunks_created: int = Field(default=0, ge=0)
    entities_extracted: int = Field(default=0, ge=0)
    message: str = Field(default="")


class SearchResult(BaseModel):
    """A single search result."""
    url: str
    title: str
    snippet: str = Field(description="Relevant text snippet from the chunk")
    score: float = Field(description="Relevance score (0-1)")
    source: str = Field(default="hybrid", description="Which retrieval method found this")
    timestamp: Optional[str] = Field(default=None, description="When the page was indexed")


class QueryResult(BaseModel):
    """Full query response with answer and sources."""
    query: str
    answer: str = Field(description="LLM-generated answer with citations")
    results: List[SearchResult] = Field(default_factory=list)
    sources_used: int = Field(default=0)
    retrieval_time_ms: float = Field(default=0.0)
    generation_time_ms: float = Field(default=0.0)
    provider: str = Field(default="", description="LLM provider actually used for the answer")
    model: str = Field(default="", description="Model id actually used for the answer")


class StatsResponse(BaseModel):
    """System statistics."""
    total_pages: int = 0
    total_chunks: int = 0
    total_entities: int = 0
    total_relations: int = 0
    index_size_mb: float = 0.0
    last_indexed: Optional[str] = None
    content_type_distribution: Dict[str, int] = Field(default_factory=dict)
    top_domains: List[Dict[str, Any]] = Field(default_factory=list)
    top_skipped_domains: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Domains most often rejected by the sensitivity/quality gates (ignore-domain candidates)",
    )


class ReferenceItem(BaseModel):
    """A single indexed reference."""
    url: str
    title: str
    content_type: str = ""
    indexed_at: str = ""
    visit_count: int = 1
    chunk_count: int = 0
    summary: str = Field(default="", description="Auto-summary or meta description")


class ReferencesResponse(BaseModel):
    """List of all indexed references."""
    references: List[ReferenceItem] = Field(default_factory=list)
    total: int = 0


class ChatMessage(BaseModel):
    """A single turn in a conversation."""
    role: str = Field(..., description="'user' or 'assistant'")
    content: str = Field(..., max_length=8192, description="Message content")
    sources: List[Dict[str, Any]] = Field(default_factory=list, description="Sources cited in this turn")


class ChatRequest(BaseModel):
    """Multi-turn chat request."""
    message: str = Field(..., min_length=1, max_length=4000, description="Current user message")
    history: List[ChatMessage] = Field(default_factory=list, max_length=50, description="Prior conversation turns")
    top_k: int = Field(default=5, ge=1, le=20, description="Sources to retrieve per turn")
    llm: str = Field(default="groq", description="LLM provider")
    model: Optional[str] = Field(default=None, description="Specific model override")
    date_from: Optional[str] = Field(default=None, description="Filter sources to pages visited on/after this ISO timestamp")
    date_to: Optional[str] = Field(default=None, description="Filter sources to pages visited on/before this ISO timestamp")

    @field_validator("date_from", "date_to")
    @classmethod
    def _check_iso(cls, v: Optional[str]) -> Optional[str]:
        return _validate_iso8601(v)


class ChatResult(BaseModel):
    """Response from the chat endpoint."""
    message: str = Field(description="Assistant reply")
    sources: List[SearchResult] = Field(default_factory=list)
    retrieval_time_ms: float = Field(default=0.0)
    generation_time_ms: float = Field(default=0.0)
    provider: str = Field(default="", description="LLM provider actually used for the reply")
    model: str = Field(default="", description="Model id actually used for the reply")


class AnnotationRequest(BaseModel):
    """Request to save a note on an indexed page."""
    url: str = Field(..., max_length=8192, description="URL of the page to annotate")
    note: str = Field(..., min_length=1, max_length=20000, description="User's note text")


class SaveHighlightRequest(BaseModel):
    """Request to save a highlighted text passage."""
    text: str = Field(..., min_length=1, max_length=20000, description="The highlighted text")
    url: str = Field(..., max_length=8192, description="Source page URL")
    title: str = Field(default="", max_length=2048, description="Source page title")
    timestamp: Optional[str] = Field(default=None, description="ISO timestamp")


class HighlightItem(BaseModel):
    """A saved text highlight."""
    id: int
    text: str
    url: str
    title: str = ""
    created_at: str = ""


class ExportData(BaseModel):
    """Full data export payload."""
    pages: List[Dict[str, Any]] = Field(default_factory=list)
    highlights: List[Dict[str, Any]] = Field(default_factory=list)
    stats: Dict[str, Any] = Field(default_factory=dict)
    exported_at: str = ""


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "ok"
    version: str = "2.0.0"
    uptime_seconds: float = 0.0
    pages_indexed: int = 0
    vector_store_ready: bool = False
    database_ready: bool = False
    default_provider: str = "groq"


# =============================================================================
# Internal Types (not exposed via API, used between modules)
# =============================================================================


class ChunkData(BaseModel):
    """A processed content chunk ready for embedding."""
    chunk_id: str = Field(description="Unique chunk identifier")
    page_url: str
    page_title: str = ""
    text: str = Field(description="Chunk text content")
    chunk_index: int = Field(default=0, description="Position within the page")
    token_count: int = Field(default=0)
    context_prefix: str = Field(default="", description="Title + URL prepended for embedding")

    @property
    def embedding_text(self) -> str:
        """Text used for generating the embedding (includes context prefix)."""
        if self.context_prefix:
            return f"{self.context_prefix}\n\n{self.text}"
        return self.text


class EntityData(BaseModel):
    """An extracted entity."""
    name: str
    entity_type: str = Field(description="person, organization, technology, concept, etc.")
    source_url: str = ""
    source_chunk_id: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class RelationData(BaseModel):
    """A relationship between two entities."""
    source_entity: str
    target_entity: str
    relation_type: str = Field(default="related_to")
    weight: float = Field(default=1.0, ge=0.0)
    source_url: str = ""
