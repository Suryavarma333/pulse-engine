# Pulse

Pulse is a predictive traffic-surge control plane for a protected commerce application. This
first implementation slice contains the PostgreSQL audit schema and a FastAPI demo application
with a critical checkout path and reversible, tiered load shedding.

## Current slice

- `POST /checkout` is critical and remains normal at shedding levels 0–3.
- `GET /recommendations` can be disabled and later emergency-rate-limited.
- `GET /catalog` can serve a stale cached response and be rate-limited.
- `GET /health` reports the active tier and policy for every endpoint.
- `GET /metrics/snapshot` exposes a rolling request-rate and latency snapshot for the Pulse agent.
- `POST /internal/load-shedding` changes the tier with token authentication and an audit reason.
- PostgreSQL records active shedding intervals and provides the remaining Pulse audit tables.

## Run with Docker Compose

```bash
cp .env.example .env
docker compose up --build
```

The API is then available at `http://localhost:8000`, with interactive documentation at
`http://localhost:8000/docs`. Compose waits for PostgreSQL, applies the Alembic migration, and
only then starts the application.

Change the shedding tier locally:

```bash
curl -X POST http://localhost:8000/internal/load-shedding \
  -H 'Content-Type: application/json' \
  -H 'X-Pulse-Control-Token: local-demo-token' \
  -d '{
    "level": 2,
    "reason_code": "manual_demo",
    "reasoning": "Demonstrate cached non-critical responses",
    "changed_by": "operator",
    "signal_evidence": {"request_acceleration_rps2": 8.5}
  }'
```

Verify that checkout is still protected:

```bash
curl -i -X POST http://localhost:8000/checkout \
  -H 'Content-Type: application/json' \
  -d '{"cart_id": "demo-cart", "item_count": 2}'
```

## Local Python development

Pulse requires Python 3.11 or newer.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
make install
make test
```

Do not use the example control token or database password in a shared environment. The complete
architecture, agents, load scenarios, dashboard, Terraform, and benchmark results will be added
in subsequent independently testable slices.
