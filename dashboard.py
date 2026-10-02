"""
ARGUS dashboard — a live view of incidents, plus the approval gate.

The watcher process (src.agent.monitor) detects incidents, starts investigations and runs them. This page only
reads state from the shared incident file and records operator decisions, so it can be opened, closed or
refreshed at any time without affecting a run. Nothing here blocks the Streamlit script thread.
"""
import datetime
import json
import os
import time

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

load_dotenv()

from src.agent.incidents import get_incident, list_incidents, request_investigation, set_decision
from src.agent.monitor import read_heartbeat
from src.agent.notify import describe_action
from src.agent.prom import monitored_services
from src.agent.runs import RUNS, is_active, start_run, submit_decision
from src.agent.scenarios import list_scenarios

SCENARIOS = list_scenarios()
RESULTS_FILE = "eval_results.jsonl"
POLL_SECS = int(os.getenv("ARGUS_POLL_SECS", "3"))
PROM_URL = os.getenv("PROMETHEUS_URL")
MONITORED = monitored_services()

INVESTIGABLE = {"Detected", "Investigated", "Rejected - no action taken", "Timed out - no action taken",
                "Investigation failed", "Remediation failed", "Remediation executed", "Recovery not confirmed"}
SEVERITY_ICON = {"critical": "🔴", "warning": "🟠"}
UTC = datetime.timezone.utc


