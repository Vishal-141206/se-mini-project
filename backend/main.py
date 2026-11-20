"""Central_API_Gateway — FastAPI backend for the Distributed Edge-ML Fleet Orchestrator.

Endpoints:
  Auth            : POST /auth/login
  Nodes (Admin)   : GET/POST/PUT/DELETE /nodes/...
  Models (DS)     : GET/POST /models/...
  Deployment      : POST /deploy, GET /deployments
  Telemetry       : POST /telemetry/{node_id} (edge, API-key auth), GET /telemetry
  Alerts          : GET /alerts
  Simulation      : POST /simulate/corrupt-hash
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

import backend.database as db_mod
from backend.database import SessionLocal, engine, get_db
from backend import models
from backend.models import Base
import core.ota_logic as ota_logic

# ---------------------------------------------------------------------------
# DB init
# ---------------------------------------------------------------------------

Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Distributed Edge-ML Fleet Orchestrator", version="1.0.0")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

# In-memory "session" store mapping token -> user_id (MVP simplicity)
SESSIONS: dict = {}

# Simulation toggle state (in-memory for MVP)
SIMULATION_STATE = {"corrupt_hash": False}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return secrets.token_hex(32)


def create_session(user_id: int) -> str:
    token = secrets.token_hex(32)
    SESSIONS[token] = user_id
    return token


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    user_id = SESSIONS.get(token)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    user = db.query(models.User).filter(models.User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def require_admin(user: models.User = Depends(get_current_user)):
    if user.role != "SystemAdmin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


def require_data_scientist(user: models.User = Depends(get_current_user)):
    if user.role != "DataScientist":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Data Scientist only")
    return user


def verify_api_key(x_api_key: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Edge-node authentication: valid API key required, else 401 + log."""
    if not x_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key required")
    node = db.query(models.EdgeNode).filter(models.EdgeNode.api_key == x_api_key).first()
    if not node:
        # Log the failed attempt (MVP: print + in-memory could be extended)
        print(f"[AUTH-FAIL] Invalid API key attempt: {x_api_key[:8]}...")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return node


def seed_database(db: Session):
    """Seed 1 default Admin and 1 default Data Scientist if empty."""
    if db.query(models.User).first():
        return  # already seeded
    admin = models.User(
        name="System Admin",
        role="SystemAdmin",
        email="admin@example.com",
        password_hash=hash_password("admin123"),
    )
    ds = models.User(
        name="Data Scientist",
        role="DataScientist",
        email="scientist@example.com",
        password_hash=hash_password("scientist123"),
    )
    db.add_all([admin, ds])
    db.commit()
    print("[SEED] Default users created: admin@example.com / scientist@example.com")


@app.on_event("startup")
def startup():
    db = SessionLocal()
    try:
        seed_database(db)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.post("/auth/login")
