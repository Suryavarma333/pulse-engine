# Pulse project context

Synchronized from the as-built `complete-predictive-surge-platform` branch on 2026-08-18 after
strict engineering review passed at `034b82df24fda3abf9bd83eb48e229e885601385`.

| File | Repository facts captured |
|------|---------------------------|
| `project.mdc` | Repository identity; Python/TypeScript/HCL runtimes; FastAPI/Next/React/Locust/Terraform frameworks; direct dependency ranges; source roots; exact local and CI verification commands; Jira/BugBot state; safety constraints |
| `architecture.mdc` | Multi-service topology; agent/demo/dashboard/load/data/infrastructure paths; response/recovery/maintenance boundaries; versioned dashboard contract; dependency supervision; PostgreSQL, HTTP, and AWS integrations; auth and failure handling |
| `coding-standards.mdc` | Python, TypeScript, Terraform, FastAPI, SQLAlchemy, Next/React, Locust, and AWS conventions; pytest/Vitest/Compose/Locust/Terraform coverage; strict review/security/entropy policy |
| `deployment.mdc` | Credential-free Compose, GitHub Actions runtime images and six PR checks, optional bounded AWS/Terraform demo, rollback requirements, logs, audit data, and readiness semantics |
| `business-flows.mdc` | Commerce protection, signals, scheduled/realtime detection, arbitration, response/recovery/reconciliation, demo evidence, dashboard/results, live AWS opt-in, and clean-stack delivery verification |

## Reconnaissance summary

- Primary repository: `Suryavarma333/pulse-engine`
- Base branch: `codex/demo-app-foundation`; current feature branch:
  `complete-predictive-surge-platform`
- Runtimes: Python 3.11+, Node.js 22, TypeScript 5.9.2, pnpm 11.19.0,
  PostgreSQL 16, Terraform 1.6+ (CI 1.9.8)
- Frameworks: FastAPI/Pydantic, SQLAlchemy/Alembic/psycopg, Next.js 16.3.1,
  React 19.2.8, Vitest, Locust, boto3, Docker Compose, and Terraform AWS provider
- Services and boundaries: PostgreSQL migration, protected commerce app, agent API plus realtime,
  scheduled, feedback, maintenance and dependency-supervisor workers, read-only dashboard, Locust
  runners, and optional bounded AWS infrastructure
- Persistence: four reversible Alembic revisions for runs, snapshots, predictions/points,
  scheduled events, scaling actions/tier outbox, response retries, and shedding intervals
- Verification: GitHub Actions runs six jobs for Python compile/Ruff/coverage, live PostgreSQL
  migration/repositories, dashboard tests/build, clean Compose and two Locust modes, Terraform/IAM,
  and secret/generated-artifact/diff hygiene

## Remaining non-blocking context

- Jira is intentionally disabled because the repository contains no Jira project evidence.
- BugBot remains intentionally disabled for this repository.
- AWS account IDs, regions, ASG names, and deployment credentials remain runtime inputs and must never be committed.
- Optional AWS apply/destroy, production observability ownership, and deployment approval processes
  remain operator/team responsibilities outside automated delivery.
