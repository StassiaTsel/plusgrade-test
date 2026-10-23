"""
Mock LLM — stands in for a hosted model provider.

DO NOT MODIFY THIS FILE. Reviewers run your submission against this exact service.

Behaviour you need to know about:
  * Latency is 600-1800ms per call, with a tail.
  * Only a few requests may be in flight at once; beyond that you get HTTP 429
    with a Retry-After header.
  * ~5% of admitted calls fail with HTTP 500.
  * Prompts over 6000 characters are rejected with HTTP 400.
  * Output is deterministic for a given prompt, so identical prompts are
    cacheable and results are reproducible.

Run:  uvicorn services.mock_llm:app --port 8082
"""

import asyncio
import hashlib
import json
import os
import random
import time
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ----------------------------------------------------------------
# Step 1: Configure the mock model service behavior and pricing.
# These values are read from environment variables so the same service can be
# tuned without changing the code. The settings emulate a capped, noisy backend
# with deterministic scoring output.
# ----------------------------------------------------------------
MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY", "5"))
FAILURE_RATE = float(os.getenv("LLM_FAILURE_RATE", "0.05"))
TAIL_RATE = float(os.getenv("LLM_TAIL_RATE", "0.04"))
MAX_PROMPT_CHARS = int(os.getenv("LLM_MAX_PROMPT_CHARS", "6000"))
SEED = int(os.getenv("LLM_SEED", "4242"))

PRICE_PER_1K_INPUT = 0.003
PRICE_PER_1K_OUTPUT = 0.015

app = FastAPI(title="Mock LLM", version="1.0.0")

# Step 2: Keep a running set of service metrics so we can observe load,
# latency, and provider health during load tests.
_stats = {
    "started_at": time.time(),
    "requests_total": 0,
    "completions_ok": 0,
    "rejected_429": 0,
    "rejected_400": 0,
    "errors_500": 0,
    "inflight": 0,
    "peak_inflight": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "latency_ms_sum": 0.0,
}
_lock = asyncio.Lock()
_rng = random.Random(SEED)


class ScoreRequest(BaseModel):
    # Step 3: Define the request contract for an LLM scoring call.
    # We accept a model name, prompt, and optional request_id so callers can
    # correlate a request with a later response.
    model: str = Field(default="upgrade-propensity-v1")
    prompt: str
    max_output_tokens: int = Field(default=256, ge=16, le=1024)
    request_id: Optional[str] = None


def _retry_after(status: int, err: str, detail: str) -> JSONResponse:
    # Step 4: Return a consistent rate-limit payload with Retry-After headers.
    return JSONResponse(status_code=status, headers={"Retry-After": "2"},
                        content={"error": err, "detail": detail})


