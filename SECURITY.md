# Security Policy

## Reporting a vulnerability

If you find a security issue in RECAP, please report it **privately** - do not open a public issue.

- Preferred: open a private advisory on GitHub (**Security → Report a vulnerability**).
- Or email: **srikanth@atliq.com**

Please include steps to reproduce and which component is affected (extension or backend). We aim to acknowledge reports within 7 days.

## Design & scope

RECAP is local-first - your browsing data, indexes, and API keys stay on your machine.

- The backend binds to `127.0.0.1`; browser cross-origin access is restricted to Chrome-extension origins (other local processes on your machine can reach it, like any loopback service).
- Page content only leaves your machine when you ask a question (sent to the LLM provider you configured) - and, if you opt into a *remote* embedding provider, when pages are indexed.
- API keys are stored locally (never synced to the cloud, never committed to the repo).

## If you run or fork this

- Never commit real API keys or the `data/` directory - both are covered by `.gitignore`.
- Keep the backend bound to loopback unless you fully understand the exposure of binding to `0.0.0.0`.
- Provide your LLM/embedding keys via `.env` (see `.env.example`) or the extension's Options page.
