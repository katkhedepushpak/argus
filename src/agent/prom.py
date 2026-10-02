import os
import subprocess

import requests


def monitored_services():
    return {s.strip() for s in os.getenv("ARGUS_MONITORED_SERVICES", "payment-service").split(",") if s.strip()}


def existing_pods():
    """namespace/name of every pod currently in the cluster, or None if kubectl isn't usable."""
    try:
        r = subprocess.run(
            ["kubectl", "get", "pods", "-A", "-o",
             r"jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name}{'\n'}{end}"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return set(r.stdout.split()) if r.returncode == 0 else None


def fetch_firing_alerts():
    """
    Firing alerts for monitored services.

    The fingerprint uses only stable labels, so a pod restart does not look like a new incident.
    Instances tied to a pod that no longer exists are ignored: after a rollout Prometheus keeps the
    replaced pod's last sample for up to 5 minutes, which would otherwise hold the alert open.
    """
    r = requests.get(f"{os.getenv('PROMETHEUS_URL')}/api/v1/alerts", timeout=5)
    r.raise_for_status()
    monitored = monitored_services()
    existing = existing_pods()
    found = {}
    for a in r.json()["data"]["alerts"]:
        labels = a["labels"]
        if a["state"] != "firing" or labels.get("service") not in monitored:
            continue
        pod, namespace = labels.get("pod"), labels.get("namespace")
        if existing is not None and pod and namespace and f"{namespace}/{pod}" not in existing:
            continue
        fp = "|".join(f"{k}={labels.get(k, '')}" for k in ("alertname", "service", "namespace", "severity"))
        active_at = a["activeAt"][:19].replace("T", " ") + "Z"
        if fp not in found or active_at < found[fp]["active_at"]:
            found[fp] = {
                "fp": fp,
                "alertname": labels.get("alertname", "unknown"),
                "severity": labels.get("severity", "unknown"),
                "service": labels["service"],
                "summary": a["annotations"].get("summary", ""),
                "active_at": active_at,
            }
    return list(found.values())
