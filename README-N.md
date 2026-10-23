# AI Engineer Take-Home: Upgrade Offer Scoring Service

**Timebox: 2–4 hours.** The problem is deliberately larger than the
timebox, so part of the exercise is choosing what to build and what to leave as
a documented decision. A small, correct, well-reasoned system with a sharp
write-up beats a sprawling half-finished one every time.

You may use AI coding assistants for this project.

---

## The scenario

We sell cabin upgrades. Before a batch of emails go out to possible prospects, we need a propensity score per booking so we can
decide who gets an offer and at what price.

Scoring one booking means:

1. Pull **passenger features** from the feature store.
2. Pull **route features** from the feature store.
3. Combine those with the booking record into a prompt.
4. Call the **model** to get a propensity score.
5. Store the result so it can be read back.

A campaign job arrives with up to 250 bookings at a time.

## What you build

A Python HTTP service. Any framework — FastAPI, Litestar, Starlette, Flask,
whatever you're fastest in. Any storage — in-memory, SQLite, Redis, Postgres.
No frontend, no auth implementation required.

### Required endpoints

```
POST /v1/score-jobs
  body: {"booking_ids": ["BKG-00001", "BKG-00002", ...]}
  -> 202 {"job_id": "..."}
  Returns immediately. Scoring happens in the background.

GET /v1/score-jobs/{job_id}
  -> 200 {"job_id": "...", "status": "pending|running|completed|failed",
          "submitted": 250, "completed": 187, "failed": 3,
          "started_at": "...", "finished_at": null,
          "results": [...], "errors": [...]}
  Must be readable while the job is still running, and must reflect
  real progress.

GET /v1/bookings/{booking_id}/score
  -> 200 with the stored score, or 404 if we have never scored it.

GET /healthz
  -> 200 when the service is able to serve traffic.
```

Shape the response bodies however you think is right — the fields above are
indicative, not a contract to match exactly.

### Requirements

1. **The API stays responsive during a job.** `POST /v1/score-jobs` returns in
   well under a second with 250 bookings, and `GET` endpoints answer promptly
   while 250 bookings are being scored.
2. **Latency**, Minimize latency as much as possible.
3. **Observable.** Structured logs, and expose whatever counters or timings you'd
   want during an incident. A `/metrics` endpoint is welcome but not required.
4. **Shuts down cleanly.** SIGTERM shouldn't corrupt job state or hang.

Stretch goals only if you have time left over: job cancellation, per-tenant
concurrency limits, a real queue, prompt/response versioning, tests beyond the
happy path.

### Data

`data/bookings.json` — 250 bookings, the input for your `booking_id` lookups.
Treat it as your bookings table. It is raw data, exactly as received from
upstream.

## The dependencies

Both are provided in `services/`. **Do not modify them** — reviewers run your
submission against these exact files.

### Feature store — `http://localhost:8081`

| Endpoint | Notes |
|---|---|
| `GET /v1/passengers/{passenger_id}/features` | 120–400 ms |
| `GET /v1/routes/{route_code}/features` | 80–250 ms, but ~3% of calls take 2.5–4 s |
| `POST /v1/passengers/features:batch` | body `{"passenger_ids": [...]}`, max 50, costs one concurrency slot |
| `GET /v1/stats` | introspection, not part of the real service |

- Max **20 concurrent** requests. Beyond that: `429` with `Retry-After`.

### Model — `http://localhost:8082`

| Endpoint | Notes |
|---|---|
| `POST /v1/score` | body `{"model": "upgrade-propensity-v1", "prompt": "...", "max_output_tokens": 256}` |
| `GET /v1/stats` | introspection, including token usage and estimated cost |

- 600–1800 ms per call, with a ~4% tail of 3–5 s.
- Prompts over **6000 characters** are rejected with `400`.
- Output is deterministic for a given prompt, so identical prompts are cacheable
  and your results are reproducible.

Response shape:

```json
{
  "model": "upgrade-propensity-v1",
  "output": {
    "propensity": 0.7421,
    "band": "high",
    "recommended_price_multiplier": 1.15,
    "top_drivers": ["loyalty_tier", "avg_load_factor"],
    "rationale": "..."
  },
  "usage": {"input_tokens": 412, "output_tokens": 61},
  "latency_ms": 1204.3
}
```

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# terminal 1
uvicorn services.mock_feature_store:app --port 8081
# terminal 2
uvicorn services.mock_llm:app --port 8082
# or: docker compose up
```

Self-check after a run:

```bash
curl -sX POST localhost:8081/v1/stats:reset
curl -sX POST localhost:8082/v1/stats:reset
# ... run your job ...
python3 scripts/check_run.py --bookings 250
```

That script reports peak concurrency, 429s you caused, calls per booking, and
token cost. It's a mirror, not a grade — but if it says you never went above one
concurrent model call, or that you made 500 feature calls for 250 bookings,
you'll want to know before you submit. Pass `--bookings 25` if that's the size
you ran.

Nothing in the mocks is hidden from you: `services/` is readable, and the
latency, error rates and caps are all environment variables. Reading the code to
understand what you're up against is fair game. Tuning the variables to make
your run look good is not — reviewers use the defaults.

## What to submit

A git repo (or zip) containing:

- Your service code.
- A short section in your own README: how to run it, what you built, what you
  deliberately left out and why, and anything you'd fix with another two hours.

## How we read it

We're looking at how you reason about concurrency and failure, how you structure
a service, and how you think about running this for real. We are not looking for
feature completeness, a clever prompt, or full test coverage. Honest
documentation of a shortcut reads far better than a shortcut we discover on our
own.

Questions while you work: reply to the thread that sent you this. Ambiguity in
the spec is fine to resolve yourself — just write down the assumption.

## Submission implementation

Run the service with:

```bash
uvicorn app:app --port 8000
```

The service loads `data/bookings.json` at startup and stores jobs, live job
progress, errors, and scores in Redis. Passenger features are deduplicated and
fetched in batches of up to 50; route requests are deduplicated and fetched
concurrently with passenger features. A process-wide semaphore respects the
feature store's 20-request limit across multiple jobs. Model calls are bounded
to the mock provider's five-request limit. Transient upstream 429/5xx responses
and timeouts are retried with backoff. `/metrics` exposes operational counters
and model latency. Interrupted jobs are marked failed during shutdown before
remaining work is cancelled.

Redis is required for the service and can be configured with `REDIS_URL`
(default: `redis://localhost:6379/0`). `docker compose up` starts a Redis 7
container with a named persistent volume, and the scoring service connects to
it through `redis://redis:6379/0`. The in-process task set still owns background work,
so jobs already running when a process is terminated are marked failed during
clean shutdown. On startup, the service does not inspect, resume, or reconcile
jobs left in Redis by an earlier process; those records remain unchanged and a
new request creates a new job. A separate durable queue would be the next step
for cross-process job recovery. Authentication and client-request cancellation
are also intentionally out of scope.


### Quick local run

```bash

docker compose up

curl -sX POST localhost:8081/v1/stats:reset
curl -sX POST localhost:8082/v1/stats:reset

# ... run your job ...
JOB_ID=$(python3 -c '
import json
with open("data/bookings.json") as f:
    bookings = json.load(f)
print(json.dumps({"booking_ids": [b["booking_id"] for b in bookings]}))
' |
curl -sS -X POST http://localhost:8000/v1/score-jobs \
  -H 'Content-Type: application/json' \
  -d @- |
python3 -c 'import sys, json; print(json.load(sys.stdin)["job_id"])'
)

echo "$JOB_ID"

### Wait till run completes.

curl -s "http://localhost:8000/v1/score-jobs/$JOB_ID" | python3 -m json.tool
```


### Redis
docker compose exec redis redis-cli
SCAN 0 MATCH upgrade-scoring:* COUNT 250

### Check run
python3 scripts/check_run.py --bookings 250

### Swagger UI results:
http://localhost:8000/docs
http://localhost:8000/docs#/default/get_score_job_v1_score_jobs__job_id__get