def _tokens(text: str) -> int:
    # Step 5: Estimate token usage from text length so the mock provider can
    # report input/output token counts and cost without a real tokenizer.
    return max(1, len(text) // 4)


@app.get("/healthz")
async def healthz():
    # Step 6: Expose a quick readiness check for load balancers and health
    # probes to confirm the service can accept traffic.
    return {"status": "ok"}


@app.post("/v1/score")
async def score(req: ScoreRequest):
    # Step 7: Start timing the request and check admission before processing.
    started = time.perf_counter()
    async with _lock:
        _stats["requests_total"] += 1
        if len(req.prompt) > MAX_PROMPT_CHARS:
            _stats["rejected_400"] += 1
            return JSONResponse(
                status_code=400,
                content={"error": "prompt_too_long",
                         "detail": f"prompt is {len(req.prompt)} chars, limit is {MAX_PROMPT_CHARS}"},
            )
        if _stats["inflight"] >= MAX_CONCURRENCY:
            _stats["rejected_429"] += 1
            return _retry_after(429, "rate_limit_exceeded",
                                f"max {MAX_CONCURRENCY} concurrent requests")
        _stats["inflight"] += 1
        _stats["peak_inflight"] = max(_stats["peak_inflight"], _stats["inflight"])

    try:
        # Step 8: Simulate realistic model latency. A small tail of slower
        # requests introduces the variability seen in production backends.
        if _rng.random() < TAIL_RATE:
            await asyncio.sleep(_rng.uniform(3.0, 5.0))
        else:
            await asyncio.sleep(_rng.uniform(0.600, 1.800))

        # Step 9: Occasionally fail the request to emulate transient backend
        # outages while keeping the failure mode safe and retryable.
        if _rng.random() < FAILURE_RATE:
            async with _lock:
                _stats["errors_500"] += 1
            return JSONResponse(status_code=500,
                                content={"error": "internal_error",
                                         "detail": "model backend error, safe to retry"})

        # Step 10: Build a deterministic score from the prompt so identical
        # prompts always yield the same output and can be cached.
        digest = hashlib.sha256(f"{req.model}\n{req.prompt}".encode()).hexdigest()
        n = int(digest, 16)
        propensity = round((n % 10_000) / 10_000, 4)
        band = "high" if propensity >= 0.66 else "medium" if propensity >= 0.33 else "low"
        drivers = [
            "loyalty_tier", "historical_upgrade_take_rate", "price_sensitivity_index",
            "days_to_departure", "avg_load_factor", "prior_upgrade_purchases",
            "offer_price_ratio", "email_engagement_score",
        ]
        picked = [drivers[(n >> (3 * i)) % len(drivers)] for i in range(3)]
        body = {
            "propensity": propensity,
            "band": band,
            "recommended_price_multiplier": round(0.8 + (n >> 31) % 700 / 1000, 3),
            "top_drivers": list(dict.fromkeys(picked)),
            "rationale": f"Signals {', '.join(dict.fromkeys(picked))} indicate {band} upgrade propensity.",
        }
        completion = json.dumps(body)
        in_tok, out_tok = _tokens(req.prompt), _tokens(completion)
        elapsed_ms = (time.perf_counter() - started) * 1000

        # Step 11: Record successful completion details for observability and
        # cost accounting.
        async with _lock:
            _stats["completions_ok"] += 1
            _stats["input_tokens"] += in_tok
            _stats["output_tokens"] += out_tok
            _stats["latency_ms_sum"] += elapsed_ms

        # Step 12: Return the model response with latency and token usage.
        return {
            "model": req.model,
            "request_id": req.request_id,
            "output": body,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
            "latency_ms": round(elapsed_ms, 1),
        }
    finally:
        # Step 13: Always release the in-flight slot so the concurrency ceiling
        # remains accurate even when the request fails or returns early.
        async with _lock:
            _stats["inflight"] -= 1


@app.get("/v1/stats")
async def stats():
    # Step 14: Provide introspection for operators and reviewers to inspect
    # current load, cost, and latency without affecting the real provider API.
    """Introspection for you and for reviewers. Not part of a real provider API."""
    ok = _stats["completions_ok"] or 1
    cost = (_stats["input_tokens"] / 1000 * PRICE_PER_1K_INPUT
            + _stats["output_tokens"] / 1000 * PRICE_PER_1K_OUTPUT)
    return {
        **_stats,
        "uptime_s": round(time.time() - _stats["started_at"], 1),
        "max_concurrency": MAX_CONCURRENCY,
        "avg_latency_ms": round(_stats["latency_ms_sum"] / ok, 1),
        "estimated_cost_usd": round(cost, 4),
    }


@app.post("/v1/stats:reset")
async def stats_reset():
    # Step 15: Reset the counters so each test run starts from a clean baseline
    # while keeping the service up for the next evaluation.
    for k in ("requests_total", "completions_ok", "rejected_429", "rejected_400",
              "errors_500", "peak_inflight", "input_tokens", "output_tokens"):
        _stats[k] = 0
    _stats["latency_ms_sum"] = 0.0
    _stats["started_at"] = time.time()
    return {"status": "reset"}
