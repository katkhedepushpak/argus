import os, sys
sys.stdout.reconfigure(encoding="utf-8")   # Windows: allow modern characters in output

from dotenv import load_dotenv
from anthropic import AnthropicFoundry

load_dotenv()
client = AnthropicFoundry(
    base_url=os.getenv("ANTHROPIC_FOUNDRY_BASE_URL"),
    api_key=os.getenv("ANTHROPIC_FOUNDRY_API_KEY"),
)

# --- Two real tools now ---
def get_alert():
    return "payment-service p99 latency up 300% (120ms -> 480ms). 5xx rising. Started ~09:42Z."

def get_metrics():
    return ("payment-service memory climbing 512MB->1520MB toward the 1536MB limit; "
            "pod restarts 0->8; began right after the 07:25Z deploy of v2.4.0.")

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

# --- The agent loop: repeat until the light turns green (or we hit the cap) ---
MAX_STEPS = 10
for step in range(MAX_STEPS):
    response = client.messages.create(
        model="claude-haiku-4-5", max_tokens=500, tools=tools, messages=messages,
    )
    print(f"[step {step+1}] stop_reason:", response.stop_reason)

    # Green light -> Claude is done.
    if response.stop_reason == "end_turn":
        print("\nFinal answer:")
        for block in response.content:
            if block.type == "text":
                print(block.text)
        break

    # Red light -> run the requested tool(s), feed results back, loop again.
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