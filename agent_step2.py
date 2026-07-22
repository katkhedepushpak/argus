import os
from dotenv import load_dotenv
from anthropic import AnthropicFoundry

load_dotenv()
client = AnthropicFoundry(
    base_url=os.getenv("ANTHROPIC_FOUNDRY_BASE_URL"),
    api_key=os.getenv("ANTHROPIC_FOUNDRY_API_KEY"),
)

# The REAL function behind the tool (hardcoded data for now).
def get_alert():
    return "payment-service p99 latency up 300% (120ms -> 480ms). 5xx errors rising. Started ~09:42Z."

tools = [
    {
        "name": "get_alert",
        "description": "Get the production alert that just fired.",
        "input_schema": {"type": "object", "properties": {}},
    }
]

# The conversation (our memory) starts as one user message.
messages = [
    {"role": "user", "content": "What is the current production incident about? Answer in one sentence."}
]

# --- Round 1: Claude asks for a tool ---
response = client.messages.create(
    model="claude-haiku-4-5", max_tokens=300, tools=tools, messages=messages,
)
print("Round 1 stop_reason:", response.stop_reason)

# Rule #1: append Claude's request BEFORE sending results.
messages.append({"role": "assistant", "content": response.content})

# Run the tool Claude named (dispatch on block.name — the proper way).
tool_results = []
for block in response.content:
    if block.type == "tool_use":
        print("Claude asked for:", block.name)
        if block.name == "get_alert":
            result = get_alert()
        else:
            result = f"(no function wired for tool '{block.name}')"
        tool_results.append({
            "type": "tool_result",
            "tool_use_id": block.id,   # Rule #2: match the ticket number
            "content": result,
        })

# Feed results back as a new user message.
messages.append({"role": "user", "content": tool_results})

# --- Round 2: Claude reads the result and answers ---
response = client.messages.create(
    model="claude-haiku-4-5", max_tokens=300, tools=tools, messages=messages,
)
print("Round 2 stop_reason:", response.stop_reason)
print("\nFinal answer:")
for block in response.content:
    if block.type == "text":
        print(block.text)