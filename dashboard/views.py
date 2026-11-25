"""Dashboard pages."""
from __future__ import annotations

import networkx as nx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard import client
from dashboard.client import ApiError

PAGES: dict = {}
SCN = {"S1": "Structuring", "S2": "Fan-in/fan-out", "S3": "Pass-through", "S4": "Layering chain",
       "S5": "Round-trip", "S6": "Shared device", "ML": "Model risk"}
STATUS_ICON = {"OPEN": "🟠", "ESCALATED": "🔴", "STR_PENDING": "🟣", "STR_FILED": "⚫",
               "CLOSED_FALSE_POSITIVE": "🟢"}


def _role() -> str:
    return st.session_state.get("role", "")


def _pkr(x) -> str:
    return f"PKR {float(x):,.0f}"


def _open_case(case_id: int) -> None:
    st.session_state["case_id"] = int(case_id)
    st.switch_page(PAGES["case"])


# ------------------------------------------------------------------ live monitor
def live_monitor() -> None:
    st.title("📡 Live monitor")
    refresh = st.toggle("Auto-refresh", value=True)

    @st.fragment(run_every=3 if refresh else None)
    def body():
        try:
            s = client.get("/monitor/stats")
            feed = client.get("/monitor/feed", limit=40)
        except ApiError as e:
            st.error(e.detail)
            return
        c = st.columns(5)
        c[0].metric("Simulated time", (s["sim_now"] or "—")[:16].replace("T", " "))
        c[1].metric("Transactions", f"{s['txns']:,}", f"{s['txns_last_60s']:,} in last 60s")
        c[2].metric("Alerts", f"{s['alerts']:,}", f"{s['alerts_last_60s']} in last 60s")
        cs = s["cases_by_status"]
        c[3].metric("Open cases", cs.get("OPEN", 0) + cs.get("ESCALATED", 0))
        c[4].metric("STRs pending / filed", f"{cs.get('STR_PENDING', 0)} / {cs.get('STR_FILED', 0)}")

        left, right = st.columns([3, 2])
        with left:
            st.subheader("Transactions per simulated hour")
            if s["txns_hourly"]:
                df = pd.DataFrame(s["txns_hourly"])
                fig = go.Figure(go.Bar(x=pd.to_datetime(df["hour"]), y=df["n"], marker_color="#4c78a8"))
                if s["alerts_hourly"]:
                    ah = pd.DataFrame(s["alerts_hourly"]).groupby("hour")["n"].sum().reset_index()
                    fig.add_trace(go.Scatter(x=pd.to_datetime(ah["hour"]), y=ah["n"], yaxis="y2",
                                             mode="lines+markers", name="alerts", line=dict(color="#e45756")))
                fig.update_layout(height=260, margin=dict(l=0, r=0, t=10, b=0), showlegend=False,
                                  yaxis=dict(title="txns"), yaxis2=dict(title="alerts", overlaying="y", side="right"))
                st.plotly_chart(fig, use_container_width=True)
        with right:
            st.subheader("Alerts by scenario")
            by = s["alerts_by_scenario"]
            if by:
                fig = go.Figure(go.Bar(y=[f"{k} {SCN[k]}" for k in by], x=list(by.values()), orientation="h",
                                       marker_color="#e45756"))
                fig.update_layout(height=260, margin=dict(l=0, r=0, t=10, b=0), yaxis=dict(autorange="reversed"))
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("No alerts yet.")

        st.subheader("Latest transactions")
        if feed:
            df = pd.DataFrame(feed)
            df["ts"] = df["ts"].str[:19].str.replace("T", " ")
            df["risk"] = df["ml_score"].astype(float)
            st.dataframe(df[["ts", "txn_type", "sender_id", "receiver_id", "amount", "city", "risk"]],
                         hide_index=True, use_container_width=True, height=420,
                         column_config={"amount": st.column_config.NumberColumn("amount (PKR)", format="%.0f"),
                                        "risk": st.column_config.ProgressColumn("ML risk", min_value=0, max_value=1,
                                                                                format="%.2f")})
        else:
            st.info("Waiting for the simulator…")
    body()


