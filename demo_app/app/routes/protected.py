from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from common.enums import EndpointMode
from demo_app.app.schemas import CheckoutRequest
from demo_app.app.shedding.policies import endpoint_mode

router = APIRouter(tags=["protected-application"])


def _mode_headers(mode: EndpointMode) -> dict[str, str]:
    return {"X-Pulse-Endpoint-Mode": mode.value}


async def _limited_response(request: Request, endpoint: str) -> JSONResponse | None:
    allowed = await request.app.state.noncritical_limiter.allow()
    if allowed:
        return None
    return JSONResponse(
        status_code=429,
        content={
            "endpoint": endpoint,
            "degraded": True,
            "mode": EndpointMode.RATE_LIMITED.value,
            "detail": "Non-critical traffic is temporarily rate-limited",
        },
        headers={**_mode_headers(EndpointMode.RATE_LIMITED), "Retry-After": "1"},
    )


@router.post("/checkout")
async def checkout(payload: CheckoutRequest, request: Request) -> JSONResponse:
    # This path deliberately does not consult the load-shedding tier.
    await asyncio.sleep(0.002)
    level = request.app.state.shedding_controller.current().level
    return JSONResponse(
        content={
            "accepted": True,
            "cart_id": payload.cart_id,
            "item_count": payload.item_count,
            "message": "Checkout remains protected at every shedding tier",
            "active_shedding_level": int(level),
        },
        headers={
            **_mode_headers(EndpointMode.NORMAL),
            "X-Pulse-Protection": "critical-never-shed",
        },
    )


@router.get("/recommendations")
async def recommendations(
    request: Request,
    user_id: str = Query(default="demo-user", min_length=1, max_length=100),
) -> JSONResponse:
    level = request.app.state.shedding_controller.current().level
    mode = endpoint_mode("recommendations", level)

    if mode is EndpointMode.DISABLED:
        return JSONResponse(
            content={
                "user_id": user_id,
                "items": [],
                "degraded": True,
                "mode": mode.value,
                "detail": "Recommendations were disabled to protect critical capacity",
            },
            headers=_mode_headers(mode),
        )
    if mode is EndpointMode.RATE_LIMITED:
        limited = await _limited_response(request, "recommendations")
        if limited is not None:
            return limited
        return JSONResponse(
            content={
                "user_id": user_id,
                "items": [],
                "degraded": True,
                "mode": mode.value,
                "detail": "Minimal response served within the emergency rate limit",
            },
            headers=_mode_headers(mode),
        )

    await asyncio.sleep(0.015)
    return JSONResponse(
        content={
            "user_id": user_id,
            "items": [
                {"sku": "rec-101", "name": "Frequently bought item"},
                {"sku": "rec-202", "name": "Trending item"},
            ],
            "degraded": False,
            "mode": mode.value,
        },
        headers=_mode_headers(mode),
    )


@router.get("/catalog")
async def catalog(request: Request) -> JSONResponse:
    level = request.app.state.shedding_controller.current().level
    mode = endpoint_mode("catalog", level)

    if mode is EndpointMode.RATE_LIMITED:
        limited = await _limited_response(request, "catalog")
        if limited is not None:
            return limited
        mode = EndpointMode.CACHED

    if mode is EndpointMode.CACHED:
        return JSONResponse(
            content={
                "items": request.app.state.cached_catalog,
                "degraded": True,
                "mode": mode.value,
                "cache_status": "stale-while-revalidate",
                "cached_at": request.app.state.catalog_cached_at,
            },
            headers={**_mode_headers(mode), "Warning": '110 - "Response is stale"'},
        )

    await asyncio.sleep(0.010)
    return JSONResponse(
        content={
            "items": [
                {"sku": "sku-101", "name": "Pulse Headphones", "inventory": 51},
                {"sku": "sku-202", "name": "Pulse Speaker", "inventory": 17},
                {"sku": "sku-303", "name": "Pulse Charger", "inventory": 103},
            ],
            "degraded": False,
            "mode": mode.value,
            "generated_at": datetime.now(UTC).isoformat(),
        },
        headers=_mode_headers(mode),
    )
