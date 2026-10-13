# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses semantic versioning.

## [0.1.0] - 2026-10-13

### Added
- Hand-written, step-capped agent loop on the Anthropic Messages API with per-run token and cost tracking.
- Five CRM tools (`find_company`, `find_contact`, `create_company`, `create_contact`, `create_deal`);
  every input is validated by pydantic before any HubSpot request.
- HubSpot client that retries 429/5xx with backoff, honours `Retry-After`, and never retries 4xx.
- SQLite audit ledger of every run and every CRM write; `history` and `cleanup` commands.
- Secret-redacting structured logs; optional workspace header for organisation-level API keys.
- CI: ruff, `mypy --strict`, offline pytest suite, gitleaks over full history.

### Fixed
- A run that fails on an API or network error is now closed in the ledger as `error` instead of
  staying `running`.
