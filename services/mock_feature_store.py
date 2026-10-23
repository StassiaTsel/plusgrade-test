"""
Mock Feature Store — stands in for the internal feature service.

DO NOT MODIFY THIS FILE. Reviewers run your submission against this exact service.
(You may change environment variables to experiment, but your submission must work
with the defaults.)

Behaviour you need to know about:
  * Every endpoint is artificially slow.
  * The service accepts a limited number of concurrent requests and returns
    HTTP 429 (with Retry-After) beyond that.
  * A small fraction of requests fail with HTTP 503.
  * The route endpoint has a fat latency tail.
  * There is a batch endpoint that is much cheaper per item.

Run:  uvicorn services.mock_feature_store:app --port 8081
"""

import asyncio
import hashlib
import os
import random
import time
from typing import Dict, List

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------- configuration

# Read service limits and simulation rates from the environment, using the
# assignment defaults when no overrides are supplied.
MAX_CONCURRENCY = int(os.getenv("FS_MAX_CONCURRENCY", "20"))
FAILURE_RATE = float(os.getenv("FS_FAILURE_RATE", "0.02"))
TAIL_RATE = float(os.getenv("FS_TAIL_RATE", "0.03"))
SEED = int(os.getenv("FS_SEED", "1337"))

# Create the FastAPI application exposed by this mock service.
app = FastAPI(title="Mock Feature Store", version="1.0.0")

# Track request volume, concurrency, errors, and served feature items.
_stats = {
    "started_at": time.time(),
    "requests_total": 0,
    "requests_by_endpoint": {},
    "items_served": 0,
    "rejected_429": 0,
    "errors_503": 0,
    "inflight": 0,
    "peak_inflight": 0,
}
# Protect concurrent updates to the in-flight request counters.
_inflight_lock = asyncio.Lock()
# Seed the random generator so simulated behavior can be reproduced.
_rng = random.Random(SEED)