# ------------------------------------------------------------------ alert queue
def alert_queue() -> None:
    st.title("🚨 Alert queue")
    c = st.columns([1, 1, 2])
    status = c[0].selectbox("Status", ["(active)", "OPEN", "ESCALATED", "STR_PENDING", "STR_FILED",
                                       "CLOSED_FALSE_POSITIVE", "(all)"])
    scenario = c[1].selectbox("Scenario", ["(any)", *SCN])
    params = {}
    if status not in ("(active)", "(all)"):
        params["status"] = status
    if scenario != "(any)":
        params["scenario"] = scenario
    try:
        rows = client.get("/cases", **params)
    except ApiError as e:
        st.error(e.detail)
        return
    if status == "(active)":
        rows = [r for r in rows if r["status"] in ("OPEN", "ESCALATED", "STR_PENDING")]
    c[2].markdown(f"<br>**{len(rows)}** cases · sorted by priority", unsafe_allow_html=True)
    if not rows:
        st.info("No cases match.")
        return
    df = pd.DataFrame(rows)
    df["status"] = df["status"].map(lambda s: f"{STATUS_ICON.get(s, '')} {s}")
    df["scenarios"] = df["scenarios"].map(lambda xs: ", ".join(x for x in xs if x))
    df["priority"] = df["priority"].astype(float)
    df["last alert (sim)"] = df["last_alert_sim_ts"].str[:16].str.replace("T", " ")
    ev = st.dataframe(
        df[["case_id", "status", "priority", "account_id", "name", "tier", "city", "scenarios", "alerts",
            "last alert (sim)"]],
        hide_index=True, use_container_width=True, height=560, on_select="rerun", selection_mode="single-row",
        column_config={"priority": st.column_config.ProgressColumn("priority", min_value=0, max_value=1,
                                                                   format="%.2f")})
    st.caption("Select a row to open the case.")
    if ev.selection.rows:
        _open_case(df.iloc[ev.selection.rows[0]]["case_id"])


# ------------------------------------------------------------------ case detail
def _graph(case_id: int) -> None:
    g = client.get(f"/cases/{case_id}/graph")
    if not g["nodes"]:
        st.info("No flows in window.")
        return
    G = nx.DiGraph()
    for n in g["nodes"]:
        G.add_node(n["account_id"], **n)
    for e in g["edges"]:
        G.add_edge(e["source"], e["target"], **e)
    pos = nx.spring_layout(G, seed=3, k=1.2 / max(1, len(G) ** 0.5))
    ann = []
    max_total = max((e["total"] for e in g["edges"]), default=1)
    for u, v, d in G.edges(data=True):
        ann.append(dict(ax=pos[u][0], ay=pos[u][1], x=pos[v][0], y=pos[v][1], xref="x", yref="y",
                        axref="x", ayref="y", showarrow=True, arrowhead=3, arrowsize=1.2,
                        arrowwidth=1 + 3 * d["total"] / max_total, arrowcolor="rgba(120,120,120,0.55)",
                        standoff=9, startstandoff=6))
    xs, ys, txt, col, sym, size = [], [], [], [], [], []
    for n, d in G.nodes(data=True):
        xs.append(pos[n][0])
        ys.append(pos[n][1])
        txt.append(f"{n}<br>{d.get('name','')}<br>{d.get('kind')} {d.get('tier') or ''}"
                   f"{'<br>⚠ has alerts' if d.get('alerted') else ''}")
        col.append("#d62728" if d.get("subject") else ("#ff9f1c" if d.get("alerted") else
                   ("#7f7f7f" if d.get("kind") == "customer" else "#9ecae1")))
        sym.append("circle" if d.get("kind") == "customer" else "square")
        size.append(22 if d.get("subject") else 14)
    fig = go.Figure(go.Scatter(x=xs, y=ys, mode="markers+text", text=[n for n in G.nodes], textposition="top center",
                               textfont=dict(size=9), hovertext=txt, hoverinfo="text",
                               marker=dict(color=col, size=size, symbol=sym, line=dict(width=1, color="white"))))
    fig.update_layout(annotations=ann, height=480, margin=dict(l=0, r=0, t=0, b=0), showlegend=False,
                      xaxis=dict(visible=False), yaxis=dict(visible=False), plot_bgcolor="rgba(0,0,0,0)")
    st.plotly_chart(fig, use_container_width=True)
    st.caption("🔴 subject · 🟠 other alerted accounts · ⚪ customers · 🟦 agents/billers/banks · "
               "arrow width = amount moved")


