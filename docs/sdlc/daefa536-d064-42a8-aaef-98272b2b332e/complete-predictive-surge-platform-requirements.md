# Requirements Discovery Document

**Artifact slug:** `complete-predictive-surge-platform` (paired SDD: `complete-predictive-surge-platform.md`)  
**Workflow ID:** `daefa536-d064-42a8-aaef-98272b2b332e`  
**Date:** `2026-08-18T04:42:20.000Z`  
**Status:** READY_FOR_SDD  
**Work type:** `feature`

## 1. Business objective

Deliver Pulse as a complete, portfolio-grade predictive traffic-surge platform for operators of high-traffic commerce systems: it must recognize both known calendar events and unplanned accelerating demand before a conventional CPU-only response, use a single safety-bounded control pipeline to pre-scale capacity and reversibly reduce non-critical work, keep checkout available, and expose reproducible evidence of latency, detection lead time, provisioning quality, and cost behavior through local and low-cost AWS demonstrations. The working end state is an end-to-end repository—not only the existing demo application—with runnable services, data persistence, load scenarios, dashboard, infrastructure-as-code, automated tests and CI, operating documentation, and an open pull request.

## 2. Functional requirements

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| FR-1 | Organize the repository into explicit control-plane agent, protected demo application, shared contracts, PostgreSQL persistence, load tests, dashboard, Terraform infrastructure, tests, scripts, and documentation boundaries, while retaining and integrating the current working foundation. | Must | User PRD deliverable 1; codebase |
| FR-2 | Persist traffic snapshots, scheduled events, surge predictions and prediction points, scaling actions, and load-shedding event intervals in PostgreSQL through SQLAlchemy 2 models and reversible Alembic migrations. Typed graph/evaluation fields and JSONB signal evidence must support prediction-versus-actual analysis and decision audit. | Must | User PRD deliverable 2; existing schema |
| FR-3 | Define and document the scheduled detector, real-time detector, and their shared response pipeline as an explicit state machine/flow with normal, watch/prewarm, surge/protect, recovery/cooldown, and failure-safe behavior that can be rendered in Mermaid. | Must | User PRD deliverable 3 |
| FR-4 | Preserve a FastAPI protected commerce application with `POST /checkout`, `GET /recommendations`, `GET /catalog`, `GET /health`, and a traffic snapshot endpoint. Checkout must remain in normal mode at shedding levels 0–3. | Must | User PRD deliverable 4; existing application |
| FR-5 | Apply reversible load-shedding policies in order: level 0 normal; level 1 disable recommendations; level 2 additionally use simplified/stale cached catalog data; level 3 rate-limit non-critical traffic with cached/minimal fallback. The authenticated internal control API must persist a transition before activating it. | Must | User safety constraints; existing application |
| FR-6 | Continuously ingest rolling request-rate, latency, error, and concurrency metrics from the demo application and support pluggable/simulated leading signals for CloudWatch origin metrics, CloudFront edge request rate, SQS queue depth/growth, and session/login activity. Missing optional signals must not stop detection. | Must | User PRD real-time mode |
| FR-7 | Compute a moving baseline, first-order request-rate change, acceleration, signal ratios/growth rates, confidence, and threshold/hysteresis decisions on configurable observation windows. A reproducible sudden spike must trigger before a configured reactive CPU/level comparator and persist the triggering snapshot and reasoning. | Must | User PRD deliverable 5 |
| FR-8 | Convert every qualifying real-time or scheduled prediction into the same response command: recommended desired capacity, optional load-shedding tier, correlation ID, evidence, and recovery plan. Equivalent repeated commands must be idempotent and no-op/capped/failed decisions must also be audited. | Must | User shared response pipeline; safety constraints |
| FR-9 | Provide one AWS Auto Scaling adapter using boto3 for capacity reads and desired-capacity changes. It must default to dry-run, cap every request at the configured global/event ceiling, expose dry-run decisions without a mutating AWS call, and record provider request IDs or errors for live attempts. | Must | User PRD deliverables 5–6; safety constraints |
| FR-10 | Read active scheduled events from PostgreSQL, honor timezone-aware start/end times and expected multipliers, construct a configurable prewarm ramp that starts before the event and reaches peak capacity before event start, and execute each due ramp point exactly once through the shared response pipeline. | Must | User PRD deliverable 6 |
| FR-11 | Recover after scheduled or real-time surges through sustained-low-load checks and a gradual stepwise scale-down plan. Cooldown and hysteresis must prevent immediate reversal or flapping, and load shedding must step back to level 0 reversibly. | Must | User response pipeline; safety constraints |
| FR-12 | Close the feedback loop by associating actual traffic with predictions and calculating actual onset/peak, prediction error, detection lead time versus the reactive comparator, over-provisioned instance-minutes, and under-provisioned seconds for completed runs. | Must | User response pipeline; deliverable 10 |
| FR-13 | Supply two runnable Locust scenarios: a timed scheduled “Diwali sale” ramp tied to a seeded calendar event, and an unannounced sudden spike. Each scenario must exercise the protected endpoints and offer a documented end-to-end invocation and reset path. | Must | User PRD deliverable 7 |
| FR-14 | Supply a lightweight React/Next.js dashboard that refreshes live data and shows traffic history, current/pending capacity state, current shedding tier and per-endpoint policy, recent/auditable actions, and a predicted-versus-actual overlay. | Must | User PRD deliverable 8 |
| FR-15 | Expose read-only status/history APIs needed by the dashboard for snapshots, predictions, scaling actions, shedding events, scheduled events, and portfolio result metrics, with time-bounded queries suitable for graph refresh. | Must | Inferred from dashboard and audit requirements |
| FR-16 | Extend Docker Compose so PostgreSQL migration, demo application, Pulse agent, and dashboard start in dependency-safe order with health checks and usable local defaults; load scenarios may run as documented on-demand profiles/commands. | Must | User deployment requirement |
| FR-17 | Provide minimal Terraform for a low-cost demo AWS environment containing a launch template, bounded Auto Scaling Group starting at one instance, CloudWatch custom metric namespace/alarm or reactive comparator, security/network inputs, and least-privilege Pulse IAM permissions. Terraform outputs and example variables must support a documented apply/destroy flow without embedding credentials. | Must | User PRD deployment requirement |
| FR-18 | Add automated tests for detectors, scheduled ramps, state transitions, idempotency, dry-run behavior, instance ceilings, cooldown/gradual recovery, audit writes, AWS adapters, APIs, migration upgrade/downgrade behavior, and both end-to-end surge modes. Maintain the existing checkout and shedding regression coverage. | Must | Acceptance criteria; coding standards |
| FR-19 | Add GitHub Actions pull-request checks for Python compilation/lint/tests and coverage, PostgreSQL migration validation, dashboard install/build/tests, Docker/Compose configuration validation, Terraform formatting/validation, and secret-safe dependency handling. | Must | Acceptance criteria; deployment context |
| FR-20 | Replace the slice-oriented README with complete architecture diagrams, prerequisites, local/AWS setup, configuration and IAM guidance, exact scheduled and sudden-spike demo scripts, troubleshooting/cleanup, and a Results section that explains how to collect and compare Pulse with a reactive-only baseline. | Must | User PRD deliverable 9 |
| FR-21 | Compute and present at least three defensible portfolio metrics from persisted/load-test data, including critical-path p99 and success rate, proactive detection lead time, and provisioning efficiency; also document two optional metrics such as error-rate reduction and recovery/cost duration. | Must | User PRD deliverable 10 |
| FR-22 | Deliver all implementation and documentation on a feature branch based on `codex/demo-app-foundation`, commit and push the complete work, and open an unmerged pull request against that base branch. | Must | Orchestrator acceptance criteria; repository policy |

