"""
payment-service — breakable demo app for ARGUS Phase 2.
Simulates the v2.4.0 memory-leak incident: unbounded in-memory cache with no eviction.
"""
import json
import os
import random
import time

import psutil
from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

app = FastAPI()

# --- Prometheus metrics ---
REQUEST_COUNT = Counter("payment_requests_total", "Total requests", ["endpoint", "status"])
REQUEST_LATENCY = Histogram("payment_request_duration_seconds", "Request latency", ["endpoint"])
MEMORY_BYTES = Gauge("payment_memory_bytes", "RSS memory in bytes")
CACHE_ENTRIES = Gauge("payment_cache_entries_total", "Cache entry count")
RESTARTS = Counter("payment_restarts_total", "Pod restart count")

# --- The bug: unbounded cache (no eviction policy) ---
_cache: dict = {}

VERSION = os.getenv("VERSION", "2.4.0")


def log(level: str, msg: str, **kwargs):
    print(json.dumps({"level": level, "msg": msg, "service": "payment-service", **kwargs}), flush=True)


@app.on_event("startup")
def startup():
    log("INFO", f"starting payment-service version={VERSION}")


@app.get("/health")
def health():
    return {"status": "ok", "version": VERSION, "cache_entries": len(_cache)}


@app.get("/charge")
def charge(order_id: str = "test"):
    start = time.time()
    time.sleep(random.uniform(0.04, 0.12))

    if order_id not in _cache:
        _cache[order_id] = {"processed": True, "payload": "x" * 1024}  # 1 KB per entry

    count = len(_cache)
    CACHE_ENTRIES.set(count)
    REQUEST_COUNT.labels(endpoint="/charge", status="200").inc()
    REQUEST_LATENCY.labels(endpoint="/charge").observe(time.time() - start)

    if count > 0 and count % 5000 == 0:
        log("WARN", f"cache: entry count {count:,} (no eviction policy configured)")

    return {"status": "charged", "order_id": order_id, "cache_entries": count}


@app.get("/leak")
def leak(entries: int = 5000):
    """Rapidly grow the cache to simulate unbounded memory accumulation."""
    base = len(_cache)
    for i in range(entries):
        _cache[f"order_{base + i}"] = {"processed": True, "payload": "x" * 1024}

    count = len(_cache)
    CACHE_ENTRIES.set(count)
    log("WARN", f"cache: entry count {count:,} (no eviction policy configured)")
    return {"added": entries, "total_cache_entries": count}


@app.get("/metrics")
def metrics():
    proc = psutil.Process(os.getpid())
    MEMORY_BYTES.set(proc.memory_info().rss)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
