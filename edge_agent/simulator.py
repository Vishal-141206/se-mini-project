"""edge_agent/simulator.py — Edge Node Simulator Daemon.

Simulates multiple remote edge devices (Jetson Nano, Raspberry Pi, ...) that:
  * register with the FastAPI backend (obtain an API key)
  * send periodic telemetry heartbeats (CPU %, RAM MB, inference latency ms)
  * maintain a local state machine (Booting -> Syncing_Telemetry -> Idle_Ready ...)
  * poll for pending OTA deployment tasks
  * perform OTA hash verification locally
  * autonomously roll back on hash error or out-of-memory (RAM) conditions
  * track missed pings and transition to Offline after 3 consecutive misses

Run:
    python edge_agent/simulator.py          # single simulated node
    python edge_agent/simulator.py --nodes 5  # five simulated nodes
"""

import argparse
import hashlib
import json
import random
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import requests

BASE_URL = "http://localhost:8000"

# ---------------------------------------------------------------------------
# Local state machine
# ---------------------------------------------------------------------------

class EdgeNodeState:
    BOOTING = "Booting"
    SYNCING_TELEMETRY = "Syncing_Telemetry"
    IDLE_READY = "Idle_Ready"
    DOWNLOADING_UPDATE = "Downloading_Update"
    EXECUTING_MODEL = "Executing_Model"
    FALLBACK_RECOVERY = "Fallback_Recovery"
    OFFLINE = "Offline"


VALID_TRANSITIONS = {
    EdgeNodeState.BOOTING: [EdgeNodeState.SYNCING_TELEMETRY],
    EdgeNodeState.SYNCING_TELEMETRY: [EdgeNodeState.IDLE_READY],
    EdgeNodeState.IDLE_READY: [
        EdgeNodeState.DOWNLOADING_UPDATE,
        EdgeNodeState.OFFLINE,
    ],
    EdgeNodeState.DOWNLOADING_UPDATE: [
        EdgeNodeState.EXECUTING_MODEL,
        EdgeNodeState.FALLBACK_RECOVERY,
        EdgeNodeState.IDLE_READY,
    ],
    EdgeNodeState.EXECUTING_MODEL: [EdgeNodeState.IDLE_READY, EdgeNodeState.OFFLINE],
    EdgeNodeState.FALLBACK_RECOVERY: [EdgeNodeState.IDLE_READY],
    EdgeNodeState.OFFLINE: [EdgeNodeState.IDLE_READY],
}


def can_transition(current: str, target: str) -> bool:
    return target in VALID_TRANSITIONS.get(current, [])


# ---------------------------------------------------------------------------
# Simulated edge device
# ---------------------------------------------------------------------------

