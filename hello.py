import os
from dotenv import load_dotenv
from anthropic import AnthropicFoundry

# 1. Load secrets from .env
load_dotenv()

# 2. Make the client (our phone line to Claude via Azure Foundry)
client = AnthropicFoundry(
    base_url=os.getenv("ANTHROPIC_FOUNDRY_BASE_URL"),
    api_key=os.getenv("ANTHROPIC_FOUNDRY_API_KEY"),
)

# 3. Send a message
text = "In one sentence, what does an SRE do?"
response = client.messages.create(
    model="claude-haiku-4-5",
    max_tokens=100,
    messages=[
        {"role": "user", "content": text}
    ],
)

# 4. Print Claude's reply
print(response.content[0].text)