# Contributing to RECAP

Thanks for helping! The full contributor guide - setup, architecture, conventions,
and security rules - lives in **[AGENTS.md](AGENTS.md)**. The short version:

1. Set up with `uv sync` (or Docker / pip - see [README](README.md#setup)).
2. Make your change. Keep `backend/app.py` a thin API layer; keep SQLite the
   single source of truth; never load remote resources in the extension.
3. Run the test suite and keep it green:

   ```bash
   python tests/test_integration.py
   ```

4. Never commit API keys, `.env`, or the `data/` directory.
5. Open a PR against `main` with a short description of what and why.

Security issues: please report privately per [SECURITY.md](SECURITY.md), not as
public issues.
