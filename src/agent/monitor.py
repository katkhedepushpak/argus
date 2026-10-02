"""
ARGUS watcher: the always-on service behind the dashboard.

It polls Prometheus, opens and closes incidents, emails on detection, starts investigations automatically,
and runs them (the dashboard only displays state and records operator decisions).

Run:  python -m src.agent.monitor
"""
import datetime
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from src.agent.incidents import claim_for_run, list_incidents, open_incident, update_incident
from src.agent.notify import build_detection_email, send_escalation
from src.agent.prom import fetch_firing_alerts

_HEARTBEAT_FILE = Path(__file__).resolve().parents[2] / ".monitor_heartbeat.json"
_PROTECTED = ("Resolved", "Investigating", "Awaiting approval", "Action approved", "Verifying recovery")
_IN_FLIGHT = ("Investigating", "Awaiting approval", "Verifying recovery")


def auto_investigate_enabled():
    return os.getenv("ARGUS_AUTO_INVESTIGATE", "1") != "0"


def max_concurrent_runs():
    return int(os.getenv("ARGUS_MAX_CONCURRENT", "3"))


def sync_incidents():
    firing = fetch_firing_alerts()
    firing_fps = {a["fp"] for a in firing}
    for alert in firing:
        inc = open_incident(alert)
        if not inc.get("detected_notified"):
            subject, text, html = build_detection_email(inc, auto_investigate_enabled())
            update_incident(inc["id"], detected_notified=True, detection_email=send_escalation(subject, text, html))
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    for inc in list_incidents():
        if inc["fp"] not in firing_fps and inc["status"] not in _PROTECTED and not inc.get("run_active"):
            update_incident(inc["id"], status="Resolved", resolved_at=now)


def dispatch_runs():
    """Start a run for every incident that wants one: new incidents (auto mode) and operator requests."""
    from src.agent import runs  # imported lazily: it pulls in the LLM client

    for inc in list_incidents():
        if len(runs.ACTIVE) >= max_concurrent_runs():
            return
        if inc["status"] == "Resolved" or inc.get("run_active"):
            continue
        auto_due = auto_investigate_enabled() and inc["status"] == "Detected" and not inc.get("auto_started")
        if not (inc.get("investigate_requested") or auto_due):
            continue
        claimed = claim_for_run(inc["id"])
        if claimed:
            runs.start_run(inc["id"], "live", runs.FIXTURE_DIR_FOR_LIVE, incident=claimed)


def recover_interrupted_runs():
    """Runs live inside this process, so anything still in flight at startup was interrupted by a restart."""
    for inc in list_incidents():
        if inc.get("run_active") or inc["status"] in _IN_FLIGHT:
            update_incident(inc["id"], status="Investigation failed", run_active=False, pending=None, decision=None,
                            investigate_requested=False, auto_started=True,
                            report="Investigation was interrupted by a watcher restart. Re-investigate to retry.")


def _write_heartbeat(error=None):
    tmp = _HEARTBEAT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ts": time.time(), "error": error}))
    os.replace(tmp, _HEARTBEAT_FILE)


def read_heartbeat():
    """Return {'ts', 'error'} from the watcher, or None if it has never run."""
    try:
        return json.loads(_HEARTBEAT_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return None


def run_forever(poll_secs):
    recover_interrupted_runs()
    print(f"ARGUS watcher started: polling {os.getenv('PROMETHEUS_URL')} every {poll_secs}s, "
          f"auto-investigate={'on' if auto_investigate_enabled() else 'off'}", flush=True)
    while True:
        error = None
        try:
            sync_incidents()
        except Exception as e:
            error = str(e)
            print(f"poll failed: {e}", flush=True)
        try:
            dispatch_runs()
        except Exception as e:
            print(f"dispatch failed: {e}", flush=True)
        _write_heartbeat(error)
        time.sleep(poll_secs)


if __name__ == "__main__":
    load_dotenv()
    if not os.getenv("PROMETHEUS_URL"):
        raise SystemExit("PROMETHEUS_URL is not set; nothing to watch.")
    run_forever(int(os.getenv("ARGUS_POLL_SECS", "5")))
