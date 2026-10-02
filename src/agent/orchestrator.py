import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
from anthropic import AnthropicFoundry
from src.agent.prompts import SYSTEM_PROMPT
from src.agent.notify import send_escalation, build_incident_email
from src.agent.incidents import next_incident_id
import datetime
import re
import subprocess
from src.agent.tools import (
    get_alert, get_metrics, get_logs, get_deploy_history, get_git_log,
    restart_pod, rollback_deployment, scale_deployment,
    TOOLS, WRITE_TOOLS,
)

load_dotenv()
client = AnthropicFoundry(
    base_url=os.getenv("ANTHROPIC_FOUNDRY_BASE_URL"),
    api_key=os.getenv("ANTHROPIC_FOUNDRY_API_KEY"),
)

def _build_dry_output(tool_name, args):
    try:
        if tool_name == "restart_pod":
            return f"kubectl rollout restart deployment/{args['service']}  (will terminate current pods and start fresh ones)"
        elif tool_name == "rollback_deployment":
            dry = subprocess.run(["kubectl", "rollout", "undo", f"deployment/{args['service']}", "--dry-run=client"], capture_output=True, text=True)
            return (dry.stdout or dry.stderr).strip()
        elif tool_name == "scale_deployment":
            dry = subprocess.run(["kubectl", "scale", f"deployment/{args['service']}", f"--replicas={args['replicas']}", "--dry-run=client"], capture_output=True, text=True)
            return (dry.stdout or dry.stderr).strip()
    except FileNotFoundError:
        return f"(kubectl not found on this machine - dry-run unavailable for {tool_name} {args})"
    return ""


def _execute_remediation(tool_name, args):
    if tool_name == "restart_pod":
        return restart_pod(args["service"])
    elif tool_name == "rollback_deployment":
        return rollback_deployment(args["service"])
    elif tool_name == "scale_deployment":
        return scale_deployment(args["service"], args["replicas"])


def _approval_gate(tool_name, args, approval_callback=None, incident=None):
    incident = incident or {}
    dry_output = _build_dry_output(tool_name, args)

    if approval_callback is not None:
        decision = approval_callback(tool_name, args, dry_output)
    else:
        print("\n" + "=" * 55)
        print(f"  ARGUS proposes: {tool_name}")
        print(f"  Args:           {args}")
        print(f"  Dry-run output: {dry_output}")
        print("=" * 55)
        decision = input("  Approve? (yes/no): ").strip().lower()

    if decision != "yes":
        outcome = "TIMED OUT with no response" if decision == "timeout" else "REJECTED by the operator"
        alert_text = incident.get("alert_text", "")
        severity = re.search(r"severity:\s*(\w+)", alert_text, re.I)
        namespace = re.search(r"namespace:\s*([\w-]+)", alert_text, re.I)
        subject, text, html = build_incident_email({
            "id": incident.get("id", "INC-UNKNOWN"),
            "severity": severity.group(1) if severity else "unknown",
            "service": args.get("service", "unknown"),
            "namespace": namespace.group(1) if namespace else "default",
            "detected_at": incident.get("detected_at", ""),
            "outcome": outcome,
            "tool_name": tool_name,
            "args": args,
            "alert_text": alert_text,
            "metrics_text": incident.get("metrics_text", ""),
            "diagnosis": incident.get("diagnosis", ""),
            "dry_output": dry_output,
        })
        email_status = send_escalation(subject, text, html)
        return (f"Action NOT executed: {outcome}. No changes were made. "
                f"Incident {incident.get('id', '')} is still open. Escalation: {email_status}.")

    return _execute_remediation(tool_name, args)


def main(incident_dir=None, silent=False, approval_callback=None, incident_id=None, event_callback=None):
    if incident_dir is None:
        incident_dir = sys.argv[1] if len(sys.argv) > 1 else "incident1"
    incident = {
        "id": incident_id or next_incident_id(),
        "detected_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "alert_text": "",
        "metrics_text": "",
        "diagnosis": "",
    }
    header = f"**Incident {incident['id']}**\n\n"
    if not silent:
        print(f"Opened {incident['id']}")
    messages = [{"role": "user", "content": f"A production alert just fired (incident {incident['id']}). Investigate it."}]
    MAX_STEPS = 10
    step = 0
    report_parts = []
    while True:
        step += 1
        if step > MAX_STEPS:
            if not silent:
                print(f"Hit max steps ({MAX_STEPS}) — stopping.")
            break
        parts_before = len(report_parts)
        with client.messages.stream(
            model="claude-haiku-4-5",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                if not silent:
                    print(text, end="", flush=True)
                report_parts.append(text)
            response = stream.get_final_message()
        if len(report_parts) > parts_before:
            report_parts.append("\n\n")
        if not silent:
            print(f"\n[step {step}] stop_reason: {response.stop_reason}")

        if response.stop_reason == "end_turn":
            return header + "".join(report_parts)

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                if not silent:
                    print("   running:", block.name)
                if event_callback:
                    event_callback("tool_start", block.name, "")
                if block.name == "get_alert":
                    result = get_alert(incident_dir)
                    incident["alert_text"] = result
                elif block.name == "get_metrics":
                    result = get_metrics(incident_dir)
                    incident["metrics_text"] = result
                elif block.name == "get_logs":
                    result = get_logs(incident_dir)
                elif block.name == "get_deploy_history":
                    result = get_deploy_history(incident_dir)
                elif block.name == "get_git_log":
                    result = get_git_log(incident_dir)
                elif block.name in WRITE_TOOLS:
                    incident["diagnosis"] = "".join(report_parts)
                    if event_callback:
                        event_callback("diagnosis", "", incident["diagnosis"])
                    result = _approval_gate(block.name, block.input, approval_callback=approval_callback, incident=incident)
                else:
                    result = f"(no function wired for '{block.name}')"
                if event_callback:
                    event_callback("tool_done", block.name, result)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages.append({"role": "user", "content": tool_results})
    return header + "".join(report_parts)

if __name__ == "__main__":
    main()