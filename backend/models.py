"""SQLAlchemy ORM models mapping to the strict ER diagram."""

import hashlib
import secrets
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from backend.database import Base


class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    role = Column(String(20), nullable=False)  # "SystemAdmin" | "DataScientist"
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)

    uploaded_models = relationship("ModelVersion", back_populates="uploader")


class EdgeNode(Base):
    __tablename__ = "edge_nodes"

    node_id = Column(Integer, primary_key=True, index=True)
    mac_address = Column(String(17), unique=True, nullable=False, index=True)
    location_name = Column(String(100), nullable=False)
    status = Column(String(30), nullable=False, default="Booting")  # status enum
    hardware_limit_ram_mb = Column(Integer, nullable=False)
    api_key = Column(String(64), unique=True, nullable=False, index=True)
    current_model_id = Column(Integer, ForeignKey("model_versions.model_id"), nullable=True)
    previous_model_id = Column(Integer, ForeignKey("model_versions.model_id"), nullable=True)
    missed_pings = Column(Integer, nullable=False, default=0)
    cpu_alert_threshold = Column(Float, nullable=False, default=85.0)

    current_model = relationship("ModelVersion", foreign_keys=[current_model_id])
    previous_model = relationship("ModelVersion", foreign_keys=[previous_model_id])
    deployment_tasks = relationship("DeploymentTask", back_populates="node")


class ModelVersion(Base):
    __tablename__ = "model_versions"

    model_id = Column(Integer, primary_key=True, index=True)
    version_tag = Column(String(100), nullable=False)
    file_hash = Column(String(64), nullable=False)  # SHA-256 hex digest
    upload_timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    file_size_mb = Column(Float, nullable=False)
    required_ram_mb = Column(Integer, nullable=False)
    uploaded_by = Column(Integer, ForeignKey("users.user_id"), nullable=False)

    uploader = relationship("User", back_populates="uploaded_models")
    deployment_tasks = relationship("DeploymentTask", back_populates="model")


class DeploymentTask(Base):
    __tablename__ = "deployment_tasks"

    task_id = Column(Integer, primary_key=True, index=True)
    deployment_status = Column(String(30), nullable=False)  # Pending | Executing | Hash_Error_Discarded | Rollback_Triggered | Success
    execution_time = Column(DateTime, default=datetime.utcnow, nullable=False)
    node_id = Column(Integer, ForeignKey("edge_nodes.node_id"), nullable=False)
    model_id = Column(Integer, ForeignKey("model_versions.model_id"), nullable=False)

    node = relationship("EdgeNode", back_populates="deployment_tasks")
    model = relationship("ModelVersion", back_populates="deployment_tasks")


class TelemetryLog(Base):
    __tablename__ = "telemetry_logs"

    log_id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    cpu_usage_percent = Column(Float, nullable=False)
    ram_usage_mb = Column(Float, nullable=False)
    inference_latency_ms = Column(Float, nullable=False)
    node_id = Column(Integer, ForeignKey("edge_nodes.node_id"), nullable=False)

    node = relationship("EdgeNode")


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """SHA-256 hash of a password (spec allows SHA-256 or bcrypt)."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    """Generate a secure, unique API key for an edge node."""
    return secrets.token_hex(32)  # 64 hex chars
