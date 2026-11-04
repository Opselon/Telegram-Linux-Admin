# Implementation Plan & Agent Ownership

## Sequencing

| # | Workstream | Owner | Depends on |
|---|---|---|---|
| 1 | Phase Zero audit, architecture contracts | Orchestrator | — |
| 2 | Service layer (`src/services/`) + migrations + `Actor`/permissions | Orchestrator | 1 |
| 3 | FastAPI backend (`src/api/`) | Orchestrator | 2 |
| 4 | React frontend (`web/`) | Frontend agent | 1 (API contract in architecture.md §3) |
| 5 | Bale adapter (`src/adapters/bale/`) | Bale agent | 2 (service contracts §2); SDK research first |
| 6 | Runtime wiring `tla-platform` + Telegram integration points | Orchestrator | 3,5 |
| 7 | Tests per area + cross-interface e2e | All | own area |
| 8 | Deployment (Docker/compose/nginx/systemd), docs | Orchestrator | 3,4 |
| 9 | Security review | Orchestrator | all |
| 10 | CI update, PR, review loop | Orchestrator | 8,9 |

Integration checkpoint: when 2+3 land on the branch, frontend and Bale agents integrate against the real backend; contract changes after checkpoint go through the orchestrator.

## Per-agent report format (post into final summary)

- Files changed (exact paths)
- What is implemented / stubbed / not done — explicitly labeled
- Tests executed + results (paste counts)
- Dependencies requested (pyproject additions go through orchestrator)
- Known risks / open questions

## Rules of engagement

- No direct pushes to `main`; all work on `feat/platform-4.0` (orchestrator merges).
- No new dependencies without orchestrator approval.
- No edits outside your ownership table (architecture.md §6).
- Every user-facing string in the web UI goes through i18n (fa + en).
- Every new route/endpoint ships with permission checks + tests.
- Mock-only features must be labeled MOCK in code comments and the report; they must not be presented as production features.
