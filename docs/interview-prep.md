# ARGUS — Interview Prep & Study Guide

## The system in one sentence
ARGUS is an AIOps agent that autonomously detects incidents in a Kubernetes-hosted microservice, gathers telemetry from Prometheus and Splunk, and diagnoses root cause using an LLM with structured tool use.

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
│  │ │payment-service│ │      │ │ Prometheus  │ │  Grafana  │  │   │
│  │ │   :8080       │◄├──────┤ │ (scrapes    │ │(dashboards│  │   │
│  │ │  /metrics     │ │      │ │  /metrics)  │ │  :3000)   │  │   │
│  │ │  /health      │ │      │ └─────┬──────┘ └───────────┘  │   │
│  │ │  /leak        │ │      │       │ evaluates alert rules  │   │
│  │ └──────┬────────┘ │      │       ▼                        │   │
│  │        │ stdout   │      │ ┌────────────────────────────┐ │   │
│  │        │ logs     │      │ │   PrometheusRule CRD        │ │   │
│  └────────┼──────────┘      │ │ - PaymentServiceHighMemory  │ │   │
│           │                 │ │ - PaymentServicePodRestarted│ │   │
│  ┌────────▼──────────┐      │ │ - PaymentServiceHighErrors  │ │   │
│  │    Fluent Bit     │      │ │ - PaymentServiceHighLatency │ │   │
│  │   (DaemonSet)     │      │ └────────────────────────────┘ │   │
│  │ tails             │      └────────────────────────────────┘   │
│  │ /var/log/         │                                            │
│  │ containers/*.log  │                                            │
│  └────────┬──────────┘                                           │
└───────────┼──────────────────────────────────────────────────────┘
            │ HTTPS / HEC                    │ HTTP
            ▼                               ▼
    ┌──────────────┐               ┌─────────────────┐
    │    Splunk    │               │   Prometheus    │
    │  (Docker)    │               │  (in-cluster)   │
    │  port 8089   │               │   port 9090     │
    │  (REST API)  │               │  /api/v1/alerts │
    │  port 8088   │               │  /api/v1/query  │
    │  (HEC ingest)│               └────────┬────────┘
    └──────┬───────┘                        │
           │                               │
           └──────────────┬────────────────┘
                          │
                 ┌────────▼────────┐
                 │     ARGUS       │
                 │  Python agent   │
                 │                 │
                 │  get_alert()    │  ← queries Prometheus /api/v1/alerts
                 │  get_metrics()  │  ← queries Prometheus /api/v1/query
                 │  get_logs()     │  ← queries Splunk REST API
                 │  get_deploy_    │  ← reads fixture file
                 │    history()    │
                 │  get_git_log()  │  ← reads fixture file
                 │                 │
                 │  Claude LLM     │  ← decides which tools to call
                 │  (tool use)     │     synthesizes diagnosis
                 └─────────────────┘
```

---

## Data flow per telemetry source

### Logs path
```
payment-service prints to stdout
        ↓
Kubernetes writes to /var/log/containers/payment-service*.log on the node
        ↓
Fluent Bit DaemonSet tails that file (tail input plugin)
        ↓
Fluent Bit ships each line to Splunk HEC at port 8088 over HTTPS
        ↓
Splunk indexes the event under index=main, sourcetype=httpevent
        ↓
get_logs() POSTs a search query to Splunk REST API at port 8089
        ↓
ARGUS reads the last 50 log lines
```

### Metrics path
```
payment-service exposes /metrics in Prometheus format
        ↓
ServiceMonitor tells Prometheus Operator: "scrape payment-service port http-metrics"
        ↓
Prometheus scrapes /metrics every 15 seconds, stores time-series
        ↓
get_metrics() calls /api/v1/query with 5 PromQL expressions
        ↓
ARGUS reads: memory_mb, cache_entries, restarts, error_rate, p99_latency
```

### Alerts path
```
Prometheus evaluates PrometheusRule expressions every 15 seconds
        ↓
If expression is true for long enough (for: duration), alert state → firing
        ↓
get_alert() calls /api/v1/alerts, filters by service=payment-service label
        ↓
ARGUS reads which alerts are currently firing and their descriptions
```

---

## Study topics by technology

### Kubernetes

**Pod lifecycle**
A pod goes through: `Pending` (being scheduled) → `Running` (container started) → `CrashLoopBackOff` (keeps crashing, Kubernetes keeps restarting with backoff delay). In ARGUS, payment-service went into a crash after hitting the memory limit — visible via `kubectl describe pod`.

**Deployments vs DaemonSets**
- Deployment: runs N replicas of a pod, scheduled on any available node. Used for payment-service — we want exactly 1 replica.
- DaemonSet: runs exactly 1 pod on every node. Used for Fluent Bit — because log files live on each node, you need an agent on every node to read them. If you used a Deployment instead, Fluent Bit might land on the wrong node and miss logs.

**Resource requests vs limits**
- Request: what Kubernetes reserves for the pod on the node. Scheduler uses this.
- Limit: the hard ceiling. If a pod exceeds memory limit, the kernel sends OOMKill. In ARGUS, payment-service has `memory limit: 150Mi`. When the leak filled the cache past that, the pod crashed.

**ServiceMonitor (Prometheus Operator CRD)**
Instead of editing Prometheus config files, you create a `ServiceMonitor` object. Prometheus Operator watches for them and automatically configures Prometheus to scrape the target. Key requirement: the ServiceMonitor must reference the Service port by **name**, not number. We added `name: http-metrics` to the Service spec for this reason.

**PrometheusRule (Prometheus Operator CRD)**
Same pattern as ServiceMonitor — declare alert rules as Kubernetes objects instead of editing config. Prometheus Operator loads them automatically if they have the matching label (`release: prometheus` in our case). We confirmed the selector with `kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}'`.

**Helm**
Helm is the package manager for Kubernetes. A chart is a bundle of templated manifests. We used:
- `kube-prometheus-stack` chart → installed Prometheus, Grafana, Prometheus Operator, and all default alert rules in one command
- `fluent-bit` chart → installed the Fluent Bit DaemonSet; we overrode `values.yaml` to configure our Splunk output

---

### Prometheus

**Pull model**
Prometheus is pull-based — it reaches out to each target and scrapes `/metrics`. This is the opposite of push-based systems (like StatsD) where the app sends metrics to the collector. Pull makes it easier to detect when a service is down (scrape just fails), but requires targets to be reachable from Prometheus.

**PromQL**
The query language for Prometheus. Key functions used in ARGUS:
- `rate(metric[5m])` — per-second rate of a counter over the last 5 minutes. Used for error rate: `rate(payment_requests_total{status="500"}[5m])`
- `histogram_quantile(0.99, rate(bucket[5m]))` — calculates p99 latency from histogram buckets
- `{label="value"}` — filter a metric by label. Used to scope restarts to payment-service: `kube_pod_container_status_restarts_total{container="payment-service"}`

**Alert states**
- `inactive` — expression evaluates to false, no problem
- `pending` — expression is true but hasn't been true for the full `for:` duration yet
- `firing` — expression has been true long enough, alert is active

The `for:` duration prevents flapping — a spike that lasts 1 second won't fire an alert with `for: 1m`. We set `for: 0m` on the restart alert so it fires immediately.

**Two key API endpoints**
- `/api/v1/query?query=<promql>` — instant query, returns current value. Used in `get_metrics()`
- `/api/v1/alerts` — returns all alerts currently in pending or firing state. Used in `get_alert()`

---

### Splunk

**HEC (HTTP Event Collector)**
A push-based HTTP endpoint for ingesting events into Splunk. Clients POST JSON payloads with an auth token. Port 8088 in our setup. Fluent Bit uses this to ship logs.

**Index vs sourcetype**
- Index: the storage partition where data lives (`index=main` in our case)
- Sourcetype: tells Splunk how to parse and display the data (`sourcetype=httpevent`)

**Two ports**
- `8088` — HEC, for ingesting data (Fluent Bit → Splunk)
- `8089` — REST API, for querying data (ARGUS → Splunk)

**SPL search used in ARGUS**
```
search index=main | sort -_time | head 50
```
- `index=main` — look in the main index
- `sort -_time` — newest events first
- `head 50` — return only the last 50 lines

**Why Splunk and not just Prometheus for logs?**
Prometheus only stores numeric time-series. It can't store raw log text. Splunk (or alternatives like Loki, Elasticsearch) is purpose-built for storing and searching unstructured text logs.

---

### Fluent Bit

**DaemonSet pattern**
Kubernetes writes container stdout/stderr to files on the host node at `/var/log/containers/`. Fluent Bit runs as a DaemonSet (one pod per node) so it always has access to the log files on its local node. A regular Deployment wouldn't guarantee co-location.

**Input → Filter → Output pipeline**
Every Fluent Bit config has three stages:
- Input: where to read from (we use `tail` — reads a file like `tail -f`)
- Filter: transform or enrich the data (we removed the Kubernetes filter because it added metadata fields that Splunk's free license rejects as "indexed fields")
- Output: where to send it (we use the `splunk` plugin — sends to HEC)

**Key config decisions in ARGUS**
- `event_key $log` — tells Fluent Bit to use the `log` field (the raw log line) as the Splunk event body. The `$` prefix is Fluent Bit's Record Accessor syntax for referencing a field.
- `TLS On, TLS.Verify Off` — Splunk HEC requires HTTPS. We turn off cert verification because we're using a self-signed cert in dev.
- Removed Kubernetes filter — it was adding pod metadata that Splunk free license rejected with error code 15.

---

### Observability

**Three pillars**
- **Metrics**: numeric, time-series data. Good for dashboards, alerting, and spotting trends. Example: memory_bytes over time.
- **Logs**: timestamped text events. Good for understanding what exactly happened at a specific moment. Example: "MemoryError: unable to allocate array"
- **Traces**: records the journey of a single request across multiple services. We haven't built this in ARGUS yet.

**SLI / SLO / SLA**
- SLI (Service Level Indicator): the actual metric being measured. Example: p99 latency.
- SLO (Service Level Objective): the internal target. Example: p99 latency < 200ms for 99.9% of requests.
- SLA (Service Level Agreement): the external contract with consequences if broken.

**MTTD / MTTR**
- MTTD (Mean Time To Detect): how long from incident start until someone knows about it. ARGUS reduces this by alerting automatically.
- MTTR (Mean Time To Resolve): how long from detection to fix. ARGUS reduces this by diagnosing root cause instantly instead of requiring manual log triage.

---

### AIOps / LLM Agent

**Tool use**
Instead of asking Claude "what's wrong with payment-service?", we give it tools it can call to gather data. Claude decides which tools to call, in what order, based on what it learns from each result. This is more reliable than free-form prompting because:
1. Data is always fresh (not baked into the prompt)
2. Claude can adaptively gather only what it needs
3. Each tool call is auditable

**Agentic loop in ARGUS**
```
1. Send initial prompt + tool definitions to Claude
2. Claude responds with a tool_use block (e.g. "call get_alert")
3. We execute get_alert(), get the result
4. We send the result back to Claude
5. Claude calls the next tool (e.g. get_metrics)
6. Repeat until Claude responds with text instead of a tool call
7. That final text is the diagnosis
```

**Why human-in-the-loop for remediation (Phase 3)**
ARGUS can diagnose, but we don't let it execute fixes autonomously because:
- Fixes can be destructive (restarting a pod mid-transaction, rolling back a working deployment)
- The LLM could misdiagnose and apply the wrong fix
- In production, every remediation action should be auditable and approved

---

## TODO
- Add mock interview Q&A per technology
- Add Phase 3 (remediation) implementation notes once built
- Add Phase 4 (MLflow tracking) notes
- Add Phase 5 (Streamlit dashboard) notes
