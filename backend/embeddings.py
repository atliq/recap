"""
RECAP - Pluggable embedding backend.

Embeddings are a STRUCTURAL choice (they define the vector index), unlike the LLM
which is swappable per query. This module builds the embedding function from
settings and returns its true dimension (derived from the model, never guessed).

Two providers, same OpenAI-compatible philosophy as the LLM layer:
  - "local"  -> sentence-transformers on-device (100% private, the default)
  - anything else -> an OpenAI-compatible /v1/embeddings endpoint
                     (OpenAI, or a self-hosted server like Ollama/vLLM via base_url)

PRIVACY NOTE: a *remote* embedding provider receives the full text of every page
as it is indexed (embeddings run at ingest over everything), which is a bigger
exposure than the LLM (query-time only). Local is the private default.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Tuple

logger = logging.getLogger(__name__)

EmbeddingFn = Callable[[List[str]], List[List[float]]]


def query_instruction_for(provider: str, model_name: str) -> str:
    """Query-side instruction for instruction-tuned retrieval models.

    bge-*-en(-v1.5) models were trained to prefix the QUERY (not the documents)
    with this instruction; omitting it measurably degrades retrieval quality.
    Documents are never prefixed. Returns "" for models that don't use one.
    """
    name = (model_name or "").lower()
    if "bge" in name and "-en" in name:
        return "Represent this sentence for searching relevant passages: "
    return ""

# OpenAI-compatible embedding endpoints (base URLs). "local" is handled separately.
EMBEDDING_PRESETS = {
    "openai": "https://api.openai.com/v1",
    "ollama": "http://localhost:11434/v1",
}


def _is_loopback(base_url: str) -> bool:
    """True if the endpoint stays on this machine (no privacy warning needed)."""
    host = (base_url or "").lower()
    return "127.0.0.1" in host or "localhost" in host or "://[::1]" in host


def build_embedding_fn(settings) -> Tuple[EmbeddingFn, int, str]:
    """
    Build the embedding function from settings.

    Returns (embed_fn, dimension, fingerprint). The fingerprint uniquely
    identifies the embedding space; when it changes, the vector index must be
    rebuilt (see IngestionProcessor.reindex_all).
    """
    provider = (getattr(settings, "embedding_provider", "local") or "local").lower()
    model_name = settings.embedding_model

    if provider == "local":
        from sentence_transformers import SentenceTransformer

        logger.info("Loading local embedding model: %s", model_name)
        model = SentenceTransformer(model_name)
        dim = int(model.get_sentence_embedding_dimension())

        def embed(texts: List[str]) -> List[List[float]]:
            return model.encode(texts, normalize_embeddings=True).tolist()

        fingerprint = f"local:{model_name}:{dim}"
        logger.info("Local embeddings ready (%s, dim=%d)", model_name, dim)
        return embed, dim, fingerprint

    # --- Remote / OpenAI-compatible embeddings ---
    base_url = settings.embedding_base_url or EMBEDDING_PRESETS.get(provider)
    if not base_url:
        raise ValueError(
            f"embedding_provider '{provider}' has no base URL. Set embedding_base_url "
            f"for a custom OpenAI-compatible embeddings endpoint."
        )
    api_key = settings.embedding_api_key or "not-needed"
    if not _is_loopback(base_url):
        logger.warning(
            "Embeddings use a REMOTE endpoint (%s): the full text of every indexed "
            "page will be sent there. Use a local model for a fully-private index.",
            base_url,
        )

    from openai import OpenAI

    client = OpenAI(base_url=base_url, api_key=api_key, timeout=60)

    def embed(texts: List[str]) -> List[List[float]]:
        resp = client.embeddings.create(model=model_name, input=texts)
        return [d.embedding for d in resp.data]

    # Derive dimension from the provider (probe once) rather than trusting config.
    dim = len(embed(["dimension probe"])[0])
    fingerprint = f"api:{base_url}:{model_name}:{dim}"
    logger.info("Remote embeddings ready (%s @ %s, dim=%d)", model_name, base_url, dim)
    return embed, dim, fingerprint
