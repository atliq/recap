"""
RECAP v2 - Configuration Management

Centralized configuration using pydantic-settings.
All secrets loaded from .env file, never hardcoded.
"""

from __future__ import annotations

from pathlib import Path
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings
from pydantic import Field, field_validator


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    # -------------------------------------------------------------------------
    # LLM Provider API Keys
    # -------------------------------------------------------------------------
    # Any provider that exposes an OpenAI-compatible endpoint works. The base
    # URLs (presets) live in backend/retrieval/answer_generator.py - here you
    # only supply an API key. For self-hosted / other OpenAI-compatible servers
    # (Ollama, vLLM, LM Studio, ...), use the "custom" triple below.
    groq_api_key: Optional[str] = Field(default=None, description="Groq API key")
    openai_api_key: Optional[str] = Field(default=None, description="OpenAI API key")
    anthropic_api_key: Optional[str] = Field(default=None, description="Anthropic API key (OpenAI-compat endpoint)")
    google_api_key: Optional[str] = Field(default=None, description="Google Gemini API key (OpenAI-compat endpoint)")
    openrouter_api_key: Optional[str] = Field(default=None, description="OpenRouter API key (gateway to most models)")

    # Custom / self-hosted OpenAI-compatible endpoint
    llm_base_url: Optional[str] = Field(default=None, description="Base URL for a custom OpenAI-compatible provider")
    llm_api_key: Optional[str] = Field(default=None, description="API key for the custom provider (blank for local servers)")
    llm_model: Optional[str] = Field(default=None, description="Model id override (applies to any provider)")
    default_provider: Optional[str] = Field(default=None, description="Preferred provider chosen at onboarding; overrides key-based auto-detection")

    # -------------------------------------------------------------------------
    # Embedding Configuration
    # -------------------------------------------------------------------------
    # Embeddings are a STRUCTURAL choice (they define the vector index) - set once
    # at onboarding, then effectively read-only (changing them requires a re-index).
    # "local" runs on-device (private default); any other value uses an
    # OpenAI-compatible /v1/embeddings endpoint (see backend/embeddings.py).
    embedding_provider: str = Field(
        default="local",
        description="'local' (on-device) or an OpenAI-compatible provider (openai/ollama/custom)",
    )
    embedding_model: str = Field(
        default="BAAI/bge-base-en-v1.5",
        description="Embedding model id (HF id for local, or the provider's model name)",
    )
    embedding_base_url: Optional[str] = Field(
        default=None,
        description="Base URL for a remote/custom OpenAI-compatible embeddings endpoint",
    )
    embedding_api_key: Optional[str] = Field(
        default=None,
        description="API key for a remote embeddings provider (blank for local servers)",
    )
    embedding_dimension: int = Field(
        default=768,
        description="Fallback/hint only - the true dimension is derived from the model at load",
    )

    # -------------------------------------------------------------------------
    # Server Configuration
    # -------------------------------------------------------------------------
    host: str = Field(default="127.0.0.1", description="Server bind host (binds loopback only by default; set to 0.0.0.0 to expose on the network)")
    port: int = Field(default=8000, description="Server bind port")
    log_level: str = Field(default="info", description="Logging level")

    # -------------------------------------------------------------------------
    # Storage Paths
    # -------------------------------------------------------------------------
    data_dir: str = Field(default="./data", description="Base data directory")
    db_filename: str = Field(default="recap.db", description="SQLite database filename")
    vector_store_dir: str = Field(default="vector_store", description="LanceDB directory")
    kg_dir: str = Field(default="knowledge_graph", description="Knowledge graph directory")

    # -------------------------------------------------------------------------
    # RAG Configuration
    # -------------------------------------------------------------------------
    min_content_quality: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Minimum content quality score to index",
    )
    max_chunk_tokens: int = Field(
        default=512,
        ge=64,
        description="Maximum tokens per semantic chunk",
    )
    min_chunk_tokens: int = Field(
        default=50,
        ge=10,
        description="Minimum tokens per semantic chunk",
    )
    retrieval_top_k: int = Field(
        default=10,
        ge=1,
        description="Number of results per retrieval method before fusion",
    )
    recency_decay: float = Field(
        default=0.98,
        ge=0.0,
        le=1.0,
        description="Per-day multiplicative decay on fused scores (~35d half-life). "
                    "Recently-visited pages rank higher; set 1.0 to disable.",
    )
    rerank_top_k: int = Field(
        default=5,
        ge=1,
        description="Final results after re-ranking",
    )
    rerank_model: str = Field(
        default="cross-encoder/ms-marco-MiniLM-L-6-v2",
        description="Cross-encoder model for re-ranking",
    )
    resurface_min_similarity: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        description="Minimum cosine similarity (bounded 0-1) between the current page and a "
                    "candidate for a live Resurface card. Precision-first: below this, no card "
                    "is shown. Raise toward 1.0 for fewer, more confident nudges. Note: this is "
                    "a single global floor (see docs/RAG_BACKLOG.md - per-corpus tuning).",
    )

    # -------------------------------------------------------------------------
    # Content Processing
    # -------------------------------------------------------------------------
    min_visit_duration: int = Field(
        default=10,
        ge=0,
        description="Minimum visit duration (seconds) to index a page",
    )
    retention_days: int = Field(
        default=120,
        ge=0,
        description="Delete pages not visited within this many days (0 = keep forever). Default ~4 months.",
    )
    enable_kg: bool = Field(
        default=False,
        description="Master switch for the knowledge graph: gates entity extraction "
                    "at ingestion AND the KG retrieval leg. Off by default (BM25 + "
                    "dense only); after enabling, backfill already-indexed pages "
                    "via POST /maintenance/rebuild_kg.",
    )
    semantic_gate_enabled: bool = Field(
        default=True,
        description="Embedding-prototype gate that skips login/account/checkout-like pages the URL rules miss",
    )
    semantic_gate_margin: float = Field(
        default=0.02,
        ge=0.0,
        le=0.5,
        description="How much closer to junk than content prototypes a page must be to be skipped",
    )
    spacy_model: str = Field(
        default="en_core_web_sm",
        description="SpaCy model for NLP",
    )

    # -------------------------------------------------------------------------
    # Computed Properties
    # -------------------------------------------------------------------------
    @property
    def data_path(self) -> Path:
        """Absolute path to the data directory."""
        return Path(self.data_dir).resolve()

    @property
    def db_path(self) -> Path:
        """Absolute path to the SQLite database."""
        return self.data_path / self.db_filename

    @property
    def vector_store_path(self) -> Path:
        """Absolute path to LanceDB storage."""
        return self.data_path / self.vector_store_dir

    @property
    def kg_path(self) -> Path:
        """Absolute path to knowledge graph storage."""
        return self.data_path / self.kg_dir

    @field_validator("min_content_quality")
    @classmethod
    def validate_quality_score(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("min_content_quality must be between 0.0 and 1.0")
        return v

    def get_api_key(self, provider: str) -> Optional[str]:
        """Get API key for a specific LLM provider."""
        key_map = {
            "groq": self.groq_api_key,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "google": self.google_api_key,
            "openrouter": self.openrouter_api_key,
            "ollama": "local",  # no API key required for local Ollama server
            "custom": self.llm_api_key,
        }
        return key_map.get(provider.lower())

    def get_default_provider(self) -> str:
        """Return the preferred provider (if set), else the first with credentials."""
        if self.default_provider:
            return self.default_provider.lower()
        for provider, key in [
            ("groq", self.groq_api_key),
            ("openai", self.openai_api_key),
            ("openrouter", self.openrouter_api_key),
            ("google", self.google_api_key),
            ("anthropic", self.anthropic_api_key),
            ("custom", self.llm_base_url),  # local servers may need no key
        ]:
            if key:
                return provider
        return "groq"  # fallback

    def ensure_directories(self) -> None:
        """Create all required data directories if they don't exist."""
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.vector_store_path.mkdir(parents=True, exist_ok=True)
        self.kg_path.mkdir(parents=True, exist_ok=True)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Get cached application settings.

    Returns a singleton Settings instance. Call with get_settings.cache_clear()
    to reload settings from .env.
    """
    return Settings()
