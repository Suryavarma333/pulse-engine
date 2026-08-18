# Pulse project context

Generated from a full repository reconnaissance on 2026-08-18.

| File | Repository facts captured |
|------|---------------------------|
| `project.mdc` | Repository identity, Python/FastAPI stack, build and test commands, GitHub branch, dependencies, and delivery constraints |
| `architecture.mdc` | Current modular architecture, concrete HTTP/business/infrastructure paths, integrations, boundaries, auth, and error behavior |
| `coding-standards.mdc` | Python conventions, FastAPI/SQLAlchemy rules, pytest and Ruff verification, review dimensions, and entropy policy |
| `deployment.mdc` | Docker Compose local environment, target AWS demo environment, rollback, CI status, and CloudWatch direction |
| `business-flows.mdc` | Checkout protection, recommendations/catalog degradation, control-plane tier changes, health, and metric collection flows |

## Reconnaissance summary

- Primary repository: `Suryavarma333/pulse-engine`
- Current default branch: `codex/demo-app-foundation`
- Current stack: Python 3.11+, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, pytest, Ruff, Docker Compose
- Current services: protected demo application, PostgreSQL audit schema, and migration runner
- Current verification: `.venv/bin/ruff check .` and `.venv/bin/pytest -q`

## Remaining non-blocking context

- Jira is intentionally disabled because the repository contains no Jira project evidence.
- GitHub Actions is not configured yet; the complete Pulse delivery is expected to add CI.
- AWS account IDs, regions, ASG names, and deployment credentials remain runtime inputs and must never be committed.
- Production observability and deployment approval processes remain outside this portfolio repository.
