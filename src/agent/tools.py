import os
import json
import subprocess
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

WRITE_TOOLS = {"restart_pod", "rollback_deployment", "scale_deployment"}

def get_alert(d):
    prom_url = os.getenv("PROMETHEUS_URL")
    if not prom_url:
        with open(f"{d}/alert.txt", encoding="utf-8") as f:
            return f.read()

    r = requests.get(f"{prom_url}/api/v1/alerts", timeout=10)
    r.raise_for_status()
    alerts = r.json()["data"]["alerts"]

    firing = [a for a in alerts if a["state"] == "firing" and a["labels"].get("service") == "payment-service"]
    if not firing:
        return "(no payment-service alerts currently firing)"

    lines = ["=== Firing Alerts (Prometheus) ==="]
    for a in firing:
        lines.append(f"alert:       {a['labels'].get('alertname', 'unknown')}")
        lines.append(f"severity:    {a['labels'].get('severity', 'unknown')}")
        lines.append(f"summary:     {a['annotations'].get('summary', '')}")
        lines.append(f"description: {a['annotations'].get('description', '')}")
        lines.append(f"since:       {a['activeAt']}")
        lines.append("")
    return "\n".join(lines)

def get_metrics(d):
    prom_url = os.getenv("PROMETHEUS_URL")
    if not prom_url:
        with open(f"{d}/metrics.txt", encoding="utf-8") as f:
            return f.read()

    def instant(promql):
        r = requests.get(f"{prom_url}/api/v1/query", params={"query": promql}, timeout=10)
        r.raise_for_status()
        results = r.json()["data"]["result"]
        return results[0]["value"][1] if results else "no data"

    memory_mb   = float(instant("payment_memory_bytes") or 0) / (1024 * 1024)
    cache       = instant("payment_cache_entries_total")
    restarts    = instant('kube_pod_container_status_restarts_total{container="payment-service",namespace="default"}')
    error_rate  = instant('rate(payment_requests_total{status="500"}[5m])')
    p99_latency = instant('histogram_quantile(0.99, rate(payment_request_duration_seconds_bucket[5m]))')

    p99_ms = float(p99_latency) * 1000 if p99_latency != "no data" else "no data"

    return "\n".join([
        "=== payment-service live metrics (Prometheus) ===",
        f"memory_rss_mb:  {memory_mb:.1f}  (limit: 150)",
        f"cache_entries:  {cache}",
        f"pod_restarts:   {restarts}",
        f"error_rate_5xx: {error_rate}/s",
        f"p99_latency_ms: {p99_ms}",
    ])

def get_logs(d):
    splunk_url = os.getenv("SPLUNK_URL")
    if not splunk_url:
        with open(f"{d}/logs.txt", encoding="utf-8") as f:
            return f.read()

    resp = requests.post(
        f"{splunk_url}/services/search/jobs/export",
        auth=(os.getenv("SPLUNK_USER", "admin"), os.getenv("SPLUNK_PASSWORD", "")),
        verify=False,
        data={
            "search": "search index=main | sort -_time | head 50",
            "output_mode": "json",
            "earliest_time": "-15m",
        },
        timeout=15,
    )
    resp.raise_for_status()

    lines = []
    for line in resp.text.strip().splitlines():
        try:
            obj = json.loads(line)
            raw = obj.get("result", {}).get("_raw")
            if raw:
                lines.append(raw)
        except json.JSONDecodeError:
            continue

    return "\n".join(lines) if lines else "(no logs in the last 15 minutes)"

def restart_pod(service):
    r = subprocess.run(
        ["kubectl", "rollout", "restart", f"deployment/{service}"],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return f"ERROR: {r.stderr.strip()}"
    s = subprocess.run(
        ["kubectl", "rollout", "status", f"deployment/{service}", "--timeout=60s"],
        capture_output=True, text=True
    )
    return f"{r.stdout.strip()}\n{s.stdout.strip()}"

def rollback_deployment(service):
    r = subprocess.run(
        ["kubectl", "rollout", "undo", f"deployment/{service}"],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return f"ERROR: {r.stderr.strip()}"
    s = subprocess.run(
        ["kubectl", "rollout", "status", f"deployment/{service}", "--timeout=60s"],
        capture_output=True, text=True
    )
    return f"{r.stdout.strip()}\n{s.stdout.strip()}"

def scale_deployment(service, replicas):
    r = subprocess.run(
        ["kubectl", "scale", f"deployment/{service}", f"--replicas={replicas}"],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return f"ERROR: {r.stderr.strip()}"
    return r.stdout.strip()

def get_deploy_history(d):
    with open(f"{d}/deploy_history.json", encoding="utf-8") as f:
        return f.read()
    
def get_git_log(d):
    with open(f"{d}/git_log.txt", encoding="utf-8") as f:
        return f.read()

TOOLS = [
    {"name": "get_alert",
    "description": "Get the production alert that just fired.",
    "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_metrics",
    "description": "Get recent metrics (memory, restarts, latency) for the affected service.",
    "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_logs",
    "description": "Get recent pod logs for the affected service — shows errors, restarts, and failure signatures like OOMKilled.",
    "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_deploy_history",
    "description": "Get the recent deployment history for the affected service — versions, timestamps, and who deployed.",
    "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_git_log",
    "description": "Get the recent git log for the affected service — shows commits, authors, and timestamps.",
    "input_schema": {"type": "object", "properties": {}}},
    {"name": "restart_pod",
    "description": "Restart the Kubernetes deployment for a service to clear a crash or memory leak. Requires human approval.",
    "input_schema": {"type": "object", "properties": {
        "service": {"type": "string", "description": "Deployment name, e.g. payment-service"}
    }, "required": ["service"]}},
    {"name": "rollback_deployment",
    "description": "Roll back a deployment to its previous version. Use when a bad release caused the incident. Requires human approval.",
    "input_schema": {"type": "object", "properties": {
        "service": {"type": "string", "description": "Deployment name, e.g. payment-service"}
    }, "required": ["service"]}},
    {"name": "scale_deployment",
    "description": "Scale a deployment to a different number of replicas. Use when the service is under heavy load. Requires human approval.",
    "input_schema": {"type": "object", "properties": {
        "service": {"type": "string", "description": "Deployment name, e.g. payment-service"},
        "replicas": {"type": "integer", "description": "Target number of replicas"}
    }, "required": ["service", "replicas"]}},
]