"""Process-wide investigation runs, shared by every browser session (state lives here, not in Streamlit)."""
import datetime
import json
import os
import queue
import re
import threading
import time

from src.agent.incidents import get_incident, list_incidents, update_incident
from src.agent.notify import build_approval_email, build_resolution_email, describe_action, send_escalation
from src.agent.orchestrator import client, main
from src.agent.prom import fetch_firing_alerts
from src.agent.scenarios import LIVE_FALLBACK_SCENARIO, resolve_scenario, scenario_path
from src.agent.tools import get_metrics

APPROVAL_TIMEOUT_SECS = int(os.getenv("ARGUS_APPROVAL_SECS", "900"))
LIVE_FIXTURE_DIR = scenario_path(LIVE_FALLBACK_SCENARIO)
VERIFY_TIMEOUT_SECS = int(os.getenv("ARGUS_VERIFY_SECS", "150"))
VERIFY_INTERVAL_SECS = int(os.getenv("ARGUS_VERIFY_INTERVAL", "10"))
CLEAR_POLLS_REQUIRED = 2
WRITE_TOOLS = {"restart_pod", "rollback_deployment", "scale_deployment"}
DECISION_STATUS = {"yes": "Action approved", "no": "Rejected - no action taken",
                   "timeout": "Timed out - no action taken"}
TOOL_LABELS = {
    "get_alert": "Checked the firing alert",
    "get_metrics": "Pulled live metrics",
    "get_logs": "Read recent pod logs",
    "get_deploy_history": "Checked deployment history",
    "get_git_log": "Checked recent commits",
    "restart_pod": "Proposed: restart the service",
    "rollback_deployment": "Proposed: roll back the deployment",
    "scale_deployment": "Proposed: scale the deployment",
}
OUTCOME_FACTS = {
    "resolved": "The approved action was executed, and the alert cleared and stayed clear. The incident is resolved.",
    "not_recovered": "The approved action was executed, but the alert was still firing at the end of the verification window. The incident is still open.",
    "remediation_failed": "The approved action failed to execute. No fix was applied and the incident is still open.",
    "executed_unverified": "The approved action was executed, but recovery could not be verified automatically.",
    "rejected": "The operator rejected the proposed action. Nothing was changed and the incident is still open.",
    "timed_out": "The approval request expired with no response. Nothing was changed and the incident is still open.",
    "no_action": "ARGUS completed its investigation without proposing an action. The incident is still open.",
}
OUTCOME_STATUS = {
    "resolved": "The incident is resolved. The alert cleared and stayed clear after the action.",
    "not_recovered": "The incident is still open. The action was executed, but the alert is still firing.",
    "remediation_failed": "The incident is still open. The action failed, so no fix has been applied.",
    "executed_unverified": "The action was executed, but recovery has not been verified. Check the service manually.",
    "rejected": "The incident is still open. The proposed action was rejected and nothing was changed.",
    "timed_out": "The incident is still open. The approval request expired and nothing was changed.",
    "no_action": "The incident is still open. ARGUS proposed no action.",
}
REPORT_SYSTEM = """You write concise post-incident reports for an SRE team.

Use ONLY the facts provided. Never invent numbers, times, causes or actions.

Tense rules, applied strictly:
- Anything that already happened (detection, investigation, decisions, actions, recovery checks) is in the PAST tense.
- The incident's current state is in the PRESENT tense.
- Follow-ups are imperative ("Add an eviction policy").
If an action was not executed (rejected, expired, failed or never proposed), say it was not executed and that the issue is still open.
Ignore any narration in the diagnosis text such as "I'll start by..." or "Now I'll...".
Measurements are point-in-time: describe metrics_during_incident as values "at the time of the investigation", and only call a value current if it comes from metrics_after_action.
Do not invent version numbers, tools, pipelines or systems.

Output exactly these markdown sections and nothing else:
## Summary
(2-3 sentences)
## Root Cause
(1-2 sentences)
## Evidence
(3-5 bullets, each citing a concrete number, timestamp or log line from the facts)
## Action Taken
(what was proposed, what the operator decided, and what was executed)
## Follow-ups
(1-3 imperative bullets taken only from the follow-up items in the diagnosis's recommended action; write "None." if it lists none)"""

RUNS = {}
ACTIVE = set()  # incident ids with a live run thread in this process
_lock = threading.Lock()


def score(report, ground_truth):
    report_lower = report.lower()
    hits = [t for t in ground_truth["key_terms"] if t.lower() in report_lower]
    misses = [t for t in ground_truth["key_terms"] if t.lower() not in report_lower]
    return hits, misses


