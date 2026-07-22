# ARGUS — Autonomous Reasoning Gateway for Unified Systems

An AIOps agent that detects, investigates, and remediates production incidents autonomously — the way an on-call SRE would, but in seconds. ARGUS reads live alerts from Prometheus, pulls real-time metrics and pod logs, checks deployment history and git commits, diagnoses the root cause, then proposes a fix and waits for human approval before executing.

Built with the Anthropic Claude API (tool-use agent loop), Azure AI Foundry, Prometheus, Splunk, Fluent Bit, and Kubernetes.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    kind Kubernetes Cluster                       │
│                                                                  │
│  ┌──────────────────┐      ┌────────────────────────────────┐   │
│  │  default namespace│      │      monitoring namespace      │   │
│  │                  │      │                                │   │
│  │ ┌──────────────┐ │      │ ┌────────────┐ ┌───────────┐  │   │
│  │ │payment-service│◄├──────┤─│ Prometheus  │ │  Grafana  │  │   │
│  │ │   :8080       │ │scrape│ │            │ │  :3000    │  │   │
│  │ │  /metrics     │ │      │ └─────┬──────┘ └───────────┘  │   │
│  │ │  /health      │ │      │       │ evaluates              │   │
│  │ │  /leak        │ │      │ ┌─────▼──────────────────────┐│   │
│  │ └──────┬────────┘ │      │ │   PrometheusRule CRD        ││   │
│  │        │ stdout   │      │ │ PaymentServiceHighMemory    ││   │
│  │        │ logs     │      │ │ PaymentServicePodRestarted  ││   │
│  └────────┼──────────┘      │ │ PaymentServiceHighErrors    ││   │
│           │                 │ │ PaymentServiceHighLatency   ││   │
│  ┌────────▼──────────┐      │ └────────────────────────────┘│   │
│  │    Fluent Bit     │      └────────────────────────────────┘   │
│  │   (DaemonSet)     │                                           │
│  │ tails             │                                           │
│  │ /var/log/         │                                           │
│  │ containers/*.log  │                                           │
│  └────────┬──────────┘                                          │
└───────────┼─────────────────────────────────────────────────────┘
            │ HTTPS/HEC :8088               HTTP :9090
            ▼                               ▼
    ┌──────────────┐               ┌─────────────────┐
    │    Splunk    │               │   Prometheus    │
    │  (Docker)    │               │  /api/v1/alerts │
    │  REST :8089  │               │  /api/v1/query  │
    └──────┬───────┘               └────────┬────────┘
           │                                │
           └───────────────┬────────────────┘
                           │
                  ┌────────▼────────┐
                  │     ARGUS       │
                  │                 │
                  │  get_alert()    │ ← Prometheus /api/v1/alerts
                  │  get_metrics()  │ ← Prometheus /api/v1/query
                  │  get_logs()     │ ← Splunk REST API
                  │  get_deploy_    │ ← fixture file
                  │    history()    │
                  │  get_git_log()  │ ← fixture file
                  │                 │
                  │  restart_pod()         ┐
                  │  rollback_deployment() ├─ kubectl via subprocess
                  │  scale_deployment()    ┘  (human approval gate)
                  │                 │
                  │  Claude LLM     │ ← decides tool order,
                  │  (tool use)     │   synthesizes diagnosis
                  └─────────────────┘
```

---

## How it works

```
Alert fires in Prometheus
        │
        ▼
ARGUS (Claude, tool-use loop)
        │
        ├── get_alert          → which alert fired and why
        ├── get_metrics        → memory, restarts, latency, error rate (live)
        ├── get_logs           → pod log lines from Splunk (live)
        ├── get_deploy_history → which version dropped and when
        └── get_git_log        → what changed in that version
        │
        ▼
## Root Cause / Evidence / Recommended Action / Confidence
        │
        ▼
ARGUS calls remediation tool (restart_pod / rollback / scale)
        │
        ▼
Approval gate: dry-run shown → human types yes/no
        │
        ▼ (yes)
kubectl executes → ARGUS confirms recovery
```

---

## Telemetry pipeline

### Logs
```
payment-service stdout
  → Fluent Bit DaemonSet tails /var/log/containers/
  → ships to Splunk HEC (HTTPS port 8088)
  → get_logs() queries Splunk REST API (port 8089)
  → ARGUS reads last 50 log lines
```

### Metrics
```
payment-service /metrics endpoint
  → Prometheus scrapes every 15s via ServiceMonitor
  → get_metrics() queries /api/v1/query (5 PromQL expressions)
  → ARGUS reads memory, cache, restarts, error rate, p99 latency
```

### Alerts
```
Prometheus evaluates PrometheusRule every 15s
  → alert state: inactive → pending → firing
  → get_alert() queries /api/v1/alerts filtered by service=payment-service
  → ARGUS reads which alerts are firing and their descriptions
```

---

## Setup

### Prerequisites
- Docker Desktop with WSL2
- `kind`, `kubectl`, `helm` installed
- Python 3.10+
- Azure AI Foundry access (or swap for direct Anthropic API)

### 1. Clone and install

```bash
git clone https://github.com/katkhedepushpak/argus.git
cd argus
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
cp .env.example .env          # fill in credentials
```

### 2. Create the kind cluster

```bash
kind create cluster --name argus
```

### 3. Install Prometheus stack

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install prometheus prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace
```

### 4. Build and deploy payment-service

```bash
docker build -t payment-service:2.4.0 k8s/payment-service/
kind load docker-image payment-service:2.4.0 --name argus
kubectl apply -f k8s/payment-service/manifests.yaml
kubectl apply -f k8s/payment-service-alerts.yaml
```

### 5. Install Fluent Bit

```bash
helm repo add fluent https://fluent.github.io/helm-charts
helm install fluent-bit fluent/fluent-bit \
  -n logging --create-namespace \
  -f k8s/fluent-bit-values.yaml
```

### 6. Start Splunk

```bash
docker run -d --name splunk \
  --memory=1500m --memory-swap=1500m \
  -p 8000:8000 -p 8088:8088 -p 8089:8089 \
  -e SPLUNK_START_ARGS=--accept-license \
  -e SPLUNK_PASSWORD=<your-password> \
  -e SPLUNK_HEC_TOKEN=<your-hec-token> \
  splunk/splunk:latest
```

### 7. Expose services locally

```bash
kubectl port-forward svc/payment-service 8080:8080 -n default &
kubectl port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 -n monitoring &
```

---

## Run ARGUS

```bash
python -m src.agent.orchestrator
```

ARGUS investigates the live cluster, diagnoses the incident, and pauses for approval before executing any fix:

```
=======================================================
  ARGUS proposes: restart_pod
  Args:           {'service': 'payment-service'}
  Dry-run output: kubectl rollout restart deployment/payment-service
=======================================================
  Approve? (yes/no):
```

### Inject a fault to test

```bash
# Fill unbounded cache until pod crashes (~200k entries hits the 150MB limit)
curl "http://localhost:8080/leak?entries=50000"
curl "http://localhost:8080/leak?entries=50000"
curl "http://localhost:8080/leak?entries=50000"
curl "http://localhost:8080/leak?entries=50000"

# Watch the pod crash and restart
kubectl get pods -n default -w
```

### Run against fixtures (no cluster needed)

```bash
python -m src.agent.orchestrator incident1   # memory leak
python -m src.agent.orchestrator incident2   # DB pool exhaustion
```

---

## Incident fixtures

| Incident | Service | Failure mode | Correct action |
|---|---|---|---|
| `incident1` | payment-service | Unbounded in-memory cache (v2.4.0) → OOMKill | Roll back to v2.3.1 |
| `incident2` | checkout-service | DB connection pool exhaustion | Investigate DB; tune pool size |

Each fixture: `alert.txt`, `metrics.txt`, `logs.txt`, `deploy_history.json`, `git_log.txt`.

---

## Project layout

```
src/
  agent/
    orchestrator.py         Claude tool-use loop + human approval gate
    tools.py                read tools (Prometheus, Splunk) + write tools (kubectl)
    prompts.py              SRE system prompt
k8s/
  payment-service/
    manifests.yaml          Deployment, Service, ServiceMonitor
    app.py                  Flask app — /metrics, /health, /leak endpoints
    Dockerfile
  fluent-bit-values.yaml    Fluent Bit Helm values — Splunk HEC output
  payment-service-alerts.yaml  PrometheusRule CRD — 4 alert rules
incident1/                  memory leak fixtures
incident2/                  DB pool exhaustion fixtures
incident3/                  additional fixture
docs/
  interview-prep.md         architecture deep-dive and study guide
eval.py                     eval harness
requirements.txt
.env.example
```

---

## Roadmap

| Phase | Status | Description |
|---|---|---|
| 1 | Done | Investigator agent over recorded fixtures |
| 2 | Done | Live telemetry: Prometheus metrics + alerts, Splunk logs, Fluent Bit pipeline |
| 3 | Done | Remediation: dry-run preview → human approval → kubectl execute |
| 4 | Planned | MLflow tracking — log every run, score diagnosis quality |
| 5 | Planned | Streamlit dashboard — live incident feed and reasoning trace |
