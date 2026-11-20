"""frontend/app.py — Streamlit dashboard for the Edge-ML Fleet Orchestrator.

Role-based login: SystemAdmin | DataScientist

Tabs:
  1. Fleet Overview        (both roles) — node status badges, alerts
  2. Telemetry & Charts    (both roles) — Plotly CPU / RAM / latency
  3. Model Registry        (Data Scientist) — upload model, list models
  4. OTA Deployment        (Data Scientist) — trigger deployment, corruption toggle
  5. Node Management       (System Admin) — register/update/revoke nodes
  6. Green Sustainability  (both roles) — baseline vs green benchmark

Run:
    streamlit run frontend/app.py
"""

import os
import sys

# Ensure project root is on the path so `backend` and `core` import cleanly
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import requests
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd

from core.green_telemetry import benchmark as green_benchmark

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("ORCHESTRATOR_BASE_URL", "http://localhost:8000")

st.set_page_config(
    page_title="Edge-ML Fleet Orchestrator",
    page_icon="🛰️",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Session state helpers
# ---------------------------------------------------------------------------

def _init_state():
    if "token" not in st.session_state:
        st.session_state.token = None
    if "role" not in st.session_state:
        st.session_state.role = None
    if "name" not in st.session_state:
        st.session_state.name = None
    if "user_id" not in st.session_state:
        st.session_state.user_id = None

_init_state()


def api_get(path: str, params=None):
    headers = {}
    if st.session_state.token:
        headers["Authorization"] = f"Bearer {st.session_state.token}"
    try:
        resp = requests.get(f"{BASE_URL}{path}", params=params, headers=headers, timeout=10)
        if resp.status_code == 401:
            st.session_state.token = None
            st.error("Session expired. Please log in again.")
            st.stop()
        return resp
    except requests.exceptions.RequestException as exc:
        st.error(f"Backend unreachable at {BASE_URL}: {exc}")
        st.stop()


def api_post(path: str, params=None, json_body=None):
    headers = {}
    if st.session_state.token:
        headers["Authorization"] = f"Bearer {st.session_state.token}"
    try:
        return requests.post(
            f"{BASE_URL}{path}", params=params, json=json_body, headers=headers, timeout=10
        )
    except requests.exceptions.RequestException as exc:
        st.error(f"Backend unreachable at {BASE_URL}: {exc}")
        st.stop()


def api_delete(path: str):
    headers = {"Authorization": f"Bearer {st.session_state.token}"} if st.session_state.token else {}
    try:
        return requests.delete(f"{BASE_URL}{path}", headers=headers, timeout=10)
    except requests.exceptions.RequestException as exc:
        st.error(f"Backend unreachable at {BASE_URL}: {exc}")
        st.stop()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login_page():
    st.title("🔐 Edge-ML Fleet Orchestrator — Login")
    st.markdown("Sign in with your role-based credentials.")

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        with st.form("login_form"):
            email = st.text_input("Email", placeholder="admin@example.com")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login", use_container_width=True)
            if submitted:
                resp = api_post("/auth/login", params={"email": email, "password": password})
                if resp.status_code == 200:
                    data = resp.json()
                    st.session_state.token = data["access_token"]
                    st.session_state.role = data["role"]
                    st.session_state.name = data["name"]
                    st.session_state.user_id = data["user_id"]
                    st.success(f"Welcome, {data['name']} ({data['role']})")
                    st.rerun()
                else:
                    st.error("Invalid email or password")

        st.markdown("---")
        st.caption("Default credentials:\n"
                   "- Admin: `admin@example.com` / `admin123`\n"
                   "- Data Scientist: `scientist@example.com` / `scientist123`")
        if st.button("Logout"):
            st.session_state.token = None
            st.session_state.role = None
            st.session_state.name = None
            st.session_state.user_id = None
            st.rerun()


def require_login():
    if not st.session_state.token:
        login_page()
        st.stop()


# ---------------------------------------------------------------------------
# UI components
# ---------------------------------------------------------------------------

STATUS_COLORS = {
    "Idle_Ready": "🟢",
    "Executing_Model": "🔵",
    "Downloading_Update": "🟡",
    "Fallback_Recovery": "🟠",
    "Offline": "🔴",
    "Booting": "⚪",
    "Syncing_Telemetry": "⚪",
}


def status_badge(status: str) -> str:
    return f"{STATUS_COLORS.get(status, '⚪')} {status}"


def nav_sidebar():
    st.sidebar.title("🛰️ Fleet Orchestrator")
    st.sidebar.write(f"**User:** {st.session_state.name}")
    st.sidebar.write(f"**Role:** {st.session_state.role}")
    st.sidebar.divider()

    common = ["Fleet Overview", "Telemetry & Charts", "Green Sustainability"]
    ds = common + ["Model Registry", "OTA Deployment"]
    admin = common + ["Node Management"]

    if st.session_state.role == "SystemAdmin":
        options = admin
    elif st.session_state.role == "DataScientist":
        options = ds
    else:
        options = common

    choice = st.sidebar.radio("Navigate", options, key="nav")
    st.sidebar.divider()
    if st.sidebar.button("Logout", use_container_width=True):
        api_post("/auth/logout")
        st.session_state.token = None
        st.session_state.role = None
        st.session_state.name = None
        st.session_state.user_id = None
        st.rerun()
    return choice


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def page_fleet_overview():
    st.header("📊 Fleet Overview")
    resp = api_get("/nodes")
    nodes = resp.json() if resp.status_code == 200 else []

    if not nodes:
        st.info("No edge nodes registered.")
        return

    # KPI cards
    total = len(nodes)
    online = sum(1 for n in nodes if n["status"] != "Offline")
    offline = sum(1 for n in nodes if n["status"] == "Offline")
    executing = sum(1 for n in nodes if n["status"] == "Executing_Model")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Nodes", total)
    c2.metric("Online", online, delta=None)
    c3.metric("Offline", offline, delta=("-" + str(offline)) if offline else None)
    c4.metric("Executing Model", executing)

    st.divider()
    st.subheader("Node Status")
    rows = []
    for n in nodes:
        rows.append(
            {
                "Node": n["node_id"],
                "MAC": n["mac_address"],
                "Location": n["location_name"],
                "Status": status_badge(n["status"]),
                "RAM Limit (MB)": n["hardware_limit_ram_mb"],
                "Missed Pings": n["missed_pings"],
                "Current Model": n["current_model_id"] or "—",
            }
        )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # Alerts
    st.divider()
    st.subheader("🚨 Alerts")
    alerts_resp = api_get("/alerts")
    alerts = alerts_resp.json() if alerts_resp.status_code == 200 else []
    if not alerts:
        st.success("No active alerts.")
    for a in alerts:
        icon = "🔴" if a["severity"] == "critical" else "🟡"
        st.warning(f"{icon} [{a['severity'].upper()}] {a['message']}")


def page_telemetry_charts():
    st.header("📈 Telemetry & Charts")
    resp = api_get("/telemetry", params={"limit": 200})
    logs = resp.json() if resp.status_code == 200 else []

    if not logs:
        st.info("No telemetry data yet. Start the edge simulator to generate heartbeats.")
        return

    df = pd.DataFrame(logs)
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    node_ids = sorted(df["node_id"].unique())
    selected = st.multiselect("Filter by node", node_ids, default=node_ids[:3])
    if selected:
        df = df[df["node_id"].isin(selected)]

    # CPU chart
    fig_cpu = px.line(
        df.sort_values("timestamp"),
        x="timestamp", y="cpu_usage_percent", color="node_id",
        title="CPU Usage % over Time",
    )
    st.plotly_chart(fig_cpu, use_container_width=True)

    # RAM chart
    fig_ram = px.line(
        df.sort_values("timestamp"),
        x="timestamp", y="ram_usage_mb", color="node_id",
        title="RAM Usage (MB) over Time",
    )
    st.plotly_chart(fig_ram, use_container_width=True)

    # Latency chart
    fig_lat = px.line(
        df.sort_values("timestamp"),
        x="timestamp", y="inference_latency_ms", color="node_id",
        title="Inference Latency (ms) over Time",
    )
    st.plotly_chart(fig_lat, use_container_width=True)

    st.divider()
    st.subheader("Recent Telemetry Log")
    st.dataframe(df.sort_values("timestamp", ascending=False), use_container_width=True, hide_index=True)


def page_model_registry():
    st.header("📦 Model Registry (Data Scientist)")
    st.subheader("Upload a New Model Version")

    with st.form("upload_model"):
        version_tag = st.text_input("Version Tag", placeholder="e.g. yolov8n-v2.1")
        file_size_mb = st.number_input("File Size (MB)", min_value=0.01, value=10.5, step=0.1)
        required_ram_mb = st.number_input("Required RAM (MB)", min_value=1, value=512, step=1)
        file_payload = st.text_area(
            "Dummy Binary Payload (hex/base64 string)",
            placeholder="deadbeef...",
            height=100,
            help="This is hashed (SHA-256) and stored as the model's file_hash.",
        )
        submitted = st.form_submit_button("Upload Model", use_container_width=True)
        if submitted:
            if not version_tag or not file_payload:
                st.error("Version tag and payload are required.")
            else:
                resp = api_post(
                    "/models",
                    params={
                        "version_tag": version_tag,
                        "file_size_mb": file_size_mb,
                        "required_ram_mb": required_ram_mb,
                        "file_payload": file_payload,
                    },
                )
                if resp.status_code == 200:
                    st.success(f"Model uploaded (hash: {resp.json()['file_hash'][:16]}...)")
                else:
                    st.error(f"Upload failed: {resp.text}")

    st.divider()
    st.subheader("Registered Models")
    resp = api_get("/models")
    models_list = resp.json() if resp.status_code == 200 else []
    if models_list:
        df = pd.DataFrame(models_list)
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No models registered yet.")


def page_ota_deployment():
    st.header("🚀 OTA Deployment (Data Scientist)")

    nodes_resp = api_get("/nodes")
    models_resp = api_get("/models")
    nodes = nodes_resp.json() if nodes_resp.status_code == 200 else []
    models_list = models_resp.json() if models_resp.status_code == 200 else []

    if not nodes:
        st.warning("No edge nodes available. Register nodes first (Admin).")
        return
    if not models_list:
        st.warning("No models available. Upload a model first.")
        return

    st.subheader("Trigger OTA Deployment")
    col1, col2 = st.columns(2)
    with col1:
        node = st.selectbox(
            "Target Node",
            options=nodes,
            format_func=lambda n: f"#{n['node_id']} — {n['mac_address']} ({n['location_name']})",
        )
    with col2:
        model = st.selectbox(
            "Model Version",
            options=models_list,
            format_func=lambda m: f"#{m['model_id']} — {m['version_tag']} ({m['required_ram_mb']}MB RAM)",
        )

    simulate_corrupt = st.toggle(
        "💥 Simulate Corrupted Hash during OTA transfer",
        value=False,
        help="When enabled, the received hash is intentionally mismatched so the "
        "node discards the update (Hash_Error_Discarded) and keeps the previous model.",
    )

    if st.button("Deploy OTA", type="primary", use_container_width=True):
        resp = api_post(
            "/deploy",
            params={
                "node_id": node["node_id"],
                "model_id": model["model_id"],
                "simulate_corrupted_hash": simulate_corrupt,
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            st.success(f"Deployment result: **{data['message']}**")
            st.json(data)
        else:
            st.error(f"Deployment failed: {resp.text}")

    st.divider()
    st.subheader("Deployment History")
    resp = api_get("/deployments")
    tasks = resp.json() if resp.status_code == 200 else []
    if tasks:
        df = pd.DataFrame(tasks)
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No deployments yet.")


def page_node_management():
    st.header("🛠️ Node Management (System Admin)")

    st.subheader("Register New Edge Node")
    with st.form("register_node"):
        mac = st.text_input("MAC Address", placeholder="00:1A:2B:3C:4D:00")
        location = st.text_input("Location Name", placeholder="Warehouse A")
        ram = st.number_input("Hardware RAM Limit (MB)", min_value=128, value=2048, step=128)
        submitted = st.form_submit_button("Register Node", use_container_width=True)
        if submitted:
            if not mac or not location:
                st.error("MAC address and location are required.")
            else:
                resp = api_post(
                    "/nodes",
                    params={
                        "mac_address": mac,
                        "location_name": location,
                        "hardware_limit_ram_mb": int(ram),
                    },
                )
                if resp.status_code == 200:
                    d = resp.json()
                    st.success(f"Registered node #{d['node_id']} — API key: `{d['api_key']}`")
                else:
                    st.error(f"Registration failed: {resp.text}")

    st.divider()
    st.subheader("Existing Nodes")
    resp = api_get("/nodes")
    nodes = resp.json() if resp.status_code == 200 else []
    if not nodes:
        st.info("No nodes registered.")
        return

    for n in nodes:
        with st.expander(f"Node #{n['node_id']} — {n['mac_address']} ({n['status']})"):
            c1, c2 = st.columns(2)
            with c1:
                new_loc = st.text_input("Location", value=n["location_name"], key=f"loc_{n['node_id']}")
                new_thresh = st.number_input(
                    "CPU Alert Threshold (%)",
                    min_value=0.0, max_value=100.0,
                    value=float(n["cpu_alert_threshold"]),
                    key=f"thresh_{n['node_id']}",
                )
            with c2:
                st.write(f"API Key: `{n['api_key']}`")
                st.write(f"RAM Limit: {n['hardware_limit_ram_mb']} MB")
                st.write(f"Missed Pings: {n['missed_pings']}")
                st.write(f"Current Model: {n['current_model_id'] or '—'}")
                st.write(f"Previous Model: {n['previous_model_id'] or '—'}")

            c3, c4 = st.columns(2)
            with c3:
                if st.button("Update Config", key=f"update_{n['node_id']}"):
                    resp = api_put_update_node(n["node_id"], new_loc, new_thresh)
                    if resp.status_code == 200:
                        st.success("Updated.")
                        st.rerun()
                    else:
                        st.error(f"Update failed: {resp.text}")
            with c4:
                if st.button("Revoke Access", key=f"revoke_{n['node_id']}", type="secondary"):
                    resp = api_delete(f"/nodes/{n['node_id']}")
                    if resp.status_code == 200:
                        st.success("Revoked.")
                        st.rerun()
                    else:
                        st.error(f"Revoke failed: {resp.text}")


def api_put_update_node(node_id, location, cpu_threshold):
    headers = {"Authorization": f"Bearer {st.session_state.token}"} if st.session_state.token else {}
    try:
        return requests.put(
            f"{BASE_URL}/nodes/{node_id}",
            params={"location_name": location, "cpu_alert_threshold": cpu_threshold},
            headers=headers,
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        st.error(f"Backend unreachable: {exc}")
        st.stop()


def page_green_sustainability():
    st.header("🌱 Green Sustainability Benchmark")

    st.markdown(
        """
        Compares two telemetry-processing pipelines on a simulated fleet:

        | | Baseline | Green |
        |---|---|---|
        | Serialization | Uncompressed full JSON | Delta-filtered (≥5% change) + zlib |
        | Offline detection | O(n × m) nested list scan | O(n) hash-set difference |
        """
    )

    num_nodes = st.number_input(
        "Simulated nodes", min_value=100, max_value=50000, value=5000, step=100
    )

    if st.button("Run Benchmark", type="primary", use_container_width=True):
        with st.spinner("Running baseline vs. green pipeline..."):
            result = green_benchmark(num_nodes=int(num_nodes))

        col1, col2 = st.columns(2)
        with col1:
            st.metric("CPU Time Saved", f"{result['cpu_time_saved_pct']}%")
            st.write(f"Baseline: {result['baseline_cpu_s']} s")
            st.write(f"Green:    {result['green_cpu_s']} s")
        with col2:
            st.metric("Network Payload Reduced", f"{result['payload_reduced_pct']}%")
            st.write(f"Baseline: {result['baseline_bytes']:,} bytes")
            st.write(f"Green:    {result['green_bytes']:,} bytes")

        st.divider()
        st.subheader("Comparison Charts")

        # CPU time bar chart
        fig_cpu = go.Figure(
            data=[
                go.Bar(name="Baseline", x=["CPU Time (s)"], y=[result["baseline_cpu_s"]]),
                go.Bar(name="Green", x=["CPU Time (s)"], y=[result["green_cpu_s"]]),
            ]
        )
        fig_cpu.update_layout(title="CPU Time: Baseline vs Green", barmode="group")
        st.plotly_chart(fig_cpu, use_container_width=True)

        # Payload bar chart
        fig_payload = go.Figure(
            data=[
                go.Bar(name="Baseline", x=["Payload (bytes)"], y=[result["baseline_bytes"]]),
                go.Bar(name="Green", x=["Payload (bytes)"], y=[result["green_bytes"]]),
            ]
        )
        fig_payload.update_layout(title="Network Payload: Baseline vs Green", barmode="group")
        st.plotly_chart(fig_payload, use_container_width=True)

        st.caption(
            f"Offline nodes detected — Baseline: {result['baseline_offline_count']}, "
            f"Green: {result['green_offline_count']}"
        )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def main():
    require_login()
    page = nav_sidebar()

    if page == "Fleet Overview":
        page_fleet_overview()
    elif page == "Telemetry & Charts":
        page_telemetry_charts()
    elif page == "Model Registry":
        page_model_registry()
    elif page == "OTA Deployment":
        page_ota_deployment()
    elif page == "Node Management":
        page_node_management()
    elif page == "Green Sustainability":
        page_green_sustainability()


if __name__ == "__main__":
    main()
