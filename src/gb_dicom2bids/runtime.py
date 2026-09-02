from __future__ import annotations

import csv
import json
import os
import shutil
import socket
import threading
import time
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from .config import ProjectConfig

TERMINAL_SERIES_STATES = {
    "converted",
    "review_ready",
    "failed",
    "skipped",
    "interrupted",
    "dry_run",
}
ACTIVE_SERIES_STATES = {
    "queued",
    "linking",
    "dcm2niix",
    "compressing",
    "validating",
    "installing",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def series_state_path(audit_root: Path, series_uid_hash: str) -> Path:
    return audit_root / "status" / f"{series_uid_hash}.json"


def update_series_state(
    audit_root: Path,
    series_uid_hash: str,
    stage: str,
    **values: Any,
) -> dict[str, Any]:
    path = series_state_path(audit_root, series_uid_hash)
    state = read_json(path)
    state.update(values)
    state.update({"series_uid_hash": series_uid_hash, "stage": stage, "updated_at": utc_now()})
    state.setdefault("started_at", state["updated_at"])
    state.setdefault("worker_pid", os.getpid())
    atomic_write_json(path, state)
    return state


def iter_series_states(audit_root: Path) -> list[dict[str, Any]]:
    root = audit_root / "status"
    if not root.is_dir():
        return []
    return [state for path in sorted(root.glob("*.json")) if (state := read_json(path))]


def process_is_alive(pid: Any) -> bool:
    try:
        numeric = int(pid)
    except (TypeError, ValueError):
        return False
    if numeric <= 0:
        return False
    return psutil.pid_exists(numeric)


def recover_stale_states(audit_root: Path) -> int:
    recovered = 0
    for state in iter_series_states(audit_root):
        if state.get("stage") not in ACTIVE_SERIES_STATES:
            continue
        if process_is_alive(state.get("worker_pid")):
            continue
        update_series_state(
            audit_root,
            str(state["series_uid_hash"]),
            "interrupted",
            finished_at=utc_now(),
            message="worker PID is no longer alive; task is eligible for resume",
            child_pid=None,
        )
        recovered += 1
    return recovered


def resource_snapshot(config: ProjectConfig) -> dict[str, Any]:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    work_usage = shutil.disk_usage(_existing_parent(config.work_root))
    staging_probe = _existing_parent(config.paths.staging_bids_root)
    staging_usage = shutil.disk_usage(staging_probe)
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    network = psutil.net_io_counters()
    return {
        "timestamp": utc_now(),
        "hostname": socket.gethostname(),
        "logical_cpus": psutil.cpu_count(logical=True),
        "physical_cpus": psutil.cpu_count(logical=False),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "load_1m": load[0],
        "load_5m": load[1],
        "load_15m": load[2],
        "memory_available_gb": memory.available / 1024**3,
        "memory_used_gb": memory.used / 1024**3,
        "swap_used_gb": swap.used / 1024**3,
        "swap_in_bytes": swap.sin,
        "swap_out_bytes": swap.sout,
        "work_free_gb": work_usage.free / 1024**3,
        "staging_free_gb": staging_usage.free / 1024**3,
        "disk_read_bytes": psutil.disk_io_counters().read_bytes if psutil.disk_io_counters() else 0,
        "disk_write_bytes": psutil.disk_io_counters().write_bytes
        if psutil.disk_io_counters()
        else 0,
        "network_sent_bytes": network.bytes_sent,
        "network_received_bytes": network.bytes_recv,
        "nfs_write_bytes": _nfs_write_bytes(config.paths.staging_bids_root),
    }


def resource_blockers(config: ProjectConfig, snapshot: dict[str, Any]) -> list[str]:
    requirements = (
        (
            "work_free_gb",
            config.runtime.minimum_work_free_gb,
            "work filesystem free space",
        ),
        (
            "staging_free_gb",
            config.runtime.minimum_staging_free_gb,
            "staging filesystem free space",
        ),
        (
            "memory_available_gb",
            config.runtime.minimum_available_memory_gb,
            "available memory",
        ),
    )
    return [
        f"{label} {float(snapshot[key]):.1f} GiB is below {minimum:.1f} GiB"
        for key, minimum, label in requirements
        if minimum and float(snapshot[key]) < minimum
    ]


class ResourceSampler:
    def __init__(self, config: ProjectConfig) -> None:
        self.config = config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        path = self.config.paths.audit_root / "resource_usage.tsv"
        fields: list[str] | None = None
        previous: dict[str, Any] | None = None
        previous_clock: float | None = None
        while not self._stop.is_set():
            with suppress(OSError):
                row = resource_snapshot(self.config)
                current_clock = time.monotonic()
                elapsed = current_clock - previous_clock if previous_clock is not None else 0.0
                for counter, rate_name in (
                    ("disk_write_bytes", "disk_write_bytes_per_second"),
                    ("network_sent_bytes", "network_sent_bytes_per_second"),
                    ("nfs_write_bytes", "nfs_write_bytes_per_second"),
                ):
                    current = row.get(counter)
                    prior = previous.get(counter) if previous else None
                    row[rate_name] = (
                        (float(current) - float(prior)) / elapsed
                        if elapsed > 0 and current is not None and prior is not None
                        else 0.0
                    )
                if fields is None:
                    fields = list(row)
                path.parent.mkdir(parents=True, exist_ok=True)
                new_file = not path.exists()
                with path.open("a", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
                    if new_file:
                        writer.writeheader()
                    writer.writerow(row)
                previous = row
                previous_clock = current_clock
            self._stop.wait(self.config.runtime.resource_interval_seconds)


def update_run_state(config: ProjectConfig, stage: str, **values: Any) -> dict[str, Any]:
    path = config.paths.audit_root / "run_status.json"
    state = read_json(path)
    state.update(values)
    state.update({"stage": stage, "updated_at": utc_now()})
    state.setdefault("started_at", state["updated_at"])
    state["pid"] = os.getpid()
    atomic_write_json(path, state)
    return state


def status_snapshot(config: ProjectConfig, *, include_processes: bool = False) -> dict[str, Any]:
    run = read_json(config.paths.audit_root / "run_status.json")
    states = iter_series_states(config.paths.audit_root)
    raw_scope = run.get("series_uid_hashes")
    if isinstance(raw_scope, list):
        scoped_hashes = set(raw_scope)
        states = [state for state in states if state.get("series_uid_hash") in scoped_hashes]
    counts = Counter(str(state.get("stage", "unknown")) for state in states)
    completed = sum(counts[state] for state in TERMINAL_SERIES_STATES)
    total = int(run.get("total_series") or len(states))
    started = run.get("conversion_started_at") or run.get("started_at")
    elapsed = 0.0
    if started:
        with suppress(ValueError):
            elapsed = max(
                0.0,
                (datetime.now(UTC) - datetime.fromisoformat(str(started))).total_seconds(),
            )
    rate = completed / elapsed if elapsed > 0 else 0.0
    eta = (total - completed) / rate if rate > 0 and total > completed else None
    try:
        resources: dict[str, Any] = resource_snapshot(config)
    except OSError as exc:
        resources = {"error": str(exc), "timestamp": utc_now()}
    payload: dict[str, Any] = {
        "run": run,
        "series_counts": dict(sorted(counts.items())),
        "completed": completed,
        "total": total,
        "percent": round((100.0 * completed / total), 2) if total else 0.0,
        "series_per_minute": round(rate * 60, 3),
        "eta_seconds": round(eta, 1) if eta is not None else None,
        "resources": resources,
        "sampled_rates": _latest_resource_rates(config.paths.audit_root / "resource_usage.tsv"),
    }
    if include_processes:
        active = []
        for state in states:
            if state.get("stage") not in ACTIVE_SERIES_STATES:
                continue
            item = dict(state)
            for key in ("worker_pid", "child_pid"):
                pid = item.get(key)
                if not process_is_alive(pid):
                    continue
                with suppress(psutil.Error, ValueError, TypeError):
                    process = psutil.Process(int(pid))
                    item[f"{key}_cpu_percent"] = process.cpu_percent(interval=None)
                    item[f"{key}_rss_mb"] = process.memory_info().rss / 1024**2
                    item[f"{key}_command"] = " ".join(process.cmdline())
            active.append(item)
        payload["processes"] = active
    return payload


def _latest_resource_rates(path: Path) -> dict[str, float]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
    except OSError:
        return {}
    if not rows:
        return {}
    latest = rows[-1]
    names = (
        "disk_write_bytes_per_second",
        "network_sent_bytes_per_second",
        "nfs_write_bytes_per_second",
    )
    result: dict[str, float] = {}
    for name in names:
        with suppress(TypeError, ValueError):
            result[name] = float(latest.get(name, ""))
    return result


def _nfs_write_bytes(path: Path) -> int | None:
    if os.name != "posix":
        return None
    try:
        resolved = path.resolve(strict=False)
        partitions = [
            partition
            for partition in psutil.disk_partitions(all=True)
            if partition.fstype.lower().startswith("nfs")
            and (
                resolved == Path(partition.mountpoint)
                or Path(partition.mountpoint) in resolved.parents
            )
        ]
        if not partitions:
            return None
        mountpoint = max(partitions, key=lambda item: len(item.mountpoint)).mountpoint
        active = False
        for line in Path("/proc/self/mountstats").read_text(encoding="utf-8").splitlines():
            if line.startswith("device "):
                active = f" mounted on {mountpoint} " in line and " with fstype nfs" in line
            elif active and line.strip().startswith("bytes:"):
                values = [int(value) for value in line.split()[1:]]
                return values[1] + values[3] if len(values) >= 4 else None
    except (OSError, ValueError):
        return None
    return None


def _existing_parent(path: Path) -> Path:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe
