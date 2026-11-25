"""WalletWatch dashboard. Talks only to the API — never to the database."""
import streamlit as st

from dashboard import client, views

st.set_page_config(page_title="WalletWatch", page_icon="🛡️", layout="wide")


def login_page() -> None:
    st.title("🛡️ WalletWatch")
    st.caption("AML transaction monitoring · synthetic mobile-wallet data")
    with st.form("login"):
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in", type="primary"):
            try:
                res = client.login(u, p)
                st.session_state.update(token=res["access_token"], username=res["username"], role=res["role"])
                st.rerun()
            except client.ApiError as e:
                st.error(e.detail)


if "token" not in st.session_state:
    st.navigation([st.Page(login_page, title="Sign in")]).run()
    st.stop()

role = st.session_state["role"]
pages = {
    "Monitoring": [st.Page(views.live_monitor, title="Live monitor", icon="📡", url_path="live", default=True),
                   st.Page(views.alert_queue, title="Alert queue", icon="🚨", url_path="queue"),
                   st.Page(views.case_detail, title="Case", icon="🗂️", url_path="case")],
    "Compliance": [st.Page(views.str_page, title="STRs", icon="📝", url_path="strs"),
                   st.Page(views.metrics_page, title="Metrics", icon="📊", url_path="metrics")],
}
if role == "auditor":
    pages["Compliance"].append(st.Page(views.audit_page, title="Audit log", icon="🔒", url_path="audit"))
views.PAGES.update({p.url_path: p for group in pages.values() for p in group})

with st.sidebar:
    st.markdown(f"**{st.session_state['username']}** · `{role}`")
    if st.button("Sign out"):
        st.session_state.clear()
        st.rerun()

st.navigation(pages).run()
