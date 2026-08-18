# Complete Predictive Surge Platform

| Field | Value |
|-------|-------|
| Date | 2026-08-18 |
| Jira | `PENDING-EPIC` — manifest-only dry run; no external Jira URL |
| Release version | TBD |
| PR | https://github.com/Suryavarma333/pulse-engine/pull/1 |

## Summary

Pulse Engine now anticipates sudden and scheduled traffic surges, safely prepares capacity, protects
non-checkout journeys when pressure rises, and gradually recovers when demand falls. The delivery also
adds a live operational dashboard and reproducible before/after evidence so product, support, and
engineering teams can understand what happened and verify that checkout remained available.

## What changed

- **Predictive protection:** The platform detects rapidly accelerating traffic and planned events, then
  prepares capacity before a conventional reactive response. All capacity decisions are bounded by a
  configured safety ceiling and run in simulation mode by default.
- **User journeys / policies:** Checkout is always protected. Catalog and recommendation traffic can be
  progressively reduced through three protection levels, while renewed traffic pressure takes priority
  over recovery and recovery proceeds gradually.
- **Operations / integrations:** Operators can run sudden-spike and scheduled-event demonstrations,
  inspect platform health and worker readiness, review decisions, and optionally connect workload CPU,
  queue, CDN, or session signals. Live AWS changes require explicit configuration and approval.
- **Notifications / reporting:** The dashboard presents a versioned view of demand, predictions,
  capacity, pending capacity, protection level, scheduled events, and comparative run results. Partial or
  unsupported data is labelled instead of being guessed.
- **Data / records:** The platform records demo settings, predictions, capacity actions, protection
  changes, delivery retries, and run results so decisions can be traced after restarts. Raw traffic
  snapshots are retained for seven days by default; higher-value audit and run records are preserved.
- **Delivery controls:** Local startup is health-gated, database changes are migration-managed, and six
  automated checks cover Python, PostgreSQL, the dashboard, containers, infrastructure policy, and
  secrets.

## Who is affected

- **Customers:** Checkout remains available through all protection levels; catalog and recommendation
  experiences may be intentionally reduced during severe pressure.
- **Product and support teams:** A dashboard and paired-run evidence explain when protection activated,
  how much lead time prediction provided, and whether the experience recovered cleanly.
- **Operators:** New configuration controls simulation versus live AWS behavior, provider regions,
  capacity ceilings, worker health, retention, scheduled events, and rollback readiness.
- **Developers and API consumers:** Health, shedding-control, snapshot-selector, and snapshot-version
  contracts are stricter and must be handled according to their documented status and validation rules.

## How to verify

- Start the clean local platform and confirm the demo application, agent, and dashboard report healthy.
- Run the sudden-spike scenario and verify that prediction is recorded before the reactive comparator,
  checkout remains normal, and protection/capacity decisions appear in the dashboard.
- Run the scheduled-event scenario and verify that each preparation step executes once, peak capacity is
  reached before the event, and recovery lowers protection and capacity gradually afterward.
- Open a completed paired run and confirm the effective settings, capacity evidence, lead-time result,
  customer-protection result, and any unavailable metric warnings are visible and traceable.
- Confirm all six automated delivery checks pass for the pull request.

## Rollback / limitations

- Before rollback, operators must drain pending capacity actions, protection-stage deliveries, and
  retries. Reverting database migrations removes the new delivery and run evidence stored by them.
- One active agent instance is supported. Operator read APIs have no application authentication and must
  remain network-restricted.
- Live AWS operation is opt-in and needs an approved target, credentials, provider regions, and safety
  ceilings. The provided infrastructure definition covers the bounded scaling target and supporting
  permissions/monitoring, not the entire application platform.
- Demo health may return HTTP 503 during a recoverable persistence outage; control clients must support
  stricter validation and structured conflict/unavailable responses. Raw traffic evidence older than the
  configured retention window must be exported in advance.

## Reference (optional, brief)

- Requirement: Complete Predictive Surge Platform
- Jira: `PENDING-EPIC` (manifest-only dry run)