def login(email: str, password: str, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user or user.password_hash != hash_password(password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token = create_session(user.user_id)
    return {"access_token": token, "role": user.role, "name": user.name, "user_id": user.user_id}


@app.post("/auth/logout")
def logout(token: str = Depends(oauth2_scheme)):
    SESSIONS.pop(token, None)
    return {"message": "Logged out"}


# ---------------------------------------------------------------------------
# Nodes (System Admin)
# ---------------------------------------------------------------------------

@app.get("/nodes")
def list_nodes(user: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(models.EdgeNode).all()


@app.post("/nodes")
def register_node(
    mac_address: str,
    location_name: str,
    hardware_limit_ram_mb: int,
    user: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if db.query(models.EdgeNode).filter(models.EdgeNode.mac_address == mac_address).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MAC address already registered")
    node = models.EdgeNode(
        mac_address=mac_address,
        location_name=location_name,
        status="Idle_Ready",
        hardware_limit_ram_mb=hardware_limit_ram_mb,
        api_key=generate_api_key(),
        missed_pings=0,
        cpu_alert_threshold=85.0,
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


@app.put("/nodes/{node_id}")
def update_node(
    node_id: int,
    location_name: Optional[str] = None,
    cpu_alert_threshold: Optional[float] = None,
    user: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    node = db.query(models.EdgeNode).filter(models.EdgeNode.node_id == node_id).first()
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    if location_name is not None:
        node.location_name = location_name
    if cpu_alert_threshold is not None:
        node.cpu_alert_threshold = cpu_alert_threshold
    db.commit()
    db.refresh(node)
    return node


@app.delete("/nodes/{node_id}")
def revoke_node(node_id: int, user: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    node = db.query(models.EdgeNode).filter(models.EdgeNode.node_id == node_id).first()
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    db.delete(node)
    db.commit()
    return {"message": f"Node {node_id} revoked"}


# ---------------------------------------------------------------------------
# Models (Data Scientist)
# ---------------------------------------------------------------------------

@app.post("/models")
def upload_model(
    version_tag: str,
    file_size_mb: float,
    required_ram_mb: int,
    file_payload: str,  # dummy binary payload as base64/hex string
    user: models.User = Depends(require_data_scientist),
    db: Session = Depends(get_db),
):
    file_hash = hashlib.sha256(file_payload.encode("utf-8")).hexdigest()
    model = models.ModelVersion(
        version_tag=version_tag,
        file_hash=file_hash,
        upload_timestamp=datetime.now(timezone.utc),
        file_size_mb=file_size_mb,
        required_ram_mb=required_ram_mb,
        uploaded_by=user.user_id,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


@app.get("/models")
def list_models(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(models.ModelVersion).all()


# ---------------------------------------------------------------------------
# OTA Deployment (Data Scientist)
# ---------------------------------------------------------------------------

@app.post("/deploy")
def deploy_ota(
    node_id: int,
    model_id: int,
    simulate_corrupted_hash: bool = False,
    user: models.User = Depends(require_data_scientist),
    db: Session = Depends(get_db),
):
    node = db.query(models.EdgeNode).filter(models.EdgeNode.node_id == node_id).first()
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    model = db.query(models.ModelVersion).filter(models.ModelVersion.model_id == model_id).first()
    if not model:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model not found")

    # Transition to Executing
    node.status = "Downloading_Update"
    task = models.DeploymentTask(
        deployment_status="Executing",
        node_id=node_id,
        model_id=model_id,
        execution_time=datetime.now(timezone.utc),
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    # Determine received hash (corrupted if simulation toggle on)
    expected_hash = model.file_hash
    received_hash = ("0" * 64) if (simulate_corrupted_hash or SIMULATION_STATE["corrupt_hash"]) else model.file_hash

    # Use the pure OTA logic (Module A)
    status_str, active_model = ota_logic.deploy_ota_model(
        expected_hash=expected_hash,
        received_hash=received_hash,
        model_ram_mb=model.required_ram_mb,
        max_ram_mb=node.hardware_limit_ram_mb,
        prev_model=node.current_model_id,
        new_model=model.model_id,
    )

    # Apply outcome
    if status_str == "Hash_Error_Discarded":
        task.deployment_status = "Hash_Error_Discarded"
        node.status = "Idle_Ready"
        # keep previous model (current_model_id unchanged)
    elif status_str == "Rollback_Triggered":
        task.deployment_status = "Rollback_Triggered"
        node.status = "Fallback_Recovery"
        # revert to previous model
        node.current_model_id = node.previous_model_id
        # after rollback recovery -> Idle_Ready
        node.status = "Idle_Ready"
    else:  # Success
        task.deployment_status = "Success"
        node.previous_model_id = node.current_model_id
        node.current_model_id = model.model_id
        node.status = "Executing_Model"

    db.commit()
    db.refresh(task)
    db.refresh(node)
    return {
        "task_id": task.task_id,
        "deployment_status": task.deployment_status,
        "node_id": node.node_id,
        "node_status": node.status,
        "current_model_id": node.current_model_id,
        "message": status_str,
    }


@app.get("/deployments")
def list_deployments(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(models.DeploymentTask).all()


# ---------------------------------------------------------------------------
# Telemetry (Edge Node auth via API key)
# ---------------------------------------------------------------------------

@app.post("/telemetry/{node_id}")
def ingest_telemetry(
    node_id: int,
    cpu_usage_percent: float,
    ram_usage_mb: float,
    inference_latency_ms: float,
    active_model_version: Optional[str] = None,
    node: models.EdgeNode = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    if node.node_id != node_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key does not match node")

    # Reset missed pings + update status
    node.missed_pings = 0
    if node.status == "Offline":
        node.status = "Idle_Ready"

    log = models.TelemetryLog(
        timestamp=datetime.now(timezone.utc),
        cpu_usage_percent=cpu_usage_percent,
        ram_usage_mb=ram_usage_mb,
        inference_latency_ms=inference_latency_ms,
        node_id=node_id,
    )
    db.add(log)
    db.commit()

    # FR-05: CPU threshold warning (MVP: print alert; dashboard reads latest)
    if cpu_usage_percent > node.cpu_alert_threshold:
        print(f"[ALERT] Node {node_id} CPU {cpu_usage_percent}% > threshold {node.cpu_alert_threshold}%")

    return {"status": "ok", "missed_pings": node.missed_pings}


@app.get("/telemetry")
def get_telemetry(
    node_id: Optional[int] = None,
    limit: int = Query(100, le=1000),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(models.TelemetryLog)
    if node_id is not None:
        q = q.filter(models.TelemetryLog.node_id == node_id)
    return q.order_by(models.TelemetryLog.timestamp.desc()).limit(limit).all()


# ---------------------------------------------------------------------------
# Alerts (FR-05)
# ---------------------------------------------------------------------------

@app.get("/alerts")
def get_alerts(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    alerts = []
    # Offline nodes: missed 3 consecutive pings
    offline_nodes = db.query(models.EdgeNode).filter(models.EdgeNode.missed_pings >= 3).all()
    for n in offline_nodes:
        alerts.append(
            {
                "node_id": n.node_id,
                "type": "Offline",
                "severity": "critical",
                "message": f"Node {n.mac_address} ({n.location_name}) is Offline (missed {n.missed_pings} pings)",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
    # CPU warnings
    nodes = db.query(models.EdgeNode).all()
    latest_by_node = {}
    for log in db.query(models.TelemetryLog).all():
        latest_by_node[log.node_id] = log
    for n in nodes:
        log = latest_by_node.get(n.node_id)
        if log and log.cpu_usage_percent > n.cpu_alert_threshold:
            alerts.append(
                {
                    "node_id": n.node_id,
                    "type": "CpuWarning",
                    "severity": "warning",
                    "message": f"Node {n.mac_address} CPU {log.cpu_usage_percent}% > threshold {n.cpu_alert_threshold}%",
                    "timestamp": log.timestamp.isoformat(),
                }
            )
    return alerts


# ---------------------------------------------------------------------------
# Offline / ping simulation (used by tests & simulator)
# ---------------------------------------------------------------------------

@app.post("/nodes/{node_id}/missed-ping")
def record_missed_ping(node_id: int, db: Session = Depends(get_db)):
    node = db.query(models.EdgeNode).filter(models.EdgeNode.node_id == node_id).first()
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    node.missed_pings += 1
    if node.missed_pings >= 3:
        node.status = "Offline"
    db.commit()
    db.refresh(node)
    return {"node_id": node.node_id, "missed_pings": node.missed_pings, "status": node.status}


@app.post("/simulate/corrupt-hash")
def set_corrupt_hash(enabled: bool, user: models.User = Depends(require_data_scientist)):
    SIMULATION_STATE["corrupt_hash"] = enabled
    return {"corrupt_hash": SIMULATION_STATE["corrupt_hash"]}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}
