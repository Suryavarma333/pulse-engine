#!/usr/bin/env bash
set -euo pipefail

seed_args=(--starts-in-seconds 120 --duration-seconds 270)
for argument in "$@"; do
  if [[ "${argument}" == "--smoke" ]]; then
    seed_args=(--smoke --starts-in-seconds 10 --duration-seconds 20)
  fi
done
"${PULSE_PYTHON:-.venv/bin/python}" scripts/seed_scheduled_events.py "${seed_args[@]}"
exec "${PULSE_PYTHON:-.venv/bin/python}" load_tests/scripts/run_scenario.py \
  --scenario scheduled-diwali "$@"
