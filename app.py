"""Upgrade propensity scoring service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field


LOG = logging.getLogger("upgrade_scoring")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(message)s",
)

# Step 1: Load the booking dataset and configure the service endpoints used to
# talk to the feature store and model provider.
ROOT = Path(__file__).resolve().parent
with (ROOT / "data" / "bookings.json").open() as f:
    BOOKINGS: dict[str, dict[str, Any]] = {
        item["booking_id"]: item for item in json.load(f)
    }

FEATURE_STORE_URL = os.getenv("FEATURE_STORE_URL", "http://localhost:8081")
MODEL_URL = os.getenv("MODEL_URL", "http://localhost:8082")
MODEL_NAME = os.getenv("MODEL_NAME", "upgrade-propensity-v1")
MAX_RETRIES = int(os.getenv("UPSTREAM_MAX_RETRIES", "3"))
REQUEST_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "10"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_PREFIX = os.getenv("REDIS_KEY_PREFIX", "upgrade-scoring")


# Step 2: Standardize timestamps so job and score metadata are easy to inspect.
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Step 3: Define the incoming request shape for a scoring batch.
class ScoreJobRequest(BaseModel):
    booking_ids: list[str] = Field(min_length=1, max_length=250)


# Step 4: Represent each scoring job and keep track of progress, results, and
# errors while the work runs in the background.
class Job:
    def __init__(self, job_id: str, booking_ids: list[str]):
        self.job_id = job_id
        self.booking_ids = booking_ids
        self.lock = asyncio.Lock()

    @property
    def key(self) -> str:
        return f"{REDIS_PREFIX}:job:{self.job_id}"

    @property
    def results_key(self) -> str:
        return f"{self.key}:results"

    @property
    def errors_key(self) -> str:
        return f"{self.key}:errors"

    async def initialize(self) -> None:
        assert redis_client is not None
        await redis_client.hset(self.key, mapping={
            "job_id": self.job_id,
            "booking_ids": json.dumps(self.booking_ids),
            "status": "pending",
            "submitted": len(self.booking_ids),
            "completed": 0,
            "failed": 0,
        })

    async def update(self, **values: Any) -> None:
        assert redis_client is not None
        await redis_client.hset(self.key, mapping=values)

    async def increment(self, field: str, amount: int = 1) -> None:
        assert redis_client is not None
        await redis_client.hincrby(self.key, field, amount)

    async def append(self, collection: str, value: dict[str, Any]) -> None:
        assert redis_client is not None
        await redis_client.rpush(collection, json.dumps(value, separators=(",", ":")))

    async def snapshot(self) -> dict[str, Any]:
        assert redis_client is not None
        data = await redis_client.hgetall(self.key)
        if not data:
            raise KeyError(self.job_id)
        results, errors = await asyncio.gather(
            redis_client.lrange(self.results_key, 0, -1),
            redis_client.lrange(self.errors_key, 0, -1),
        )
        return {
            "job_id": data["job_id"],
            "status": data["status"],
            "submitted": int(data["submitted"]),
            "completed": int(data["completed"]),
            "failed": int(data["failed"]),
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
            "results": [json.loads(item) for item in results],
            "errors": [json.loads(item) for item in errors],
        }

    @classmethod
    async def load(cls, job_id: str) -> "Job | None":
        assert redis_client is not None
        data = await redis_client.hgetall(f"{REDIS_PREFIX}:job:{job_id}")
        if not data:
            return None
        return cls(job_id, json.loads(data["booking_ids"]))


# Step 5: Redis stores job state, live progress, results, errors, and scores.
# The local set only tracks tasks owned by this process for shutdown. Startup
# does not scan or resume jobs left by an earlier process.
redis_client: redis.Redis | None = None
background_tasks: set[asyncio.Task] = set()
client: httpx.AsyncClient | None = None

# Step 6: Track operational counters so we can observe retries, throughput, and
# latency during incidents or tuning work.
metrics = {
    "jobs_submitted": 0,
    "jobs_completed": 0,
    "jobs_failed": 0,
    "bookings_completed": 0,
    "bookings_failed": 0,
    "model_calls": 0,
    "model_failures": 0,
    "upstream_retries": 0,
    "model_latency_ms_total": 0.0,
}
metrics_lock = asyncio.Lock()

# The feature store's concurrency limit is process-wide, not per endpoint or
# per job. Sharing this semaphore prevents concurrent jobs from collectively
# exceeding the provider's limit.
feature_store_semaphore = asyncio.Semaphore(20)


# Step 7: Initialize the shared HTTP client and ensure only this process's
# background work is shut down cleanly when the process begins stopping.
@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, redis_client
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await redis_client.ping()
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=3.0),
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=40),
        )
        yield
    finally:
        tasks = list(background_tasks)
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=15.0)
            # Do not leave work running against a client that is about to be
            # closed. Uvicorn normally gives this path enough time to finish;
            # cancellation is the bounded fallback for a long upstream tail.
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if client is not None:
            await client.aclose()
            client = None
        if redis_client is not None:
            await redis_client.aclose()
            redis_client = None


app = FastAPI(title="Upgrade Offer Scoring Service", version="1.0", lifespan=lifespan)


# Step 8: Update shared metrics safely under a lock so concurrent job workers do
# not corrupt the counters.
async def metric(name: str, amount: int | float = 1) -> None:
    async with metrics_lock:
        metrics[name] += amount


# Step 9: Retry transient upstream failures with bounded backoff so a short
# outage does not fail the entire job or stall the API.
async def upstream_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    assert client is not None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await client.request(method, url, **kwargs)
            if response.status_code not in (429, 500, 502, 503, 504):
                response.raise_for_status()
                return response
            if attempt == MAX_RETRIES:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After")
            delay = min(float(retry_after) if retry_after else 0.25 * (2**attempt), 3.0)
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == MAX_RETRIES:
                raise
            delay = min(0.25 * (2**attempt), 3.0)
        await metric("upstream_retries")
        await asyncio.sleep(delay)
    raise RuntimeError("unreachable")


# Step 10: Deduplicate feature lookups and batch passenger requests so the job
# stays fast without overwhelming the feature-store concurrency limits. Route
# requests are issued concurrently, with the shared semaphore enforcing the cap.
async def fetch_passengers(
    passenger_ids: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Fetch unique passengers in batches; each batch consumes one FS slot."""

    async def fetch_batch(batch: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        try:
            async with feature_store_semaphore:
                response = await upstream_request(
                    "POST", f"{FEATURE_STORE_URL}/v1/passengers/features:batch",
                    json={"passenger_ids": batch},
                )
            return (
                {item["passenger_id"]: item for item in response.json()["features"]},
                {},
            )
        except Exception as exc:
            message = f"passenger feature lookup failed: {exc}"
            return {}, {passenger_id: message for passenger_id in batch}

    batches = [passenger_ids[i : i + 50] for i in range(0, len(passenger_ids), 50)]
    values = await asyncio.gather(*(fetch_batch(batch) for batch in batches))
    features = {key: value for result, _ in values for key, value in result.items()}
    errors = {key: value for _, result in values for key, value in result.items()}
    return features, errors


async def fetch_routes(
    route_codes: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    async def fetch(route_code: str) -> tuple[str, dict[str, Any] | None, str | None]:
        try:
            async with feature_store_semaphore:
                response = await upstream_request(
                    "GET", f"{FEATURE_STORE_URL}/v1/routes/{route_code}/features",
                )
            return route_code, response.json(), None
        except Exception as exc:
            return route_code, None, f"route feature lookup failed: {exc}"

    values = await asyncio.gather(*(fetch(code) for code in route_codes))
    features = {code: feature for code, feature, _ in values if feature is not None}
    errors = {code: error for code, _, error in values if error is not None}
    return features, errors


# Step 11: Build a compact, deterministic prompt for the model using the booking,
# passenger, and route context while trimming oversized text fields.
def make_prompt(booking: dict[str, Any], passenger: dict[str, Any], route: dict[str, Any]) -> str:
    # Explicit JSON makes the model input reproducible. Notes are free-form
    # upstream text, so cap them rather than allowing one unusually large note
    # to reject a whole booking's scoring request.
    booking_for_prompt = dict(booking)
    note = booking_for_prompt.get("customer_note", "")
    if isinstance(note, str) and len(note) > 1000:
        booking_for_prompt["customer_note"] = booking_for_prompt["customer_note"][:1000]
    prompt = json.dumps(
        {"booking": booking_for_prompt, "passenger_features": passenger, "route_features": route},
        sort_keys=True,
        separators=(",", ":"),
    )
    # Leave headroom below the provider's hard 6000-character limit. This
    # second pass handles unexpectedly large raw booking fields as well.
    if len(prompt) > 5900 and isinstance(booking_for_prompt.get("customer_note"), str):
        excess = len(prompt) - 5900
        note = booking_for_prompt["customer_note"]
        booking_for_prompt["customer_note"] = note[: max(0, len(note) - excess)]
        prompt = json.dumps(
            {"booking": booking_for_prompt, "passenger_features": passenger, "route_features": route},
            sort_keys=True,
            separators=(",", ":"),
        )
    if len(prompt) > 6000:
        raise ValueError("prompt exceeds model provider limit")
    return prompt


# Step 12: Limit concurrent model calls so the app respects the backend's rate
# limit while still scaling across multiple bookings.
model_semaphore = asyncio.Semaphore(5)


# Step 13: Score one booking by sending the constructed prompt to the model and
# collecting the returned output plus timing metadata.
async def score_one(booking: dict[str, Any], passenger: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    prompt = make_prompt(booking, passenger, route)
    started = time.perf_counter()
    try:
        async with model_semaphore:
            response = await upstream_request(
                "POST", f"{MODEL_URL}/v1/score",
                json={"model": MODEL_NAME, "prompt": prompt, "max_output_tokens": 256},
            )
    except Exception:
        await metric("model_failures")
        raise
    elapsed = (time.perf_counter() - started) * 1000
    await metric("model_calls")
    await metric("model_latency_ms_total", elapsed)
    body = response.json()
    return {
        "booking_id": booking["booking_id"],
        "model": body.get("model", MODEL_NAME),
        "output": body["output"],
        "usage": body.get("usage"),
        "scored_at": now_iso(),
    }


# Step 14: Execute the full job lifecycle: validate required booking fields,
# fetch features, score each booking, and update live progress while the job is
# still running.
async def run_job(job: Job) -> None:
    started_at = now_iso()
    await job.update(status="running", started_at=started_at)
    LOG.info(json.dumps({"event": "job_started", "job_id": job.job_id, "submitted": len(job.booking_ids)}))
    try:
        bookings = [BOOKINGS[booking_id] for booking_id in job.booking_ids if booking_id in BOOKINGS]
        valid_bookings = []
        for booking in bookings:
            missing = [
                field for field in ("passenger_id", "route_code")
                if not isinstance(booking.get(field), str) or not booking[field]
            ]
            if missing:
                error = {
                    "booking_id": booking["booking_id"],
                    "error": f"missing required booking fields: {', '.join(missing)}",
                }
                async with job.lock:
                    await job.append(job.errors_key, error)
                    await job.increment("failed")
                await metric("bookings_failed")
                LOG.error(json.dumps({"event": "booking_failed", "job_id": job.job_id, **error}))
            else:
                valid_bookings.append(booking)

        if valid_bookings:
            # These independent upstream phases run together to minimize job
            # latency while the shared semaphore still enforces the global FS cap.
            (passengers, passenger_errors), (routes, route_errors) = await asyncio.gather(
                fetch_passengers(sorted({b["passenger_id"] for b in valid_bookings})),
                fetch_routes(sorted({b["route_code"] for b in valid_bookings})),
            )
        else:
            passengers, passenger_errors = {}, {}
            routes, route_errors = {}, {}

        async def process(booking: dict[str, Any]) -> None:
            try:
                passenger = passengers.get(booking["passenger_id"])
                route = routes.get(booking["route_code"])
                if passenger is None or route is None:
                    missing = []
                    if passenger is None:
                        missing.append(passenger_errors.get(
                            booking["passenger_id"], "passenger features unavailable"
                        ))
                    if route is None:
                        missing.append(route_errors.get(
                            booking["route_code"], "route features unavailable"
                        ))
                    raise RuntimeError(f"missing {', '.join(missing)}")
                result = await score_one(booking, passenger, route)
                assert redis_client is not None
                await redis_client.set(
                    f"{REDIS_PREFIX}:score:{booking['booking_id']}",
                    json.dumps(result, separators=(",", ":")),
                )
                await job.append(job.results_key, result)
                await job.increment("completed")
                await metric("bookings_completed")
            except Exception as exc:
                error = {"booking_id": booking["booking_id"], "error": str(exc)}
                await job.append(job.errors_key, error)
                await job.increment("failed")
                await metric("bookings_failed")
                LOG.error(json.dumps({"event": "booking_failed", "job_id": job.job_id, **error}))

        await asyncio.gather(*(process(booking) for booking in valid_bookings))
        snapshot = await job.snapshot()
        final_status = "failed" if snapshot["failed"] and not snapshot["completed"] else "completed"
        await job.update(status=final_status, finished_at=now_iso())
        await metric("jobs_failed" if final_status == "failed" else "jobs_completed")
    except asyncio.CancelledError:
        # Cancellation is expected during bounded shutdown. Mark the job
        # terminal before propagating cancellation so polling never observes a
        # permanently running job after the process has stopped.
        snapshot = await job.snapshot()
        missing = snapshot["submitted"] - snapshot["completed"] - snapshot["failed"]
        if missing > 0:
            await job.increment("failed", missing)
        await job.update(status="failed", finished_at=now_iso())
        await job.append(job.errors_key, {"error": "job cancelled during shutdown"})
        await metric("jobs_failed")
        raise
    except Exception as exc:
        snapshot = await job.snapshot()
        missing = snapshot["submitted"] - snapshot["completed"] - snapshot["failed"]
        if missing > 0:
            await job.increment("failed", missing)
        await job.update(status="failed", finished_at=now_iso())
        await job.append(job.errors_key, {"error": str(exc)})
        await metric("jobs_failed")
        LOG.exception(json.dumps({"event": "job_failed", "job_id": job.job_id, "error": str(exc)}))
    finally:
        final = await job.snapshot()
        LOG.info(json.dumps({"event": "job_finished", "job_id": job.job_id, "status": final["status"]}))


# Step 15: Launch each job in a background task so POST /v1/score-jobs returns
# immediately while scoring continues asynchronously.
def schedule(job: Job) -> None:
    task = asyncio.create_task(run_job(job), name=f"score-job-{job.job_id}")
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


# Step 16: Expose the job-creation endpoint that validates input and starts the
# background processing without blocking on the work itself.
@app.post("/v1/score-jobs", status_code=status.HTTP_202_ACCEPTED)
async def create_score_job(request: ScoreJobRequest):
    if len(set(request.booking_ids)) != len(request.booking_ids):
        raise HTTPException(status_code=400, detail="booking_ids must be unique")
    unknown = sorted(set(request.booking_ids) - BOOKINGS.keys())
    if unknown:
        raise HTTPException(status_code=400, detail={"unknown_booking_ids": unknown})
    job = Job(str(uuid.uuid4()), request.booking_ids)
    await job.initialize()
    await metric("jobs_submitted")
    schedule(job)
    return {"job_id": job.job_id, "status": "pending"}


# Step 17: Allow clients to poll job status and observe live progress while the
# job is still running.
@app.get("/v1/score-jobs/{job_id}")
async def get_score_job(job_id: str):
    job = await Job.load(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return await job.snapshot()


# Step 18: Expose read-only access to stored booking scores for quick lookups.
@app.get("/v1/bookings/{booking_id}/score")
async def get_booking_score(booking_id: str):
    assert redis_client is not None
    value = await redis_client.get(f"{REDIS_PREFIX}:score:{booking_id}")
    result = json.loads(value) if value else None
    if result is None:
        raise HTTPException(status_code=404, detail="score not found")
    return result


# Step 19: Provide a lightweight health response and runtime counters/timings.
@app.get("/healthz")
async def healthz():
    assert redis_client is not None
    await redis_client.ping()
    return {"status": "ok", "bookings_loaded": len(BOOKINGS), "redis": "ok"}


@app.get("/metrics")
async def get_metrics():
    async with metrics_lock:
        result = dict(metrics)
    assert redis_client is not None
    result["active_jobs"] = 0
    result["stored_scores"] = 0
    async for key in redis_client.scan_iter(match=f"{REDIS_PREFIX}:job:*"):
        if key.endswith(":results") or key.endswith(":errors"):
            continue
        state = await redis_client.hget(key, "status")
        if state in ("pending", "running"):
            result["active_jobs"] += 1
    async for _ in redis_client.scan_iter(match=f"{REDIS_PREFIX}:score:*"):
        result["stored_scores"] += 1
    result["model_avg_latency_ms"] = round(
        result["model_latency_ms_total"] / max(result["model_calls"], 1), 1
    )
    return result
