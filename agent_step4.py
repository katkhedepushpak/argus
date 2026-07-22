import os, sys
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
from anthropic import AnthropicFoundry

load_dotenv()
client = AnthropicFoundry(
    base_url=os.getenv("ANTHROPIC_FOUNDRY_BASE_URL"),
    api_key=os.getenv("ANTHROPIC_FOUNDRY_API_KEY"),
)

# --- Tools now read REAL data from files (signature unchanged!) ---
def get_alert():
    with open("incident1/alert.txt", encoding="utf-8") as f:
        return f.read()

def get_metrics():
    with open("incident1/metrics.txt", encoding="utf-8") as f:
        return f.read()

tools = [
    {"name": "get_alert",
    "description": "Get the production alert that just fired.",
    "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_metrics",
    "description": "Get recent metrics (memory, restarts, latency) for the affected service.",
    "input_schema": {"type": "object", "properties": {}}},
]

messages = [
    {"role": "user", "content": "Investigate the current incident and tell me the likely root cause in one sentence."}
]

MAX_STEPS = 10
step = 0
while True:
    step += 1
    if step > MAX_STEPS:
        print("Hit safety cap — stopping.")
        break

    response = client.messages.create(
        model="claude-haiku-4-5", max_tokens=500, tools=tools, messages=messages,
    )
    print(f"[step {step}] stop_reason:", response.stop_reason)

    if response.stop_reason == "end_turn":
        print("\nFinal answer:")
        for block in response.content:
            if block.type == "text":
                print(block.text)
        break

    messages.append({"role": "assistant", "content": response.content})
    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            print("   running:", block.name)
            if block.name == "get_alert":
                result = get_alert()
            elif block.name == "get_metrics":
                result = get_metrics()
            else:
                result = f"(no function wired for '{block.name}')"
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })
    messages.append({"role": "user", "content": tool_results})