class _Admitted:
    """Counts in-flight requests and rejects past the cap instead of queueing."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.ok = False

    async def __aenter__(self):
        # Update request totals and check the concurrency limit atomically.
        async with _inflight_lock:
            _stats["requests_total"] += 1
            _stats["requests_by_endpoint"][self.endpoint] = (
                _stats["requests_by_endpoint"].get(self.endpoint, 0) + 1
            )
            if _stats["inflight"] >= MAX_CONCURRENCY:
                # Reject excess work immediately rather than queueing it.
                _stats["rejected_429"] += 1
                return self
            # Admit this request and record the new concurrency peak.
            _stats["inflight"] += 1
            _stats["peak_inflight"] = max(_stats["peak_inflight"], _stats["inflight"])
            self.ok = True
        return self

    async def __aexit__(self, *exc):
        # Release the concurrency slot even when endpoint processing raises.
        if self.ok:
            async with _inflight_lock:
                _stats["inflight"] -= 1
        return False


def _too_many_requests() -> JSONResponse:
    # Tell callers when to retry after they exceed the concurrency cap.
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": "1"},
        content={"error": "too_many_requests", "detail": f"max {MAX_CONCURRENCY} concurrent requests"},
    )


def _unavailable() -> JSONResponse:
    # Return a retryable error for simulated transient upstream failures.
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "1"},
        content={"error": "service_unavailable", "detail": "transient upstream failure, retry"},
    )


def _h(*parts: str) -> int:
    # Produce a stable integer from identifiers for deterministic mock data.
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest(), 16)


# ------------------------------------------------------------- feature builders

TIERS = ["none", "silver", "gold", "platinum"]


def _passenger_features(passenger_id: str) -> Dict:
    # Derive stable passenger attributes from the passenger identifier.
    n = _h("pax", passenger_id)
    return {
        "passenger_id": passenger_id,
        "loyalty_tier": TIERS[n % 4],
        "lifetime_bookings": n % 87,
        "prior_upgrade_purchases": (n >> 7) % 12,
        "prior_offers_seen": (n >> 11) % 40,
        "avg_ancillary_spend_usd": round(10 + (n >> 13) % 24000 / 100, 2),
        "days_since_last_trip": (n >> 17) % 400,
        "email_engagement_score": round(((n >> 19) % 1000) / 1000, 3),
        "price_sensitivity_index": round(((n >> 23) % 1000) / 1000, 3),
    }


def _route_features(route_code: str) -> Dict:
    # Derive stable route attributes from the route code.
    n = _h("route", route_code)
    return {
        "route_code": route_code,
        "avg_load_factor": round(0.55 + ((n % 400) / 1000), 3),
        "premium_cabin_seats": 8 + (n >> 5) % 40,
        "historical_upgrade_take_rate": round(((n >> 9) % 250) / 1000, 4),
        "avg_accepted_offer_usd": round(80 + ((n >> 13) % 42000) / 100, 2),
        "competitive_density": (n >> 17) % 5,
        "seasonality_index": round(0.7 + ((n >> 21) % 600) / 1000, 3),
    }


async def _sleep_passenger():
    # Simulate the passenger feature lookup latency.
    await asyncio.sleep(_rng.uniform(0.120, 0.400))


async def _sleep_route():
    # Simulate the route lookup latency, including its slow tail.
    if _rng.random() < TAIL_RATE:
        await asyncio.sleep(_rng.uniform(2.5, 4.0))  # fat tail — mind your timeouts
    else:
        await asyncio.sleep(_rng.uniform(0.080, 0.250))


# -------------------------------------------------------------------- endpoints


@app.get("/healthz")
async def healthz():
    # Report that the mock service is available.
    return {"status": "ok"}


@app.get("/v1/passengers/{passenger_id}/features")
async def passenger_features(passenger_id: str):
    # Reserve a concurrency slot before beginning the simulated lookup.
    async with _Admitted("passenger") as adm:
        if not adm.ok:
            return _too_many_requests()
        # Wait for the mock dependency, then simulate a transient failure.
        await _sleep_passenger()
        if _rng.random() < FAILURE_RATE:
            _stats["errors_503"] += 1
            return _unavailable()
        # Count and return the requested passenger's deterministic features.
        _stats["items_served"] += 1
        return _passenger_features(passenger_id)


@app.get("/v1/routes/{route_code}/features")
async def route_features(route_code: str):
    # Reserve a concurrency slot before beginning the simulated lookup.
    async with _Admitted("route") as adm:
        if not adm.ok:
            return _too_many_requests()
        # Wait for the mock dependency, then simulate a transient failure.
        await _sleep_route()
        if _rng.random() < FAILURE_RATE:
            _stats["errors_503"] += 1
            return _unavailable()
        # Count and return the requested route's deterministic features.
        _stats["items_served"] += 1
        return _route_features(route_code)


class BatchRequest(BaseModel):
    # Accept between one and fifty passenger identifiers per batch request.
    passenger_ids: List[str] = Field(..., min_length=1, max_length=50)


@app.post("/v1/passengers/features:batch")
async def passenger_features_batch(req: BatchRequest):
    """Up to 50 passengers per call. Costs one slot against the concurrency cap."""
    # Reserve one concurrency slot for the whole batch.
    async with _Admitted("passenger_batch") as adm:
        if not adm.ok:
            return _too_many_requests()
        # Simulate batch latency, which grows with the number of passengers.
        await asyncio.sleep(0.200 + 0.015 * len(req.passenger_ids))
        if _rng.random() < FAILURE_RATE:
            _stats["errors_503"] += 1
            return _unavailable()
        # Count all returned items and build each passenger's features.
        _stats["items_served"] += len(req.passenger_ids)
        return {"features": [_passenger_features(p) for p in req.passenger_ids]}


@app.get("/v1/stats")
async def stats():
    """Introspection for you and for reviewers. Not part of the real service."""
    # Return a snapshot of counters with uptime and the configured concurrency cap.
    return {**_stats, "uptime_s": round(time.time() - _stats["started_at"], 1),
            "max_concurrency": MAX_CONCURRENCY}


@app.post("/v1/stats:reset")
async def stats_reset():
    # Clear run-specific counters so a new measurement starts from a clean slate.
    for k, v in (("requests_total", 0), ("items_served", 0), ("rejected_429", 0),
                 ("errors_503", 0), ("peak_inflight", 0)):
        _stats[k] = v
    # Reset per-endpoint counts and restart the uptime clock.
    _stats["requests_by_endpoint"] = {}
    _stats["started_at"] = time.time()
    return {"status": "reset"}
