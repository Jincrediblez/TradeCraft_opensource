# Security policy

## Supported versions

Security fixes are applied to the latest tagged release and `main`.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature. Do not open a public issue containing exploit details, credentials, broker exports, or personal financial data.

Include:

- affected version or commit;
- reproduction steps using synthetic data;
- expected impact;
- suggested remediation, if known.

## Security boundary

TradeCraft listens only on `127.0.0.1` and does not implement user authentication. Do not bind it to a public or shared network without adding an appropriate authenticated reverse proxy.

The application is local-first but optional features may contact:

- Yahoo Finance for market data;
- Kimi/Moonshot only after the user explicitly requests an AI summary;
- embedded market-view providers when the Market page is opened.

Never commit `.env`, broker exports, generated state, workbooks, SQLite databases, caches, or logs.