## 3. Non-functional requirements

| ID | Category | Requirement | Metric / verification |
|----|----------|-------------|-----------------------|
| NFR-1 | Safety / Cost | Dry-run is the default in source, Compose, and examples; live mutation requires an explicit opt-in. Every requested desired capacity is clamped before reaching boto3. | Tests prove no mutating client call in default mode and `applied_desired_capacity <= max_instance_ceiling` in all action paths. |
| NFR-2 | Reliability | The critical checkout endpoint is never disabled, cached, or rate-limited by Pulse, including at the highest shedding tier and during detector/recovery failures. | API and end-to-end tests cover levels 0–3 with no Pulse-induced checkout rejection and `X-Pulse-Endpoint-Mode: normal`. |
| NFR-3 | Reliability | Scaling and tier decisions are idempotent and concurrent-safe enough for repeated worker polls; recovery requires configurable sustained-low evidence, cooldown, and bounded capacity decrements. | Deterministic tests cover duplicate commands, cooldown rejection, hysteresis, and stepwise scale-down. |
| NFR-4 | Auditability | Every prediction, scaling decision (including skipped/capped/dry-run/failed), and actual shedding state change records UTC timestamp, correlation, machine-readable reason, human-readable reasoning, and signal evidence. | Integration tests query the persisted records for each response-pipeline outcome. |
| NFR-5 | Performance | Metrics collection and status APIs must remain lightweight and must not intentionally add blocking I/O to protected request paths. Dashboard queries must be time-bounded/index-backed. | Code review plus tests; load reports compare checkout p99 against baseline and disclose environment/scenario. |
| NFR-6 | Detection quality | Real-time evaluation must use slope/acceleration and leading-signal confirmation rather than only a raw load threshold, and expose the data needed to compare its trigger with reactive CPU/level behavior. | Sudden-spike run yields a persisted lead-time measurement; thresholds and evidence are visible in API/dashboard. |
| NFR-7 | Security | No secrets, AWS credentials, private keys, `.env`, Terraform state, or generated sensitive artifacts may be committed. Internal mutating application controls require constant-time token authentication. | Secret scan/review, `.gitignore`, API authorization tests, and documented environment/credential injection. |
| NFR-8 | AWS Security | IAM must be least privilege and resource-scoped where AWS supports it; the agent must not create arbitrary resources at runtime. | Terraform policy review and validation; runtime adapter is limited to metrics/describes and ASG desired-capacity actions. |
| NFR-9 | Maintainability | Python 3.11+ code uses typed service boundaries, dependency injection for external clients/clocks where needed, UTC-aware datetimes, asynchronous database access, and modules aligned to the existing modular-monolith boundaries. | Ruff/compile checks and strict review; no placeholder or parallel superseded implementation. |
| NFR-10 | Testability | New business behavior has deterministic unit/integration tests, externally timed behavior uses injectable clocks or bounded polling, and overall Python coverage remains at least 85%. | `pytest --cov` succeeds at the configured threshold; dashboard and infrastructure checks also pass. |
| NFR-11 | Operability | Services expose health/status sufficient to identify database, metric-source, detector, response-pipeline, AWS execution-mode, capacity, cooldown, and shedding state without exposing credentials. | Compose health checks and documented status inspection commands succeed. |
| NFR-12 | Portability | The complete local demonstration runs through Docker Compose without AWS credentials; CloudFront, SQS, session/login, and CPU signals can be simulated or omitted with explicit status. | Both Locust scenarios complete locally in dry-run mode on a clean setup. |
| NFR-13 | Data integrity | Schema changes remain reversible, persisted timestamps are timezone-aware, relationships preserve audit history where appropriate, and graph/evaluation columns are queryable without parsing all evidence JSON. | PostgreSQL migration upgrade/downgrade checks and schema tests pass. |
| NFR-14 | Scalability | Poll intervals, metric windows, retention/query windows, thresholds, capacity ratios, and dashboard refresh intervals are configurable; no unbounded in-memory history or dashboard query is introduced. | Configuration validation tests and bounded collection/query implementation review. |
| NFR-15 | Reproducibility | Demo and results documentation records scenario parameters, thresholds, execution mode, timestamps, baseline definition, and formulas so portfolio claims are traceable rather than invented. | A generated/exported run summary can be reconciled to snapshots, predictions, and actions. |

