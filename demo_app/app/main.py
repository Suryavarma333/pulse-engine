from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request

from db.session import Database
from demo_app.app.config import AppSettings
from demo_app.app.metrics import TrafficMetrics
from demo_app.app.routes.operations import router as operations_router
from demo_app.app.routes.protected import router as protected_router
from demo_app.app.shedding.rate_limiter import TokenBucket
from demo_app.app.shedding.state import LoadSheddingController
from demo_app.app.shedding.store import (
    InMemoryLoadSheddingStore,
    LoadSheddingStore,
    SqlAlchemyLoadSheddingStore,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

_METRICS_EXCLUDED_PREFIXES = ("/health", "/metrics", "/internal", "/docs", "/openapi.json")


def create_app(
    settings: AppSettings | None = None,
    *,
    store: LoadSheddingStore | None = None,
) -> FastAPI:
    app_settings = settings or AppSettings.from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database: Database | None = None
        selected_store = store
        if selected_store is None and app_settings.database_url:
            database = Database(app_settings.database_url)
            selected_store = SqlAlchemyLoadSheddingStore(database.session_factory)
        if selected_store is None:
            selected_store = InMemoryLoadSheddingStore()

        controller = LoadSheddingController(selected_store, app_settings.environment)
        await controller.load()
        app.state.shedding_controller = controller
        app.state.shedding_store = selected_store
        app.state.database = database
        try:
            yield
        finally:
            if database is not None:
                await database.dispose()

    app = FastAPI(
        title="Pulse Protected Demo Application",
        version="0.1.0",
        description="A minimal commerce API with tiered, reversible load shedding.",
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.traffic_metrics = TrafficMetrics(app_settings.metrics_window_seconds)
    app.state.noncritical_limiter = TokenBucket(
        rate_per_second=app_settings.noncritical_rate_per_second,
        burst=app_settings.noncritical_rate_burst,
    )
    app.state.cached_catalog = [
        {"sku": "sku-101", "name": "Pulse Headphones"},
        {"sku": "sku-202", "name": "Pulse Speaker"},
    ]
    app.state.catalog_cached_at = datetime.now(UTC).isoformat()

    @app.middleware("http")
    async def collect_request_metrics(request: Request, call_next):
        excluded = request.url.path.startswith(_METRICS_EXCLUDED_PREFIXES)
        if excluded:
            return await call_next(request)

        await app.state.traffic_metrics.request_started()
        started_at = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            level = app.state.shedding_controller.current().level
            response.headers["X-Pulse-Shedding-Level"] = str(int(level))
            return response
        finally:
            latency_ms = (time.perf_counter() - started_at) * 1000
            await app.state.traffic_metrics.request_finished(
                path=request.url.path,
                latency_ms=latency_ms,
                status_code=status_code,
            )

    app.include_router(protected_router)
    app.include_router(operations_router)
    return app


app = create_app()
