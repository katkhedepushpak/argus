# ARGUS — Autonomous Reasoning Gateway for Unified Systems

An AIOps agent that detects, investigates, and remediates production incidents the way an on-call SRE would, but in seconds. A watcher service polls Prometheus, opens an incident for every firing alert, and starts an investigation on its own. The agent pulls live metrics and pod logs, checks deployment history and git commits, diagnoses the root cause, and proposes a fix. **Nothing executes without explicit human approval.** After an approved fix, ARGUS verifies recovery, closes the incident, and writes a post-incident report.

Built with the Anthropic Claude API (tool-use agent loop) on Azure AI Foundry, Prometheus, Fluent Bit, Kubernetes, Streamlit, and Splunk (optional).

**Contents:** [Quick start](#quick-start) · [Architecture](#architecture) · [How it works](#how-it-works) · [Detailed activity diagrams](#detailed-activity-diagrams) · [Status reference](#incident-status-reference) · [Failure handling](#failure-handling) · [Configuration](#configuration) · [Setup](#setup) · [Project layout](#project-layout) · [Known limitations](#known-limitations) · [Roadmap](#roadmap)

---

## Quick start

```bash
scripts/start_argus.sh      # starts the watcher (detection + investigation) and the dashboard
open http://localhost:8501
scripts/rearm_demo.sh       # demo only: redeploys payment-service and leaks memory so an alert fires
```

Within about 90 seconds the alert fires, ARGUS opens an incident (`INC0001xxx`), emails you, investigates on its own, and asks for approval in the dashboard and by email.

---

## Architecture

### Telemetry and cluster

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
    │  (optional)  │               │  /api/v1/alerts │
    │  REST :8089  │               │  /api/v1/query  │
    └──────┬───────┘               └────────┬────────┘
           │                                │
           └───────────────┬────────────────┘
                           ▼
                  ARGUS agent tools (see below)
```

### ARGUS components

Two processes cooperate through three small files. The **watcher** owns detection and runs every investigation. The **dashboard** is a thin view: it reads state and records operator decisions. Closing, refreshing, or restarting the dashboard never affects a run.

```mermaid
flowchart LR
    subgraph K8S["Kubernetes cluster"]
        PS["payment-service<br/>/metrics /health /leak"]
        PROM["Prometheus<br/>scrape and rules every 15s"]
        FB["Fluent Bit DaemonSet"]
        PS -->|"scraped via ServiceMonitor"| PROM
        PS -->|"stdout logs"| FB
    end

    SPLUNK["Splunk (optional)<br/>HEC 8088, REST 8089"]
    FB -->|"HEC"| SPLUNK

    subgraph WATCHER["Watcher process: python -m src.agent.monitor"]
        DET["Detector<br/>polls every 5s"]
        DISP["Dispatcher<br/>max 3 concurrent runs"]
        RUN["Run threads<br/>Claude tool-use loop,<br/>approval wait, verification,<br/>report"]
        DET --> DISP
        DISP --> RUN
    end

    subgraph DASH["Dashboard process: Streamlit"]
        FEED["Live feed fragment<br/>refreshes every 3s"]
        MODAL["Incident modal fragment<br/>refreshes every 1s"]
        ALERTS["Browser alerts<br/>title, banner, chime, toast"]
        FEED --> ALERTS
    end

    FILES[("Shared state files<br/>.incident_registry.json + lock<br/>.incident_counter<br/>.monitor_heartbeat.json")]

    CLAUDE["Claude<br/>Azure AI Foundry"]
    KUBECTL["kubectl<br/>dry-run and execute"]
    SMTP["SMTP server<br/>emails"]
    HUMAN(["Operator"])

    DET -->|"GET /api/v1/alerts"| PROM
    RUN -->|"PromQL queries"| PROM
    RUN -->|"logs: Splunk REST,<br/>else kubectl logs"| SPLUNK
    RUN -->|"messages + tools"| CLAUDE
    RUN -->|"remediation"| KUBECTL
    DET -->|"detection email"| SMTP
    RUN -->|"approval, escalation,<br/>closure emails"| SMTP
    SMTP -.->|"inbox"| HUMAN

    DET <-->|"open and close incidents"| FILES
    RUN <-->|"progress, pending approval,<br/>decision, report"| FILES
    FEED -->|"read"| FILES
    MODAL <-->|"read; write decision<br/>and investigate request"| FILES
    DASH -->|"read heartbeat"| FILES
    HUMAN -->|"approve or reject"| MODAL
    ALERTS -.->|"attention"| HUMAN

    classDef ext fill:#f3e5f5,stroke:#6a1b9a,color:#000
    classDef file fill:#fff8e1,stroke:#f9a825,color:#000
    classDef human fill:#e3f2fd,stroke:#1565c0,color:#000
    class CLAUDE,KUBECTL,SMTP,SPLUNK ext
    class FILES file
    class HUMAN human
```

### Agent tools

| Tool | Kind | Live source | Offline fallback |
|---|---|---|---|
| `get_alert` | read | Prometheus `/api/v1/alerts` (firing, `service=payment-service`) | `alert.txt` |
| `get_metrics` | read | Prometheus `/api/v1/query` (memory, cache, restarts, 5xx rate, p99), restricted to pods that exist now | `metrics.txt` |
| `get_logs` | read | Splunk REST, else `kubectl logs --tail=50` | `logs.txt` |
| `get_deploy_history` | read | recorded fixture | `deploy_history.json` |
| `get_git_log` | read | recorded fixture | `git_log.txt` |
| `restart_pod` | **write, approval required** | `kubectl rollout restart` + `rollout status` | none |
| `rollback_deployment` | **write, approval required** | `kubectl rollout undo` + `rollout status` | none |
| `scale_deployment` | **write, approval required** | `kubectl scale` | none |

---

## How it works

1. **Detect.** The watcher polls Prometheus every 5s. A new firing alert for a monitored service becomes an incident with a sequential number and a "DETECTED" email.
2. **Dispatch.** With auto-investigate on (default), the incident is claimed and a run thread starts immediately. Operators can also request an investigation from the dashboard.
3. **Investigate.** Claude runs a tool-use loop: it chooses which read tools to call, in what order, then writes a structured diagnosis (Root Cause, Evidence, Recommended Action, Confidence) and calls a remediation tool.
4. **Approve.** The remediation call is intercepted by the approval gate. ARGUS records a dry-run, emails "APPROVAL NEEDED", and waits (default 15 minutes) for an operator decision in the dashboard. Rejected or expired requests execute nothing and escalate by email.
5. **Execute.** An approved action runs through `kubectl`.
6. **Verify.** ARGUS polls Prometheus until the alert stays clear on two consecutive checks, or gives up after 150s.
7. **Report and close.** ARGUS writes a post-incident report (timeline, outcome, follow-ups), sets the final status, and sends the closure email.

The next section shows every step, branch, and failure path.

---

## Detailed activity diagrams

Diagram index:
[Master flow](#a-master-flow) ·
[1. Detection](#1-detection-the-watcher-poll-tick) ·
[2. Dispatch](#2-dispatch-who-gets-an-investigation) ·
[3. Investigation](#3-investigation-the-agent-loop) ·
[4. Approval](#4-approval-gate) ·
[5. Execution](#5-execution) ·
[6. Verification](#6-verification-and-outcome) ·
[7. Report and closure](#7-post-incident-report-and-closure) ·
[Sequence](#b-end-to-end-sequence) ·
[Approval handshake](#c-approval-handshake-between-processes) ·
[State machine](#d-incident-state-machine) ·
[Browser alerts](#e-browser-attention-alerts)

### A. Master flow

Every incident ends in exactly one of the colored outcomes. Boxes are expanded in the numbered diagrams below.

```mermaid
flowchart TD
    START(["Alert fires in Prometheus"]) --> P1

    P1["1. DETECTION<br/>watcher opens INC number,<br/>sends DETECTED email"] --> P2
    P2["2. DISPATCH<br/>claim incident,<br/>start run thread"] --> P3
    P3["3. INVESTIGATION<br/>Claude tool-use loop reads alert,<br/>metrics, logs, deploys, commits"] --> Q1

    Q1{"Remediation<br/>proposed?"}
    Q1 -->|"no"| O_NOACT["Investigated<br/>incident still open"]
    Q1 -->|"yes"| P4

    P4["4. APPROVAL GATE<br/>APPROVAL NEEDED email,<br/>dashboard modal, alerts"] --> Q2
    Q2{"Operator<br/>decision"}
    Q2 -->|"rejected"| O_REJ["Rejected<br/>nothing changed,<br/>escalation email"]
    Q2 -->|"no response<br/>before expiry"| O_TO["Timed out<br/>nothing changed,<br/>escalation email"]
    Q2 -->|"approved"| P5

    P5["5. EXECUTION<br/>kubectl restart, rollback or scale"] --> Q3
    Q3{"kubectl<br/>succeeded?"}
    Q3 -->|"no"| O_FAIL["Remediation failed<br/>REMEDIATION FAILED email"]
    Q3 -->|"yes"| P6

    P6["6. VERIFICATION<br/>alert must stay clear on<br/>2 consecutive checks within 150s"] --> Q4
    Q4{"Recovered?"}
    Q4 -->|"yes"| O_OK["Resolved<br/>closure email"]
    Q4 -->|"no"| O_NR["Recovery not confirmed<br/>ACTION REQUIRED email"]

    O_NOACT --> P7
    O_REJ --> P7
    O_TO --> P7
    O_FAIL --> P7
    O_OK --> P7
    O_NR --> P7
    P7["7. POST-INCIDENT REPORT<br/>narrative + outcome + timeline<br/>saved to the incident"] --> END(["Operator reads report in the modal"])

    O_REJ -.->|"Re-investigate"| P2
    O_TO -.->|"Re-investigate"| P2
    O_NR -.->|"Re-investigate"| P2
    O_FAIL -.->|"Re-investigate"| P2

    classDef good fill:#e8f5e9,stroke:#2e7d32,color:#000
    classDef bad fill:#ffebee,stroke:#c62828,color:#000
    classDef warn fill:#fff3e0,stroke:#ef6c00,color:#000
    classDef phase fill:#e3f2fd,stroke:#1565c0,color:#000
    class O_OK good
    class O_FAIL,O_NR bad
    class O_REJ,O_TO,O_NOACT warn
    class P1,P2,P3,P4,P5,P6,P7 phase
```

### 1. Detection: the watcher poll tick

Runs every `ARGUS_POLL_SECS` (5s). The watcher is independent of any browser, so incidents are opened even if nobody is looking at the dashboard.

```mermaid
flowchart TD
    A(["Poll tick every 5s"]) --> B["GET Prometheus /api/v1/alerts"]
    B --> C{"Prometheus<br/>reachable?"}
    C -->|"no"| C1["Write the error into the heartbeat file<br/>Keep last known incidents<br/>Dashboard shows a warning"]
    C1 --> Z
    C -->|"yes"| D["Keep alerts whose state is firing"]
    D --> E{"service label is in<br/>ARGUS_MONITORED_SERVICES?"}
    E -->|"no"| E1["Ignore<br/>for example kind control-plane noise"]
    E -->|"yes"| F["kubectl get pods -A<br/>to list pods that exist now"]
    F --> G{"Alert is tied to a pod<br/>that no longer exists?"}
    G -->|"yes"| G1["Ignore as a ghost alert<br/>Prometheus keeps a replaced pod's last<br/>sample for up to 5 minutes"]
    G -->|"no"| H["Fingerprint = alertname + service<br/>+ namespace + severity<br/>pod and instance are excluded,<br/>so a pod restart is not a new incident"]
    H --> I{"Open incident with<br/>this fingerprint?"}
    I -->|"yes"| I1["Reuse it. No new incident"]
    I -->|"no"| J["Create incident:<br/>sequential ID INC + 7 digits<br/>status = Detected"]
    J --> K{"DETECTED email<br/>already sent?"}
    I1 --> K
    K -->|"no"| K1["Send DETECTED email once<br/>with link to the dashboard"]
    K -->|"yes"| L
    K1 --> L["Look at every non-Resolved incident<br/>whose fingerprint is no longer firing"]
    L --> M{"Status is Investigating, Awaiting approval,<br/>Action approved or Verifying recovery,<br/>or a run is active?"}
    M -->|"yes"| M1["Leave it alone<br/>a run owns this incident"]
    M -->|"no"| N["status = Resolved<br/>resolved_at = now"]
    M1 --> Z
    N --> Z
    G1 --> Z
    E1 --> Z
    Z["Detection step complete"] --> NEXT(["Dispatch: diagram 2<br/>then the heartbeat is written at the end of the tick"])

    classDef warn fill:#fff3e0,stroke:#ef6c00,color:#000
    classDef good fill:#e8f5e9,stroke:#2e7d32,color:#000
    class C1,G1,E1 warn
    class N,K1 good
```

### 2. Dispatch: who gets an investigation

Runs right after detection on every tick. A run starts for new incidents (auto mode) and for operator requests.

```mermaid
flowchart TD
    A(["After the detection step"]) --> B["For each incident, newest first"]
    B --> C{"Active runs in this<br/>process >= ARGUS_MAX_CONCURRENT (3)?"}
    C -->|"yes"| C1["Stop. Remaining incidents wait for the next tick"]
    C -->|"no"| D{"Status is Resolved,<br/>or a run is already active?"}
    D -->|"yes"| D1["Skip this incident"]
    D -->|"no"| E{"Operator clicked Investigate or Re-investigate,<br/>OR auto-investigate is on AND status is Detected<br/>AND it was never auto-started?"}
    E -->|"no"| E1["Skip this incident"]
    E -->|"yes"| F["claim_for_run under the file lock, atomically:<br/>run_active = true, clear the request flag,<br/>auto_started = true,<br/>clear progress, pending and decision"]
    F --> G{"Claim succeeded?"}
    G -->|"no"| G1["Another claimer won. Skip"]
    G -->|"yes"| H["Start the run thread<br/>add incident to the active set"]
    H --> NEXT(["Investigation: diagram 3"])
    D1 --> B
    E1 --> B
    G1 --> B

    classDef good fill:#e8f5e9,stroke:#2e7d32,color:#000
    class H good
```

Auto-mode only fires once per incident (`auto_started`), so an incident that was rejected or timed out is never silently re-run. Use **Re-investigate** for that.

### 3. Investigation: the agent loop

The model decides which tools to call and in what order. The code only executes the requested tool and returns the result.

```mermaid
flowchart TD
    A(["Run thread starts"]) --> B["status = Investigating<br/>timeline: ARGUS started its investigation"]
    B --> C["messages = one user message:<br/>A production alert just fired (incident INC...). Investigate it."]
    C --> D{"step > MAX_STEPS (10)?"}
    D -->|"yes"| D1["Stop and return the text produced so far"]
    D -->|"no"| E["Stream a Claude response<br/>claude-haiku-4-5, max 1500 tokens,<br/>SRE system prompt, 8 tools"]
    E --> F{"stop_reason"}
    F -->|"end_turn"| Z["Return the accumulated text<br/>diagnosis + closing message"]
    F -->|"tool_use"| G["Append assistant turn to messages<br/>For each tool_use block:"]
    G --> H["Event tool_start<br/>dashboard shows a running step"]
    H --> I{"Which tool?"}
    I -->|"get_alert"| I1["Prometheus alerts (live)<br/>or alert.txt (fixture)<br/>result saved for emails and report"]
    I -->|"get_metrics"| I2["5 PromQL queries, live pods only<br/>memory, cache, restarts, 5xx rate, p99<br/>first result saved as metrics-before"]
    I -->|"get_logs"| I3["Splunk REST, else kubectl logs --tail=50,<br/>else logs.txt"]
    I -->|"get_deploy_history"| I4["recorded fixture"]
    I -->|"get_git_log"| I5["recorded fixture"]
    I -->|"restart_pod, rollback_deployment<br/>or scale_deployment"| W["Approval gate: diagram 4<br/>blocks until a decision"]
    I1 --> J
    I2 --> J
    I3 --> J
    I4 --> J
    I5 --> J
    W --> J["Event tool_done<br/>step marked complete in the dashboard"]
    J --> K["Append tool_result to messages"]
    K --> D
    Z --> NEXT(["Verification and outcome: diagram 6"])
    D1 --> NEXT

    classDef ext fill:#f3e5f5,stroke:#6a1b9a,color:#000
    classDef gate fill:#fff3e0,stroke:#ef6c00,color:#000
    class E ext
    class W gate
```

The system prompt tells the model to investigate first, write the diagnosis in a fixed structure, then call the right remediation tool. The model's closing message after a rejected or expired action is not used as the report: the report is rebuilt from recorded facts (diagram 7).

### 4. Approval gate

Entered when the model calls a write tool. Nothing executes until a human approves.

```mermaid
flowchart TD
    A(["Model calls a write tool"]) --> B["Emit a diagnosis event with the text so far<br/>modal shows Why ARGUS proposes this"]
    B --> C["Build a dry-run<br/>rollback and scale: kubectl --dry-run=client<br/>restart: the command is described, not simulated"]
    C --> D{"kubectl installed?"}
    D -->|"no"| D1["Dry-run text says kubectl was not found<br/>Nothing crashes"]
    D -->|"yes"| E
    D1 --> E["Publish the pending approval to the shared file:<br/>tool, args, dry-run,<br/>deadline = now + ARGUS_APPROVAL_SECS (900s)<br/>Clear any stale decision first<br/>status = Awaiting approval"]
    E --> F["Send APPROVAL NEEDED email<br/>action, reasoning, expiry, link"]
    F --> G["Dashboard cues fire:<br/>toast, tab title count, OS banner, chime"]
    G --> H["Wait loop: check the shared file every 0.5s"]
    H --> I{"Decision recorded?"}
    I -->|"approve"| Y["decision = yes"]
    I -->|"reject"| N["decision = no"]
    I -->|"nothing yet"| J{"Past the halfway point<br/>and no reminder sent?"}
    J -->|"yes"| J1["Send REMINDER email once"]
    J -->|"no"| K
    J1 --> K{"Deadline reached?"}
    K -->|"no"| H
    K -->|"yes"| L["Close the window first (pending = none)<br/>then take any decision that raced the expiry"]
    L --> M{"Decision found?"}
    M -->|"yes"| I
    M -->|"no"| T["decision = timeout"]

    Y --> Y1["status = Action approved<br/>timeline: operator approved"]
    Y1 --> EXEC(["Execution: diagram 5"])

    N --> N1["status = Rejected - no action taken<br/>timeline: operator rejected"]
    T --> T1["status = Timed out - no action taken<br/>timeline: request expired"]
    N1 --> ESC["Send escalation email:<br/>action REJECTED or TIMED OUT, with diagnosis,<br/>metrics snapshot and dry-run"]
    T1 --> ESC
    ESC --> R["Return tool_result: Action NOT executed,<br/>incident still open"]
    R --> BACK(["Back to the agent loop:<br/>Claude writes a closing message, loop ends"])

    classDef good fill:#e8f5e9,stroke:#2e7d32,color:#000
    classDef warn fill:#fff3e0,stroke:#ef6c00,color:#000
    classDef human fill:#e3f2fd,stroke:#1565c0,color:#000
    class Y,Y1 good
    class N,N1,T,T1,ESC warn
    class I human
```

### 5. Execution

Only reachable after an explicit approval.

```mermaid
flowchart TD
    A(["Decision = yes"]) --> B{"Which tool?"}
    B -->|"restart_pod"| C["kubectl rollout restart<br/>deployment/service"]
    B -->|"rollback_deployment"| D["kubectl rollout undo<br/>deployment/service"]
    B -->|"scale_deployment"| E["kubectl scale deployment/service<br/>--replicas=N"]
    C --> F{"Return code<br/>non-zero?"}
    D --> F
    F -->|"yes"| F1["Result = ERROR: stderr text"]
    F -->|"no"| G["kubectl rollout status --timeout=60s"]
    G --> G1["Result = command output"]
    E --> H{"Return code<br/>non-zero?"}
    H -->|"yes"| F1
    H -->|"no"| H1["Result = command output"]
    F1 --> I
    G1 --> I
    H1 --> I{"Result starts<br/>with ERROR?"}
    I -->|"yes"| J["remediation = failed<br/>status = Remediation failed<br/>timeline: the action failed"]
    I -->|"no"| K{"PROMETHEUS_URL set,<br/>so recovery can be checked?"}
    K -->|"yes"| L["remediation = ok<br/>status = Verifying recovery<br/>timeline: action completed"]
    K -->|"no"| M["remediation = ok<br/>status = Remediation executed"]
    J --> N
    L --> N
    M --> N["Return the result to Claude<br/>Claude writes a closing message<br/>the agent loop ends"]
    N --> NEXT(["Verification and outcome: diagram 6"])

    classDef good fill:#e8f5e9,stroke:#2e7d32,color:#000
    classDef bad fill:#ffebee,stroke:#c62828,color:#000
    class L,M good
    class J,F1 bad
```

### 6. Verification and outcome

After the agent loop ends, ARGUS decides the outcome. For an approved, successful fix it verifies recovery before closing anything.

```mermaid
flowchart TD
    A(["Agent loop finished"]) --> B{"What happened<br/>at the gate?"}
    B -->|"fix ran OK, Prometheus available"| V0
    B -->|"fix ran OK, no Prometheus"| O5["Outcome: executed but unverified<br/>status stays Remediation executed"]
    B -->|"fix failed"| O4["Outcome: remediation failed"]
    B -->|"operator rejected"| O3["Outcome: rejected"]
    B -->|"request expired"| O2["Outcome: timed out"]
    B -->|"no write tool was proposed"| O1["Outcome: no action<br/>status becomes Investigated"]

    V0["Verification loop starts<br/>deadline = now + ARGUS_VERIFY_SECS (150s)<br/>clear_polls = 0"] --> V1["Sleep ARGUS_VERIFY_INTERVAL (10s)"]
    V1 --> V2["Fetch firing alerts<br/>same ghost-pod filter as detection"]
    V2 --> V3{"Request failed?"}
    V3 -->|"yes"| V6
    V3 -->|"no"| V4{"This incident's fingerprint<br/>still firing?"}
    V4 -->|"yes"| V4a["clear_polls = 0"]
    V4 -->|"no"| V4b["clear_polls = clear_polls + 1"]
    V4a --> V6
    V4b --> V5{"clear_polls >= 2?<br/>two consecutive clear checks"}
    V5 -->|"yes"| REC["recovered = true"]
    V5 -->|"no"| V6{"Deadline reached?"}
    V6 -->|"no"| V1
    V6 -->|"yes"| NREC["recovered = false"]

    REC --> M["Read metrics again, restricted to pods that exist now<br/>so the replaced pod's stale numbers are never shown"]
    NREC --> M
    M --> R1{"recovered?"}
    R1 -->|"yes"| OK["Outcome: resolved<br/>timeline: alert cleared and stayed clear"]
    R1 -->|"no"| NR["Outcome: not recovered<br/>timeline: still firing after 150s"]

    OK --> NEXT(["Post-incident report: diagram 7"])
    NR --> NEXT
    O1 --> NEXT
    O2 --> NEXT
    O3 --> NEXT
    O4 --> NEXT
    O5 --> NEXT

    classDef good fill:#e8f5e9,stroke:#2e7d32,color:#000
    classDef bad fill:#ffebee,stroke:#c62828,color:#000
    classDef warn fill:#fff3e0,stroke:#ef6c00,color:#000
    class OK good
    class NR,O4 bad
    class O1,O2,O3,O5 warn
```

A flaky Prometheus request is retried and does not count as either clear or firing, so a transient error can never close an incident.

### 7. Post-incident report and closure

The report is assembled from recorded facts, not from the model's chat. The model writes only the narrative; the Outcome and Timeline are generated in code so their tense and content are correct by construction.

```mermaid
flowchart TD
    A(["Outcome known"]) --> B["Collect facts: alert details, metrics during the incident,<br/>metrics after the action, diagnosis, proposed action,<br/>operator decision, action result, timeline events"]
    B --> C["Claude writes the narrative sections:<br/>Summary, Root Cause, Evidence, Action Taken, Follow-ups<br/>Strict rules: past tense for events, present tense for<br/>current state, imperative follow-ups, no invented facts"]
    C --> D{"LLM call succeeded and<br/>output contains Summary?"}
    D -->|"no"| D1["Use a minimal deterministic<br/>narrative so the run never fails"]
    D -->|"yes"| E
    D1 --> E["Insert the Outcome section generated in code<br/>(present tense) plus the latest measurements"]
    E --> F["Append the Timeline built from recorded events<br/>with UTC timestamps"]
    F --> G["Add header: generated time<br/>and current status Open or Resolved"]
    G --> H["One atomic update to the incident:<br/>report + final status<br/>Resolved with resolved_at,<br/>Recovery not confirmed,<br/>or Investigated"]
    H --> I{"Outcome?"}
    I -->|"resolved"| J1["Send closure email<br/>green banner, duration, action,<br/>root cause, metrics before and after"]
    I -->|"not recovered"| J2["Send ACTION REQUIRED email<br/>alert still firing"]
    I -->|"remediation failed"| J3["Send REMEDIATION FAILED email"]
    I -->|"rejected, timed out, or no action"| J4["No extra email<br/>escalation already went out at the gate"]
    J1 --> K
    J2 --> K
    J3 --> K
    J4 --> K["finally: run_active = false<br/>release the concurrency slot<br/>clear pending and decision"]
    K --> L(["Dashboard modal shows the report<br/>and the status of every email sent"])

    classDef good fill:#e8f5e9,stroke:#2e7d32,color:#000
    classDef bad fill:#ffebee,stroke:#c62828,color:#000
    classDef ext fill:#f3e5f5,stroke:#6a1b9a,color:#000
    class J1 good
    class J2,J3 bad
    class C ext
```

### B. End-to-end sequence

One incident from alert to closure, showing which component does what. The alternatives show the rejected and expired paths.

```mermaid
sequenceDiagram
    autonumber
    participant PR as Prometheus
    participant W as Watcher
    participant F as Shared files
    participant R as Run thread
    participant C as Claude
    participant K as kubectl and cluster
    participant D as Dashboard
    participant H as Operator
    participant M as SMTP email

    PR->>W: alert state becomes firing
    W->>PR: GET alerts every 5s
    W->>F: open incident INC, status Detected
    W->>M: DETECTED email
    W->>F: claim incident, run_active true
    W->>R: start run thread
    R->>F: status Investigating
    loop agent loop, at most 10 steps
        R->>C: messages and tool definitions
        C-->>R: tool_use request
        R->>PR: get_alert, get_metrics
        R->>K: get_logs via kubectl logs
        R->>F: publish progress step
        R->>C: tool_result
    end
    C-->>R: diagnosis, then write tool call
    R->>F: publish diagnosis and pending approval
    R->>M: APPROVAL NEEDED email
    D->>F: read pending, show cues and modal
    H->>D: opens modal, reads reasoning
    alt operator approves
        H->>D: Approve and execute
        D->>F: set_decision yes, atomic one-shot
        R->>F: poll every 0.5s, receives yes
        R->>K: kubectl rollout undo or restart or scale
        K-->>R: result
        R->>F: status Verifying recovery
        loop every 10s up to 150s
            R->>PR: fetch firing alerts
        end
        R->>C: write report narrative
        R->>F: save report, status Resolved
        R->>M: closure email
    else operator rejects
        H->>D: Reject
        D->>F: set_decision no
        R->>M: escalation email, rejected
        R->>C: write report narrative
        R->>F: save report, status Rejected, run ends
    else no response before expiry
        R->>M: REMINDER email at the halfway point
        R->>F: window expires, decision timeout
        R->>M: escalation email, timed out
        R->>C: write report narrative
        R->>F: save report, status Timed out, run ends
    end
    W->>F: alert cleared, resolve any idle incident
```

### C. Approval handshake between processes

The watcher and the dashboard are separate processes. They coordinate through one file protected by an OS file lock, so every step below is atomic and a stale or duplicate click can never approve the wrong action.

```mermaid
sequenceDiagram
    autonumber
    participant R as Run thread in watcher
    participant F as Registry file with lock
    participant D as Dashboard modal
    participant H as Operator

    R->>F: clear stale decision, write pending with deadline
    D->>F: read every second
    F-->>D: pending approval
    D-->>H: show action, reasoning, countdown
    H->>D: click Approve
    D->>F: set_decision under lock
    Note over F: accepted only if pending exists,<br/>no decision yet, and not expired
    F-->>D: true
    H->>D: double-click Approve
    D->>F: set_decision under lock
    F-->>D: false, a decision is already recorded
    R->>F: poll every 0.5s, sees decision
    R->>F: clear decision and pending
    R->>R: execute the action
    H->>D: late click after the run moved on
    D->>F: set_decision under lock
    F-->>D: false, nothing is pending
    Note over R,F: At expiry the run closes the window first, then<br/>takes any decision that arrived at the last moment,<br/>so a click that races the deadline is honored once
```

### D. Incident state machine

The status shown in the dashboard. Statuses with a run attached are protected from auto-resolution by the watcher.

```mermaid
stateDiagram-v2
    [*] --> Detected: new alert fingerprint fires
    Detected --> Investigating: auto-start or operator request
    Detected --> Resolved: alert clears before any run

    Investigating --> AwaitingApproval: write tool proposed
    Investigating --> Investigated: finished, no action proposed
    Investigating --> InvestigationFailed: error, or watcher restarted mid-run

    AwaitingApproval --> ActionApproved: operator approves
    AwaitingApproval --> Rejected: operator rejects
    AwaitingApproval --> TimedOut: window expires

    ActionApproved --> VerifyingRecovery: kubectl succeeded
    ActionApproved --> RemediationFailed: kubectl error
    ActionApproved --> RemediationExecuted: succeeded, no Prometheus to verify

    VerifyingRecovery --> Resolved: alert stays clear
    VerifyingRecovery --> RecoveryNotConfirmed: still firing after 150s

    Investigated --> Investigating: Re-investigate
    Rejected --> Investigating: Re-investigate
    TimedOut --> Investigating: Re-investigate
    InvestigationFailed --> Investigating: Re-investigate
    RemediationFailed --> Investigating: Re-investigate
    RecoveryNotConfirmed --> Investigating: Re-investigate

    Investigated --> Resolved: alert clears
    Rejected --> Resolved: alert clears
    TimedOut --> Resolved: alert clears
    InvestigationFailed --> Resolved: alert clears
    RemediationFailed --> Resolved: alert clears
    RecoveryNotConfirmed --> Resolved: alert clears
    RemediationExecuted --> Resolved: alert clears

    Resolved --> [*]
    note right of Resolved
        If the same alert fires again later,
        a new incident with a new INC number
        is opened. Resolved incidents are never reopened.
    end note
```

### E. Browser attention alerts

How the dashboard gets a student's attention, without annoying them. Runs inside the live feed, which refreshes every 3 seconds.

```mermaid
flowchart TD
    A(["Live feed refreshes (3s)"]) --> B["Build payload:<br/>open incidents and pending approvals<br/>approval key = incident + deadline"]
    B --> C["Server side: compare with this session's<br/>previously seen items"]
    C --> C1["st.toast for each new incident<br/>or new approval"]
    B --> D["Hand the payload to a small script<br/>in the browser"]
    D --> E["Set the tab title:<br/>(N) Approval needed, or (N) Incident open,<br/>or plain ARGUS when idle"]
    D --> F{"First payload since<br/>this page loaded?"}
    F -->|"yes"| F1["Remember what is already on screen<br/>No alert, so reopening the page is quiet"]
    F -->|"no"| G{"New approval key<br/>not seen before?"}
    G -->|"yes"| G1["Urgent chime: 880, 880, 1100 Hz<br/>plus a sticky OS notification"]
    G -->|"no"| H{"New incident ID<br/>not seen before?"}
    H -->|"yes"| H1["Soft single tone 660 Hz<br/>plus an OS notification"]
    H -->|"no"| I{"Approval still pending<br/>and 2 minutes since last chime?"}
    I -->|"yes"| I1["Repeat the urgent chime"]
    I -->|"no"| J["Nothing to do"]
    G1 --> K
    H1 --> K
    I1 --> K
    K{"Sound muted<br/>or audio locked?"}
    K -->|"muted"| K1["Silence the tones<br/>banners and title still fire"]
    K -->|"unlocked"| K2["Tones play"]

    S(["Enable alerts button, first time"]) --> S1["Browser asks for notification permission<br/>and unlocks audio"]
    S1 --> S2["Preference stored locally<br/>Any later click on the page re-unlocks sound<br/>after a reload"]

    O(["Operator has an incident modal open"]) --> O1["Modal shows a warning when a different<br/>incident needs approval:<br/>close this window to review it"]

    classDef good fill:#e8f5e9,stroke:#2e7d32,color:#000
    classDef warn fill:#fff3e0,stroke:#ef6c00,color:#000
    class G1,H1,I1 warn
    class F1,K1,K2 good
```

---

## Incident status reference

| Status | Meaning | Who set it | Terminal? |
|---|---|---|---|
| Detected | Alert is firing, no run yet | watcher | no |
| Investigating | Run thread is active | run thread | no |
| Awaiting approval | A fix is proposed and waiting for a decision | run thread | no |
| Action approved | Operator approved, command executing | run thread | no |
| Verifying recovery | Fix applied, confirming the alert stays clear | run thread | no |
| Resolved | Alert cleared and stayed clear (or cleared on its own) | run thread or watcher | **yes** |
| Remediation executed | Fix applied but recovery could not be checked (no Prometheus) | run thread | no |
| Investigated | Finished without proposing an action | run thread | no |
| Rejected - no action taken | Operator rejected the fix | run thread | no |
| Timed out - no action taken | No decision before the window expired | run thread | no |
| Remediation failed | The approved kubectl command failed | run thread | no |
| Recovery not confirmed | Fix applied but the alert was still firing after 150s | run thread | no |
| Investigation failed | Error in the run, or the watcher restarted mid-run | run thread or watcher | no |

Every non-terminal status other than the four in-flight ones (Investigating, Awaiting approval, Action approved, Verifying recovery) can be re-investigated, and moves to Resolved on its own if the alert stops firing.

### Emails sent

| Email | When | Contents |
|---|---|---|
| DETECTED | Once, when the incident is opened | alert, service, severity, firing since, link |
| APPROVAL NEEDED | When a fix needs a decision | action in plain English, root cause, expiry time, link |
| REMINDER | Once, at the halfway point of the wait | same as above |
| Escalation (REJECTED or TIMED OUT) | At the gate, when nothing executes | diagnosis, metrics snapshot, dry-run |
| RESOLVED | After verified recovery | summary, duration, action, root cause, metrics before and after |
| ACTION REQUIRED | Fix ran but the alert is still firing | same layout, red banner |
| REMEDIATION FAILED | The approved command failed | same layout, red banner |

---

## Failure handling

| What goes wrong | What ARGUS does |
|---|---|
| Prometheus unreachable | Watcher records the error in its heartbeat and keeps last known incidents. Dashboard shows a warning. Nothing is closed. |
| Watcher is down | Dashboard shows a red banner when the heartbeat is older than 15s. No new alerts are detected or investigated. |
| Watcher restarts mid-run | At startup, in-flight incidents become "Investigation failed" with an explanatory report. Re-investigate to retry. |
| Dashboard closed, refreshed, or restarted | No effect. The dashboard owns no run state. |
| Claude API error during a run | Incident becomes "Investigation failed" with the error text. |
| Report model call fails | A minimal deterministic report is used. The run still completes. |
| SMTP unavailable | The email step records `email failed: reason` and carries on. It never raises. |
| `kubectl` missing or failing | The dry-run text says so. A failing approved command becomes "Remediation failed" and sends an email. |
| Operator never responds | Reminder at the halfway point, then the request expires. Nothing executes. Escalation email goes out. |
| Double click, stale click, click after expiry | Refused. Decisions are one-shot and expiry-checked under a file lock. |
| Replaced pod keeps firing an alert | Alerts tied to pods that no longer exist are ignored, so they can neither hold an incident open nor create a duplicate. |
| Verification request fails | Retried, and counted as neither clear nor firing. |
| More than 3 incidents at once | Three run concurrently. The rest wait for a free slot. |

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_FOUNDRY_BASE_URL`, `ANTHROPIC_FOUNDRY_API_KEY` | none | Claude via Azure AI Foundry |
| `PROMETHEUS_URL` | none | Enables live mode. Without it, tools read recorded fixtures. |
| `SPLUNK_URL`, `SPLUNK_USER`, `SPLUNK_PASSWORD` | none | Live log source. If unset, see `KUBECTL_LOGS_TARGET`. |
| `KUBECTL_LOGS_TARGET` | none | e.g. `deployment/payment-service`. Reads logs with `kubectl logs` when Splunk is not used. |
| `ARGUS_MONITORED_SERVICES` | `payment-service` | Comma-separated services whose alerts become incidents |
| `ARGUS_POLL_SECS` | `5` | Watcher poll interval |
| `ARGUS_AUTO_INVESTIGATE` | `1` | `0` disables auto-start, so an operator clicks Investigate |
| `ARGUS_APPROVAL_SECS` | `900` | How long an approval request waits before it expires |
| `ARGUS_VERIFY_SECS` | `150` | Verification window after an approved fix |
| `ARGUS_VERIFY_INTERVAL` | `10` | Seconds between verification checks |
| `ARGUS_MAX_CONCURRENT` | `3` | Parallel investigations |
| `ARGUS_DASHBOARD_URL` | `http://localhost:8501` | Link used in emails |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | none | Email transport (STARTTLS on 587, SSL on 465) |
| `ALERT_EMAIL_TO`, `ALERT_EMAIL_FROM` | none | Recipient and sender |
| `SMTP_STARTTLS` | `1` | `0` for plain local test servers |

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
Without Splunk, `get_logs()` falls back to `kubectl logs --tail=50` when `KUBECTL_LOGS_TARGET` is set.

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
  → the watcher reads /api/v1/alerts and opens an incident per firing alert
```

---

## Setup

### Prerequisites
- Docker (Docker Desktop, or Colima on macOS)
- `kind`, `kubectl`, `helm` installed
- Python 3.9+
- Azure AI Foundry access (or swap for direct Anthropic API)

### 1. Clone and install

```bash
git clone https://github.com/katkhedepushpak/argus.git
cd argus
python -m venv .venv
source .venv/bin/activate      # macOS/Linux
# .venv\Scripts\activate       # Windows
pip install -r requirements.txt
cp .env.example .env           # fill in credentials
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

To make `rollback_deployment` meaningful, deploy an earlier revision first (for example, apply the manifest once with a `2.3.1` tag, then apply `2.4.0`), so the Deployment has a previous revision to return to.

### 5. Install Fluent Bit

```bash
helm repo add fluent https://fluent.github.io/helm-charts
helm install fluent-bit fluent/fluent-bit \
  -n logging --create-namespace \
  -f k8s/fluent-bit-values.yaml
```

### 6. Log source: Splunk or kubectl logs

```bash
docker run -d --name splunk \
  --memory=1500m --memory-swap=1500m \
  -p 8000:8000 -p 8088:8088 -p 8089:8089 \
  -e SPLUNK_START_ARGS=--accept-license \
  -e SPLUNK_PASSWORD=<your-password> \
  -e SPLUNK_HEC_TOKEN=<your-hec-token> \
  splunk/splunk:latest
```

The Splunk image is x86-only and does not start reliably under emulation on Apple Silicon. In that case skip Splunk and set `KUBECTL_LOGS_TARGET=deployment/payment-service` instead.

### 7. Expose services locally

```bash
kubectl port-forward svc/payment-service 8080:8080 -n default &
kubectl port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 -n monitoring &
```

A `kubectl port-forward` to a Service is bound to one pod. After a rollout, restart it (`scripts/rearm_demo.sh` does this for the payment-service).

---

## Run ARGUS

```bash
scripts/start_argus.sh
```

This starts the **watcher** (`python -m src.agent.monitor`) and the **dashboard** (`streamlit run dashboard.py`) together and sets live-mode defaults (`PROMETHEUS_URL`, `KUBECTL_LOGS_TARGET`). Open http://localhost:8501. If the watcher is not running, the dashboard says so with a red banner.

### Inject a fault to test

```bash
scripts/rearm_demo.sh
```

It redeploys payment-service, restarts its port-forward, and leaks about 230,000 cache entries (about 113 MiB of the 150 MiB limit). `PaymentServiceHighMemory` fires within about 90 seconds. Or do it by hand:

```bash
curl "http://localhost:8080/leak?entries=230000"
```

### Run once from the terminal (no dashboard)

```bash
python argus.py incident1       # recorded fixture
```

The CLI uses a plain `yes/no` prompt at the approval gate. It is a single investigation, without the watcher, emails, verification, or post-incident report.

### Run the eval harness

```bash
python eval.py                  # scores each fixture against ground truth (keywords + LLM judge)
```

---

## Incident fixtures

| Incident | Service | Failure mode | Correct action |
|---|---|---|---|
| `incident1` | payment-service | Unbounded in-memory cache (v2.4.0) → OOMKill | Roll back to v2.3.1 |
| `incident2` | checkout-service | DB connection pool exhaustion | Investigate DB; tune pool size |
| `incident3` | additional scenario | see `ground_truth.json` | see `ground_truth.json` |

Each fixture: `alert.txt`, `metrics.txt`, `logs.txt`, `deploy_history.json`, `git_log.txt`, `ground_truth.json`. In live mode, deploy history and git log still come from `incident1` (see Known limitations).

---

## Project layout

```
src/agent/
  monitor.py        watcher: poll Prometheus, open/close incidents, detection email, dispatch, recover on restart
  runs.py           run threads: agent run, approval wait, verification, post-incident report, closure email
  orchestrator.py   Claude tool-use loop and the approval gate
  tools.py          read tools (Prometheus, Splunk, kubectl logs, fixtures) and write tools (kubectl)
  incidents.py      shared incident registry: file lock, atomic writes, INC numbers, decision and claim operations
  prom.py           firing-alert fetch, stable fingerprint, ghost-pod filter
  notify.py         SMTP sender and all email templates (HTML + text)
  prompts.py        SRE system prompt
dashboard.py        Streamlit UI: live feed, incident modal, approval buttons, browser alerts, demo tools
argus.py            single-run CLI entry point
eval.py             eval harness (keyword score + LLM judge)
scripts/
  start_argus.sh    start watcher and dashboard together
  rearm_demo.sh     redeploy payment-service and leak memory to fire the alert
k8s/
  payment-service/  manifests.yaml, app.py (FastAPI: /metrics /health /leak /charge), Dockerfile
  fluent-bit-values.yaml          Fluent Bit Helm values (Splunk HEC output)
  payment-service-alerts.yaml     PrometheusRule CRD (4 alert rules)
incident1/ incident2/ incident3/  recorded fixtures
docs/interview-prep.md            architecture deep-dive and study guide
tests/                            eval tests
```

Runtime state (gitignored): `.incident_registry.json` and its `.lock`, `.incident_counter`, `.monitor_heartbeat.json`.

---

## Known limitations

- **Deploy history and git log are recorded fixtures**, even in live mode, so a report can quote a deploy date that does not match the live cluster. The planned fix is to build them from `kubectl rollout history` and ReplicaSet timestamps plus the GitHub API.
- **The model's choice of remediation varies** between runs for the same incident (rollback, restart, or scale). The approval gate is the control; per-alert runbooks or an allowlist would constrain it further.
- **"Recovered" means the alert stayed clear**, not that every metric looks healthy.
- **Single cluster, single watcher process.** Runs live in the watcher's memory; a watcher restart marks in-flight runs as failed rather than resuming them.
- **Email is the only out-of-band channel.** Production would use PagerDuty or Slack with acknowledgement tracking and escalation tiers.
- **Browser alerts need a one-time click** to grant permission and unlock audio, and only reach someone with the browser open.
- **Incident numbers come from a local counter file**, safe for one host. A shared deployment should use the ticketing system's IDs or a database sequence.

---

## Roadmap

| Phase | Status | Description |
|---|---|---|
| 1 | Done | Investigator agent over recorded fixtures |
| 2 | Done | Live telemetry: Prometheus metrics and alerts, Splunk or kubectl logs, Fluent Bit pipeline |
| 3 | Done | Remediation: dry-run preview, human approval, kubectl execute |
| 4 | Done | Streamlit dashboard: live incident feed, per-incident modal, approval gate |
| 5 | Done | Always-on watcher: detection, incident numbering, auto-investigate, restart recovery |
| 6 | Done | Notifications: detection, approval, reminder, escalation, closure emails; browser alerts |
| 7 | Done | Post-remediation verification, closure, and post-incident reports |
| 8 | Planned | Real deploy history and git log (Kubernetes ReplicaSets, GitHub API) |
| 9 | Planned | MLflow tracking: log every run, score diagnosis quality over time |
| 10 | Planned | PagerDuty or Slack integration with acknowledgement and escalation tiers |
