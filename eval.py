"""
ARGUS eval harness — runs each incident and scores the report against ground truth.
"""
import json
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

from src.agent.orchestrator import main

INCIDENTS = ["incident1", "incident2"]

def score(report, ground_truth):
    report_lower = report.lower()
    hits = [t for t in ground_truth["key_terms"] if t.lower() in report_lower]
    misses = [t for t in ground_truth["key_terms"] if t.lower() not in report_lower]
    return hits, misses

if __name__ == "__main__":
    for incident in INCIDENTS:
        print(f"\n{'='*60}")
        print(f"Running: {incident}")
        print("=" * 60)
        report = main(incident)

        with open(f"{incident}/ground_truth.json", encoding="utf-8") as f:
            gt = json.load(f)

        hits, misses = score(report, gt)
        print(f"\nScore: {len(hits)}/{len(gt['key_terms'])} key terms found")
        print(f"  Found : {hits}")
        if misses:
            print(f"  Missed: {misses}")