def firing_for(active_at):
    try:
        since = datetime.datetime.strptime(active_at, "%Y-%m-%d %H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return active_at
    secs = max(0, int((datetime.datetime.now(UTC) - since).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h {secs % 3600 // 60}m"
    return f"{secs // 86400}d {secs % 86400 // 3600}h"


def render_replay_result(run):
    result = run["result"]
    if result["error"]:
        st.error(f"Replay failed: {result['error']}")
        return
    col_report, col_eval = st.columns([3, 1])
    with col_report:
        st.markdown(f"**Report — {run['scenario']}**")
        st.markdown(result["report"] or "")
    with col_eval:
        ev = result["eval"]
        st.markdown("**Eval**")
        if not ev:
            st.caption("Scoring...")
        elif ev.get("error"):
            st.warning(f"Eval unavailable: {ev['error']}")
        else:
            st.metric("Keywords", f"{len(ev['hits'])}/{len(ev['gt']['key_terms'])}")
            if ev["misses"]:
                st.caption(f"Missed: {', '.join(ev['misses'])}")
            (st.success if ev["verdict"].startswith("PASS") else st.error)(ev["verdict"])


ATTENTION_JS = """
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:13px;display:flex;gap:10px;align-items:center">
  <button id="btn" style="cursor:pointer;border:1px solid #cfd8dc;background:#fff;border-radius:6px;padding:5px 12px;font-size:13px"></button>
  <span id="note" style="color:#78909c"></span>
</div>
<script>
(function () {
  const P = window.parent;
  const data = __PAYLOAD__;
  const A = P.__argus = P.__argus || {seenInc: null, seenApp: null, ctx: null, lastChime: 0, hooked: false};
  const LS = P.localStorage;
  const btn = document.getElementById('btn'), note = document.getElementById('note');

  function unlockAudio() {
    try {
      if (!A.ctx) A.ctx = new (P.AudioContext || P.webkitAudioContext)();
      if (A.ctx.state === 'suspended') A.ctx.resume();
    } catch (e) {}
  }
  const muted = () => LS.getItem('argus_mute') === '1';
  function beep(freq, start, dur) {
    const c = A.ctx;
    if (!c || c.state !== 'running' || muted()) return;
    const o = c.createOscillator(), g = c.createGain();
    o.type = 'sine'; o.frequency.value = freq; g.gain.value = 0.15;
    o.connect(g); g.connect(c.destination);
    o.start(c.currentTime + start); o.stop(c.currentTime + start + dur);
  }
  function chime(urgent) {
    if (urgent) { beep(880, 0, 0.18); beep(880, 0.28, 0.18); beep(1100, 0.56, 0.3); } else { beep(660, 0, 0.25); }
  }
  function notify(title, body, tag, sticky) {
    if (!P.Notification || P.Notification.permission !== 'granted') return;
    try {
      const n = new P.Notification(title, {body: body, tag: tag, requireInteraction: sticky});
      n.onclick = function () { P.focus(); n.close(); };
    } catch (e) {}
  }

  // Browsers only allow sound after a click on the page: once the user has opted in, any click unlocks it again.
  if (LS.getItem('argus_alerts') === '1' && !A.hooked) {
    A.hooked = true;
    P.document.addEventListener('click', unlockAudio, true);
  }

  function render() {
    const perm = P.Notification ? P.Notification.permission : 'unsupported';
    const on = perm === 'granted' && A.ctx && A.ctx.state === 'running';
    if (!on) {
      btn.textContent = '🔔 Enable alerts';
      note.textContent = perm === 'denied' ? 'Notifications are blocked in your browser settings.'
        : 'Get a banner and sound when an incident or approval needs you.';
    } else if (muted()) {
      btn.textContent = '🔇 Sound muted'; note.textContent = 'Banners are still on. Click to unmute.';
    } else {
      btn.textContent = '🔊 Alerts on'; note.textContent = 'Click to mute the sound.';
    }
  }
  btn.onclick = async function () {
    const perm = P.Notification ? P.Notification.permission : 'unsupported';
    const on = perm === 'granted' && A.ctx && A.ctx.state === 'running';
    if (!on) {
      unlockAudio();
      if (P.Notification && perm === 'default') { await P.Notification.requestPermission(); }
      LS.setItem('argus_alerts', '1'); LS.setItem('argus_mute', '0');
    } else {
      LS.setItem('argus_mute', muted() ? '0' : '1');
    }
    render();
  };
  render();
  setInterval(render, 1000);

  const incIds = data.incidents.map(function (i) { return i.id; });
  const appKeys = data.approvals.map(function (a) { return a.key; });
  if (A.seenInc === null) {          // first payload after a page load: what is already on screen is not "new"
    A.seenInc = new Set(incIds); A.seenApp = new Set(appKeys);
  } else {
    const newApps = data.approvals.filter(function (a) { return !A.seenApp.has(a.key); });
    const newIncs = data.incidents.filter(function (i) { return !A.seenInc.has(i.id); });
    newApps.forEach(function (a) { A.seenApp.add(a.key); });
    newIncs.forEach(function (i) { A.seenInc.add(i.id); });
    if (newApps.length) {
      chime(true); A.lastChime = Date.now();
      newApps.forEach(function (a) { notify('ARGUS: approval needed', a.id + ' - ' + a.action, 'app-' + a.key, true); });
    } else if (newIncs.length) {
      chime(false);
      newIncs.forEach(function (i) { notify('ARGUS: new incident ' + i.id, i.alertname + ' (' + i.severity + ')', 'inc-' + i.id, false); });
    }
  }
  if (data.approvals.length && Date.now() - A.lastChime > 120000) { chime(true); A.lastChime = Date.now(); }

  P.document.title = data.approvals.length ? '(' + data.approvals.length + ') Approval needed - ARGUS'
    : (data.open ? '(' + data.open + ') Incident open - ARGUS' : 'ARGUS');
})();
</script>
"""


def attention_alerts(open_incidents):
    """Browser-side cues: tab-title count, OS banner, chime. Fires only for items that appear after the page loads."""
    approvals = [{"id": i["id"], "key": f"{i['id']}:{int(i['pending']['deadline'])}",
                  "action": describe_action(i["pending"]["tool_name"], i["pending"]["args"]).replace("`", "")}
                 for i in open_incidents if i["status"] == "Awaiting approval" and i.get("pending")]
    incidents = [{"id": i["id"], "alertname": i["alertname"], "severity": i["severity"]} for i in open_incidents]
    payload = json.dumps({"approvals": approvals, "incidents": incidents, "open": len(open_incidents)}).replace("</", "<\\/")
    components.html(ATTENTION_JS.replace("__PAYLOAD__", payload), height=44)

    seen = st.session_state.get("toast_seen")
    current = {"inc": {i["id"] for i in open_incidents}, "app": {a["key"] for a in approvals}}
    if seen is not None:
        for inc in open_incidents:
            if inc["id"] not in seen["inc"]:
                st.toast(f"New incident {inc['id']}: {inc['alertname']}", icon="🚨")
        for a in approvals:
            if a["key"] not in seen["app"]:
                st.toast(f"Approval needed for {a['id']}: {a['action']}", icon="⚠️")
    st.session_state["toast_seen"] = current


def _on_dismiss():
    st.session_state.modal = None


@st.fragment(run_every=1)
def modal_body(modal):
    live = modal["kind"] == "live"
    if live:
        inc = get_incident(modal["id"])
        if inc is None:
            st.error("Incident not found.")
            return
        st.markdown(f"### {inc['id']} · {SEVERITY_ICON.get(inc['severity'], '⚪')} {inc['severity']}")
        st.markdown(f"**{inc['alertname']}** on `{inc['service']}` — {inc['summary']}")
        st.caption(f"Firing for {firing_for(inc['active_at'])} (since {inc['active_at']})")
        st.markdown(f"**Status:** {inc['status']}")
        active = bool(inc.get("run_active"))
        queued = bool(inc.get("investigate_requested")) and not active
        progress = inc.get("progress") or []
        pending = inc.get("pending") if active and not inc.get("decision") else None
        diagnosis = inc.get("diagnosis") or ""
        decide = lambda decision: set_decision(inc["id"], decision)  # noqa: E731
        for label, field in (("Detected", "detection_email"), ("Approval request", "approval_email"),
                             ("Closure", "notification")):
            if inc.get(field):
                st.caption(f"📧 {label}: {inc[field]}")
    else:
        st.markdown(f"### Replay — {modal['dir']}")
        inc, run = None, RUNS.get("replay")
        active, queued = is_active("replay"), False
        progress = run["progress"] if run else []
        pending = run["pending"] if run and active else None
        diagnosis = run["diagnosis"] if run else ""
        decide = lambda decision: submit_decision("replay", decision)  # noqa: E731

    others = [i for i in list_incidents()
              if i["status"] == "Awaiting approval" and not (live and i["id"] == inc["id"])]
    if others:
        st.warning(f"⚠️ {', '.join(o['id'] for o in others)} needs approval. Close this window to review it.")

    if queued:
        st.info("Investigation requested — the watcher will start it within a few seconds.")
    for step in progress:
        st.markdown(f"{'✅' if step['state'] == 'done' else '⏳'} {step['label']}")
        if step.get("detail"):
            st.caption(step["detail"])
    if active and not progress:
        st.markdown("⏳ Starting investigation...")

    if pending:
        left = max(0, int(pending["deadline"] - time.time()))
        st.divider()
        st.warning("**Approval required** — ARGUS will not act until you approve.")
        st.markdown(f"**Proposed action:** {describe_action(pending['tool_name'], pending['args'])}")
        if diagnosis:
            start = diagnosis.find("## Root Cause")
            with st.expander("Why ARGUS proposes this", expanded=True):
                st.markdown(diagnosis[start:] if start >= 0 else diagnosis)
        with st.expander("Technical details (kubectl dry-run)"):
            st.code(f"{pending['tool_name']} {pending['args']}\n\n{pending['dry_output']}", language="bash")
        st.progress(min(1.0, left / pending.get("timeout_secs", 60)),
                    text=f"Auto-rejects in {left // 60}m {left % 60:02d}s")
        col_approve, col_reject = st.columns(2)
        key = modal.get("id", "replay")
        if col_approve.button("✅ Approve & execute", type="primary", use_container_width=True, key=f"approve_{key}"):
            if not decide("yes"):
                st.toast("That approval request has expired.")
            st.rerun(scope="fragment")
        if col_reject.button("❌ Reject", use_container_width=True, key=f"reject_{key}"):
            decide("no")
            st.rerun(scope="fragment")
    elif active:
        st.caption("You can close this window — the investigation keeps running in the background.")

    if not active:
        if live:
            if inc.get("report"):
                st.divider()
                st.markdown(inc["report"])
            if inc["status"] in INVESTIGABLE and not queued:
                label = "Investigate" if inc["status"] == "Detected" else "Re-investigate"
                if st.button(label, key=f"reinv_{inc['id']}"):
                    request_investigation(inc["id"])
                    st.rerun(scope="fragment")
        elif run and run["result"]["done"]:
            st.divider()
            render_replay_result(run)


@st.dialog("ARGUS investigation", width="large", on_dismiss=_on_dismiss)
def incident_modal(modal):
    modal_body(modal)


def render_incident(inc):
    with st.container(border=True):
        c1, c2, c3, c4, c5 = st.columns([2, 4, 2, 3, 2])
        c1.markdown(f"**{inc['id']}**  \n{SEVERITY_ICON.get(inc['severity'], '⚪')} {inc['severity']}")
        c2.markdown(f"**{inc['alertname']}**  \n`{inc['service']}` — {inc['summary']}")
        if inc["status"] == "Resolved":
            c3.markdown(f"Resolved  \n{inc.get('resolved_at', '')}")
        else:
            c3.markdown(f"Firing for  \n{firing_for(inc['active_at'])}")
        c4.markdown(f"Status  \n**{inc['status']}**")
        modal = {"kind": "live", "id": inc["id"]}
        idle_detected = (inc["status"] == "Detected" and not inc.get("run_active")
                         and not inc.get("investigate_requested") and not inc.get("auto_started"))
        if idle_detected:
            if c5.button("Investigate", key=f"inv_{inc['id']}", type="primary"):
                request_investigation(inc["id"])
                st.session_state.modal = modal
                st.rerun()
        else:
            needs_approval = inc["status"] == "Awaiting approval"
            label = "Review & approve" if needs_approval else "Open incident"
            if c5.button(label, key=f"open_{inc['id']}", type="primary" if needs_approval else "secondary"):
                st.session_state.modal = modal
                st.rerun()


@st.fragment(run_every=POLL_SECS)
def live_feed():
    if not PROM_URL:
        st.info("Live feed is off. Set PROMETHEUS_URL to watch for firing alerts, or use the demo tools below.")
        return
    beat = read_heartbeat()
    age = None if beat is None else time.time() - beat["ts"]
    watcher_down = age is None or age > max(15, POLL_SECS * 4)
    error = None if watcher_down or beat is None else beat["error"]
    incidents = list_incidents()
    open_incidents = [i for i in incidents if i["status"] != "Resolved"]
    resolved = [i for i in incidents if i["status"] == "Resolved"]

    attention_alerts(open_incidents)
    m1, m2, m3 = st.columns(3)
    m1.metric("Open incidents", len(open_incidents))
    m2.metric("Awaiting approval", sum(i["status"] == "Awaiting approval" for i in open_incidents))
    m3.metric("Resolved", len(resolved))
    st.caption(f"Watching: {', '.join(sorted(MONITORED))} · updated {datetime.datetime.now().strftime('%H:%M:%S')} "
               "· deploy history and git log use recorded fixtures")
    if watcher_down:
        st.error("The ARGUS watcher is not running, so new alerts will not be detected or investigated. "
                 "Start it with `python -m src.agent.monitor` (or `scripts/start_argus.sh`).")
    elif error:
        st.warning(f"The watcher cannot reach Prometheus ({error}). Showing last known incidents.")
    if not open_incidents:
        st.success("No open incidents. All monitored services are healthy.")
    for inc in open_incidents:
        render_incident(inc)
    if resolved:
        with st.expander(f"Resolved ({len(resolved)})"):
            for inc in resolved[:20]:
                render_incident(inc)


st.set_page_config(page_title="ARGUS", layout="wide")
st.session_state.setdefault("modal", None)

st.title("ARGUS — Autonomous Incident Investigator")
st.subheader("Live Incidents")
live_feed()

with st.expander("Demo tools — replay recorded incidents and eval history"):
    replay_choice = st.selectbox("Recorded scenario", SCENARIOS, disabled=is_active("replay"))
    if st.button("Run replay", disabled=is_active("replay")):
        start_run("replay", "replay", replay_choice)
        st.session_state.modal = {"kind": "replay", "dir": replay_choice}
    replay_run = RUNS.get("replay")
    if replay_run and replay_run["result"]["done"]:
        render_replay_result(replay_run)
    st.markdown("**Eval history**")
    rows = []
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
    if rows:
        st.dataframe(rows, use_container_width=True)
    else:
        st.caption("No history yet — run `python eval.py` to record eval runs.")

if st.session_state.modal:
    incident_modal(st.session_state.modal)