def _shap_chart(shap: dict, title: str) -> None:
    items = sorted(shap.items(), key=lambda kv: kv[1]["shap"])
    fig = go.Figure(go.Bar(x=[v["shap"] for _, v in items],
                           y=[f"{k} = {v['value']:g}" for k, v in items], orientation="h",
                           marker_color=["#d62728" if v["shap"] > 0 else "#2ca02c" for _, v in items]))
    fig.update_layout(height=220, margin=dict(l=0, r=0, t=30, b=0), title=dict(text=title, font=dict(size=13)),
                      xaxis_title="SHAP (log-odds) → pushes towards suspicious")
    st.plotly_chart(fig, use_container_width=True)


def case_detail() -> None:
    case_id = st.session_state.get("case_id")
    c1, c2 = st.columns([1, 4])
    picked = c1.number_input("Case #", min_value=1, value=int(case_id or 1), step=1)
    if picked != case_id:
        st.session_state["case_id"] = case_id = int(picked)
    try:
        d = client.get(f"/cases/{case_id}")
    except ApiError as e:
        if e.status == 404:
            c2.title("🗂️ Case")
            st.info(f"There is no case #{case_id} yet. Open a case from the **Alert queue**, "
                    "or enter another case number.")
        else:
            st.error(e.detail)
        return
    case, acc, alerts = d["case"], d["account"], d["alerts"]
    c2.title(f"🗂️ Case {case_id} — {STATUS_ICON.get(case['status'], '')} {case['status']}")

    a1, a2, a3, a4 = st.columns(4)
    a1.markdown(f"**{acc['name']}**  \n`{acc['account_id']}` · {acc['kind']} · tier **{acc['tier']}**")
    a2.markdown(f"CNIC `{acc['cnic']}`  \nMSISDN `{acc['msisdn']}`")
    a3.markdown(f"City: {acc['city']}  \nDevice: `{acc['device_id']}`")
    a4.metric("Priority", f"{float(case['priority']):.2f}")
    if _role() == "mlro":
        with st.expander("🔓 Unmask PII (audited)"):
            with st.form("unmask"):
                reason = st.text_input("Reason (min 10 chars)")
                if st.form_submit_button("Reveal"):
                    try:
                        r = client.post("/pii/unmask", {"account_id": acc["account_id"], "reason": reason})
                        st.warning(f"CNIC **{r['cnic']}** · MSISDN **{r['msisdn']}** — this access was logged.")
                    except ApiError as e:
                        st.error(e.detail)
    if case.get("disposition_reason"):
        st.info(f"Disposition note ({case.get('disposed_by_name')}): {case['disposition_reason']}")

    tab_alerts, tab_graph, tab_txns, tab_str = st.tabs(["Alerts & evidence", "Money-flow graph",
                                                        "Transactions", "STR"])
    with tab_alerts:
        for a in alerts:
            ok = "✅ evidence intact" if a["integrity_ok"] else "❌ EVIDENCE HASH MISMATCH"
            with st.expander(f"**{a['scenario']} · {a['scenario_name']}** — score {float(a['score']):.2f} · "
                             f"{a['sim_ts'][:16].replace('T', ' ')} · {ok}", expanded=len(alerts) <= 3):
                ev = a["evidence"]
                st.markdown(f"**Rule:** {ev.get('rule', '')}")
                facts = {k: v for k, v in ev.items() if k not in
                         ("rule", "txns", "occurrences", "wallets", "shap_top5", "scenario", "account_id",
                          "sim_ts", "accounts", "hop_ratios")}
                st.json(facts, expanded=False)
                if a["scenario"] == "ML" and ev.get("shap_top5"):
                    _shap_chart(ev["shap_top5"], f"Why the model scored {ev['probability']:.2f}")
                for key in ("txns", "occurrences", "wallets"):
                    if ev.get(key):
                        st.dataframe(pd.DataFrame(ev[key]), hide_index=True, use_container_width=True)
                if ev.get("accounts"):
                    st.markdown("**Path:** " + " → ".join(f"`{x}`" for x in ev["accounts"]))
                st.caption(f"evidence SHA-256 `{a['evidence_hash']}`")
    with tab_graph:
        _graph(case_id)
    with tab_txns:
        tx = d["transactions"]
        if tx:
            df = pd.DataFrame(tx)
            df["ts"] = df["ts"].str[:19].str.replace("T", " ")
            df["direction"] = df.apply(lambda r: "OUT" if r["sender_id"] == acc["account_id"] else "IN", axis=1)
            df["ml_score"] = df["ml_score"].astype(float)
            st.dataframe(df[["ts", "direction", "txn_type", "sender_id", "receiver_id", "amount", "device_id",
                             "ml_score"]], hide_index=True, use_container_width=True, height=380,
                         column_config={"ml_score": st.column_config.ProgressColumn("ML risk", min_value=0,
                                                                                    max_value=1, format="%.2f")})
            top = [t for t in tx if t.get("shap") and float(t["ml_score"] or 0) >= 0.5]
            if top:
                t = max(top, key=lambda t: float(t["ml_score"]))
                _shap_chart(t["shap"], f"Highest-risk txn {t['txn_id']} (score {float(t['ml_score']):.2f})")
    with tab_str:
        for s in d["strs"]:
            st.markdown(f"STR-{s['str_id']:05d} · **{s['status']}** · drafted by `{s['drafted_by']}`"
                        + (f" · reviewed by `{s['reviewed_by']}`: {s['review_comment']}" if s["reviewed_by"] else ""))
        _actions(case)