def llm_judge(report, ground_truth):
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        messages=[{"role": "user", "content": f"""You are an SRE evaluating an incident report.
Ground truth root cause: {ground_truth['root_cause']}
Recommended action: {ground_truth['recommended_action']}
Report:
{report}
Did the report correctly identify the root cause and recommend the right action?
Reply with PASS or FAIL and one sentence explaining why."""}],
    )
    return response.content[0].text


def is_active(key):
    run = RUNS.get(key)
    return bool(run and not run["result"]["done"])


def start_run(key, kind, incident_dir, incident=None):
    incident_dir = resolve_scenario(incident_dir)
    run = {
        "key": key, "kind": kind, "incident_dir": incident_dir, "scenario": incident_dir.rsplit("/", 1)[-1],
        "incident_id": incident["id"] if incident else None,
        "response_q": queue.Queue(), "pending": None, "decision": None, "progress": [], "timeline": [],
        "action": None, "action_result": "", "remediation": None,
        "alert_text": "", "metrics_before": "", "diagnosis": "",
        "result": {"done": False, "report": None, "error": None, "eval": None},
        "started_at": time.time(),
    }
    RUNS[key] = run
    if incident:
        ACTIVE.add(incident["id"])
    threading.Thread(target=_work, args=(run, incident), daemon=True).start()
    return run


def submit_decision(key, decision):
    """Replay runs only (live runs decide through incidents.set_decision). One-shot, so a stale click can never approve a later action."""
    run = RUNS.get(key)
    with _lock:
        if not run or run["pending"] is None or time.time() > run["pending"]["deadline"]:
            return False
        run["pending"] = None
        run["response_q"].put(decision)
        return True


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _window(secs):
    return f"{secs // 60} min" if secs % 60 == 0 else f"{secs}s"


def _log(run, text):
    run["timeline"].append((_utc_now(), text))


def _publish(run):
    """Mirror UI-visible run state into the shared incident file (live runs only)."""
    if run["incident_id"]:
        update_incident(run["incident_id"], progress=[dict(s) for s in run["progress"]], diagnosis=run["diagnosis"])


def _step(run, name, label, state="running", detail=""):
    step = {"name": name, "label": label, "state": state, "detail": detail}
    run["progress"].append(step)
    _publish(run)
    return step


def _section(report, name):
    match = re.search(rf"## {name}\s*(.+?)(?:\n## |\Z)", report or "", re.S)
    return match.group(1).strip() if match else ""


def _timeline_md(run, incident):
    rows = [(incident["active_at"], f"The `{incident['alertname']}` alert started firing."),
            (incident.get("opened_at", ""), f"ARGUS detected the alert and opened {incident['id']}.")] + run["timeline"]
    return "## Timeline (UTC)\n" + "\n".join(f"- {t[11:]} — {text}" for t, text in rows if t)


def _post_incident_report(run, incident, outcome, metrics_after, resolved_at, raw_report):
    """Final report: narrative from the model under strict tense rules, timeline built from recorded events."""
    facts = {
        "incident": incident["id"], "service": incident["service"], "severity": incident["severity"],
        "alert": incident["alertname"], "alert_summary": incident["summary"],
        "firing_since_utc": incident["active_at"], "detected_by_argus_utc": incident.get("opened_at"),
        "alert_details": run["alert_text"], "metrics_during_incident": run["metrics_before"],
        "metrics_after_action": metrics_after or "(not measured)",
        "arguss_diagnosis": run["diagnosis"] or raw_report,
        "proposed_action": describe_action(run["action"]["tool_name"], run["action"]["args"]).replace("`", "") if run["action"] else "none",
        "operator_decision": run["decision"] or "none (no action was proposed)",
        "action_result": run["action_result"] or "(action not executed)",
        "outcome": OUTCOME_FACTS[outcome],
        "events": [f"{t[11:]} {text}" for t, text in run["timeline"]],
    }
    try:
        response = client.messages.create(
            model="claude-haiku-4-5", max_tokens=900, system=REPORT_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(facts, indent=2)}],
        )
        narrative = response.content[0].text.strip()
        narrative = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", narrative)
        if "## Summary" not in narrative:
            raise ValueError("report missing required sections")
    except Exception:
        narrative = (f"## Summary\nARGUS investigated the `{incident['alertname']}` alert on `{incident['service']}`. "
                     f"{OUTCOME_FACTS[outcome]}")
    outcome_md = "## Outcome\n" + OUTCOME_STATUS[outcome]
    if metrics_after:
        outcome_md += f"\n\nLatest measurements (after the action):\n```\n{metrics_after.strip()}\n```"
    head, sep, followups = narrative.partition("## Follow-ups")
    narrative = head.rstrip() + "\n\n" + outcome_md + ("\n\n## Follow-ups" + followups if sep else "")
    status = "Resolved" if outcome == "resolved" else "Open"
    header = (f"**{incident['id']} — Post-incident report**  \n"
              f"*Generated {_utc_now()} · Current status: {status}*\n\n")
    return header + narrative + "\n\n" + _timeline_md(run, incident)


