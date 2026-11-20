# Distributed Edge-ML Fleet Orchestrator

A complete, runnable MVP for orchestrating a fleet of edge-ML devices. Built with **FastAPI + SQLite**, **Streamlit + Plotly**, and a Python **edge-node simulator**.

## Project Structure

```
se-mini-project/
├── backend/
│   ├── main.py          # FastAPI Central_API_Gateway + all REST endpoints
│   ├── database.py      # SQLAlchemy engine, session, Base
│   └── models.py        # SQLAlchemy ORM models (5 tables) + helpers
├── core/
│   ├── ota_logic.py          # Module A: pure deploy_ota_model (0 data-flow anomalies)
│   └── green_telemetry.py    # Module B: baseline vs green telemetry pipelines
├── edge_agent/
│   └── simulator.py          # Edge node simulator daemon (state machine, OTA, rollback)
├── frontend/
│   └── app.py                # Streamlit dashboard (role-based login, Plotly charts)
├── tests/
│   └── test_orchestrator.py  # pytest: unit, integration, All-Uses data-flow
├── requirements.txt
└── README.md
```

Database (SQLite): `orchestrator.db` (created in project root on first run).

## Tech Stack

- **Backend:** Python, FastAPI, SQLAlchemy, SQLite
- **Dashboard:** Streamlit, Plotly
- **Edge Simulator:** Python (`requests`), local state machine
- **Tests:** `pytest`

## Prerequisites

Python 3.10+ (tested on 3.14).

## Installation

```bash
# 1. Clone / open the project
cd /workspaces/se-mini-project

# 2. Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

# 3. Install dependencies
pip install -r requirements.txt
```

## Database Schema (5 tables)

| Table | Key Columns |
|-------|-------------|
| `users` | user_id PK, name, role, email unique, password_hash |
| `edge_nodes` | node_id PK, mac_address unique, status, api_key unique, current_model_id FK, previous_model_id FK, missed_pings, cpu_alert_threshold |
| `model_versions` | model_id PK, version_tag, file_hash, upload_timestamp, file_size_mb, required_ram_mb, uploaded_by FK |
| `deployment_tasks` | task_id PK, deployment_status, execution_time, node_id FK, model_id FK |
| `telemetry_logs` | log_id PK, timestamp, cpu_usage_percent, ram_usage_mb, inference_latency_ms, node_id FK |

Seed data: 1 System Admin + 1 Data Scientist created automatically on first backend startup.

## Running the System

### A. Seed data + start the FastAPI backend

```bash
cd /workspaces/se-mini-project
source .venv/bin/activate

# The backend auto-creates tables and seeds default users on startup.
python -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

Default credentials (created on first startup):

| Role | Email | Password |
|------|-------|----------|
| System Admin | `admin@example.com` | `admin123` |
| Data Scientist | `scientist@example.com` | `scientist123` |

API docs: http://localhost:8000/docs

### B. Launch the Streamlit dashboard

```bash
cd /workspaces/se-mini-project
source .venv/bin/activate

streamlit run frontend/app.py
```

Dashboard opens at http://localhost:8501. Log in with one of the default accounts above.

Tabs available depend on role:
- **Fleet Overview** — node status badges, alerts
- **Telemetry & Charts** — Plotly CPU/RAM/latency over time
- **Model Registry** (Data Scientist) — upload model, list models
- **OTA Deployment** (Data Scientist) — trigger deployment, corruption toggle
- **Node Management** (System Admin) — register/update/revoke nodes
- **Green Sustainability** — baseline vs green benchmark (Plotly bar charts)

### C. Run the edge simulator

```bash
cd /workspaces/se-mini-project
source .venv/bin/activate

# Single simulated node
python edge_agent/simulator.py

# Multiple simulated nodes (e.g., 5)
python edge_agent/simulator.py --nodes 5
```

The simulator registers nodes, sends telemetry heartbeats, polls for OTA tasks, and performs autonomous rollback on hash errors / OOM.

### D. Run the test suite

```bash
cd /workspaces/se-mini-project
source .venv/bin/activate

pytest -v
```

All tests must pass (unit, integration, and All-Uses data-flow tests for `deploy_ota_model`).

## Core Requirements Covered

- **FR-01** Node registration & auth — API key generation, MAC uniqueness, 401 on invalid key
- **FR-02** OTA model deployment — model registry, deployment trigger, corrupted-hash simulation toggle
- **FR-03** Telemetry sync — heartbeats, fleet health dashboard, Plotly charts
- **FR-04** Autonomous rollback — hash error discard, RAM-based rollback with Fallback_Recovery → Idle_Ready
- **FR-05** Alert generation — Offline after 3 missed pings, CPU threshold warnings

## Beyond-Syllabus Modules

- **Module A — Data Flow Testing:** `core/ota_logic.py` (`deploy_ota_model`, zero dd/du/ur anomalies) + `tests/test_orchestrator.py` All-Uses cases (corrupted hash, OOM rollback, valid push).
- **Module B — Green Software Engineering:** `core/green_telemetry.py` (baseline vs green pipelines) + "Green Sustainability Benchmark" tab in the Streamlit dashboard showing % CPU Time Saved and % Network Payload Reduced.
