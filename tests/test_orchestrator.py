"""tests/test_orchestrator.py — Unit, Integration, and Data Flow (All-Uses) tests.

Run:
    pytest -v
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.database as db_mod
from backend.database import Base
from backend import models as orm_models
from backend.main import app
import core.ota_logic as ota_logic

# ---------------------------------------------------------------------------
# Test database setup (separate temp file so we never touch orchestrator.db)
# ---------------------------------------------------------------------------

TEST_DB_PATH = tempfile.mktemp(suffix=".db")
TEST_DB_URL = f"sqlite:///{TEST_DB_PATH}"
test_engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[db_mod.get_db] = override_get_db

Base.metadata.create_all(bind=test_engine)

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_users(db):
    """Idempotently seed the default Admin + Data Scientist users."""
    if db.query(orm_models.User).first():
        admin = db.query(orm_models.User).filter(orm_models.User.email == "admin@example.com").first()
        ds = db.query(orm_models.User).filter(orm_models.User.email == "scientist@example.com").first()
        return admin, ds
    admin = orm_models.User(
        name="Admin", role="SystemAdmin",
        email="admin@example.com",
        password_hash=orm_models.hash_password("admin123"),
    )
    ds = orm_models.User(
        name="Scientist", role="DataScientist",
        email="scientist@example.com",
        password_hash=orm_models.hash_password("scientist123"),
    )
    db.add_all([admin, ds])
    db.commit()
    db.refresh(admin)
    db.refresh(ds)
    return admin, ds


def _get_db():
    return TestingSessionLocal()


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------

class TestUnit:
    def test_sha256_hash_generation(self):
        """SHA-256 produces a fixed 64-char hex digest and is deterministic."""
        h1 = orm_models.hash_password("secret")
        h2 = orm_models.hash_password("secret")
        h3 = orm_models.hash_password("different")
        assert len(h1) == 64
        assert all(c in "0123456789abcdef" for c in h1)
        assert h1 == h2  # deterministic
        assert h1 != h3  # sensitive to input

    def test_api_key_generation(self):
        """generate_api_key returns a unique 64-hex-char string."""
        k1 = orm_models.generate_api_key()
        k2 = orm_models.generate_api_key()
        assert len(k1) == 64
        assert all(c in "0123456789abcdef" for c in k1)
        assert k1 != k2  # unique

    def test_node_initialization_defaults(self):
        """EdgeNode defaults: status Booting, missed_pings 0, cpu threshold 85.0."""
        db = _get_db()
        try:
            node = orm_models.EdgeNode(
                mac_address="AA:BB:CC:DD:EE:FF",
                location_name="Lab",
                hardware_limit_ram_mb=4096,
                api_key="testkey",
            )
            db.add(node)
            db.flush()  # apply SQLAlchemy column defaults
            assert node.status == "Booting"
            assert node.missed_pings == 0
            assert node.cpu_alert_threshold == 85.0
            assert node.current_model_id is None
            assert node.previous_model_id is None
        finally:
            db.rollback()
            db.close()

    def test_valid_api_key_auth(self):
        """verify_api_key accepts a valid key and rejects an invalid one."""
        db = _get_db()
        try:
            node = orm_models.EdgeNode(
                mac_address="11:22:33:44:55:66",
                location_name="Test",
                hardware_limit_ram_mb=2048,
                api_key="valid-key-1234",
            )
            db.add(node)
            db.commit()

            # Valid key returns the node
            from backend.main import verify_api_key
            result = verify_api_key(x_api_key="valid-key-1234", db=db)
            assert result.node_id == node.node_id

            # Invalid key raises 401
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as excinfo:
                verify_api_key(x_api_key="wrong-key", db=db)
            assert excinfo.value.status_code == 401

            # Missing key raises 401
            with pytest.raises(HTTPException) as excinfo:
                verify_api_key(x_api_key=None, db=db)
            assert excinfo.value.status_code == 401
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------

class TestIntegration:
    def _login(self, email, password):
        resp = client.post("/auth/login", params={"email": email, "password": password})
        assert resp.status_code == 200, resp.text
        return resp.json()["access_token"]

    def test_end_to_end_ota_deployment_success(self):
        """Admin registers node; DS uploads model; DS deploys → Success."""
        db = _get_db()
        try:
            admin, ds = _seed_users(db)

            # Admin registers a node
            token = self._login("admin@example.com", "admin123")
            resp = client.post(
                "/nodes",
                params={
                    "mac_address": "DE:AD:BE:EF:00:01",
                    "location_name": "TestLab",
                    "hardware_limit_ram_mb": 4096,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200, resp.text
            node = resp.json()
            node_id = node["node_id"]
            api_key = node["api_key"]

            # Data Scientist uploads a model
            token_ds = self._login("scientist@example.com", "scientist123")
            resp = client.post(
                "/models",
                params={
                    "version_tag": "mobilenet-v1",
                    "file_size_mb": 14.5,
                    "required_ram_mb": 512,
                    "file_payload": "dummy-payload-bytes",
                },
                headers={"Authorization": f"Bearer {token_ds}"},
            )
            assert resp.status_code == 200, resp.text
            model = resp.json()
            model_id = model["model_id"]

            # Edge node sends telemetry (auth via API key)
            resp = client.post(
                f"/telemetry/{node_id}",
                params={
                    "cpu_usage_percent": 30.0,
                    "ram_usage_mb": 1024.0,
                    "inference_latency_ms": 25.0,
                },
                headers={"X-API-Key": api_key},
            )
            assert resp.status_code == 200, resp.text

            # Data Scientist deploys OTA (valid hash, RAM fits)
            resp = client.post(
                "/deploy",
                params={
                    "node_id": node_id,
                    "model_id": model_id,
                    "simulate_corrupted_hash": False,
                },
                headers={"Authorization": f"Bearer {token_ds}"},
            )
            assert resp.status_code == 200, resp.text
            result = resp.json()
            assert result["message"] == "Success"
            assert result["current_model_id"] == model_id

            # Verify DB state
            db_node = db.query(orm_models.EdgeNode).filter(orm_models.EdgeNode.node_id == node_id).first()
            assert db_node.current_model_id == model_id
            assert db_node.status == "Executing_Model"

            task = db.query(orm_models.DeploymentTask).filter(
                orm_models.DeploymentTask.node_id == node_id
            ).first()
            assert task.deployment_status == "Success"
        finally:
            db.close()

    def test_ota_rollback_on_insufficient_ram(self):
        """Model requires more RAM than node limit → Rollback_Triggered."""
        db = _get_db()
        try:
            admin, ds = _seed_users(db)

            token = self._login("admin@example.com", "admin123")
            resp = client.post(
                "/nodes",
                params={
                    "mac_address": "DE:AD:BE:EF:00:02",
                    "location_name": "LowRamLab",
                    "hardware_limit_ram_mb": 256,  # tiny limit
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200, resp.text
            node_id = resp.json()["node_id"]

            token_ds = self._login("scientist@example.com", "scientist123")
            resp = client.post(
                "/models",
                params={
                    "version_tag": "big-model",
                    "file_size_mb": 100.0,
                    "required_ram_mb": 8192,  # exceeds node limit
                    "file_payload": "big-payload",
                },
                headers={"Authorization": f"Bearer {token_ds}"},
            )
            assert resp.status_code == 200, resp.text
            model_id = resp.json()["model_id"]

            resp = client.post(
                "/deploy",
                params={
                    "node_id": node_id,
                    "model_id": model_id,
                    "simulate_corrupted_hash": False,
                },
                headers={"Authorization": f"Bearer {token_ds}"},
            )
            assert resp.status_code == 200, resp.text
            result = resp.json()
            assert result["message"] == "Rollback_Triggered"

            db_node = db.query(orm_models.EdgeNode).filter(orm_models.EdgeNode.node_id == node_id).first()
            assert db_node.current_model_id is None  # rolled back, no previous model
            assert db_node.status == "Idle_Ready"

            task = db.query(orm_models.DeploymentTask).filter(
                orm_models.DeploymentTask.node_id == node_id
            ).first()
            assert task.deployment_status == "Rollback_Triggered"
        finally:
            db.close()

    def test_ota_corrupted_hash_discarded(self):
        """Simulated corrupted hash → Hash_Error_Discarded, keep previous model."""
        db = _get_db()
        try:
            admin, ds = _seed_users(db)

            token = self._login("admin@example.com", "admin123")
            resp = client.post(
                "/nodes",
                params={
                    "mac_address": "DE:AD:BE:EF:00:03",
                    "location_name": "HashLab",
                    "hardware_limit_ram_mb": 4096,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200, resp.text
            node_id = resp.json()["node_id"]

            token_ds = self._login("scientist@example.com", "scientist123")
            resp = client.post(
                "/models",
                params={
                    "version_tag": "hash-model",
                    "file_size_mb": 10.0,
                    "required_ram_mb": 256,
                    "file_payload": "payload",
                },
                headers={"Authorization": f"Bearer {token_ds}"},
            )
            assert resp.status_code == 200, resp.text
            model_id = resp.json()["model_id"]

            # Deploy with simulated corrupted hash
            resp = client.post(
                "/deploy",
                params={
                    "node_id": node_id,
                    "model_id": model_id,
                    "simulate_corrupted_hash": True,
                },
                headers={"Authorization": f"Bearer {token_ds}"},
            )
            assert resp.status_code == 200, resp.text
            result = resp.json()
            assert result["message"] == "Hash_Error_Discarded"
            assert result["current_model_id"] is None  # nothing installed yet

            task = db.query(orm_models.DeploymentTask).filter(
                orm_models.DeploymentTask.node_id == node_id
            ).first()
            assert task.deployment_status == "Hash_Error_Discarded"
        finally:
            db.close()

    def test_api_key_required_for_telemetry(self):
        """Telemetry without API key → 401."""
        resp = client.post(
            "/telemetry/9999",
            params={"cpu_usage_percent": 10.0, "ram_usage_mb": 100.0, "inference_latency_ms": 5.0},
        )
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Data Flow Tests (All-Uses coverage for deploy_ota_model)
# ---------------------------------------------------------------------------

class TestDataFlow:
    """All-Uses coverage of deploy_ota_model:

    1. Corrupted Hash        → Hash_Error_Discarded
    2. Out-of-Memory Rollback → Rollback_Triggered
    3. Valid OTA Push        → Success
    """

    def test_corrupted_hash_all_uses(self):
        status, active = ota_logic.deploy_ota_model(
            expected_hash="abc123",
            received_hash="def456",   # mismatch
            model_ram_mb=256,
            max_ram_mb=1024,
            prev_model="prev-1",
            new_model="new-1",
        )
        assert status == "Hash_Error_Discarded"
        assert active == "prev-1"

    def test_out_of_memory_rollback_all_uses(self):
        status, active = ota_logic.deploy_ota_model(
            expected_hash="abc123",
            received_hash="abc123",   # match
            model_ram_mb=8192,        # exceeds limit
            max_ram_mb=1024,
            prev_model="prev-1",
            new_model="new-1",
        )
        assert status == "Rollback_Triggered"
        assert active == "prev-1"

    def test_valid_ota_push_all_uses(self):
        status, active = ota_logic.deploy_ota_model(
            expected_hash="abc123",
            received_hash="abc123",   # match
            model_ram_mb=512,
            max_ram_mb=1024,          # fits
            prev_model="prev-1",
            new_model="new-1",
        )
        assert status == "Success"
        assert active == "new-1"

    def test_all_parameters_are_used(self):
        """Sanity: every parameter influences at least one code path."""
        base = dict(
            expected_hash="h", received_hash="h",
            model_ram_mb=100, max_ram_mb=200,
            prev_model="P", new_model="N",
        )
        # flip each param and ensure outcome can change
        h_ok, _ = ota_logic.deploy_ota_model(**base)
        base2 = dict(base, received_hash="bad")
        h_bad, _ = ota_logic.deploy_ota_model(**base2)
        assert h_ok != h_bad  # received_hash matters

        base3 = dict(base, model_ram_mb=9999, max_ram_mb=1)
        h_oom, _ = ota_logic.deploy_ota_model(**base3)
        assert h_oom == "Rollback_Triggered"  # model_ram_mb & max_ram_mb matter

        base4 = dict(base, prev_model="P2", new_model="N2")
        _, active = ota_logic.deploy_ota_model(**base4)
        assert active == "N2"  # new_model used on success
        _, active2 = ota_logic.deploy_ota_model(**{**base4, "received_hash": "bad"})
        assert active2 == "P2"  # prev_model used on hash error