def _actions(case: dict) -> None:
    role, status, cid = _role(), case["status"], case["case_id"]
    if role == "auditor":
        st.caption("Auditors have read-only access.")
        return
    if status == "OPEN":
        with st.form("disposition"):
            reason = st.text_area("Reason (min 20 characters) — recorded in the audit log")
            c = st.columns(2)
            close = c[0].form_submit_button("Close as false positive")
            esc = c[1].form_submit_button("Escalate", type="primary")
            if close or esc:
                try:
                    client.post(f"/cases/{cid}/{'close' if close else 'escalate'}", {"reason": reason})
                    st.rerun()
                except ApiError as e:
                    st.error(e.detail)
    elif status == "ESCALATED":
        st.subheader("Draft STR")
        key = f"str_draft_{cid}"
        if key not in st.session_state:
            st.session_state[key] = client.get(f"/cases/{cid}/str-template")["narrative"]
        with st.form("str"):
            text = st.text_area("Narrative", key=key, height=300)
            if st.form_submit_button("Submit STR for MLRO approval", type="primary"):
                try:
                    client.post(f"/cases/{cid}/str", {"narrative": text})
                    st.rerun()
                except ApiError as e:
                    st.error(e.detail)
    elif status == "STR_PENDING":
        st.info("Waiting for MLRO review (see STRs page). The drafter cannot approve their own STR.")


