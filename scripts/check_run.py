"""
Self-check. Run this AFTER a scoring job finishes to see how your service
behaved against the two dependencies.

    python3 scripts/check_run.py --bookings 250

Reset the counters before each run:

    curl -sX POST localhost:8081/v1/stats:reset
    curl -sX POST localhost:8082/v1/stats:reset

This is a mirror, not a grade. It tells you whether you actually ran work in
parallel, whether you cached, and whether you hammered the dependencies.
"""

import argparse
import json
import urllib.request

FS = "http://localhost:8081/v1/stats"
LLM = "http://localhost:8082/v1/stats"


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)


def line(label, value, note=""):
    print(f"  {label:<34} {str(value):<12} {note}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bookings", type=int, required=True,
                    help="number of bookings in the job you just ran")
    args = ap.parse_args()
    n = args.bookings

    fs, llm = get(FS), get(LLM)

    print("\nFeature store")
    line("requests total", fs["requests_total"])
    line("by endpoint", fs["requests_by_endpoint"])
    line("peak concurrent", fs["peak_inflight"],
         f"cap is {fs['max_concurrency']}")
    line("429s received", fs["rejected_429"],
         "you exceeded the cap" if fs["rejected_429"] else "")
    line("503s received", fs["errors_503"])

    print("\nMock LLM")
    line("requests total", llm["requests_total"])
    line("successful completions", llm["completions_ok"])
    line("peak concurrent", llm["peak_inflight"],
         f"cap is {llm['max_concurrency']}")
    line("429s received", llm["rejected_429"])
    line("500s received", llm["errors_500"])
    line("400s (prompt too long)", llm["rejected_400"])
    line("avg latency", f"{llm['avg_latency_ms']} ms")
    line("tokens in/out", f"{llm['input_tokens']}/{llm['output_tokens']}")
    line("estimated cost", f"${llm['estimated_cost_usd']}")

    print("\nRatios")
    line("LLM calls per booking", round(llm["requests_total"] / max(n, 1), 2),
         "1.0 is ideal; >1.3 means retries or missing dedupe")
    line("feature calls per booking", round(fs["requests_total"] / max(n, 1), 2),
         "2.0 is the naive baseline; caching and batching beat it")

    print("\nSanity checks")
    checks = [
        ("ran work concurrently", llm["peak_inflight"] > 1),
        ("stayed inside the LLM concurrency cap", llm["rejected_429"] == 0),
        ("stayed inside the feature store cap", fs["rejected_429"] == 0),
        ("no oversized prompts", llm["rejected_400"] == 0),
        ("did not duplicate LLM work", llm["requests_total"] <= n * 1.3),
        ("cached or batched features", fs["requests_total"] < n * 2),
    ]
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'LOOK'}] {label}")
    print()


if __name__ == "__main__":
    main()