def _send_outcome_email(run, incident, outcome, report, resolved_at=None, metrics_after=""):
    namespace = re.search(r"namespace:\s*([\w-]+)", run["alert_text"], re.I)
    step = _step(run, "email", "Sending incident email")
    subject, text, html = build_resolution_email({
        "id": incident["id"], "outcome": outcome, "severity": incident["severity"],
        "service": incident["service"], "namespace": namespace.group(1) if namespace else "default",
        "detected_at": incident.get("opened_at", ""), "firing_since": incident["active_at"],
        "resolved_at": resolved_at, "tool_name": run["action"]["tool_name"], "args": run["action"]["args"],
        "action_result": run["action_result"], "summary": _section(report, "Summary"),
        "root_cause": _section(report, "Root Cause"),
        "metrics_before": run["metrics_before"], "metrics_after": metrics_after,
    })
    status = send_escalation(subject, text, html)
    step["state"], step["detail"] = "done", status
    update_incident(incident["id"], notification=status)
    _publish(run)


def _verify(run, incident):
    """After an approved fix: confirm the alert stays clear. Returns (recovered, metrics_after)."""
    step = _step(run, "verify", "Verifying recovery (alert must stay clear)")
    deadline = time.time() + VERIFY_TIMEOUT_SECS
    clear_polls = 0
    recovered = False
    while time.time() < deadline:
        time.sleep(VERIFY_INTERVAL_SECS)
        try:
            still_firing = incident["fp"] in {a["fp"] for a in fetch_firing_alerts()}
        except Exception:
            continue
        clear_polls = 0 if still_firing else clear_polls + 1
        if clear_polls >= CLEAR_POLLS_REQUIRED:
            recovered = True
            break
    try:
        metrics_after = get_metrics(run["incident_dir"])
    except Exception as e:
        metrics_after = f"(could not read metrics: {e})"
    step["state"] = "done"
    if recovered:
        step["detail"] = "Alert cleared and stayed clear."
        _log(run, "The alert cleared and stayed clear on consecutive checks.")
    else:
        step["detail"] = f"Alert still firing after {VERIFY_TIMEOUT_SECS}s."
        _log(run, f"The alert was still firing after {VERIFY_TIMEOUT_SECS}s of verification.")
    _publish(run)
    return recovered, metrics_after


def _finalize_incident(run, incident, report, verify_enabled):
    """Work out the outcome, write the post-incident report, set the final status, and send the closure email."""
    incident_id = incident["id"]
    metrics_after, recovered = "", None
    if run["remediation"] == "ok" and verify_enabled:
        recovered, metrics_after = _verify(run, incident)
        outcome = "resolved" if recovered else "not_recovered"
    elif run["remediation"] == "ok":
        outcome = "executed_unverified"
    elif run["remediation"] == "failed":
        outcome = "remediation_failed"
    elif run["decision"] == "no":
        outcome = "rejected"
    elif run["decision"] == "timeout":
        outcome = "timed_out"
    else:
        outcome = "no_action"

    resolved_at = _utc_now() if outcome == "resolved" else None
    final_report = _post_incident_report(run, incident, outcome, metrics_after, resolved_at, report)
    fields = {"report": final_report, "diagnosis": run["diagnosis"]}
    current = next((i for i in list_incidents() if i["id"] == incident_id), {})
    if outcome == "resolved":
        fields.update(status="Resolved", resolved_at=resolved_at)
    elif outcome == "not_recovered":
        fields["status"] = "Recovery not confirmed"
    elif outcome == "no_action" and current.get("status") == "Investigating":
        fields["status"] = "Investigated"
    update_incident(incident_id, **fields)

    if outcome in ("resolved", "not_recovered", "remediation_failed"):
        email_outcome = "failed" if outcome == "remediation_failed" else outcome
        _send_outcome_email(run, incident, email_outcome, final_report, resolved_at, metrics_after)


def _approval_email(run, incident, pending, reminder):
    why = _section(run["diagnosis"], "Root Cause") or run["diagnosis"][-400:]
    expires = datetime.datetime.fromtimestamp(pending["deadline"], datetime.timezone.utc).strftime("%H:%M:%SZ")
    subject, text, html = build_approval_email(incident, pending["tool_name"], pending["args"], why, expires, reminder)
    update_incident(incident["id"], approval_email=send_escalation(subject, text, html))


