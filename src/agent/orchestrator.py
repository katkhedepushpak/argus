import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
from anthropic import AnthropicFoundry
from src.agent.prompts import SYSTEM_PROMPT
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

def _approval_gate(tool_name, args):
    if tool_name == "restart_pod":
        dry_output = f"kubectl rollout restart deployment/{args['service']}  (will terminate current pods and start fresh ones)"
    elif tool_name == "rollback_deployment":
        dry = subprocess.run(["kubectl", "rollout", "undo", f"deployment/{args['service']}", "--dry-run=client"], capture_output=True, text=True)
        dry_output = (dry.stdout or dry.stderr).strip()
    elif tool_name == "scale_deployment":
        dry = subprocess.run(["kubectl", "scale", f"deployment/{args['service']}", f"--replicas={args['replicas']}", "--dry-run=client"], capture_output=True, text=True)
        dry_output = (dry.stdout or dry.stderr).strip()

    print("\n" + "=" * 55)
    print(f"  ARGUS proposes: {tool_name}")
    print(f"  Args:           {args}")
    print(f"  Dry-run output: {dry_output}")
    print("=" * 55)
    answer = input("  Approve? (yes/no): ").strip().lower()
    if answer != "yes":
        return "Action rejected by user. No changes were made."

    if tool_name == "restart_pod":
        return restart_pod(args["service"])
    elif tool_name == "rollback_deployment":
        return rollback_deployment(args["service"])
    elif tool_name == "scale_deployment":
        return scale_deployment(args["service"], args["replicas"])


def main(incident_dir=None, silent=False):
    if incident_dir is None:
        incident_dir = sys.argv[1] if len(sys.argv) > 1 else "incident1"
    messages = [{"role": "user", "content": "A production alert just fired. Investigate it."}]
    MAX_STEPS = 10
    step = 0
    while True:
        step += 1
        if step > MAX_STEPS:
            if not silent:
                print(f"Hit max steps ({MAX_STEPS}) — stopping.")
            break
        report_parts = []
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
        if not silent:
            print(f"\n[step {step}] stop_reason: {response.stop_reason}")

        if response.stop_reason == "end_turn":
            return "".join(report_parts)

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                if not silent:
                    print("   running:", block.name)
                if block.name == "get_alert":
                    result = get_alert(incident_dir)
                elif block.name == "get_metrics":
                    result = get_metrics(incident_dir)
                elif block.name == "get_logs":
                    result = get_logs(incident_dir)
                elif block.name == "get_deploy_history":
                    result = get_deploy_history(incident_dir)
                elif block.name == "get_git_log":
                    result = get_git_log(incident_dir)
                elif block.name in WRITE_TOOLS:
                    result = _approval_gate(block.name, block.input)
                else:
                    result = f"(no function wired for '{block.name}')"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages.append({"role": "user", "content": tool_results})
    return ""

if __name__ == "__main__":
    main()