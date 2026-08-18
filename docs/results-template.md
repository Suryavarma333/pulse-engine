# Pulse evidence-backed results template

Use one copy per environment/scenario pair. Values must come from completed persisted runs and
exported evidence. Leave a cell `not available` when its required evidence is absent; never infer
an improvement from a smoke-only correctness run.

## Run identity

| Field | Reactive-only | Pulse |
|---|---|---|
| run ID | `not available` | `not available` |
| pair group ID / pair role | `not available` | `not available` |
| evidence URI/path | `not available` | `not available` |
| scenario | `sudden-spike` or `scheduled-diwali` | same |
| status | `not available` | `not available` |
| environment / host | `not available` | same |
| execution mode | `dry_run` or authorized `live` | same |
| UTC start/end | `not available` | `not available` |
| seed | `not available` | same |
| duration / users / spawn stages | `not available` | same |
| endpoint weights | checkout 5, catalog 3, recommendations 2 | same |
| detector thresholds | `not available` | `not available` |
| settings snapshot version | `v1` | `v1` |
| snapshot schema version | `pulse.snapshot.v1` | `pulse.snapshot.v1` |
| capacity per instance / floor / ceilings | `not available` | same |
| provider regions (workload / CloudFront) | `not available` | same |
| formula version | `v1` | `v1` |
| warnings | `not available` | `not available` |

## Required portfolio metrics

| Metric | Reactive-only | Pulse | Difference / interpretation |
|---|---:|---:|---|
| checkout p99 latency (ms) | `not available` | `not available` | report absolute and percent only when both exist |
| checkout success rate | `not available` | `not available` | checkout must remain protected in both runs |
| proactive detection lead (s) | `not available` | `not available` | comparator time minus prediction time |
| provisioning efficiency (%) | `not available` | `not available` | required instance-minutes / provisioned instance-minutes |

## Additional metrics

| Metric | Reactive-only | Pulse | Notes |
|---|---:|---:|---|
| prediction error (%) | `not available` | `not available` | absolute peak forecast error |
| over-provisioned instance-minutes | `not available` | `not available` | lower is efficient only if reliability remains acceptable |
| under-provisioned seconds | `not available` | `not available` | contextualize with checkout p99/success |
| request error rate | `not available` | `not available` | all endpoints or explicitly named subset |
| recovery duration (s) | `not available` | `not available` | peak/end to return-to-normal evidence |
| cost duration (s) | `not available` | `not available` | time above minimum desired capacity |

## Formula reconciliation (v1)

- Checkout success rate = successful checkout attempts / checkout attempts.
- Detection lead seconds = `reactive_comparator_crossed_at - prediction.created_at`. A negative
  value is reported as negative; a missing comparator is `not available`.
- Required instances per sample = `ceil(actual_rps / configured_rps_per_instance)`.
- Provisioning efficiency = required instance-minutes / provisioned instance-minutes × 100.
- Prediction error = `abs(predicted_peak_rps - actual_peak_rps) / max(actual_peak_rps, epsilon)` ×
  100.
- Over-provisioned instance-minutes integrate `max(provisioned - required, 0)` over time.
- Under-provisioned seconds integrate intervals where required instances exceed provisioned.

Zero denominators, incomplete intervals, missing raw latency data, absent comparator crossings,
and unobserved recovery must remain `not available` with their emitted warning.

## Evidence checklist

- [ ] Both run IDs resolve through `/api/v1/results/runs/<RUN_UUID>` and have completed status.
- [ ] Scenario, seed, host, duration, stages, endpoint weights, thresholds, execution mode, and
      capacity assumptions match.
- [ ] The exported raw Locust summary reconciles request/failure and checkout percentile values.
- [ ] Prediction, snapshot, scaling-action, and shedding-event references resolve.
- [ ] Each traffic row declares `pulse.snapshot.v1` and includes ASG desired, in-service, derived
      pending capacity, and the persisted per-instance capacity assumption used by formulas.
- [ ] The server-recorded effective settings snapshot is version `v1`, and the paired runs share the
      same pair group with complementary reactive/Pulse roles.
- [ ] Sudden-spike prediction precedes the comparator for any positive lead claim.
- [ ] Scheduled peak action is due and recorded before the event start and appears once.
- [ ] All capacity outcomes are at or below the effective ceiling.
- [ ] Checkout stays `normal` at levels 0–3 and its success/p99 claims use actual attempts.
- [ ] Warnings and unavailable metrics remain visible in the published result.
- [ ] Environment variance and dry-run versus live limitations are disclosed.
- [ ] Raw evidence needed beyond the default seven-day snapshot-retention window was exported before
      cleanup, and the evidence URI remains accessible to reviewers.

## Narrative

> In `<environment>`, paired `<scenario>` runs `<reactive-run-id>` and `<pulse-run-id>` used the
> same `<seed/config>`. Pulse detected the surge `<value or not available>` seconds before the
> reactive comparator. Checkout p99 was `<value or not available>` versus
> `<value or not available>`, with success rates `<value or not available>`. Provisioning
> efficiency was `<value or not available>`. These values use formula `v1`; warnings were
> `<warnings or none>`. No broader production claim is implied.

## Current repository evidence

No benchmark run artifact is committed. Measured improvement values are therefore **not
available** in source control. Run the documented equal-input workflow, retain the generated
run-ID-linked exports outside ignored build output, and complete this template from that evidence.
