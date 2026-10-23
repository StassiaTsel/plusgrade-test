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
