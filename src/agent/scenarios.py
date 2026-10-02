"""Recorded incident scenarios: evidence files plus a ground-truth answer.

Used for offline runs, dashboard replay and the eval harness. Adding a scenario means adding a folder under
scenarios/ with alert.txt, metrics.txt, logs.txt, deploy_history.json, git_log.txt and ground_truth.json.
"""
from pathlib import Path

SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "scenarios"
DEFAULT_SCENARIO = "payment-memory-leak"
# Live runs still borrow deploy history and git log from here: those two tools have no live source yet.
LIVE_FALLBACK_SCENARIO = "payment-memory-leak"


def list_scenarios():
    if not SCENARIOS_DIR.is_dir():
        return []
    return sorted(p.name for p in SCENARIOS_DIR.iterdir() if (p / "ground_truth.json").is_file())


def scenario_path(name):
    return str(SCENARIOS_DIR / name)


def resolve_scenario(name_or_path):
    """Accept a scenario name or a directory path; return an absolute directory path."""
    candidate = Path(name_or_path)
    if candidate.is_dir():
        return str(candidate.resolve())
    if (SCENARIOS_DIR / name_or_path).is_dir():
        return scenario_path(name_or_path)
    raise ValueError(f"Unknown scenario {name_or_path!r}. Available: {', '.join(list_scenarios()) or 'none'}")
