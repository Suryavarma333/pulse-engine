#!/usr/bin/env bash
set -euo pipefail

exec "${PULSE_PYTHON:-.venv/bin/python}" load_tests/scripts/run_pair.py "$@"
