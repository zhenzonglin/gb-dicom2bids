"""Subject-level visual QC, isolated candidate conversion, and explicit reviewed installation."""

from __future__ import annotations

import csv
import shutil
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pydicom

from .bids import ensure_dataset_metadata, update_participants, update_scans
from .config import ProjectConfig
from .manifest import (
    CONVERSION_FIELDS,
    _write_tsv,
    load_private_records,
    load_selection,
    write_selection,
)
from .models import SelectionRow
from .orientation import classify_orientation
from .qc_images import VolumeCache
from .qc_state import (
    ConflictError,
    assert_pipeline_idle,
    candidate_id,
    digest,
    file_lock,
    qc_root,
    read_decision,
    record_digest,
    save_decision,
    writer_lock,
)
from .runtime import (
    atomic_write_json,
    read_json,
    resource_blockers,
    resource_snapshot,
    series_state_path,
    update_series_state,
    utc_now,
)


class ReviewService:
    def __init__(self, config: ProjectConfig, workers: int = 2, *, activate: bool = True) -> None:
        if not 1 <= workers <= 16:
            raise ValueError("preview workers must be between 1 and 16")
        self.config = config
        self.root = qc_root(config)
        if activate:
            self.root.mkdir(parents=True, exist_ok=True)
            if not (self.root / "enabled.json").exists():
                with writer_lock(config):
                    assert_pipeline_idle(config)
                    atomic_write_json(
                        self.root / "enabled.json", {"enabled_at": utc_now(), "version": 1}
                    )
        records = load_private_records(config.paths.audit_root)
        self.records = {candidate_id(record): record for record in records}
        if len(records) != len(self.records):
            raise ValueError("duplicate candidate identities")
        self.by_subject: dict[str, list[str]] = defaultdict(list)
        for uid, record in self.records.items():
            self.by_subject[record.subject_id].append(uid)
        current = config.paths.audit_root / "selection_manifest.tsv"
        baseline = self.root / "baseline_selection.tsv"
        if activate and not baseline.exists():
            with file_lock(self.root / ".decisions.lock"):
                if not baseline.exists():
                    shutil.copy2(current, baseline)
        self.baseline = load_selection(baseline if baseline.exists() else current)
        self.selection = {
            (row.subject_id, row.study_uid_hash, row.series_uid_hash): row for row in self.baseline
        }
        self.record_keys = {(r.subject_id, r.study_uid_hash, r.series_uid_hash): r for r in records}
        saved_subjects = {p.stem for p in (self.root / "subjects").glob("*.json")}
        saved_subjects |= {p.name for p in (self.root / "history").glob("*") if p.is_dir()}
        self.decisions = {s: read_decision(self.root, s) for s in saved_subjects}
        self.pilot = set()
        pilot = config.paths.audit_root / "pilot_manifest.tsv"
        if pilot.is_file():
            with pilot.open(encoding="utf-8", newline="") as handle:
                self.pilot = {row["subject_id"] for row in csv.DictReader(handle, delimiter="\t")}
        self.volumes = VolumeCache()
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="qc-preview")
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self._times: dict[str, dict[str, str]] = {}
        self._verified: dict[str, tuple] = {}

    def close(self) -> None:
        self.stop.set()
        self.executor.shutdown(wait=True, cancel_futures=True)

    def require_uid(self, uid: str):
        if uid not in self.records:
            raise ValueError("unknown candidate")
        return self.records[uid]

    def timing(self, uid: str) -> dict[str, str]:
        if uid in self._times:
            return self._times[uid]
        record = self.require_uid(uid)
        if self.config.nifti_import.enabled:
            result = {"note": "NIfTI-only source; acquisition date/time unavailable"}
            self._times[uid] = result
            return result
        private_path = self.root / "timings" / f"{uid}.json"
        saved = read_json(private_path)
        if saved.get("record_digest") == record_digest(record):
            self._times[uid] = saved["timing"]
            return saved["timing"]
        fields = [
            "StudyDate",
            "StudyTime",
            "AcquisitionDate",
            "AcquisitionTime",
            "AcquisitionDateTime",
        ]
        result: dict[str, str] = {}
        try:
            path = (self.config.paths.dicom_root / record.source_relpaths[0]).resolve()
            path.relative_to(self.config.paths.dicom_root.resolve())
            dataset = pydicom.dcmread(
                path, stop_before_pixels=True, force=True, specific_tags=fields
            )
            result = {key: str(getattr(dataset, key, "")) for key in fields}
        except (OSError, ValueError, IndexError) as exc:
            result["error"] = str(exc)
        self._times[uid] = result
        with self.lock:
            atomic_write_json(
                private_path, {"record_digest": record_digest(record), "timing": result}
            )
        return result

    def list_subjects(self, filters: dict[str, str]) -> dict[str, Any]:
        result = []
        query = filters.get("q", "").lower()
        for subject, uids in sorted(self.by_subject.items()):
            records = [self.records[uid] for uid in uids]
            if filters.get("pilot") == "1" and subject not in self.pilot:
                continue
            decision = self.decisions.get(subject, {})
            ratings = decision.get("candidates", {})
            counts = Counter(
                ratings.get(uid, {}).get("modality") or self.records[uid].candidate_type
                for uid in uids
            )
            if not counts["t1"] and not counts["flair"] and filters.get("others") != "1":
                continue
            candidate_match = False
            for record in records:
                row = self.selection.get((subject, record.study_uid_hash, record.series_uid_hash))
                text = " ".join([subject, record.series_description, record.protocol_name]).lower()
                if query and query not in text:
                    continue
                if filters.get("center") and filters["center"].lower() not in record.center.lower():
                    continue
                if (
                    filters.get("protocol")
                    and filters["protocol"].lower() not in record.protocol_id.lower()
                ):
                    continue
                if filters.get("status") and (not row or row.decision_status != filters["status"]):
                    continue
                if filters.get("reason") and (
                    not row or filters["reason"].lower() not in row.reason.lower()
                ):
                    continue
                candidate_match = True
                break
            if not candidate_match:
                continue
            resolved = sum(
                bool(group.get("choice") or group.get("none"))
                for group in decision.get("groups", {}).values()
            )
            reviewed = sum(
                rating.get("quality", "unreviewed") != "unreviewed" for rating in ratings.values()
            )
            if filters.get("pending") == "1" and resolved >= sum(
                counts[k] > 0 for k in ("t1", "flair")
            ):
                continue
            result.append(
                {
                    "id": subject,
                    "center": records[0].center,
                    "t1": counts["t1"],
                    "flair": counts["flair"],
                    "other": counts["other"],
                    "reviewed": reviewed,
                    "resolved": resolved,
                    "pilot": subject in self.pilot,
                }
            )
        offset = max(0, int(filters.get("offset", 0)))
        return {"total": len(result), "subjects": result[offset : offset + 100]}

    def subject(self, subject: str) -> dict[str, Any]:
        if subject not in self.by_subject:
            raise ValueError("unknown subject")
        decision = read_decision(self.root, subject)
        candidates = []
        for uid in self.by_subject[subject]:
            record = self.records[uid]
            row = self.selection.get((subject, record.study_uid_hash, record.series_uid_hash))
            item = record.public_dict()
            item.update(
                {
                    "id": uid,
                    "auto_status": row.decision_status if row else "excluded",
                    "auto_reason": row.reason if row else "unclassified",
                    "score": row.score if row else None,
                    "timing": self.timing(uid),
                }
            )
            candidates.append(item)
        return {"subject": subject, "decision": decision, "candidates": candidates}

    def artifact(self, uid: str, *, deep: bool = False) -> dict[str, Any]:
        with self.lock:
            return self._artifact(uid, deep=deep)

    def _artifact(self, uid: str, *, deep: bool = False) -> dict[str, Any]:
        record = self.require_uid(uid)
        path = self.root / "artifacts" / f"{uid}.json"
        value = read_json(path)
        if not value:
            previous = read_json(
                series_state_path(self.config.paths.audit_root, record.series_uid_hash)
            )
            if (
                previous.get("subject_id") == record.subject_id
                and previous.get("series_uid_hash") == record.series_uid_hash
                and previous.get("stage") in {"converted", "review_ready"}
            ):
                value = {
                    "image": previous.get("output_path"),
                    "sidecar": previous.get("sidecar_path"),
                    "image_sha256": previous.get("output_sha256"),
                    "sidecar_sha256": previous.get("sidecar_sha256"),
                    "record_digest": record_digest(record),
                    "id": uid,
                    "log": previous.get("log_path", ""),
                }
        if not value or value.get("record_digest") != record_digest(record):
            return {}
        try:
            image = Path(value["image"])
            sidecar = Path(value["sidecar"])
            allowed = [
                self.config.paths.audit_root.resolve(),
                self.config.paths.staging_bids_root.resolve(),
            ]
            if self.config.nifti_import.source_root is not None:
                allowed.append(self.config.nifti_import.source_root.resolve())
            if not all(
                any(file.resolve().is_relative_to(root) for root in allowed)
                for file in (image, sidecar)
            ):
                return {}
            stats = (
                image.stat().st_size,
                image.stat().st_mtime_ns,
                sidecar.stat().st_size,
                sidecar.stat().st_mtime_ns,
            )
            signature = (
                str(image),
                str(sidecar),
                *stats,
                value.get("image_sha256"),
                value.get("sidecar_sha256"),
            )
            if deep or self._verified.get(uid) != signature:
                if digest(image) != value.get("image_sha256") or digest(sidecar) != value.get(
                    "sidecar_sha256"
                ):
                    return {}
                self._verified[uid] = signature
            metadata = self.volumes.metadata(image)
            value["metadata"] = metadata
            if image.resolve().is_relative_to(self.config.paths.staging_bids_root.resolve()):
                # Freeze reviewed provenance before a later selection overwrites this BIDS path.
                freeze = self.root / "frozen" / uid
                freeze.mkdir(parents=True, exist_ok=True)
                frozen_image, frozen_json = freeze / "candidate.nii.gz", freeze / "candidate.json"
                if not frozen_image.exists() or digest(frozen_image) != value["image_sha256"]:
                    shutil.copy2(image, frozen_image)
                if not frozen_json.exists() or digest(frozen_json) != value["sidecar_sha256"]:
                    shutil.copy2(sidecar, frozen_json)
                value.update(image=str(frozen_image), sidecar=str(frozen_json))
                atomic_write_json(path, value)
            if not path.exists():
                atomic_write_json(path, value)
            return value
        except (OSError, TypeError, KeyError, ValueError):
            return {}

    def prepare(self, uid: str, retry: bool = False) -> dict[str, Any]:
        self.require_uid(uid)
        artifact = self.artifact(uid)
        if artifact:
            return {"state": "ready", "metadata": artifact["metadata"], "log": self.log_text(uid)}
        with self.lock:
            if uid in self.jobs and (not retry or self.jobs[uid]["state"] != "failed"):
                return dict(self.jobs[uid])
            if (
                sum(
                    job["state"] in {"queued", "converting", "paused_resources"}
                    for job in self.jobs.values()
                )
                >= 32
            ):
                raise ValueError("preview queue is full; wait for existing jobs")
            self.jobs[uid] = {"state": "queued"}
            self.executor.submit(self._prepare_job, uid)
            return dict(self.jobs[uid])

    def _prepare_job(self, uid: str) -> None:
        from .convert import _convert_one

        record = self.require_uid(uid)
        try:
            while not self.stop.is_set():
                try:
                    blockers = resource_blockers(self.config, resource_snapshot(self.config))
                except OSError as exc:
                    blockers = [str(exc)]
                if not blockers:
                    break
                self.jobs[uid] = {"state": "paused_resources", "error": "; ".join(blockers)}
                self.stop.wait(5)
            if self.stop.is_set():
                self.jobs[uid] = {"state": "interrupted"}
                return
            self.jobs[uid] = {"state": "converting"}
            if self.config.nifti_import.enabled:
                source_root = self.config.nifti_import.source_root
                if source_root is None or len(record.source_relpaths) != 1:
                    raise ValueError("invalid NIfTI source record")
                image = (source_root / record.source_relpaths[0]).resolve()
                image.relative_to(source_root.resolve())
                self.volumes.metadata(image)
                sidecar = self.root / "sidecars" / f"{uid}.json"
                atomic_write_json(
                    sidecar,
                    {
                        "SourceFormat": "preconverted_nifti",
                        "SourceMetadataAvailable": False,
                    },
                )
                value = {
                    "id": uid,
                    "record_digest": record_digest(record),
                    "image": str(image),
                    "sidecar": str(sidecar),
                    "image_sha256": digest(image),
                    "sidecar_sha256": digest(sidecar),
                    "log": "",
                    "created_at": utc_now(),
                }
                with self.lock:
                    atomic_write_json(self.root / "artifacts" / f"{uid}.json", value)
                self.jobs[uid] = {"state": "ready"}
                return
            isolated = replace(
                self.config,
                paths=replace(
                    self.config.paths,
                    audit_root=self.root / "jobs" / uid,
                    work_root=self.config.work_root / "visual-qc",
                ),
                conversion=replace(
                    self.config.conversion, compression="pigz", compression_threads=2
                ),
            )
            row = SelectionRow(
                record.center,
                record.subject_id,
                record.study_uid_hash,
                record.series_uid_hash,
                record.candidate_type,
                "review",
                0,
                "visual_qc_preview",
                record.plane,
                record.source_kind,
                record.protocol_id,
            )
            result, _ = _convert_one(isolated, record, row, preview_only=True)
            value = {
                "id": uid,
                "record_digest": record_digest(record),
                "image": result.output_path,
                "sidecar": result.sidecar_path,
                "image_sha256": result.output_sha256,
                "sidecar_sha256": result.sidecar_sha256,
                "log": result.log_path,
                "created_at": utc_now(),
            }
            with self.lock:
                atomic_write_json(self.root / "artifacts" / f"{uid}.json", value)
            self.jobs[uid] = {"state": "ready"}
        except Exception as exc:
            self.jobs[uid] = {"state": "failed", "error": str(exc)}
            atomic_write_json(self.root / "errors" / f"{uid}.json", self.jobs[uid])

    def log_text(self, uid: str) -> str:
        self.require_uid(uid)
        files = sorted((self.root / "jobs" / uid / "logs").glob("*.log"))
        result = []
        for path in files[-3:]:
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 8000))
                result.append(path.name + "\n" + handle.read().decode("utf-8", errors="replace"))
        error = read_json(self.root / "errors" / f"{uid}.json")
        return (str(error.get("error", "")) + "\n" + "\n".join(result)).strip()

    def validate_decision(self, subject: str, raw: dict[str, Any]) -> dict[str, Any]:
        from .convert import _validate_converted_pair

        if subject not in self.by_subject:
            raise ValueError("unknown subject")
        if not isinstance(raw, dict):
            raise ValueError("decision must be an object")
        if not isinstance(raw.get("revision"), int) or raw["revision"] < 0:
            raise ValueError("invalid revision")
        reviewer = str(raw.get("reviewer", "")).strip()
        if not reviewer:
            raise ValueError("reviewer is required")
        clean = {
            "revision": raw["revision"],
            "reviewer": reviewer,
            "candidates": {},
            "groups": {},
            "episode_confirmed": raw.get("episode_confirmed") is True,
            "episode_reason": str(raw.get("episode_reason", "")).strip(),
        }
        ratings = raw.get("candidates", {})
        groups = raw.get("groups", {})
        if not isinstance(ratings, dict) or not isinstance(groups, dict):
            raise ValueError("invalid decision structure")
        groups = {"t1": {}, "flair": {}, **groups}
        for uid, rating in ratings.items():
            record = self.require_uid(uid)
            if record.subject_id != subject or not isinstance(rating, dict):
                raise ValueError("candidate belongs to another subject")
            quality = rating.get("quality", "unreviewed")
            modality = rating.get("modality") or record.candidate_type
            reason = str(rating.get("reason", "")).strip()
            if quality not in {"unreviewed", "pass", "fail", "defer"} or modality not in {
                "t1",
                "flair",
                "other",
            }:
                raise ValueError("invalid quality or modality")
            if (quality == "fail" or modality != record.candidate_type) and not reason:
                raise ValueError("failure and reclassification require a reason")
            item = {
                "quality": quality,
                "modality": modality,
                "reason": reason,
                "record_digest": record_digest(record),
            }
            if quality == "pass":
                artifact = self.artifact(uid, deep=True)
                if not artifact or artifact["metadata"]["errors"]:
                    raise ValueError("prepare a readable, spatially valid image before approval")
                item.update(
                    {
                        "image_sha256": artifact["image_sha256"],
                        "sidecar_sha256": artifact["sidecar_sha256"],
                    }
                )
            clean["candidates"][uid] = item
        selected = []
        for modality, group in groups.items():
            if modality not in {"t1", "flair"} or not isinstance(group, dict):
                raise ValueError("invalid modality group")
            uid = group.get("choice") or None
            none = group.get("none") is True
            reason = str(group.get("reason", "")).strip()
            if uid and none:
                raise ValueError("choose one candidate OR no usable candidate")
            if none and not reason:
                raise ValueError("no usable candidate requires a reason")
            if uid:
                record = self.require_uid(uid)
                rating = clean["candidates"].get(uid, {})
                if (
                    record.subject_id != subject
                    or rating.get("quality") != "pass"
                    or rating.get("modality") != modality
                ):
                    raise ValueError(
                        "final choice must be an approved candidate of this subject/modality"
                    )
                if record.modality != "MR":
                    raise ValueError("final choice must be an MR image")
                if not self.config.nifti_import.enabled:
                    source_plane = classify_orientation(record.image_orientation_patient, 20)
                    if not record.orientation_consistent or source_plane.normal is None:
                        raise ValueError("final choice requires reliable source geometry")
                    if modality == "t1" and (
                        source_plane.plane != "axial"
                        or record.plane != "axial"
                        or record.source_kind not in {"original", "derived_mpr"}
                    ):
                        raise ValueError(
                            "T1 must be original axial or axial MPR; sagittal fallback is disabled"
                        )
                artifact = self.artifact(uid)
                _validate_converted_pair(
                    Path(artifact["image"]),
                    Path(artifact["sidecar"]),
                    replace(record, candidate_type=modality),
                )
                selected.append(uid)
            clean["groups"][modality] = {"choice": uid, "none": none, "reason": reason}
        studies = {self.records[uid].study_uid_hash for uid in selected}
        dates = {
            self.timing(uid).get("AcquisitionDate") or self.timing(uid).get("StudyDate")
            for uid in selected
        }
        dates.discard(None)
        dates.discard("")
        if (len(studies) > 1 or len(dates) > 1) and (
            not clean["episode_confirmed"] or not clean["episode_reason"]
        ):
            raise ValueError(
                "confirm these repeated scans belong to the same examination and give a reason"
            )
        return clean

    def save(self, subject: str, raw: dict[str, Any]) -> dict[str, Any]:
        with file_lock(self.root / ".decisions.lock"):
            clean = self.validate_decision(subject, raw)
            updated = save_decision(self.root, subject, clean)
            self.decisions[subject] = updated
            self.write_accepted()
            return updated

    def write_accepted(self) -> list[dict[str, Any]]:
        rows = []
        for path in sorted((self.root / "certifications").glob("*.json")):
            cert = read_json(path)
            decision = read_decision(self.root, cert["subject_id"])
            if cert.get("revision") != decision["revision"] or cert.get("status") != "installed":
                continue
            transaction = (
                self.root
                / "transactions"
                / f"{cert['subject_id']}_{cert['modality']}_{cert['revision']}.json"
            )
            if read_json(transaction).get("state") != "completed":
                continue
            try:
                image, sidecar = Path(cert["image"]), Path(cert["sidecar"])
                if [
                    image.stat().st_size,
                    image.stat().st_mtime_ns,
                    sidecar.stat().st_size,
                    sidecar.stat().st_mtime_ns,
                ] != cert["file_stats"]:
                    continue
            except OSError:
                continue
            rows.append(cert)
        fields = [
            "subject_id",
            "modality",
            "candidate_id",
            "revision",
            "image",
            "sidecar",
            "image_sha256",
            "sidecar_sha256",
            "reviewer",
            "verified_at",
        ]
        _write_tsv(self.root / "accepted_manifest.tsv", rows, fields)
        return rows

    def apply(self, *, dry_run: bool = True) -> list[dict[str, Any]]:
        from .convert import _install_selected, _validate_converted_pair, _write_diff

        actions = []
        with writer_lock(self.config), file_lock(self.root / ".decisions.lock"):
            assert_pipeline_idle(self.config)
            seed = read_json(self.config.paths.audit_root / "staging_seed.json")
            if self.config.conversion.seed_from_existing_bids and seed.get("status") != "completed":
                raise ValueError(
                    "staging copy is not complete; QC apply never restarts or skips the copy"
                )
            decisions = {}
            saved_subjects = {p.stem for p in (self.root / "subjects").glob("*.json")}
            saved_subjects |= {p.name for p in (self.root / "history").glob("*") if p.is_dir()}
            for subject in sorted(saved_subjects):
                decision = read_decision(self.root, subject)
                if not decision["revision"]:
                    continue
                clean = self.validate_decision(subject, decision)
                for uid, rating in clean["candidates"].items():
                    prior = decision["candidates"][uid]
                    if rating.get("record_digest") != prior.get("record_digest") or (
                        rating["quality"] == "pass"
                        and (
                            rating.get("image_sha256") != prior.get("image_sha256")
                            or rating.get("sidecar_sha256") != prior.get("sidecar_sha256")
                        )
                    ):
                        raise ConflictError("reviewed artifact or inventory changed; re-review it")
                decisions[subject] = decision
                for modality, group in decision.get("groups", {}).items():
                    prior_cert = read_json(
                        self.root / "certifications" / f"{subject}_{modality}.json"
                    )
                    if (
                        group.get("choice")
                        or group.get("none")
                        or prior_cert.get("status") in {"installed", "applying"}
                    ):
                        action = {
                            "subject_id": subject,
                            "modality": modality,
                            "action": "install" if group.get("choice") else "quarantine",
                            "candidate_id": group.get("choice"),
                            "revision": decision["revision"],
                        }
                        if not self._already_applied(action, prior_cert):
                            actions.append(action)
            if dry_run:
                return actions
            if not actions:
                self.coverage_report()
                return actions
            blockers = resource_blockers(self.config, resource_snapshot(self.config))
            if blockers:
                raise ValueError("apply paused by resource guard: " + "; ".join(blockers))
            for name in (
                "dataset_description.json",
                "participants.tsv",
                "participants.json",
                "README",
            ):
                if (self.config.paths.staging_bids_root / name).is_symlink():
                    raise ValueError("refusing to write symlinked BIDS metadata")
            ensure_dataset_metadata(self.config)
            live_rows = load_selection(self.config.paths.audit_root / "selection_manifest.tsv")
            effective = {
                uid: replace(
                    record,
                    candidate_type=decisions.get(record.subject_id, {})
                    .get("candidates", {})
                    .get(uid, {})
                    .get("modality")
                    or record.candidate_type,
                )
                for uid, record in self.records.items()
            }
            rows_by_group = defaultdict(list)
            for row in live_rows:
                original = self.record_keys.get(
                    (row.subject_id, row.study_uid_hash, row.series_uid_hash)
                )
                if original is not None:
                    rid = candidate_id(original)
                    rows_by_group[(original.subject_id, effective[rid].candidate_type)].append(
                        (row, rid)
                    )
            if not self.config.nifti_import.enabled:
                update_participants(self.config, list(effective.values()))
            pending = []
            for action in actions:
                cert_path = (
                    self.root
                    / "certifications"
                    / f"{action['subject_id']}_{action['modality']}.json"
                )
                atomic_write_json(cert_path, dict(action, status="applying"))
            self.write_accepted()
            for action in actions:
                subject, modality, uid = (
                    action["subject_id"],
                    action["modality"],
                    action["candidate_id"],
                )
                decision = decisions[subject]
                transaction = (
                    self.root / "transactions" / f"{subject}_{modality}_{decision['revision']}.json"
                )
                previous = read_json(transaction)
                atomic_write_json(
                    transaction,
                    dict(
                        action, state="started", at=utc_now(), previous_state=previous.get("state")
                    ),
                )
                try:
                    blockers = resource_blockers(self.config, resource_snapshot(self.config))
                    if blockers:
                        raise ValueError("apply paused by resource guard: " + "; ".join(blockers))
                    self._backup_group(subject, modality, decision["revision"], remove=not uid)
                    if uid:
                        record = effective[uid]
                        artifact = self.artifact(uid, deep=True)
                        image, sidecar = Path(artifact["image"]), Path(artifact["sidecar"])
                        _validate_converted_pair(image, sidecar, record)
                        manual = (
                            "accept_preconverted"
                            if self.config.nifti_import.enabled
                            else (
                                "accept_flair"
                                if modality == "flair"
                                else (
                                    "accept_mpr"
                                    if record.source_kind == "derived_mpr"
                                    else "accept_original"
                                )
                            )
                        )
                        suffix = "FLAIR" if modality == "flair" else "T1w"
                        entity = "_rec-axialmpr" if manual == "accept_mpr" else ""
                        selected = SelectionRow(
                            record.center,
                            subject,
                            record.study_uid_hash,
                            record.series_uid_hash,
                            modality,
                            "selected",
                            0,
                            "visual_qc_approved",
                            record.plane,
                            record.source_kind,
                            record.protocol_id,
                            f"sub-{subject}{entity}_{suffix}",
                            decision["reviewer"],
                            manual,
                        )
                        installed, diffs = _install_selected(
                            self.config, record, selected, image, sidecar, visual_apply=True
                        )
                        target_json = installed.with_name(
                            installed.name.removesuffix(".nii.gz") + ".json"
                        )
                        if (
                            digest(installed) != artifact["image_sha256"]
                            or digest(target_json) != artifact["sidecar_sha256"]
                        ):
                            raise ValueError("installed checksum mismatch")
                        update_scans(self.config, record, selected, installed)
                        cert = dict(
                            action,
                            status="installed",
                            image=str(installed),
                            sidecar=str(target_json),
                            image_sha256=artifact["image_sha256"],
                            sidecar_sha256=artifact["sidecar_sha256"],
                            reviewer=decision["reviewer"],
                            verified_at=utc_now(),
                            file_stats=[
                                installed.stat().st_size,
                                installed.stat().st_mtime_ns,
                                target_json.stat().st_size,
                                target_json.stat().st_mtime_ns,
                            ],
                        )
                    else:
                        selected = None
                        diffs = [
                            {
                                "action": "quarantined",
                                "reason": decision["groups"][modality]["reason"],
                            }
                        ]
                        cert = dict(
                            action, status="no_usable_candidate", reviewer=decision["reviewer"]
                        )
                    for row, rid in rows_by_group[(subject, modality)]:
                        row.candidate_type = modality
                        unresolved = not uid and not decision["groups"][modality].get("none")
                        row.decision_status = (
                            "selected" if rid == uid else ("review" if unresolved else "excluded")
                        )
                        row.manual_decision = (
                            selected.manual_decision
                            if rid == uid
                            else ("" if unresolved else "exclude")
                        )
                        row.reviewer = decision["reviewer"]
                        row.reason = "visual_qc_approved" if rid == uid else "visual_qc_not_chosen"
                        row.output_basename = selected.output_basename if rid == uid else ""
                    atomic_write_json(
                        transaction,
                        dict(
                            action,
                            state="files_ready",
                            changes=diffs,
                            certificate=cert,
                            at=utc_now(),
                        ),
                    )
                    pending.append((action, transaction, cert, diffs))
                except Exception as exc:
                    atomic_write_json(
                        transaction, dict(action, state="failed", error=str(exc), at=utc_now())
                    )
                    self.write_accepted()
                    raise
            if self.config.nifti_import.enabled:
                installed_subjects = {
                    path.parent.parent.name.removeprefix("sub-")
                    for path in self.config.paths.staging_bids_root.glob(
                        "sub-*/anat/*.nii.gz"
                    )
                }
                update_participants(
                    self.config,
                    [
                        record
                        for record in effective.values()
                        if record.subject_id in installed_subjects
                    ],
                    replace_existing=True,
                )
            # Write large cohort manifests once, not once per subject. Incomplete transactions
            # have no installed certificate and are replayed safely after an interruption.
            write_selection(self.config.paths.audit_root, live_rows, list(effective.values()))
            diff_name = (
                "nifti_to_bids_diff.tsv"
                if self.config.nifti_import.enabled
                else "old_vs_v4_diff.tsv"
            )
            _write_diff(
                self.config.paths.audit_root / diff_name,
                [diff for _, _, _, changes in pending for diff in changes],
            )
            for action, _transaction, cert, _diffs in pending:
                if action["candidate_id"]:
                    record = effective[action["candidate_id"]]
                    update_series_state(
                        self.config.paths.audit_root,
                        record.series_uid_hash,
                        "converted",
                        subject_id=record.subject_id,
                        candidate_type=record.candidate_type,
                        output_path=cert["image"],
                        sidecar_path=cert["sidecar"],
                        output_sha256=cert["image_sha256"],
                        sidecar_sha256=cert["sidecar_sha256"],
                        visual_qc_revision=cert["revision"],
                        finished_at=utc_now(),
                        child_pid=None,
                    )
            status_path = self.config.paths.audit_root / "conversion_status.tsv"
            status_rows = {}
            if status_path.is_file():
                with status_path.open(encoding="utf-8", newline="") as handle:
                    status_rows = {
                        (r["subject_id"], r["series_uid_hash"]): r
                        for r in csv.DictReader(handle, delimiter="\t")
                    }
            for action, _, cert, _ in pending:
                if action["candidate_id"]:
                    record = effective[action["candidate_id"]]
                    status_rows[(record.subject_id, record.series_uid_hash)] = {
                        "subject_id": record.subject_id,
                        "series_uid_hash": record.series_uid_hash,
                        "candidate_type": record.candidate_type,
                        "status": "converted",
                        "mode": (
                            "preconverted_nifti_visual_qc_apply"
                            if self.config.nifti_import.enabled
                            else "visual_qc_apply"
                        ),
                        "output_path": cert["image"],
                        "sidecar_path": cert["sidecar"],
                        "output_sha256": cert["image_sha256"],
                        "sidecar_sha256": cert["sidecar_sha256"],
                        "finished_at": utc_now(),
                    }
            _write_tsv(status_path, status_rows.values(), CONVERSION_FIELDS)
            for action, transaction, cert, diffs in pending:
                atomic_write_json(
                    self.root
                    / "certifications"
                    / f"{action['subject_id']}_{action['modality']}.json",
                    cert,
                )
                atomic_write_json(
                    transaction, dict(action, state="completed", changes=diffs, at=utc_now())
                )
            self.coverage_report()
        return actions

    def _already_applied(self, action: dict[str, Any], cert: dict[str, Any]) -> bool:
        if (
            cert.get("revision") != action["revision"]
            or cert.get("candidate_id") != action["candidate_id"]
        ):
            return False
        transaction = (
            self.root
            / "transactions"
            / f"{action['subject_id']}_{action['modality']}_{action['revision']}.json"
        )
        if read_json(transaction).get("state") != "completed":
            return False
        suffix = "T1w" if action["modality"] == "t1" else "FLAIR"
        anat = self.config.paths.staging_bids_root / f"sub-{action['subject_id']}" / "anat"
        if action["action"] == "quarantine":
            return not list(anat.glob(f"*{suffix}.nii*")) and not list(anat.glob(f"*{suffix}.json"))
        if cert.get("status") != "installed":
            return False
        try:
            return (
                digest(Path(cert["image"])) == cert["image_sha256"]
                and digest(Path(cert["sidecar"])) == cert["sidecar_sha256"]
                and len(list(anat.glob(f"*{suffix}.nii*"))) == 1
            )
        except (KeyError, OSError):
            return False

    def _backup_group(self, subject: str, modality: str, revision: int, *, remove: bool) -> None:
        suffix = "T1w" if modality == "t1" else "FLAIR"
        anat = self.config.paths.staging_bids_root / f"sub-{subject}" / "anat"
        if not anat.resolve().is_relative_to(self.config.paths.staging_bids_root.resolve()):
            raise ValueError("staging path escapes dataset")
        backup = self.root / "backups" / subject / f"{revision}_{modality}"
        files = sorted(anat.glob(f"*{suffix}.nii*")) + sorted(anat.glob(f"*{suffix}.json"))
        for path in files:
            if path.is_symlink():
                raise ValueError("refusing to mutate symlinked anatomical output")
            backup.mkdir(parents=True, exist_ok=True)
            destination = backup / path.name
            if not destination.exists():
                shutil.copy2(path, destination)
            if remove:
                if digest(path) != digest(destination):
                    raise ValueError("quarantine backup mismatch")
                path.unlink()
        scans = anat.parent / f"sub-{subject}_scans.tsv"
        if scans.is_symlink() or scans.with_suffix(".json").is_symlink():
            raise ValueError("refusing to write symlinked scans metadata")
        if remove and scans.exists():
            backup.mkdir(parents=True, exist_ok=True)
            if not (backup / scans.name).exists():
                shutil.copy2(scans, backup / scans.name)
            with scans.open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle, delimiter="\t")
                fields = list(reader.fieldnames or [])
                rows = [
                    row
                    for row in reader
                    if not (
                        row.get("filename", "").startswith("anat/")
                        and row.get("filename", "").endswith((suffix + ".nii", suffix + ".nii.gz"))
                    )
                ]
            _write_tsv(scans, rows, fields)

    def coverage_report(self) -> None:
        accepted = {(row["subject_id"], row["modality"]): row for row in self.write_accepted()}
        old = self.config.paths.existing_bids_root
        staging = self.config.paths.staging_bids_root
        subjects = set(self.by_subject) | {
            p.name[4:] for p in staging.glob("sub-*") if p.is_dir()
        }
        if not self.config.nifti_import.enabled:
            subjects |= {p.name[4:] for p in old.glob("sub-*") if p.is_dir()}
        rows = []
        for subject in sorted(subjects):
            decision = read_decision(self.root, subject)
            for modality, suffix in (("t1", "T1w"), ("flair", "FLAIR")):
                candidates = [
                    uid
                    for uid in self.by_subject.get(subject, [])
                    if (
                        decision.get("candidates", {}).get(uid, {}).get("modality")
                        or self.records[uid].candidate_type
                    )
                    == modality
                ]
                old_files = list((old / f"sub-{subject}" / "anat").glob(f"*{suffix}.nii*"))
                new_files = list((staging / f"sub-{subject}" / "anat").glob(f"*{suffix}.nii*"))
                cert = accepted.get((subject, modality))
                group = decision.get("groups", {}).get(modality, {})
                reason = (
                    ""
                    if cert
                    else (
                        "no_usable_candidate"
                        if group.get("none")
                        else (
                            "awaiting_apply_or_revalidation"
                            if group.get("choice")
                            else (
                                "no_candidate_classified"
                                if not candidates
                                else "awaiting_visual_qc"
                            )
                        )
                    )
                )
                rows.append(
                    {
                        "subject_id": subject,
                        "modality": modality,
                        (
                            "source_candidates"
                            if self.config.nifti_import.enabled
                            else "dicom_candidates"
                        ): len(candidates),
                        "old_bids_files": (
                            "not_used" if self.config.nifti_import.enabled else len(old_files)
                        ),
                        "staging_files": len(new_files),
                        "certified": bool(cert),
                        "newly_added": bool(cert and not old_files),
                        "reason": reason,
                        "qc_status": "certified" if cert else "uncertified",
                        "review_reason": group.get("reason", ""),
                    }
                )
        _write_tsv(self.root / "coverage.tsv", rows, list(rows[0]) if rows else ["subject_id"])