# ------------------------------------------------------------------ STRs
def str_page() -> None:
    st.title("📝 Suspicious Transaction Reports")
    status = st.radio("Show", ["PENDING", "FILED", "REJECTED"], horizontal=True)
    try:
        rows = client.get("/strs", status=status)
    except ApiError as e:
        st.error(e.detail)
        return
    if not rows:
        st.info(f"No {status.lower()} STRs.")
    for s in rows:
        with st.expander(f"STR-{s['str_id']:05d} · case {s['case_id']} · {s['account_id']} · "
                         f"drafted by {s['drafted_by']}", expanded=status == "PENDING"):
            st.text(s["narrative"])
            if s["reviewed_by"]:
                st.markdown(f"Reviewed by **{s['reviewed_by']}** — {s['review_comment'] or ''}")
            if st.button("Open case", key=f"open{s['str_id']}"):
                _open_case(s["case_id"])
            if _role() == "mlro" and status == "PENDING":
                with st.form(f"rev{s['str_id']}"):
                    comment = st.text_input("Comment (required to reject)")
                    c = st.columns(2)
                    ok = c[0].form_submit_button("Approve & file", type="primary")
                    no = c[1].form_submit_button("Reject")
                    if ok or no:
                        try:
                            client.post(f"/strs/{s['str_id']}/{'approve' if ok else 'reject'}", {"comment": comment})
                            st.rerun()
                        except ApiError as e:
                            st.error(e.detail)
            if _role() == "mlro" and status == "FILED":
                if st.button("Export for goAML (Markdown)", key=f"exp{s['str_id']}"):
                    md = client.get(f"/strs/{s['str_id']}/export")
                    st.download_button("Download", md, file_name=f"STR-{s['str_id']:05d}.md",
                                       key=f"dl{s['str_id']}")
                    st.markdown(md)


# ------------------------------------------------------------------ metrics
def metrics_page() -> None:
    st.title("📊 Metrics")
    try:
        m = client.get("/metrics")
    except ApiError as e:
        st.error(e.detail)
        return
    ops = m["operations"]
    c = st.columns(3)
    c[0].metric("Cases", ops["cases_total"])
    fp_rate = ops["analyst_fp_rate"]
    c[1].metric("Analyst false-positive rate", "—" if fp_rate is None else f"{fp_rate:.0%}",
                help="closed as false positive ÷ (closed FP + STR filed)")
    c[2].metric("Case → STR conversion", "—" if ops["case_to_str_conversion"] is None else
                f"{ops['case_to_str_conversion']:.0%}")

    ev = m["ground_truth_eval"]
    st.subheader("Detection quality vs ground truth")
    st.caption("Computed by the separate evaluator (read-only DB role). The API and analysts never see "
               "per-account answers — only these aggregates.")
    if not ev:
        st.info("Evaluator hasn't produced a snapshot yet.")
    else:
        o = ev["overall"]
        c = st.columns(4)
        c[0].metric("Accounts alerted", o["accounts_alerted"])
        c[1].metric("Precision (account level)", "—" if o["precision"] is None else f"{o['precision']:.0%}")
        c[2].metric("Recall (active illicit accounts)", "—" if o["recall"] is None else f"{o['recall']:.0%}")
        c[3].metric("Illicit accounts active", o["illicit_accounts_active"])
        left, right = st.columns(2)
        with left:
            st.markdown("**By scenario** (accounts alerted)")
            df = pd.DataFrame(ev["by_scenario"]).T.reset_index(names="scenario")
            df["scenario"] = df["scenario"].map(lambda s: f"{s} {SCN.get(s, '')}")
            st.dataframe(df, hide_index=True, use_container_width=True)
        with right:
            st.markdown("**Recall by typology** (rules vs rules + ML)")
            if ev["by_typology"]:
                df = pd.DataFrame(ev["by_typology"]).T.reset_index(names="typology")
                st.dataframe(df[["typology", "active", "recall_rules", "recall_rules_plus_ml"]],
                             hide_index=True, use_container_width=True)
        left, right = st.columns(2)
        with left:
            st.markdown("**Time to detect** (simulated minutes from ring's first illicit txn)")
            if ev["time_to_detect"]:
                st.dataframe(pd.DataFrame(ev["time_to_detect"]).T.reset_index(names="ring"),
                             hide_index=True, use_container_width=True)
        with right:
            st.markdown("**Benign look-alikes flagged** (honest false positives)")
            if ev["lookalikes"]:
                st.dataframe(pd.DataFrame(ev["lookalikes"]).T.reset_index(names="behaviour"),
                             hide_index=True, use_container_width=True)
    model = m.get("model")
    if model:
        st.subheader("ML model (offline, held-out world)")
        t = model["test"]
        c = st.columns(4)
        c[0].metric("PR-AUC", t["pr_auc"])
        c[1].metric("ROC-AUC", t["roc_auc"])
        c[2].metric(f"Precision @ {model['threshold']}", t["precision_at_thr"])
        c[3].metric(f"Recall @ {model['threshold']}", t["recall_at_thr"])
        st.caption(f"Model {model['version']} · trained on seeds {model['train']['seeds']}, tested on seed {t['seed']}")
        fi = model["top_features"]
        fig = go.Figure(go.Bar(x=list(fi.values()), y=list(fi.keys()), orientation="h", marker_color="#4c78a8"))
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0), yaxis=dict(autorange="reversed"),
                          xaxis_title="feature importance (gain)")
        st.plotly_chart(fig, use_container_width=True)


