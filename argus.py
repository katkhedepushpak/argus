"""
ARGUS — Autonomous Reasoning Gateway for Unified Systems.

An LLM agent that investigates a production incident like an on-call SRE:
it calls read-only tools to gather evidence (alert, metrics), reasons about
the root cause, and returns a structured report. It only investigates and
recommends — a human approves any remediation.
"""

import os
import sys

sys.stdout.reconfigure(encoding="utf-8")  # Windows: allow modern characters in output

from dotenv import load_dotenv
from anthropic import AnthropicFoundry

load_dotenv()
client = AnthropicFoundry(
    base_url=os.getenv("ANTHROPIC_FOUNDRY_BASE_URL"),
    api_key=os.getenv("ANTHROPIC_FOUNDRY_API_KEY"),
)

SYSTEM_PROMPT = """You are ARGUS, an autonomous Site Reliability Engineer (SRE) investigating a production incident.
Use your read-only tools to gather evidence before concluding — never guess before you have data.
When confident, STOP calling tools and reply in EXACTLY this structure:

## Root Cause
<1-2 sentences naming the specific cause>

## Evidence
- <a bullet tying a specific observation to your conclusion>

## Recommended Action
<the single most important action to restore service, plus follow-up>

## Confidence
<High | Medium | Low> - <one line>

Cite concrete numbers and timestamps. You only investigate and recommend — never execute or claim to have executed a fix. A human approves all actions."""


# --- Tools (read-only): each reads telemetry for the incident ---
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


def main():
    messages = [
        {"role": "user", "content": "A production alert just fired. Investigate it."}
    ]

    MAX_STEPS = 10
    step = 0
    while True:
        step += 1
        if step > MAX_STEPS:
            print("Hit safety cap — stopping.")
            break

        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )
        print(f"[step {step}] stop_reason:", response.stop_reason)

        if response.stop_reason == "end_turn":
            print("\n" + "=" * 60)
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


if __name__ == "__main__":
    main()
