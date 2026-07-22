"""
ARGUS eval harness — runs each incident and scores the report against ground truth.
"""
import datetime
import json
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

from src.agent.orchestrator import main
from dotenv import load_dotenv
from anthropic import AnthropicFoundry

load_dotenv()
client = AnthropicFoundry(
    base_url=os.getenv("ANTHROPIC_FOUNDRY_BASE_URL"),
    api_key=os.getenv("ANTHROPIC_FOUNDRY_API_KEY"),
)


INCIDENTS = ["incident1", "incident2", "incident3"]

def score(report, ground_truth):
    report_lower = report.lower()
    hits = [t for t in ground_truth["key_terms"] if t.lower() in report_lower]
    misses = [t for t in ground_truth["key_terms"] if t.lower() not in report_lower]
    return hits, misses


def llm_judge(report, ground_truth):
      verdict = client.messages.create(
          model="claude-haiku-4-5",
          max_tokens=200,
          messages=[{"role": "user", "content": f"""You are an SRE evaluating an incident report against the ground truth. 
          Ground truth root cause: {ground_truth['root_cause']}
          Recommended action: {ground_truth['recommended_action']}
          Report to evaluate: {report}
          Did the report correctly identify the root cause and recommend the right action?
          Reply with PASS or FAIL and one sentence explaining why."""}]
          )
      return verdict.content[0].text

if __name__ == "__main__":
    for incident in INCIDENTS:
        print(f"\n{'='*60}")
        print(f"Running: {incident}")
        print("=" * 60)
        report = main(incident, silent=True)

        with open(f"{incident}/ground_truth.json", encoding="utf-8") as f:
            gt = json.load(f)

        hits, misses = score(report, gt)
        print(f"\nScore: {len(hits)}/{len(gt['key_terms'])} key terms found")
        print(f"  Found : {hits}")
        if misses:
            print(f"  Missed: {misses}")

        verdict = llm_judge(report, gt)
        print(f"LLM judge: {verdict}")

        result = {
            "incident": incident,
            "score": f"{len(hits)}/{len(gt['key_terms'])}",
            "keywords_found": hits,
            "keywords_missed": misses,
            "judge": verdict.split(".")[0],
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        }
        with open("eval_results.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(result) + "\n")