import contextlib
import datetime
import json
import os
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: in-process locking only
    fcntl = None

_ROOT = Path(__file__).resolve().parents[2]
_COUNTER_FILE = _ROOT / ".incident_counter"
_REGISTRY_FILE = _ROOT / ".incident_registry.json"
_LOCK_FILE = _ROOT / ".incident_registry.lock"
_FIRST_NUMBER = 1001
_thread_lock = threading.RLock()


@contextlib.contextmanager
def _locked():
    """Serialises registry access across threads and across processes (watcher + dashboard)."""
    with _thread_lock:
        with open(_LOCK_FILE, "w") as lock_file:
            if fcntl:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)


def _write_atomic(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _next_incident_id_unlocked():
    try:
        last = int(_COUNTER_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        last = _FIRST_NUMBER - 1
    current = last + 1
    _write_atomic(_COUNTER_FILE, str(current))
    return f"INC{current:07d}"


def next_incident_id():
    """Return the next sequential incident ID, e.g. INC0001001, persisted across runs."""
    with _locked():
        return _next_incident_id_unlocked()


def _load():
    try:
        return json.loads(_REGISTRY_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save(registry):
    _write_atomic(_REGISTRY_FILE, json.dumps(registry, indent=2))


def open_incident(alert):
    """Return the open incident for this alert fingerprint, creating one if none exists."""
    with _locked():
        registry = _load()
        for inc in registry.values():
            if inc["fp"] == alert["fp"] and inc["status"] != "Resolved":
                return inc
        inc = {
            **alert,
            "id": _next_incident_id_unlocked(),
            "status": "Detected",
            "report": "",
            "opened_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        }
        registry[inc["id"]] = inc
        _save(registry)
        return inc


def update_incident(incident_id, **fields):
    with _locked():
        registry = _load()
        if incident_id in registry:
            registry[incident_id].update(fields)
            _save(registry)


def list_incidents():
    with _locked():
        return sorted(_load().values(), key=lambda i: i["id"], reverse=True)


def get_incident(incident_id):
    with _locked():
        return _load().get(incident_id)


def set_decision(incident_id, decision):
    """Record the operator's decision. One-shot: only while an unexpired approval is pending and undecided."""
    with _locked():
        registry = _load()
        inc = registry.get(incident_id)
        pending = inc.get("pending") if inc else None
        if not pending or inc.get("decision") or time.time() > pending["deadline"]:
            return False
        inc["decision"] = decision
        _save(registry)
        return True


def request_investigation(incident_id):
    """Ask the watcher to (re)start an investigation. Refused if one is already running or the incident is closed."""
    with _locked():
        registry = _load()
        inc = registry.get(incident_id)
        if not inc or inc.get("run_active") or inc["status"] == "Resolved":
            return False
        inc["investigate_requested"] = True
        _save(registry)
        return True


def claim_for_run(incident_id):
    """Atomically mark an incident as running; returns the incident, or None if it is already claimed."""
    with _locked():
        registry = _load()
        inc = registry.get(incident_id)
        if not inc or inc.get("run_active"):
            return None
        inc.update(run_active=True, investigate_requested=False, auto_started=True,
                   progress=[], pending=None, decision=None, notification="")
        _save(registry)
        return inc
