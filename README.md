# ARGUS — Autonomous Reasoning Gateway for Unified Systems

An LLM agent that investigates production incidents the way an on-call SRE would — reads the alert, pulls metrics, pod logs, deployment history, and git commits, then produces a structured root-cause report. It investigates and recommends; a human approves any remediation.

Built with the Anthropic Claude API (tool-use agent loop) and Azure AI Foundry.

---

## How it works

```
Alert fired
    │
    ▼
ARGUS (Claude, tool-use loop)
    │
    ├── get_alert          → what triggered
    ├── get_metrics        → memory, restarts, latency, error rate
    ├── get_logs           → OOMKilled, OutOfMemoryError, pool exhaustion
    ├── get_deploy_history → which version dropped and when
    └── get_git_log        → what changed in that version
    │
    ▼
Root Cause / Evidence / Recommended Action / Confidence
    │
    ▼
Human approves remediation
```

The agent decides which tools to call and in what order. All tools are read-only.

---

## Setup

```bash
git clone https://github.com/katkhedepushpak/argus.git
cd argus
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then fill in your Foundry credentials
```

## Run

```bash
python argus.py incident1        # memory leak — payment-service v2.4.0
python argus.py incident2        # DB connection pool exhaustion — checkout-service
```

## Eval

```bash
python eval.py
```

Runs ARGUS against both incidents and scores each report against `ground_truth.json` — checking that key terms (version, failure signature, recommended action) appear in the output.

---

## Incident fixtures

| Incident | Service | Failure mode | Correct action |
|----------|---------|--------------|----------------|
| `incident1` | payment-service | Unbounded in-memory cache (v2.4.0) → OOMKill loop | Roll back to v2.3.1 |
| `incident2` | checkout-service | DB connection pool exhaustion from slow queries | Investigate DB; tune pool size |

Each fixture: `alert.txt`, `metrics.txt`, `logs.txt`, `deploy_history.json`, `git_log.txt`, `ground_truth.json`.

---

## Project layout

```
argus.py                  entry point
eval.py                   eval harness — scores report vs ground truth
src/
  agent/
    orchestrator.py       Claude tool-use loop
    tools.py              read-only investigation tools
    prompts.py            SRE system prompt
incident1/                memory leak incident fixtures
incident2/                DB pool exhaustion incident fixtures
requirements.txt
```

---

## Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| 1 | Done | Investigator agent over recorded fixtures — runs anywhere, no cluster |
| 2 | Planned | Live telemetry from a `kind` cluster with fault injection |
| 3 | Planned | Remediation agent: dry-run → human approval → execute |
| 4 | Planned | Eval harness with MLflow tracking; vector DB for similar past incidents |
| 5 | Planned | Streamlit dashboard — incident feed + reasoning trace |
