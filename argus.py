"""
ARGUS — Autonomous Reasoning Gateway for Unified Systems.
An LLM agent that investigates production incidents like an on-call SRE.
"""
from src.agent.orchestrator import main

if __name__ == "__main__":
    report = main()
    print("\n" + "=" * 60)
    print(report)