# RECAP backend - reproducible container image.
#
# This runs ONLY the local FastAPI backend. The Chrome extension is still loaded
# in your own browser (chrome://extensions → Developer mode → Load unpacked →
# select the extension/ folder), pointed at http://localhost:8000.
FROM python:3.12-slim

# uv (fast, reproducible installer/resolver) copied from its official image.
COPY --from=ghcr.io/astral-sh/uv:0.8.0 /uv /uvx /bin/

# curl is used by the compose healthcheck; the ML wheels are self-contained.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Resolve + install dependencies first so this layer is cached across code changes.
# Copy only the manifests; uv.lock (if committed) pins exact versions.
COPY pyproject.toml ./
COPY uv.lock* ./
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_HTTP_TIMEOUT=300

# --frozen uses the committed lock for reproducibility; fall back to a fresh
# resolve if no lock is present in the build context.
RUN uv sync --frozen 2>/dev/null || uv sync

# Application code (backend only - the extension ships separately).
COPY main.py ./
COPY backend/ ./backend/

# Sentence-transformers / cross-encoder weights download on first run into this
# cache. Mount a volume here (see docker-compose.yml) to persist them.
ENV HF_HOME=/app/.cache/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/app/.cache/huggingface

# Inside the container we bind 0.0.0.0 so the published port is reachable, but
# docker-compose publishes ONLY to the host loopback (127.0.0.1) - so the backend
# is never exposed to the local network. Do not publish on 0.0.0.0.
ENV HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000
CMD ["uv", "run", "python", "main.py"]