def _wait_decision(run, incident, pending):
    """Block until the operator decides or the request expires. Live runs read decisions from the shared file."""
    incident_id = incident["id"] if incident else None
    halfway = pending["deadline"] - pending["timeout_secs"] / 2
    reminded = False

    def take():
        if incident_id:
            decision = (get_incident(incident_id) or {}).get("decision")
            if decision:
                update_incident(incident_id, decision=None, pending=None)
            return decision
        try:
            return run["response_q"].get_nowait()
        except queue.Empty:
            return None

    decision = None
    while time.time() < pending["deadline"]:
        decision = take()
        if decision:
            break
        if incident_id and not reminded and time.time() > halfway:
            reminded = True
            _approval_email(run, incident, pending, reminder=True)
        time.sleep(0.5)
    if not decision:
        if incident_id:
            update_incident(incident_id, pending=None)  # close the window, then pick up any decision that raced the expiry
        decision = take() or "timeout"
    with _lock:
        run["pending"] = None
    return decision


def _work(run, incident):
    result = run["result"]
    incident_id = incident["id"] if incident else None
    verify_enabled = bool(incident_id and os.getenv("PROMETHEUS_URL"))

    def set_status(status):
        if incident_id:
            update_incident(incident_id, status=status)

    def approval_callback(tool_name, args, dry_output):
        while not run["response_q"].empty():
            run["response_q"].get_nowait()
        run["action"] = {"tool_name": tool_name, "args": args}
        set_status("Awaiting approval")
        _log(run, f"ARGUS proposed an action: {describe_action(tool_name, args).replace('`', '')}")
        pending = {"tool_name": tool_name, "args": args, "dry_output": dry_output,
                   "deadline": time.time() + APPROVAL_TIMEOUT_SECS, "timeout_secs": APPROVAL_TIMEOUT_SECS}
        if incident_id:
            update_incident(incident_id, decision=None, pending=pending)
        with _lock:
            run["pending"] = pending
        if incident_id:
            _approval_email(run, incident, pending, reminder=False)
        decision = _wait_decision(run, incident, pending)
        run["decision"] = decision
        _log(run, {"yes": "The operator approved the action.",
                   "no": "The operator rejected the action.",
                   "timeout": f"The approval request expired after {_window(APPROVAL_TIMEOUT_SECS)} with no response."}[decision])
        set_status(DECISION_STATUS.get(decision, "Investigated"))
        return decision

    def on_event(kind, name, detail):
        if kind == "diagnosis":
            run["diagnosis"] = detail
            _log(run, "ARGUS completed its diagnosis.")
            _publish(run)
            return
        if kind == "tool_start":
            _step(run, name, TOOL_LABELS.get(name, name))
            return
        if name == "get_alert":
            run["alert_text"] = detail
        elif name == "get_metrics" and not run["metrics_before"]:
            run["metrics_before"] = detail
        for step in reversed(run["progress"]):
            if step["name"] == name and step["state"] == "running":
                step["state"] = "done"
                if name in WRITE_TOOLS:
                    step["detail"] = " · ".join(line.strip() for line in detail.splitlines() if line.strip())[:500]
                break
        if name in WRITE_TOOLS and run["decision"] == "yes":
            run["action_result"] = detail
            if detail.startswith("ERROR"):
                run["remediation"] = "failed"
                _log(run, f"The action failed: {detail[:120]}")
                set_status("Remediation failed")
            else:
                run["remediation"] = "ok"
                _log(run, "The action completed successfully.")
                set_status("Verifying recovery" if verify_enabled else "Remediation executed")
        _publish(run)

    try:
        set_status("Investigating")
        _log(run, "ARGUS started its investigation.")
        report = main(run["incident_dir"], silent=True, approval_callback=approval_callback,
                      incident_id=incident_id, event_callback=on_event, offline=run["kind"] == "replay")
        result["report"] = report
        if run["kind"] == "replay":
            try:
                with open(f"{run['incident_dir']}/ground_truth.json", encoding="utf-8") as f:
                    gt = json.load(f)
                hits, misses = score(report, gt)
                result["eval"] = {"gt": gt, "hits": hits, "misses": misses, "verdict": llm_judge(report, gt)}
            except Exception as e:
                result["eval"] = {"error": str(e)}
        if incident_id:
            update_incident(incident_id, report=report)
            _finalize_incident(run, incident, report, verify_enabled)
    except Exception as e:
        result["error"] = str(e)
        if incident_id:
            update_incident(incident_id, status="Investigation failed", report=f"Investigation failed: {e}")
    finally:
        with _lock:
            run["pending"] = None
        if incident_id:
            ACTIVE.discard(incident_id)
            update_incident(incident_id, run_active=False, pending=None, decision=None)
        result["done"] = True