## 4. Repositories

### Involved

| Repo | Role | Notes |
|------|------|-------|
| `Suryavarma333/pulse-engine` | modifiable; primary; local workspace | Current default/base branch is `codex/demo-app-foundation`. Python package uses pip/setuptools and FastAPI/SQLAlchemy/Alembic; Docker Compose runs PostgreSQL, migration, and the protected app. The repository currently has no `agent/`, `load_tests/`, `dashboard/`, `infra/`, architecture docs, or GitHub Actions workflow. |

### Modifiable

- `Suryavarma333/pulse-engine` — full implementation, tests, infrastructure, CI, and RDD/SDD documentation may be changed in later workflow phases.

### Read-only

- None.

### Current behavior and relevant paths reviewed

- `demo_app/app/main.py`, `routes/protected.py`, and `routes/operations.py` provide application startup, rolling metrics, protected commerce routes, health, metrics, and authenticated tier control.
- `demo_app/app/shedding/` centralizes the level 0–3 policy, token-bucket limiter, controller, and in-memory/PostgreSQL transition stores. Persistence occurs before a new policy becomes active.
- `db/models/` and `db/migrations/versions/20260818_0001_initial_schema.py` already define the six intended audit/forecasting tables and a reversible initial migration.
- `tests/unit/demo_app/` verifies checkout protection, endpoint degradation, control authentication, audit/no-op behavior, and metric collection. `tests/unit/db/` verifies model registration and PostgreSQL DDL compilation.
- `docker-compose.yml` currently starts PostgreSQL, applies migrations, and starts only the demo application. `pyproject.toml` has no boto3, HTTP client, forecasting, Locust, or dashboard dependencies.
- `README.md` accurately labels the implementation as a first slice and explicitly defers the agents, scenarios, dashboard, Terraform, complete architecture, and benchmark results; those deferred capabilities are the desired behavior of this workflow.

