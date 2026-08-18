# Pulse demo runbook

This runbook operates the credential-free local stack, executes scheduled and sudden surge
workflows, captures evidence, and restores state. Live AWS use is a separate explicit procedure.

## 1. Preflight

```bash
docker version
docker compose version
python3 --version
cp .env.example .env
```

Edit the uncommitted `.env` and replace `PULSE_CONTROL_TOKEN`. Keep
`PULSE_EXECUTION_MODE=dry_run`, `PULSE_MAX_INSTANCE_CEILING=3`, and
`PULSE_SIMULATED_DESIRED_CAPACITY=1`. Do not add AWS credentials to the file.

Check the resolved topology and start it:

```bash
docker compose config --quiet
docker compose up --build --wait
docker compose ps
```

Expected order is PostgreSQL healthy → migration complete → demo app healthy → agent healthy →
dashboard healthy. Confirm status:

```bash
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8100/health
curl -fsS http://localhost:8100/api/v1/status
```

The agent status must say `dry_run`, its global ceiling must be at most three, and the protected
app must report `critical_path_protected: true`.

## 2. Scheduled Diwali smoke

The wrapper seeds one deterministic `source=load_test` event relative to the current UTC clock.
Its smoke ramp begins eight seconds before the event and reaches capacity three six seconds before
the event start.

```bash
PULSE_PYTHON=.venv/bin/python load_tests/scripts/run_scheduled.sh \
  --control-token '<YOUR_LOCAL_TOKEN>' \
  --smoke
```

For the full timed profile, omit `--smoke`. Verify the event, forecast, and exact-once ramp:

```bash
curl -fsS 'http://localhost:8100/api/v1/scheduled-events?status=active&limit=20'
curl -fsS 'http://localhost:8100/api/v1/predictions?mode=scheduled&limit=20'
curl -fsS 'http://localhost:8100/api/v1/scaling-actions?execution_mode=dry_run&limit=50'
```

The ramp keys have `scheduled:<event-id>:<offset>:<capacity>` form. Repeated polling returns the
same stored actions and does not create another provider attempt.

## 3. Sudden-spike smoke

```bash
PULSE_PYTHON=.venv/bin/python load_tests/scripts/run_sudden_spike.sh \
  --control-token '<YOUR_LOCAL_TOKEN>' \
  --smoke
```

The fixture publishes edge/queue/session-shaped evidence before its CPU comparator frame. The
Compose profile defaults `PULSE_REACTIVE_LOAD_THRESHOLD_RPS` to `100000`, so the bounded smoke
measures lead against that deterministic CPU frame instead of letting generated request rate trip
the optional raw-load comparator first. Lower the setting when you intentionally want to exercise
the raw-load comparator. Verify:

```bash
curl -fsS 'http://localhost:8100/api/v1/predictions?mode=realtime&limit=20'
curl -fsS 'http://localhost:8100/api/v1/scaling-actions?limit=50'
curl -i -X POST http://localhost:8000/checkout \
  -H 'Content-Type: application/json' \
  -d '{"cart_id":"runbook-checkout","item_count":1}'
```

The checkout response remains successful with `X-Pulse-Endpoint-Mode: normal`. A valid predictive
run has a prediction timestamp before `reactive_comparator_crossed_at`; if the comparator never
crosses, lead time is explicitly unavailable.

## 4. Equal-input comparison

Run reactive-only first, reset scoped state, and then run Pulse with the same scenario/seed:

```bash
PULSE_PYTHON=.venv/bin/python load_tests/scripts/run_pair.sh \
  --scenario sudden-spike \
  --control-token '<YOUR_LOCAL_TOKEN>' \
  --seed 20260818 \
  --smoke
```

Repeat without `--smoke` for portfolio evidence. Keep host, seed, endpoint mix, duration,
thresholds, execution mode, instance capacity assumption, and workstation/cloud environment
identical. Do not compare runs with warnings that identify incomplete or mismatched evidence.

## 5. Evidence and results

Each wrapper prints `run_id=<UUID>`. Inspect or export that exact persisted run:

```bash
curl -fsS 'http://localhost:8100/api/v1/results/runs/<RUN_UUID>'
.venv/bin/python scripts/export_results.py --run-id '<RUN_UUID>'
```

Files under `load_tests/results/` are ignored. Copy reviewed reports to an external portfolio
location or fill [results-template.md](results-template.md) with the evidence URI/run IDs. Never
commit a claim that cannot be reconciled to a completed run.

## 6. Reset and cleanup

Return the tier to zero, cancel an unfinished generated run, and optionally delete only generated
scenario runs/events:

```bash
.venv/bin/python scripts/reset_demo.py \
  --control-token '<YOUR_LOCAL_TOKEN>'
.venv/bin/python scripts/reset_demo.py \
  --control-token '<YOUR_LOCAL_TOKEN>' \
  --purge-generated
```

Stop while retaining PostgreSQL data:

```bash
docker compose down
```

Delete the named local data volume only after confirming its demo data is disposable:

```bash
docker compose down --volumes
```

The automated `scripts/smoke_stack.sh` uses a separately named Compose project/volume, runs both
smokes with bounded plans, verifies all four checkout tiers, and removes only that isolated stack.

## 7. Troubleshooting

| Symptom | Check | Safe response |
|---|---|---|
| `migrate` exits | `docker compose logs migrate postgres` | fix DB URL/readiness; rerun `docker compose up --wait` |
| agent 503 | agent log plus `/health` database/worker fields | keep dry-run; restore origin/DB; no mutation is attempted |
| no real-time prediction | `/metrics/snapshot`, provider health, sample/confirmation/confidence evidence | run the deterministic smoke; do not lower thresholds blindly |
| scheduled point absent | event UTC times, timezone, status, ramp offsets, agent clock | reseed relative event; verify clock/NTP |
| run stuck pending | feedback worker health and run evidence window | wait bounded evaluation timeout; reset/cancel if incomplete |
| dashboard partial warning | browser-visible agent URL/CORS and failing API path | fix `NEXT_PUBLIC_PULSE_API_URL`; no token belongs in browser |
| port in use | `docker compose ps` and local listeners | change `PULSE_*_PORT` values in uncommitted `.env` |
| live startup blocked | execution mode, region, ASG, ceiling, standard credential chain | treat as intentional fail-closed; return to dry-run |

## 8. Optional AWS plan/apply/destroy

Use only after supplying reviewed existing VPC/subnet/security-group/AMI values and setting a
spend ceiling. Credentials come from the standard AWS chain, never files in this repository.

```bash
cp infra/environments/demo.tfvars.example infra/environments/demo.auto.tfvars
terraform -chdir=infra fmt -check -recursive
terraform -chdir=infra init -backend=false
terraform -chdir=infra validate
terraform -chdir=infra plan \
  -var-file=environments/demo.auto.tfvars \
  -out=pulse-demo.tfplan
terraform -chdir=infra show pulse-demo.tfplan
```

After explicit authorization and plan review only:

```bash
terraform -chdir=infra apply pulse-demo.tfplan
terraform -chdir=infra output
terraform -chdir=infra plan -destroy -var-file=environments/demo.auto.tfvars
terraform -chdir=infra destroy -var-file=environments/demo.auto.tfvars
```

Confirm the ASG, launch template, alarm, instance profile, role, and policy are gone. Applying the
stack alone does not enable live Pulse mutation; `PULSE_EXECUTION_MODE=live`, region, ASG name,
application ceiling, and credentials are separate explicit runtime inputs.
