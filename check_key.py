"""A tiny check that our API key loads correctly — without ever printing it."""

import os
from dotenv import load_dotenv

# load_dotenv() reads the .env file and puts its NAME=value pairs into the
# environment, so os.getenv can find them.
load_dotenv()

key = os.getenv("ANTHROPIC_API_KEY")

if not key or key == "paste-your-key-here":
    print("No key found yet — open .env, paste your real key after the '=', and save.")
else:
    # Show only the first 7 and last 4 characters — never the whole secret.
    print(f"Key loaded: starts {key[:7]}..., ends ...{key[-4:]}, length {len(key)}")