## 5. Constraints

- Use Python 3.11+, FastAPI, boto3, PostgreSQL 16 through SQLAlchemy 2/Alembic/psycopg 3, Locust, a lightweight React/Next.js dashboard, Docker Compose, and Terraform. A simple moving-average/derivative and exponential-smoothing boundary is preferred over heavy v1 ML.
- Use the existing modular-monolith foundation and architectural boundaries; shared contracts must not create circular service dependencies.
- AWS mutation remains behind one adapter, defaults to dry-run, enforces a configurable global/event instance ceiling, and cannot be bypassed by either detector.
- Scale-down is gradual and cooldown protected. Load shedding is reversible and cannot affect checkout.
- All decisions require durable timestamped reasoning and evidence; persistence failure must not silently activate an unaudited control action.
- Keep the AWS example mindful of free-tier/limited-credit accounts: default desired capacity one, low maximum, small configurable instance types, short demos, and documented destruction.
- Do not commit credentials, tokens, private keys, `.env`, generated Terraform state, or any secret-bearing examples.
- Internal control routes remain separated and authenticated; user-facing demo endpoints remain unauthenticated so Locust can generate traffic.
- Use UTC-aware persisted/API timestamps and preserve event IANA timezone data for display/recurrence interpretation.
- Maintain at least 85% Python coverage and add CI checks for backend, dashboard, migrations, Compose, and Terraform.
- Follow conventional commits. The delivery branch uses the shared artifact slug and is based on `codex/demo-app-foundation`; the finished pull request stays open and unmerged.
- Do not modify `.cursor` kit files outside `.cursor/project-context`; discovery itself creates only this RDD.
- The user explicitly superseded the PRD's “wait for confirmation” cadence: this workflow proceeds autonomously without phase approvals to the complete end state.

## 6. External dependencies

