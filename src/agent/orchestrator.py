import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
from anthropic import AnthropicFoundry
from src.agent.prompts import SYSTEM_PROMPT
from src.agent.tools import get_alert, get_metrics, get_logs, get_deploy_history, get_git_log, TOOLS

load_dotenv()
client = AnthropicFoundry(
    base_url=os.getenv("ANTHROPIC_FOUNDRY_BASE_URL"),
    api_key=os.getenv("ANTHROPIC_FOUNDRY_API_KEY"),
)

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
                else:
                    result = f"(no function wired for '{block.name}')"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages.append({"role": "user", "content": tool_results})
    return ""