class SimulatedEdgeNode:
    def __init__(
        self,
        name: str,
        mac_address: str,
        location: str,
        ram_limit_mb: int,
        base_url: str = BASE_URL,
    ):
        self.name = name
        self.mac_address = mac_address
        self.location = location
        self.ram_limit_mb = ram_limit_mb
        self.base_url = base_url

        self.api_key: Optional[str] = None
        self.node_id: Optional[int] = None
        self.current_model_id: Optional[int] = None
        self.previous_model_id: Optional[int] = None
        self.state = EdgeNodeState.BOOTING
        self.missed_pings = 0
        self.running = False
        self._thread: Optional[threading.Thread] = None

    # -- registration -------------------------------------------------------

    def register(self) -> bool:
        try:
            # Attempt to register; the backend rejects duplicate MACs.
            resp = requests.post(
                f"{self.base_url}/nodes",
                params={
                    "mac_address": self.mac_address,
                    "location_name": self.location,
                    "hardware_limit_ram_mb": self.ram_limit_mb,
                },
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                self.api_key = data["api_key"]
                self.node_id = data["node_id"]
                self.state = EdgeNodeState.SYNCING_TELEMETRY
                print(f"[{self.name}] Registered. node_id={self.node_id} api_key={self.api_key[:8]}...")
                return True
            if resp.status_code == 400 and "already registered" in resp.text:
                # Node already exists: look it up by MAC
                nodes = requests.get(f"{self.base_url}/nodes", timeout=5).json()
                for n in nodes:
                    if n["mac_address"] == self.mac_address:
                        self.node_id = n["node_id"]
                        self.api_key = n["api_key"]
                        self.state = EdgeNodeState.SYNCING_TELEMETRY
                        print(f"[{self.name}] Re-using existing node {self.node_id}")
                        return True
                return False
            print(f"[{self.name}] Registration failed: {resp.status_code} {resp.text}")
            return False
        except Exception as exc:  # pragma: no cover - network
            print(f"[{self.name}] Registration error: {exc}")
            return False

    # -- telemetry ----------------------------------------------------------

    def send_telemetry(self) -> bool:
        if not self.api_key or self.node_id is None:
            return False
        cpu = round(random.uniform(20, 90), 2)
        ram = round(random.uniform(1024, self.ram_limit_mb), 2)
        latency = round(random.uniform(5, 120), 2)
        try:
            resp = requests.post(
                f"{self.base_url}/telemetry/{self.node_id}",
                params={
                    "cpu_usage_percent": cpu,
                    "ram_usage_mb": ram,
                    "inference_latency_ms": latency,
                },
                headers={"X-API-Key": self.api_key},
                timeout=5,
            )
            if resp.status_code == 200:
                self.missed_pings = 0
                if can_transition(self.state, EdgeNodeState.IDLE_READY):
                    self.state = EdgeNodeState.IDLE_READY
                return True
            self.missed_pings += 1
        except Exception:  # pragma: no cover - network
            self.missed_pings += 1
        return False

    # -- OTA polling --------------------------------------------------------

    def poll_deployments(self) -> None:
        """Check for pending deployment tasks targeting this node."""
        if not self.api_key or self.node_id is None:
            return
        try:
            # No auth required for read in MVP read path; in prod we'd scope it.
            tasks = requests.get(f"{self.base_url}/deployments", timeout=5).json()
        except Exception:
            return

        # Find latest Executing task for this node not yet applied locally
        pending = [
            t
            for t in tasks
            if t["node_id"] == self.node_id and t["deployment_status"] == "Executing"
        ]
        if not pending:
            return
        task = pending[-1]  # most recent
        self._handle_ota(task)

    def _handle_ota(self, task: Dict) -> None:
        """Execute an OTA deployment locally: hash check + RAM check + rollback."""
        model_id = task["model_id"]
        # Fetch model metadata to get hash + RAM requirement
        try:
            models_list = requests.get(f"{self.base_url}/models", timeout=5).json()
        except Exception:
            return
        model = next((m for m in models_list if m["model_id"] == model_id), None)
        if not model:
            return

        expected_hash = model["file_hash"]
        # Simulate transfer: compute received hash. In real life this comes from
        # the transferred bytes; here we mirror expected unless corruption.
        received_hash = expected_hash  # assume intact transfer
        model_ram = model["required_ram_mb"]

        # Transition to downloading
        if can_transition(self.state, EdgeNodeState.DOWNLOADING_UPDATE):
            self.state = EdgeNodeState.DOWNLOADING_UPDATE

        # --- Pure OTA decision (Module A) ----------------------------------
        status_str, active_model = ota_logic.deploy_ota_model(
            expected_hash=expected_hash,
            received_hash=received_hash,
            model_ram_mb=model_ram,
            max_ram_mb=self.ram_limit_mb,
            prev_model=self.current_model_id,
            new_model=model_id,
        )

        if status_str == "Hash_Error_Discarded":
            print(f"[{self.name}] OTA hash error -> discard, keep model {self.current_model_id}")
            if can_transition(self.state, EdgeNodeState.IDLE_READY):
                self.state = EdgeNodeState.IDLE_READY
        elif status_str == "Rollback_Triggered":
            print(
                f"[{self.name}] OOM rollback -> required {model_ram}MB > limit {self.ram_limit_mb}MB"
            )
            self.previous_model_id = self.current_model_id
            self.current_model_id = self.previous_model_id
            if can_transition(self.state, EdgeNodeState.FALLBACK_RECOVERY):
                self.state = EdgeNodeState.FALLBACK_RECOVERY
            if can_transition(self.state, EdgeNodeState.IDLE_READY):
                self.state = EdgeNodeState.IDLE_READY
        else:  # Success
            self.previous_model_id = self.current_model_id
            self.current_model_id = model_id
            if can_transition(self.state, EdgeNodeState.EXECUTING_MODEL):
                self.state = EdgeNodeState.EXECUTING_MODEL
            print(f"[{self.name}] OTA success -> now running model {model_id}")

    # -- main loop ----------------------------------------------------------

    def start(self, interval: float = 3.0) -> None:
        self.running = True
        self._thread = threading.Thread(target=self._loop, args=(interval,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self, interval: float) -> None:
        while self.running:
            if self.state == EdgeNodeState.BOOTING:
                if can_transition(self.state, EdgeNodeState.SYNCING_TELEMETRY):
                    self.state = EdgeNodeState.SYNCING_TELEMETRY
            elif self.state == EdgeNodeState.SYNCING_TELEMETRY:
                self.send_telemetry()
                if can_transition(self.state, EdgeNodeState.IDLE_READY):
                    self.state = EdgeNodeState.IDLE_READY
            else:
                self.send_telemetry()
                self.poll_deployments()
                if self.missed_pings >= 3:
                    self.state = EdgeNodeState.OFFLINE
                    print(f"[{self.name}] OFFLINE (missed {self.missed_pings} pings)")
            time.sleep(interval)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Edge Node Simulator Daemon")
    parser.add_argument("--nodes", type=int, default=1, help="Number of simulated nodes")
    parser.add_argument("--interval", type=float, default=3.0, help="Telemetry interval (seconds)")
    parser.add_argument("--base-url", default=BASE_URL, help="Backend base URL")
    args = parser.parse_args()

    nodes: List[SimulatedEdgeNode] = []
    hw_profiles = [
        ("Jetson-Nano", 4096),
        ("Raspberry-Pi-4", 2048),
        ("Raspberry-Pi-Zero", 512),
        ("Xavier-NX", 8192),
    ]

    for i in range(args.nodes):
        profile = hw_profiles[i % len(hw_profiles)]
        name = f"{profile[0]}-{i}"
        mac = f"00:1A:2B:3C:4D:{i:02X}"
        node = SimulatedEdgeNode(
            name=name,
            mac_address=mac,
            location=f"Rack-{i // 4 + 1}",
            ram_limit_mb=profile[1],
            base_url=args.base_url,
        )
        if node.register():
            nodes.append(node)

    print(f"Started {len(nodes)} simulated edge node(s). Ctrl+C to stop.")
    for n in nodes:
        n.start(interval=args.interval)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping simulator...")
        for n in nodes:
            n.stop()


if __name__ == "__main__":
    main()