| System | Purpose | Integration style | Owner/team (if known) |
|--------|---------|-------------------|------------------------|
| PostgreSQL 16 | Durable traffic history, calendar, forecasts, scaling audit, shedding audit, and results data | Async SQLAlchemy/psycopg; Alembic migrations | Project owner |
| Protected demo application | Origin request/latency signals and tier application | HTTP JSON endpoints with `X-Pulse-Control-Token` on internal mutations | Pulse repository |
| AWS EC2 Auto Scaling | Read and safely change demo ASG desired capacity | boto3 `autoscaling` client; dry-run wrapper because the API has no native dry-run for desired capacity | User AWS account |
| Amazon CloudWatch | Read/publish origin and custom Pulse metrics; represent reactive comparator/alarm | boto3 CloudWatch APIs and Terraform | User AWS account |
| Amazon CloudFront | Optional leading edge request-rate source | Read-only CloudWatch metrics or local simulated provider | User AWS account / simulated locally |
| Amazon SQS | Optional queue depth and growth-rate source | Read-only queue attributes/CloudWatch metrics or local simulated provider | User AWS account / simulated locally |
| Docker Engine / Compose | Reproducible local multi-service environment | Container images, Compose health/dependency orchestration | Developer workstation |
| Locust | Scheduled and sudden-spike traffic generation and run statistics | Python load-test scenarios and CLI/web runner | Open source |
| Node.js / Next.js | Operator dashboard and charts | Browser UI calling read-only Pulse APIs | Open source |
| Terraform AWS provider | Minimal demo AWS resources and IAM | Declarative infrastructure, validate/plan/apply/destroy | HashiCorp/AWS |
| GitHub Actions | Pull-request verification | Repository workflow jobs | GitHub |
| GitHub | Source branch and open pull-request delivery | Git/`gh` authenticated as repository owner | `Suryavarma333` |

## 7. Assumptions

1. **A-1:** The existing demo app, initial migration, and tests are a valid foundation to extend; they are not throwaway code, although interfaces may be refactored when necessary.
2. **A-2:** V1 runs one active decision worker per environment. Database uniqueness/idempotency still guards repeated polls and retry behavior, but distributed leader election is not required for this portfolio deployment.
3. **A-3:** Local dry-run is the mandatory fully reproducible acceptance path. Live AWS apply and mutation are supported and documented but can only be exercised when the user supplies an AWS account, region, credentials, and chooses explicit live mode.
4. **A-4:** CloudFront, SQS, concurrent-session, login-rate, and CPU inputs use provider interfaces; local scenarios may publish simulated values, so those AWS products do not need to be provisioned solely to demonstrate detection.
5. **A-5:** A simple moving baseline plus derivative/acceleration thresholds, confirmation windows, and optional exponential smoothing satisfies v1. Prophet/LSTM integration is documented as a replaceable future model boundary, not added as a runtime dependency.
6. **A-6:** A configurable CPU/load threshold can act as the reactive-only comparator in local runs. Terraform may add a conservative CloudWatch alarm/target-tracking example, but Pulse remains the only application controller in dry-run tests.
7. **A-7:** Scheduled events may be managed through seed scripts and operational APIs; a full user-authenticated calendar administration product is not required.
8. **A-8:** The dashboard may poll REST endpoints at a short configurable interval instead of requiring WebSockets; “live” means continuously refreshing during a demo.
9. **A-9:** Portfolio metrics are calculated from observed runs and rendered as values or templates; no fabricated improvement percentage is committed when a benchmark has not been executed on the documented environment.
10. **A-10:** Terraform consumes an existing or explicitly supplied VPC/subnet where practical, or creates only minimal networking when needed. It must keep instance type/count configurable and default to a low-cost configuration.
11. **A-11:** GitHub Actions can validate Terraform without AWS credentials and can run backend/database/dashboard checks without deploying AWS resources.
12. **A-12:** The open pull request targets `codex/demo-app-foundation`, the repository's only current default branch, and is not merged by this workflow.

## 8. Risks

