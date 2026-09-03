"""Persistent visual decisions and dataset writer exclusion; no image mutation here."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import psutil

from .config import ProjectConfig
from .models import SeriesRecord
from .orientation import classify_orientation
from .runtime import atomic_write_json, read_json, utc_now


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def candidate_id(record: SeriesRecord) -> str:
    identity = [record.center, record.subject_id, record.study_uid_hash, record.series_uid_hash]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]


def record_digest(record: SeriesRecord) -> str:
    values = record.private_dict()
    # The manual modality overlay is not a change to the source geometry or identity.
    values.pop("candidate_type", None)
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def qc_root(config: ProjectConfig) -> Path:
    return config.paths.audit_root / "visual_qc"


def enabled(config: ProjectConfig) -> bool:
    return (qc_root(config) / "enabled.json").is_file()


class BusyError(RuntimeError):
    pass


class ConflictError(ValueError):
    pass


_registry_lock = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}


@contextmanager
def _local_lock(lock):
    if not lock.acquire(blocking=False):
        raise BusyError("another QC operation is writing; wait for it to finish")
    try:
        yield
    finally:
        lock.release()


@contextmanager
def file_lock(path: Path):
    """OS lock: released on exit/crash; supports Linux/NFS advisory locks and Windows tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _registry_lock:
        local_lock = _thread_locks.setdefault(str(path.resolve()), threading.RLock())
    with _local_lock(local_lock), path.open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BusyError(f"another writer holds {path.name}") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def assert_pipeline_idle(config: ProjectConfig) -> None:
    pids = [read_json(config.paths.audit_root / "run_status.json").get("pid")]
    launch = config.paths.audit_root / "run.launch"
    if launch.is_file():
        for line in launch.read_text(encoding="utf-8").splitlines():
            if line.startswith("PID="):
                pids.append(line.partition("=")[2])
    for value in pids:
        try:
            pid = int(value)
        except (ValueError, TypeError):
            continue
        if pid != os.getpid() and pid > 0 and psutil.pid_exists(pid):
            raise BusyError(f"recorded pipeline PID {pid} is alive; finish or safely stop it first")
    # A stopped parent does not prove that its conversion children have exited.
    active = {"linking", "dcm2niix", "compressing", "validating", "installing", "running"}
    for path in (config.paths.audit_root / "status").glob("*.json"):
        state = read_json(path)
        if state.get("stage") not in active:
            continue
        for field in ("worker_pid", "child_pid"):
            pid = state.get(field)
            if isinstance(pid, int) and pid > 0 and pid != os.getpid() and psutil.pid_exists(pid):
                raise BusyError(f"pipeline {field} {pid} is still alive")


def writer_lock(config: ProjectConfig):
    return file_lock(config.paths.audit_root / ".bids-writer.lock")


def decision_path(root: Path, subject: str) -> Path:
    if not subject.isalnum():
        raise ValueError("invalid subject label")
    return root / "subjects" / f"{subject}.json"


def read_decision(root: Path, subject: str) -> dict[str, Any]:
    snapshot = read_json(decision_path(root, subject))
    history = root / "history" / subject
    events = sorted(history.glob("*.json")) if history.is_dir() else []
    if events:
        latest = read_json(events[-1])
        if latest.get("revision", 0) > snapshot.get("revision", 0):
            snapshot = latest
    return snapshot or {
        "subject_id": subject,
        "revision": 0,
        "reviewer": "zhenzong",
        "candidates": {},
        "groups": {},
        "episode_confirmed": False,
        "episode_reason": "",
    }


def save_decision(root: Path, subject: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Caller validates clinical choices under the same decisions lock."""
    prior = read_decision(root, subject)
    if payload.get("revision") != prior["revision"]:
        raise ConflictError("decision changed in another window; reload before saving")
    updated = dict(payload, subject_id=subject, revision=prior["revision"] + 1, saved_at=utc_now())
    event = root / "history" / subject / f"{updated['revision']:09d}.json"
    if event.exists():
        raise ConflictError("history revision already exists")
    atomic_write_json(event, updated)
    atomic_write_json(decision_path(root, subject), updated)
    return updated


def authorized_choice(
    config: ProjectConfig, record: SeriesRecord, image: Path | None = None
) -> bool:
    if not enabled(config):
        return True
    decision = read_decision(qc_root(config), record.subject_id)
    uid = candidate_id(record)
    rating = decision.get("candidates", {}).get(uid, {})
    modality = rating.get("modality") or record.candidate_type
    choice = decision.get("groups", {}).get(modality, {})
    plane = classify_orientation(record.image_orientation_patient, 20)
    valid = (
        rating.get("quality") == "pass"
        and choice.get("choice") == uid
        and not choice.get("none")
        and modality == record.candidate_type
        and rating.get("record_digest") == record_digest(record)
        and record.modality == "MR"
        and record.orientation_consistent
        and plane.normal is not None
        and (
            modality == "flair"
            or (
                modality == "t1"
                and plane.plane == "axial"
                and record.plane == "axial"
                and record.source_kind in {"original", "derived_mpr"}
            )
        )
    )
    if image is not None and valid:
        valid = rating.get("image_sha256") == digest(image)
    return bool(valid)


def applied_choice(config: ProjectConfig, record: SeriesRecord) -> bool:
    """Only already applied decisions may be treated as installed by ordinary resume."""
    if not enabled(config):
        return True
    decision = read_decision(qc_root(config), record.subject_id)
    cert = read_json(
        qc_root(config) / "certifications" / f"{record.subject_id}_{record.candidate_type}.json"
    )
    return bool(
        authorized_choice(config, record)
        and cert.get("status") == "installed"
        and cert.get("revision") == decision["revision"]
        and cert.get("candidate_id") == candidate_id(record)
    )


def overlay_records(config: ProjectConfig, records: list[SeriesRecord]) -> list[SeriesRecord]:
    if not enabled(config):
        return records
    decisions: dict[str, dict[str, Any]] = {}
    result = []
    for record in records:
        decision = decisions.setdefault(record.subject_id, {})
        if not decision:
            decision.update(read_decision(qc_root(config), record.subject_id))
        rating = decision.get("candidates", {}).get(candidate_id(record), {})
        modality = rating.get("modality") or record.candidate_type
        result.append(replace(record, candidate_type=modality))
    return result
