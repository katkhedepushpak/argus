# ARGUS — Lessons Log

A running record of concepts learned and what was built each session.

---

## Session 2 — 2026-07-21/22

### Lesson 9 — The 3-Edits Pattern (get_logs)
- Every new tool requires exactly 3 edits: write the function, describe it in the tools list, wire into dispatch
- Added `get_logs` — ARGUS now reads pod logs alongside alert and metrics

### Lesson 10 — JSON as a Data Format (get_deploy_history)
- Production telemetry is almost always JSON, not plain text
- Claude reads JSON fluently — pass it as a string, no parsing needed on your end
- Added `get_deploy_history` — ARGUS confirmed deploy timing from real deploy records
- Claude started batching all tools in one step (parallel tool calls, no code needed)

### Lesson 11 — CLI Arguments (sys.argv)
- `sys.argv` is a list Python gives you of everything typed on the command line
- `sys.argv[1]` is the first argument; `len(sys.argv) > 1` checks if one exists
- `python argus.py incident1` → `sys.argv = ['argus.py', 'incident1']`
- ARGUS went from a hardcoded script to a real tool

### Lesson 12 — Second Incident (fixture-driven testing)
- Same agent code, different data, different diagnosis
- Incident 2: DB connection pool exhaustion from slow queries
- ARGUS correctly ruled out deploy (3 days ago, no correlation) and found the DB bottleneck
- Key insight: the payoff of sys.argv — no code changes, just point at a new incident

### Lesson 13 — requirements.txt
- Lists every library the project needs so anyone can run `pip install -r requirements.txt`
- Standard signal that a repo is a real project, not a notebook dump

### Lesson 14 — Python Modules (src/agent/ package)
- Split one big file into a proper package: `prompts.py`, `tools.py`, `orchestrator.py`
- A directory becomes a Python package by adding an empty `__init__.py`
- `from src.agent.tools import get_alert` — explicit imports from other files
- Lesson learned: every function used from another file must be explicitly imported

### Lesson 15 — Eval Harness + ground_truth.json
- Every AI system that makes decisions needs a way to measure if it's correct
- `ground_truth.json`: the known correct answer for each incident
- Keyword scoring: check if key terms appear in the report (fast, brittle)
- `main()` refactored to accept `incident_dir` as a parameter and return report text

### Lesson 16 — get_git_log (completing the evidence chain)
- Git log tells you *what changed* in the deployed version — the "smoking gun"
- ARGUS now cites the exact commit message that introduced the bug
- Lesson learned: imports must be updated in `orchestrator.py` when adding new tools

### Lesson 17 — Close the Loop on Eval
- Updated `ground_truth.json` key terms to include commit hash (`a1b9f22`)
- Keyword eval misses plurals and synonyms — LLM-as-judge is more robust

### Lesson 18 — Merge dev → main
- `dev` is where you build; `main` is what you ship
- Fast-forward merge: `git checkout main && git merge dev && git push origin main`

### Lesson 19 — README
- The front door of your repo — answers: what is this, how do I run it, what does it do
- Also added `.env.example` so setup instructions are actually followable
- Architecture diagram, incident table, roadmap — makes it look like a real project

### Lesson 20 — Streaming Output
- `client.messages.stream()` context manager yields tokens as Claude generates them
- `stream.text_stream` iterates tokens; `stream.get_final_message()` gets the full response
- `flush=True` forces Python to print each token immediately (no buffering)
- Collect streamed text into `report_parts` to return it for eval while still streaming to stdout

### Lesson 21 — LLM-as-Judge
- Keyword matching breaks on plurals, synonyms, phrasing variations
- LLM-as-judge: ask Claude "did this report correctly identify the root cause?"
- Handles nuance that string matching can't — the production standard for ML eval

### Lesson 22 — Silent Mode (Boolean Parameters)
- Functions can accept a `silent=False` parameter to suppress print statements
- Lets `eval.py` call `main(incident, silent=True)` for clean eval output
- Lesson learned: module-level `print()` calls must all be guarded with `if not silent:`

### Lesson 23 — Eval Persistence (eval_results.jsonl)
- `.jsonl` = one JSON object per line — append-only, easy to query
- `datetime.datetime.utcnow().isoformat()` — timestamp each run
- Generated data belongs in `.gitignore`, not in source control

### Lesson 24 — Streamlit Dashboard
- Streamlit reruns the whole script top-to-bottom on every user interaction
- `st.selectbox`, `st.button`, `st.spinner`, `st.markdown`, `st.metric`, `st.columns`
- `st.success` / `st.error` — green/red verdict boxes
- `st.dataframe(rows)` — renders a list of dicts as a sortable table automatically

### Lesson 25 — Incident 3: Upstream Dependency Failure
- Hardest test: order-service is healthy, but its upstream (payment-service) is down
- ARGUS must look outward, not inward, and correctly exonerate the alerting service
- Circuit breaker pattern: open when upstream fails, fast-fail instead of waiting

### Lesson 26 — pytest Unit Tests
- Functions named `test_*` are automatically found and run by pytest
- `assert` — if the condition is false, the test fails with the actual vs expected value
- `tests/__init__.py` — empty file that makes the directory a Python package
- Test one behavior per test function — granular failure messages

### Lesson 27 — GitHub Actions CI
- `.github/workflows/ci.yml` — triggers on push; runs pytest in a fresh Ubuntu VM
- `uses: actions/checkout@v4` — clones the repo onto the CI machine
- `run:` — runs shell commands directly
- `conftest.py` — pytest's setup file, runs before any imports; used to set dummy env vars so module-level client creation doesn't crash in CI

---

## Phase 2 Setup — 2026-07-22

### Infrastructure installed
- **kind** — Kubernetes in Docker; `kind create cluster --name argus` spins up a full local cluster
- **kubectl** — command-line tool to talk to the cluster (was already installed)
- **Helm** — Kubernetes package manager (`pip` for Kubernetes)
- **kube-prometheus-stack** — single Helm chart that installs Prometheus, Grafana, Alertmanager, node-exporter, kube-state-metrics into the `monitoring` namespace

### payment-service demo app created
- FastAPI app with `/charge`, `/leak`, `/health`, `/metrics` endpoints
- `/leak` endpoint grows an unbounded in-memory cache — reproduces the incident1 OOMKill scenario on real infrastructure
- Exposes Prometheus metrics via `prometheus-client`
- Structured JSON logging for Splunk ingestion
- Memory limit: 150Mi — low enough that the leak triggers a real OOMKill
- Kubernetes manifests: Deployment + Service + ServiceMonitor (tells Prometheus to scrape it)

### Next
- Build and deploy payment-service into the argus cluster
- Install Splunk (replacing Loki) for log aggregation
- Inject fault (call `/leak`) and watch real OOMKills in Prometheus
- Repoint ARGUS tools from fixture files to live Prometheus + Splunk APIs