# ------------------------------------------------------------------ audit
def audit_page() -> None:
    st.title("🔒 Audit log")
    st.caption("Append-only, HMAC-chained. Each entry's MAC covers the previous one, so any edit or "
               "deletion breaks the chain from that point on.")
    if st.button("Verify integrity", type="primary"):
        try:
            r = client.get("/audit/verify")
            if r["ok"]:
                st.success(f"Chain intact — {r['entries_checked']:,} entries verified. Head `{r['head_hash'][:16]}…`")
            else:
                st.error(f"TAMPERING DETECTED at entry #{r['broken_at']}: {r['reason']} "
                         f"({r['entries_checked']} entries checked)")
        except ApiError as e:
            st.error(e.detail)
    c = st.columns(2)
    actor = c[0].text_input("Filter by user")
    action = c[1].selectbox("Action", ["(any)", "LOGIN_SUCCESS", "LOGIN_FAILED", "ACCESS_DENIED", "CASE_VIEW",
                                       "CASE_CLOSE_FP", "CASE_ESCALATE", "STR_DRAFT", "STR_APPROVE", "STR_REJECT",
                                       "STR_SELF_REVIEW_BLOCKED", "STR_EXPORT", "PII_UNMASK", "AUDIT_VIEW",
                                       "AUDIT_VERIFY"])
    params = {"limit": 500}
    if actor:
        params["actor"] = actor
    if action != "(any)":
        params["action"] = action
    try:
        rows = client.get("/audit", **params)
    except ApiError as e:
        st.error(e.detail)
        return
    df = pd.DataFrame(rows)
    if df.empty:
        st.info("No entries.")
        return
    df["ts"] = df["ts"].str[:19].str.replace("T", " ")
    df["details"] = df["details"].map(lambda d: ", ".join(f"{k}={v}" for k, v in d.items()) if d else "")
    df["hash"] = df["hash"].str[:12] + "…"
    df["prev_hash"] = df["prev_hash"].str[:12] + "…"
    st.dataframe(df[["entry_id", "ts", "actor", "action", "target", "details", "prev_hash", "hash"]],
                 hide_index=True, use_container_width=True, height=560)
