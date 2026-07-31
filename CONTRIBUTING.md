# Contributing to TradeCraft

## Set up

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
pytest -q
```

## Pull requests

- Keep the service loopback-only and the frontend build-free.
- Preserve stable API fields unless the change is documented.
- Add or update English and Simplified Chinese catalog entries together.
- Use only deterministic synthetic fixtures in tests and documentation.
- Add tests for behavior changes and run the privacy checks.
- Keep optional AI synthesis separate from deterministic calculations.

## Prohibited data

Do not submit:

- broker account identifiers or exports;
- real trades, positions, performance, watchlists, or research notes;
- private names, email addresses, phone numbers, or local absolute paths;
- API keys, tokens, `.env`, databases, logs, caches, or generated state.

By contributing, you agree that your contributions are licensed under Apache-2.0.
