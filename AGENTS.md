# AGENTS.md

Guidance for AI coding agents - and humans - working in the RECAP repository. This is the **single source of truth** for how to set up, run, test, and change this project; tool-specific files (`CLAUDE.md`, etc.) import it. The human-facing overview is in [`README.md`](README.md); the security policy is in [`SECURITY.md`](SECURITY.md).

## What RECAP is

RECAP is a **local-first** Chrome extension (Manifest V3) plus a local Python (FastAPI) RAG backend. It passively indexes the pages a user actually reads and lets them search their browsing history in natural language. Everything runs on the user's machine; page content only leaves the device when the user asks a question - and only the retrieved snippets, sent to the LLM provider the user configured.

## Setup & run

Requires **Docker** (recommended) or **Python 3.11+**, plus Chrome. Three paths, all of
which install spaCy + the `en_core_web_sm` model (so the knowledge graph works out of the box):

```bash
# Docker - recommended: reproducible, one command. Serves http://127.0.0.1:8000 (loopback only).
docker compose up --build

# uv - fast, reproducible local dev. `uv sync` installs deps + the spaCy model.
uv sync && uv run python main.py

# pip - simple fallback.
pip install -r requirements.txt && python main.py
```

`pyproject.toml` (+ `uv.lock`) is the canonical dependency set for uv/Docker; `requirements.txt`
mirrors it for the pip path - **keep the two in sync** when changing versions. torch is pinned to
the CPU wheel (`tool.uv.sources`) so images stay small and it runs anywhere; GPU users override it.

- Copy `.env.example` → `.env` and set at least one LLM key (or point at a local Ollama). See **LLM & embeddings** below.
- **Docker + a host Ollama:** the container can't see the host's `localhost` - set `LLM_BASE_URL`/`EMBEDDING_BASE_URL` to `http://host.docker.internal:11434/v1` (compose already maps `host.docker.internal`).
- Load the extension: `chrome://extensions` → enable **Developer mode** → **Load unpacked** → select the `extension/` folder.
- **Knowledge graph:** entities need spaCy (now installed by default). If you enabled it *after* indexing pages, backfill them without re-browsing via `POST /maintenance/rebuild_kg` (re-runs NER over stored text; SQLite is the source of truth).

## Testing

```bash
python tests/test_integration.py
```

Ten checks (config, models, SQLite/FTS5, LanceDB, knowledge graph, classifier, chunker, reranker, end-to-end pipeline, semantic gate). No pytest needed. On a Windows console, prefix `PYTHONUTF8=1` if you hit a Unicode error. **Run this before opening a PR and keep it green.**

## Architecture - the one rule that matters

**SQLite is the single source of truth; the keyword index and vector index are derived and reconstructible.**

- `data/recap.db` (SQLite) holds the canonical chunk **text** + all page/chunk metadata - stored exactly once.
- **FTS5** keyword search is an *external-content* table synced by triggers - it holds the inverted index only, no second copy of the text.
- **LanceDB** (`data/vector_store/`) stores only `chunk_id + vector` - no text, no metadata.
- Retrieval fuses BM25 + dense + KG with **weighted Reciprocal Rank Fusion** (k=60) → cross-encoder rerank → **time-decay recency** → then **hydrates** text/metadata from SQLite. Hydration is the single place chunk text is read and the single place the date filter is applied.
- Because the indexes are derived, changing the embedding model **rebuilds vectors from SQLite** (`POST /maintenance/reindex`) without losing any pages.

**Ingestion order:** `content_classifier` → semantic `chunker` (token-aware) → spaCy NER (optional; degrades gracefully) → SQLite → FTS (via triggers) → LanceDB → knowledge graph. Write SQLite (truth) **first**, derived indexes after, and commit the page's `content_hash` **last** - so a failure mid-pipeline re-indexes cleanly on the next visit instead of leaving a half-indexed page.

## Directory map

```
main.py                     entry point (uvicorn -> backend.app:app)
backend/
  app.py                    thin FastAPI layer - NO business logic here
  config.py                 pydantic-settings Settings (env / .env)
  models.py                 all Pydantic request/response models (bounded)
  bootstrap.py              schema-version clean-rebuild of local data/
  prompts.py                ALL LLM prompt templates + untrusted-content sanitizer
  embeddings.py             pluggable embedding fn (local OR OpenAI-compatible)
  ingestion/                processor, chunker, content_classifier, entity_extractor, semantic_gate
  retrieval/                hybrid_retriever (RRF + recency), reranker, answer_generator
  storage/                  database (SQLite/FTS5), vector_store (LanceDB), knowledge_graph
extension/                  MV3 extension
  theme.css                 shared "Index Catalog" design system (every page imports it)
  background.js             service worker: tab tracking, domain/path blocklist, gating
  content.js                extraction, Shadow-DOM omnibar (Ctrl+Shift+K), highlight (Ctrl+Shift+S)
  newtab/popup/options/onboarding/digest/flashcards/graph
tests/test_integration.py   plain-python test runner
```

