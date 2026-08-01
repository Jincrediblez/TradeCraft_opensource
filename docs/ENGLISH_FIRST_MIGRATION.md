# English-first breaking changes

TradeCraft now treats English as the only canonical system language. Simplified Chinese remains available as an optional browser-interface locale.

## What changed

- Browser locale values are limited to `en` and `zh-CN`. English is the default. The former `auto` value and browser-language detection have been removed.
- API responses, errors, logs, CLI output, configuration, deterministic findings, and AI reports are English only. Language query parameters and `Accept-Language` no longer change API output. `Content-Language` is always `en`.
- Controlled setup values use canonical English strings in API payloads and persisted state. A Chinese browser label never changes the value submitted to the server.
- AI reports use one English prompt and one report file. Report locale parameters and language-specific report caches have been removed.
- Repository documentation is English only. `README.md`, `TradeCraft_User_Manual.md`, `TradeCraft_User_Manual.pdf`, and `docs/images/feature-guide/` are now the default paths.
- The state and Demo schema versions have increased. A workspace containing legacy Chinese controlled values is rejected with an English rebuild instruction.

## Existing workspaces

TradeCraft does not translate, delete, or overwrite an incompatible workspace. Keep the existing files as a backup, then rebuild the workspace from the original broker exports under the current version. Free-form user content such as theses, notes, conditions, reviews, and imported source text is preserved exactly as entered when it is accepted by the current schema.

## Integrations

API and automation clients must send canonical English setup values. Clients should use stable `messageKey` and `messageParams` fields when they need to render a localized browser message; the English `message` remains the API fallback and source of record.
