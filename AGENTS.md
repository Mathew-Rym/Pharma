# AGENTS.md

Guidance for coding agents working in this repository.

## 1) What this repository is

Pharma OS is a WhatsApp-first operations layer for pharmacies. Core backend logic lives in FastAPI, with a thin WhatsApp gateway and a Streamlit dashboard.

High-level components:
- `api/` — core business logic and HTTP API (FastAPI)
- `dashboard/` — operations UI (Streamlit)
- `wa-gateway/` and `wa-gowa/` — WhatsApp gateway/runtime
- `db/` — schema and migrations
- `tests/` — unit and integration coverage
- `run.sh` — primary local runner for API/dashboard/WhatsApp/checks/tests

## 2) Core architecture rules

- Keep business logic in `api/`.
- Keep `wa-gateway/index.js` as a transport adapter (no business rules there).
- Prefer adding workflow/report behavior in the existing API modules (`router.py`, `reports.py`, `jobs.py`, etc.) instead of adding parallel logic paths.
- Preserve inventory correctness invariants (ledger and batch quantities must remain consistent).

## 3) Local workflow commands

Use the project runner from repository root:

- Start API: `./run.sh api`
- Start dashboard: `./run.sh dashboard`
- Start both (background): `./run.sh all`
- Start WhatsApp service: `./run.sh whatsapp`
- Pair QR flow: `./run.sh qr`
- Stop local services: `./run.sh stop`
- DB/schema check: `./run.sh check`
- Apply DB migrations: `./run.sh migrate`
- Run full test suite: `./run.sh test`
- Simulate inbound WhatsApp message (no phone required): `./run.sh say "HELP"`

## 4) Testing expectations

- Run relevant tests for touched areas; prefer `./run.sh test` before finalizing.
- For fast iteration, run targeted tests in `tests/` first, then full suite.
- Treat stock and ledger invariants as critical safety checks.

## 5) Secrets and environment

- Never commit `.env` or any credentials.
- Keep `SUPABASE_SERVICE_KEY`, `SHARED_SECRET`, gateway secrets, and API keys out of source code and logs.
- Assume anything in repository text may become public unless explicitly protected.

## 6) Change discipline

- Make surgical, minimal changes.
- Avoid unrelated refactors.
- Update docs when behavior or operator workflow changes.
- If modifying DB behavior, ensure schema/migration consistency and keep `./run.sh check` green.

## 7) Preferred edit map

- New WhatsApp command routing: `api/router.py`
- Report/tooling behavior: `api/reports.py`
- Goods receiving flow: `api/grn.py`
- Prescription/payment flow: `api/rx.py`, `api/mpesa.py`
- Scheduled jobs: `api/jobs.py` and `.github/workflows/cron.yml`
- Dashboard behavior: `dashboard/app.py`

## 8) Done criteria for agent tasks

- Code and tests relevant to the change pass locally.
- No new secrets introduced.
- Behavior remains consistent with architecture rules above.