## LLM & embeddings

- **One OpenAI-compatible client** (the `openai` SDK) serves every LLM provider via a `(base_url, api_key, model)` triple. Presets: `openai`, `groq`, `openrouter`, `google`, `anthropic`, `ollama`; `custom` uses `llm_base_url`. **Do not add per-provider SDKs** (`groq`, `anthropic`, `google-generativeai` are intentionally not dependencies).
- **Embeddings are pluggable the same way**: `local` sentence-transformers (default `BAAI/bge-base-en-v1.5`) or any OpenAI-compatible `/v1/embeddings`. The true vector **dimension is derived from the model at load - never hard-code it**.
- Keys come from `.env` or the extension Options page (`POST /update_api_keys`, session-only).
- LLM calls have a hard timeout; the system prompt includes an injection guard treating retrieved page text as untrusted data.

## Conventions

**Python**
- `from __future__ import annotations`; full type hints; small, single-purpose functions; docstrings on public methods.
- All request/response shapes live in `backend/models.py` as Pydantic models **with bounds** (`max_length`, numeric ranges, ISO-date validators).
- Keep `app.py` a **thin API layer** - delegate to `IngestionProcessor` / `HybridRetriever` / `AnswerGenerator` / storage classes.
- Blocking work (embedding, DB, vector, LLM) inside an `async` handler must run via `asyncio.to_thread` (or be a sync `def` handler) so it doesn't block the event loop.

**Extension (vanilla JS, no build step)**
- **IMPORTANT: MV3's CSP forbids inline `<script>` blocks and inline `on*=` handlers.** Put JS in an external `.js` file and wire events with `addEventListener`.
- **NEVER load remote resources** (fonts, scripts, styles, analytics). The extension must work fully offline for privacy - use system-font stacks, not font CDNs.
- **Escape all untrusted text** - page titles, URLs, and LLM output (which is derived from indexed pages) - before assigning to `innerHTML`; route link `href`s through a `safeUrl()` that allows only `http(s):`. This prevents XSS from a malicious indexed page.
- Every page imports `extension/theme.css` first and uses its tokens/classes. Don't reintroduce a per-page palette or a dark theme.

## Security & privacy - non-negotiable

- **NEVER commit API keys or the `data/` directory.** Both are in `.gitignore`; secrets belong only in `.env`. See [`SECURITY.md`](SECURITY.md).
- The backend **binds `127.0.0.1`** and restricts CORS to the extension origin. **Do not** change the default bind to `0.0.0.0`.
- Local embeddings + a local LLM (Ollama) is a fully private setup. A **remote** embedding provider sends the text of *every indexed page* off-device - keep it opt-in and keep the warning.
- Sensitive domains (banking, email, auth, password managers) are excluded from both tracking and content capture - keep that gating intact.
- Sensitive-page detection is **layered**; keep every layer intact: (1) URL blocklists in the extension + backend (defense in depth for known-bad domains); (2) a **live-DOM structural gate in `content.js`** (`detectSensitivePage`: visible password/payment fields, `noindex`, form dominance, auth-phrase walls) that refuses extraction so sensitive text **never leaves the tab**; (3) a backend PII/auth-text backstop (`detect_sensitive_content`: Luhn-valid cards, SSN, IBAN, contact density); (4) an embedding-prototype `SemanticGate` catching login/account/checkout-like pages on unlisted domains (`semantic_gate_enabled`; fails open). Layers 3-4 run **before** any SQLite write. The DOM check must stay client-side - moving it to the backend would send the text over the wire first. Skip telemetry lands in the `skip_stats` table and surfaces as `top_skipped_domains` in `/stats` (ignore-domain candidates).

## Adding things (quick pointers)

- **New LLM/embedding provider** - add a preset base URL in `retrieval/answer_generator.py` / `embeddings.py`; no new dependency if it's OpenAI-compatible.
- **New endpoint** - define models in `models.py`, add the route in `app.py`, delegate the logic to a service, wrap blocking work in `to_thread`.
- **Storage/schema change** - bump `SCHEMA_VERSION` in `bootstrap.py` (triggers a clean local rebuild on next start); keep SQLite the source of truth.

## Gotchas

- Changing `embedding_model` / provider / dimension invalidates the vector space → the app **auto-reindexes from SQLite on next start**; expect a one-time rebuild.
- `data/` is per-user runtime state; deleting it is safe (it re-indexes as you browse). It is intentionally wiped on a `SCHEMA_VERSION` bump.
- The knowledge graph needs spaCy (`en_core_web_sm`); without it, entities/graph are empty but BM25 + dense retrieval still work.
- With a **remote** embedding provider, the `SemanticGate` embeds the head of pages it may then reject - one more reason remote embeddings are opt-in. Fully-local setups are unaffected.
- Commit conventions: run the test suite first; never bypass hooks; keep secrets and `data/` out of every commit.
