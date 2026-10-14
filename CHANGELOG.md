# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses semantic versioning.

## [0.2.0] - 2026-10-14

### Added
- Propose-then-approve: the create tools record proposals and return placeholder ids; nothing is
  written to HubSpot until `leadflow approve` (or `run` with a confirmation / `--yes`).
- `leadflow approve` executes a plan in order, re-checks company/contact duplicates at approval time,
  stops at the first failure and resumes on retry without creating a record twice.
- `leadflow reject` discards a plan; `history` shows runs awaiting approval.
- Links may only use ids the run received from a tool, and a deal must link the run's contact.

### Changed
- The system prompt describes proposals and asks for sequential create calls.

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
