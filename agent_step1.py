import os
from dotenv import load_dotenv
from anthropic import AnthropicFoundry

load_dotenv()
client = AnthropicFoundry(
    base_url=os.getenv("ANTHROPIC_FOUNDRY_BASE_URL"),
    api_key=os.getenv("ANTHROPIC_FOUNDRY_API_KEY"),
)

# Describe ONE tool to Claude. We are not running it yet — just offering it.
tools = [
    {
        "name": "get_alert",
        "description": "Get the production alert that just fired. Use this when you need to know what the current incident is about.",
        "input_schema": {"type": "object", "properties": {}},
    }
]

response = client.messages.create(
    model="claude-haiku-4-5",
    max_tokens=200,
    tools=tools,   # <-- the new part: we hand Claude the tool list
    messages=[
        {"role": "user", "content": "What is the current production incident about?"}
    ],
)

# Inspect what Claude decided to do.
print("stop_reason:", response.stop_reason)
for block in response.content:
    print("block type:", block.type)
    if block.type == "tool_use":
        print("  -> Claude wants to call:", block.name)