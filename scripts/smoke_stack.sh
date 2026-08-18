#!/usr/bin/env bash
set -euo pipefail

project_name="${PULSE_SMOKE_PROJECT_NAME:-pulse-smoke}"
control_token="${PULSE_CONTROL_TOKEN:-pulse-smoke-ephemeral-token}"
results_dir="load_tests/results"
database_url="postgresql+psycopg://pulse:pulse-local-only@postgres:5432/pulse"

export COMPOSE_PROJECT_NAME="${project_name}"
export PULSE_POSTGRES_VOLUME="${project_name}-postgres-data"
export PULSE_CONTROL_TOKEN="${control_token}"

cleanup() {
  local exit_code=$?
  if ((exit_code != 0)); then
    docker compose ps --all || true
    docker compose logs --tail=200 postgres migrate demo-app agent dashboard || true
  fi
  docker compose down --volumes --remove-orphans || true
  exit "${exit_code}"
}
trap cleanup EXIT

docker compose config --quiet
mkdir -p "${results_dir}"
chmod 0777 "${results_dir}"
docker compose up --detach --build --wait postgres migrate demo-app agent dashboard
python scripts/verify_stack.py --control-token "${control_token}"

docker compose --profile load run --rm --entrypoint python locust \
  scripts/seed_scheduled_events.py \
  --database-url "${database_url}" \
  --starts-in-seconds 10 \
  --duration-seconds 20 \
  --peak-capacity 3 \
  --ceiling 3 \
  --smoke
PULSE_SCENARIO=scheduled-diwali PULSE_BASELINE_TYPE=pulse \
  docker compose --profile load run --rm locust

docker compose --profile load run --rm --entrypoint python locust \
  scripts/reset_demo.py \
  --database-url "${database_url}" \
  --agent-url http://agent:8100 \
  --demo-app-url http://demo-app:8000 \
  --control-token "${control_token}" \
  --complete-generated-events
PULSE_SCENARIO=sudden-spike PULSE_BASELINE_TYPE=pulse \
  docker compose --profile load run --rm locust

python -m scripts.verify_smoke_results --directory "${results_dir}"
python scripts/verify_stack.py --control-token "${control_token}"
test -d "${results_dir}"
test "$(find "${results_dir}" -type f \( -name '*.json' -o -name '*.md' \) | wc -l)" -ge 4
