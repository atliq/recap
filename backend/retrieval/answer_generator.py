"""
RECAP v2 - Answer Generator

LLM-based answer generation over a single OpenAI-compatible client. Any provider
that speaks the OpenAI wire format works - OpenAI, Groq, OpenRouter, Google and
Anthropic (via their OpenAI-compat endpoints), and Ollama / vLLM / LM Studio or
any self-hosted server - selected by a (base_url, api_key, model) triple.
Prompt templates and the untrusted-content sanitizer live in backend/prompts.py.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from backend.config import Settings
from backend.prompts import (
    CHAT_CONTEXT_TEMPLATE,
    CHAT_NO_CONTEXT_TEMPLATE,
    CHAT_SYSTEM_PROMPT,
    KG_BLOCK_TEMPLATE,
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
    sanitize_untrusted,
)

logger = logging.getLogger(__name__)

# Hard timeout (seconds) applied to every provider call so no request hangs forever
LLM_TIMEOUT_SECONDS = 30

# OpenAI-compatible provider presets: base URL + a sensible default model.
# Users only supply an API key (and optionally a model). For anything not listed
# (self-hosted, other gateways), use provider="custom" with settings.llm_base_url.
PROVIDER_PRESETS = {
    "openai":     {"base_url": "https://api.openai.com/v1",          "default_model": "gpt-4o-mini"},
    "groq":       {"base_url": "https://api.groq.com/openai/v1",     "default_model": "llama-3.3-70b-versatile"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1",       "default_model": "openai/gpt-4o-mini"},
    "google":     {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "default_model": "gemini-2.0-flash"},
    "anthropic":  {"base_url": "https://api.anthropic.com/v1/",      "default_model": "claude-3-5-haiku-20241022"},
    "ollama":     {"base_url": "http://localhost:11434/v1",          "default_model": "gemma3:4b"},
}


class AnswerGenerator:
    """
    Generates answers using an LLM with retrieved context.
    Supports multiple providers with a unified interface.
    """

    def __init__(self, settings: Settings):
        """
        Args:
            settings: Application settings with API keys.
        """
        self.settings = settings
        # Cache clients so we don't recreate HTTP sessions on every call
        self._clients: dict = {}

    def generate(
        self,
        query: str,
        context_results: List[Dict[str, Any]],
        provider: str = "groq",
        model: Optional[str] = None,
        kg_context: str = "",
    ) -> str:
        """
        Generate an answer using the specified LLM.

        Args:
            query: User's question.
            context_results: Retrieved and re-ranked results.
            provider: LLM provider ("groq", "openai", "anthropic", "google").
            model: Specific model ID (optional, uses default per provider).
            kg_context: Knowledge graph context string.

        Returns:
            Generated answer string with citations.
        """
        if not context_results:
            return "I haven't found any relevant information in your browsing history for this query."

        # Build context from results; KG context is page-derived (untrusted),
        # so it rides in the user message's data block, never the system prompt.
        context = self._format_context(context_results)
        user_prompt = USER_PROMPT_TEMPLATE.format(
            context=context, kg_block=self._kg_block(kg_context), query=query
        )

        # Select model (falls back to the provider preset default)
        model_id = model or self._default_model(provider)

        start_time = time.time()

        try:
            answer = self._call_llm(provider, model_id, SYSTEM_PROMPT, user_prompt)
        except Exception as e:
            logger.error("LLM generation failed (%s/%s): %s", provider, model_id, e)
            # Fallback: return context summary without LLM
            return self._fallback_answer(query, context_results)

        elapsed = time.time() - start_time
        logger.info("Generated answer in %.2fs using %s/%s", elapsed, provider, model_id)

        return answer

    def generate_chat(
        self,
        message: str,
        history: List[Dict[str, Any]],
        context_results: List[Dict[str, Any]],
        provider: str = "groq",
        model: Optional[str] = None,
        kg_context: str = "",
    ) -> str:
        """
        Generate a conversational reply with full history context.

        Args:
            message: The current user message.
            history: Prior turns as [{"role": "user"|"assistant", "content": "..."}].
            context_results: Retrieved and re-ranked results for this turn.
            provider: LLM provider.
            model: Specific model ID (optional).
            kg_context: Knowledge graph context string.

        Returns:
            Assistant reply string.
        """
        # Build the current user turn - retrieved context and KG context are
        # page-derived (untrusted), so both go in the user turn's data blocks.
        if context_results:
            context = self._format_context(context_results)
            current_user_content = CHAT_CONTEXT_TEMPLATE.format(
                context=context, kg_block=self._kg_block(kg_context), message=message
            )
        else:
            current_user_content = CHAT_NO_CONTEXT_TEMPLATE.format(message=message)

        # Build full messages list: history turns + current turn. Assistant
        # turns were generated from page-derived text, so they are sanitized
        # too - a prior reply must not re-open a data tag on a later turn.
        messages = []
        for turn in history:
            role = turn.get("role", "user")
            content = turn.get("content", "")
            if role in ("user", "assistant") and content:
                if role == "assistant":
                    content = sanitize_untrusted(content)
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": current_user_content})

        model_id = model or self._default_model(provider)

        try:
            return self._call_llm_chat(provider, model_id, CHAT_SYSTEM_PROMPT, messages)
        except Exception as e:
            logger.error("Chat LLM failed (%s/%s): %s", provider, model_id, e)
            return self._fallback_answer(message, context_results)

    def ping(self, provider: str, model: Optional[str] = None) -> Dict[str, Any]:
        """Send a trivial prompt to verify the provider/model/key actually work.

        Used by the /test_llm endpoint. Raises on any failure (bad key, unknown
        model, unreachable endpoint) so the caller can surface the provider's own
        error message; a non-empty reply means the round-trip succeeded.
        """
        model_id = model or self._default_model(provider)
        reply = self._complete(
            provider, model_id, [{"role": "user", "content": "hi"}], max_tokens=16
        )
        return {"model": model_id, "reply": (reply or "")[:200]}

    def _call_llm_chat(
        self,
        provider: str,
        model_id: str,
        system: str,
        messages: List[Dict[str, str]],
    ) -> str:
        """Chat completion with a full message history (OpenAI-compatible)."""
        full = [{"role": "system", "content": system}] + messages
        return self._complete(provider, model_id, full)

    # Max chars per chunk sent to the LLM - keeps prompts tight and generation fast
    _CHUNK_CHAR_LIMIT = 600

    @staticmethod
    def _kg_block(kg_context: str) -> str:
        """Wrap KG context (untrusted, page-derived) in its data tag, or ''."""
        if not kg_context:
            return ""
        return KG_BLOCK_TEMPLATE.format(kg_context=sanitize_untrusted(kg_context))

    def _format_context(self, results: List[Dict[str, Any]]) -> str:
        """Format retrieved results into context for the LLM.

        Title, URL, and text all originate from indexed pages, so each is
        sanitized against data-tag breakout before interpolation.
        """
        context_parts = []
        for i, result in enumerate(results, 1):
            title = sanitize_untrusted(result.get("page_title", "Unknown"))
            url = sanitize_untrusted(result.get("page_url", ""))
            text = sanitize_untrusted(result.get("text", "")[:self._CHUNK_CHAR_LIMIT])
            context_parts.append(
                f"[Source {i}: {title}]({url})\n{text}"
            )
        return "\n\n---\n\n".join(context_parts)

    def _call_llm(
        self,
        provider: str,
        model_id: Optional[str],
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """Single-shot completion. Used by generate() and by /flashcards, /digest."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return self._complete(provider, model_id or self._default_model(provider), messages)

    # -- OpenAI-compatible plumbing -------------------------------------------

    def resolve_model(self, provider: str, model: Optional[str] = None) -> str:
        """Public: the model id that will actually be used (explicit override wins)."""
        return model or self._default_model(provider)

    def _default_model(self, provider: str) -> str:
        """Resolve the model id for a provider (an explicit override wins)."""
        if self.settings.llm_model:
            return self.settings.llm_model
        preset = PROVIDER_PRESETS.get((provider or "").lower())
        return preset["default_model"] if preset else "gpt-4o-mini"

    def _endpoint(self, provider: str):
        """Resolve (base_url, api_key) from presets or the custom config."""
        p = (provider or "").lower()
        if p in PROVIDER_PRESETS:
            base_url = PROVIDER_PRESETS[p]["base_url"]
        else:
            base_url = self.settings.llm_base_url  # custom / self-hosted
        if not base_url:
            raise ValueError(
                f"No base URL for provider '{provider}'. Pick a known provider or "
                f"set llm_base_url for a custom OpenAI-compatible endpoint."
            )
        # Local servers (Ollama, etc.) often need no key; send a harmless placeholder.
        api_key = self.settings.get_api_key(p) or self.settings.llm_api_key or "not-needed"
        return base_url, api_key

    def _client_for(self, base_url: str, api_key: str):
        """Cache one OpenAI client per (base_url, api_key) so key changes take effect."""
        cache_key = (base_url, api_key)
        if cache_key not in self._clients:
            from openai import OpenAI
            self._clients[cache_key] = OpenAI(
                base_url=base_url, api_key=api_key, timeout=LLM_TIMEOUT_SECONDS,
            )
        return self._clients[cache_key]

    def _complete(self, provider: str, model_id: str, messages: List[Dict[str, str]], max_tokens: int = 800) -> str:
        """Run a chat completion against any OpenAI-compatible provider."""
        base_url, api_key = self._endpoint(provider)
        client = self._client_for(base_url, api_key)
        response = client.chat.completions.create(
            model=model_id,
            messages=messages,
            temperature=0.2,
            max_tokens=max_tokens,
        )
        choice = response.choices[0].message.content if getattr(response, "choices", None) else None
        return (choice or "").strip()

    def _fallback_answer(self, query: str, results: List[Dict[str, Any]]) -> str:
        """Generate a basic answer without LLM when all providers fail.

        Sanitized even though no LLM sees it directly: in chat, this string
        becomes an assistant history turn on the next request.
        """
        answer_parts = [
            f"I found {len(results)} relevant results for: **{query}**\n",
            "*(LLM unavailable - showing raw results)*\n",
        ]
        for i, result in enumerate(results[:5], 1):
            title = sanitize_untrusted(result.get("page_title", "Unknown"))
            url = sanitize_untrusted(result.get("page_url", ""))
            text = sanitize_untrusted(result.get("text", "")[:200])
            answer_parts.append(f"{i}. **[{title}]({url})**\n   {text}...")

        return "\n".join(answer_parts)
