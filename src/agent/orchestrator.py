import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
from anthropic import AnthropicFoundry
from src.agent.prompts import SYSTEM_PROMPT
from src.agent.tools import get_alert, get_metrics, get_logs, get_deploy_history, TOOLS

load_dotenv()
client = AnthropicFoundry(
    base_url=os.getenv("ANTHROPIC_FOUNDRY_BASE_URL"),
    api_key=os.getenv("ANTHROPIC_FOUNDRY_API_KEY"),
)

def main(incident_dir=None):
    if incident_dir is None:
        incident_dir = sys.argv[1] if len(sys.argv) > 1 else "incident1"
    messages = [{"role": "user", "content": "A production alert just fired. Investigate it."}]
    MAX_STEPS = 10
    step = 0
    while True:
        step += 1
        if step > MAX_STEPS:
            print("Hit safety cap — stopping.")
            break
        response = client.messages.create(
            model="claude-haiku-4-5", max_tokens=1500,
            system=SYSTEM_PROMPT, tools=TOOLS, messages=messages,
        )
        print(f"[step {step}] stop_reason:", response.stop_reason)
        if response.stop_reason == "end_turn":
            for block in response.content:
                if block.type == "text":
                    return block.text
            break
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                print("   running:", block.name)
                if block.name == "get_alert":
                    result = get_alert(incident_dir)
                elif block.name == "get_metrics":
                    result = get_metrics(incident_dir)
                elif block.name == "get_logs":
                    result = get_logs(incident_dir)
                elif block.name == "get_deploy_history":
                    result = get_deploy_history(incident_dir)
                else:
                    result = f"(no function wired for '{block.name}')"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages.append({"role": "user", "content": tool_results})
    return ""