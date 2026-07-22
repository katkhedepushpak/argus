"""
ARGUS dashboard — run incident investigations and review eval history.
"""
import json
import os

import streamlit as st
from dotenv import load_dotenv
from anthropic import AnthropicFoundry

load_dotenv()
client = AnthropicFoundry(
    base_url=os.getenv("ANTHROPIC_FOUNDRY_BASE_URL"),
    api_key=os.getenv("ANTHROPIC_FOUNDRY_API_KEY"),
)

from src.agent.orchestrator import main

INCIDENTS = ["incident1", "incident2"]
RESULTS_FILE = "eval_results.jsonl"


def score(report, ground_truth):
    report_lower = report.lower()
    hits = [t for t in ground_truth["key_terms"] if t.lower() in report_lower]
    misses = [t for t in ground_truth["key_terms"] if t.lower() not in report_lower]
    return hits, misses


def llm_judge(report, ground_truth):
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        messages=[{"role": "user", "content": f"""You are an SRE evaluating an incident report.
Ground truth root cause: {ground_truth['root_cause']}
Recommended action: {ground_truth['recommended_action']}
Report:
{report}
Did the report correctly identify the root cause and recommend the right action?
Reply with PASS or FAIL and one sentence explaining why."""}],
    )
    return response.content[0].text


st.set_page_config(page_title="ARGUS", layout="wide")
st.title("ARGUS — Autonomous Incident Investigator")
st.caption("Select an incident, run the investigation, and review the scored report.")

incident = st.selectbox("Incident", INCIDENTS)

if st.button("Run Investigation", type="primary"):
    with st.spinner("Investigating..."):
        report = main(incident, silent=True)

    with open(f"{incident}/ground_truth.json", encoding="utf-8") as f:
        gt = json.load(f)

    hits, misses = score(report, gt)

    with st.spinner("Judge evaluating..."):
        verdict = llm_judge(report, gt)

    col_report, col_eval = st.columns([3, 1])

    with col_report:
        st.subheader("Report")
        st.markdown(report)

    with col_eval:
        st.subheader("Eval")
        st.metric("Keywords", f"{len(hits)}/{len(gt['key_terms'])}")
        if misses:
            st.caption(f"Missed: {', '.join(misses)}")
        if verdict.startswith("PASS"):
            st.success(verdict)
        else:
            st.error(verdict)

st.divider()
st.subheader("Eval History")
if os.path.exists(RESULTS_FILE):
    rows = []
    with open(RESULTS_FILE, encoding="utf-8") as f:
        for line in f:
            rows.append(json.loads(line))
    if rows:
        st.dataframe(rows, use_container_width=True)
    else:
        st.info("No history yet — run an investigation above.")
else:
    st.info("No history yet — run an investigation above.")