| ID | Risk | Impact | Likelihood | Mitigation |
|----|------|--------|------------|------------|
| R-1 | An erroneous detector or configuration could request excessive live AWS capacity. | High cost/account impact | Medium | Default dry-run, mandatory global/event clamp in the single adapter, validation, low Terraform defaults, alarms/logging, and explicit live opt-in. |
| R-2 | Noisy rates or a single transient sample could cause false positives or tier/capacity flapping. | Medium service/cost impact | Medium | Rolling windows, minimum sample count, confirmation periods, hysteresis, cooldown, idempotency, and deterministic noisy-signal tests. |
| R-3 | AWS instances may take longer to become healthy than a steep real-world spike allows. | High availability impact | Medium | Detect acceleration and edge/queue indicators early, model warm-up time in capacity recommendations, prewarm scheduled events, and shed non-critical work while capacity joins. |
| R-4 | Optional CloudFront/SQS/session signals may be unavailable or delayed. | Reduced forecast confidence | High in local demos | Provider health/status, graceful omission, signal freshness checks, application-rate fallback, and explicit simulated inputs for reproducible demos. |
| R-5 | PostgreSQL failure could prevent audit writes and therefore control transitions. | High control-plane availability impact | Low | Fail safe against unaudited mutation, structured error/status, bounded retries, health reporting, and recovery documentation. |
| R-6 | Multiple poll iterations could issue duplicate scheduled ramp or scaling actions. | Medium cost/audit impact | Medium | Stable idempotency keys, unique persistence constraint, transaction boundaries, and duplicate/concurrency tests. |
| R-7 | Load-test timing and p99 results may vary by workstation or AWS instance size. | Medium credibility impact | High | Store scenario configuration and raw summaries, use warm-up/repeated runs, report environment and medians/ranges, and avoid hard-coded resume claims. |
| R-8 | Dashboard polling could add unnecessary load or expose internal control data. | Low-to-medium performance/security impact | Medium | Read-only time-bounded APIs, configurable polling, no secret values in responses, CORS restricted/configurable, and no dashboard mutation credentials in the browser. |
| R-9 | Terraform networking/IAM choices may not fit every personal AWS account's quotas or default VPC state. | Medium deployment impact | Medium | Accept explicit VPC/subnet variables, keep resources minimal, validate inputs, document prerequisites and destroy procedure, and do not run apply in CI. |
| R-10 | Scope breadth across backend, frontend, load tests, infrastructure, CI, and documentation could leave superficially implemented placeholders. | High delivery-quality impact | Medium | Phase the implementation by independently testable vertical slices, enforce requirements traceability and entropy rules, and block final review on placeholder or superseded paths. |

## 9. Open questions

These questions do not block design or local implementation; the assumptions above define safe defaults.

1. Which AWS region, VPC/subnets, instance type, and account-specific maximum should be used for an optional live demo? Until supplied, examples will use variables, a conservative maximum, and dry-run mode.
2. What numeric portfolio improvements will the user's hardware or AWS demo produce? The workflow will implement the formulas and collection/export path; claims will remain templated or will use only measurements produced by an actual documented run.

## 10. Appendix

### Glossary

- **Acceleration:** Change in request-rate slope over time, used to detect demand building before raw load reaches a reactive threshold.
- **Leading signal:** Edge requests, queue growth, login/session growth, or acceleration that can precede origin CPU saturation.
- **Reactive comparator:** A configured CPU or request-level threshold representing when conventional reactive auto-scaling would trigger.
- **Prewarm:** Requesting capacity early enough for instances to launch and become healthy before peak traffic.
- **Shedding tier:** Reversible level 0–3 policy that progressively reduces non-critical work while protecting checkout.
- **Instance ceiling:** Hard configured upper bound applied to every desired-capacity command.
- **Correlation ID:** Identifier linking a snapshot/prediction, scaling action, shedding transition, and results evaluation.

### Reference files reviewed

- User PRD: `/Users/chirukuri.suryava/.codex/attachments/466abb41-e39f-42a4-ae63-0ca454365dbb/pasted-text.txt`
- `README.md`, `pyproject.toml`, `Makefile`, `.env.example`, `docker-compose.yml`, and `demo_app/Dockerfile`
- `common/enums.py`
- `demo_app/app/main.py`, `config.py`, `metrics.py`, `schemas.py`, `routes/`, and `shedding/`
- `db/base.py`, `db/session.py`, `db/types.py`, `db/models/`, and the initial Alembic migration
- `tests/unit/demo_app/` and `tests/unit/db/`
- `.cursor/project-context/project.mdc`, `architecture.mdc`, `coding-standards.mdc`, `deployment.mdc`, and `business-flows.mdc`

### Explicitly out of scope

- A production multi-region control plane, distributed consensus/leader election, enterprise SSO, or a shopper-facing commerce product.
- Advanced ML/LLM forecasting such as Prophet/LSTM in v1; the replacement boundary and future path will be documented.
- Production-scale CloudFront distributions, SQS workloads, NAT gateways, managed databases, or other resources beyond the minimal low-cost Terraform demonstration.
- Automatic merge or production deployment from the pull request.
