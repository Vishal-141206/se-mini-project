"""Module B — Green Software Engineering Benchmark.

Two pipelines for processing simulated edge-node telemetry:

* Baseline : uncompressed full JSON + naive O(n * m) list scan for
             offline-node detection.
* Green    : delta filtering (>= 5% metric change) + zlib compression
             + O(n) hash-set difference for offline detection.

Both expose the same public functions so the Streamlit dashboard can
run a live side-by-side comparison.
"""

import json
import time
import zlib
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# Synthetic telemetry generation
# ---------------------------------------------------------------------------


def generate_telemetry(num_nodes: int, seed: int = 42) -> List[Dict]:
    """Produce ``num_nodes`` telemetry records with random metric values."""
    import random

    rng = random.Random(seed)
    nodes = []
    for i in range(num_nodes):
        node_id = f"node-{i:05d}"
        online = rng.random() > 0.05  # 95% online
        nodes.append(
            {
                "node_id": node_id,
                "online": online,
                "cpu": round(rng.uniform(10, 95), 2),
                "ram": round(rng.uniform(512, 8192), 2),
                "latency": round(rng.uniform(5, 500), 2),
            }
        )
    return nodes


# ---------------------------------------------------------------------------
# Baseline pipeline (uncompressed + O(n*m) list scan)
# ---------------------------------------------------------------------------


def baseline_serialize(records: List[Dict]) -> bytes:
    """Uncompressed full JSON serialisation."""
    return json.dumps(records).encode("utf-8")


def baseline_detect_offline(
    current: List[Dict], previous: List[Dict]
) -> List[str]:
    """Naive O(n * m) offline detection: nested list scan."""
    previous_ids = [r["node_id"] for r in previous]  # list, O(n) lookup
    offline = []
    for r in current:  # outer O(n)
        if r["node_id"] not in previous_ids:  # inner O(m)
            offline.append(r["node_id"])
    return offline


def baseline_run(current: List[Dict], previous: List[Dict]) -> Tuple[float, float, int]:
    """Run the baseline pipeline; return (cpu_seconds, payload_bytes, offline_count)."""
    t0 = time.perf_counter()
    payload = baseline_serialize(current)
    serialize_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    offline = baseline_detect_offline(current, previous)
    detect_s = time.perf_counter() - t0

    return serialize_s + detect_s, len(payload), len(offline)


# ---------------------------------------------------------------------------
# Green pipeline (delta + zlib + O(n) hash-set)
# ---------------------------------------------------------------------------


def green_serialize(records: List[Dict], previous: List[Dict]) -> bytes:
    """Delta-filter (>= 5% change) then zlib-compress."""
    prev_by_id = {r["node_id"]: r for r in previous}
    delta = []
    for r in records:
        prev = prev_by_id.get(r["node_id"])
        if prev is None:
            delta.append(r)  # new node -> always include
            continue
        changed = False
        for key in ("cpu", "ram", "latency"):
            old_v = prev[key]
            new_v = r[key]
            if old_v == 0:
                changed = new_v != 0
            elif abs(new_v - old_v) / abs(old_v) >= 0.05:
                changed = True
        if changed:
            delta.append(r)
    return zlib.compress(json.dumps(delta).encode("utf-8"))


def green_detect_offline(current: List[Dict], previous: List[Dict]) -> List[str]:
    """O(n) offline detection using hash-set difference."""
    previous_ids = {r["node_id"] for r in previous}
    current_ids = {r["node_id"] for r in current}
    return sorted(current_ids - previous_ids)


def green_run(current: List[Dict], previous: List[Dict]) -> Tuple[float, float, int]:
    """Run the green pipeline; return (cpu_seconds, payload_bytes, offline_count)."""
    t0 = time.perf_counter()
    payload = green_serialize(current, previous)
    serialize_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    offline = green_detect_offline(current, previous)
    detect_s = time.perf_counter() - t0

    return serialize_s + detect_s, len(payload), len(offline)


# ---------------------------------------------------------------------------
# Comparison helper
# ---------------------------------------------------------------------------


def benchmark(num_nodes: int = 5000) -> Dict:
    """Run baseline vs green and return a comparison dict."""
    previous = generate_telemetry(num_nodes, seed=1)
    current = generate_telemetry(num_nodes, seed=2)

    base_cpu, base_bytes, base_offline = baseline_run(current, previous)
    green_cpu, green_bytes, green_offline = green_run(current, previous)

    cpu_saved = 0.0 if base_cpu == 0 else (base_cpu - green_cpu) / base_cpu * 100
    payload_reduced = 0.0 if base_bytes == 0 else (base_bytes - green_bytes) / base_bytes * 100

    return {
        "num_nodes": num_nodes,
        "baseline_cpu_s": round(base_cpu, 6),
        "green_cpu_s": round(green_cpu, 6),
        "cpu_time_saved_pct": round(cpu_saved, 2),
        "baseline_bytes": base_bytes,
        "green_bytes": green_bytes,
        "payload_reduced_pct": round(payload_reduced, 2),
        "baseline_offline_count": base_offline,
        "green_offline_count": green_offline,
    }
