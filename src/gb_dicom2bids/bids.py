from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from .config import ProjectConfig
from .models import SelectionRow, SeriesRecord


def ensure_dataset_metadata(config: ProjectConfig) -> None:
    root = config.paths.staging_bids_root
    root.mkdir(parents=True, exist_ok=True)
    description_path = root / "dataset_description.json"
    description: dict[str, Any] = {}
    if description_path.exists():
        description = json.loads(description_path.read_text(encoding="utf-8"))
    description.update(
        {
            "Name": config.dataset.name,
            "BIDSVersion": config.dataset.bids_version,
            "DatasetType": "raw",
        }
    )
    description_path.write_text(
        json.dumps(description, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    readme = root / "README"
    if not readme.exists():
        readme.write_text(
            "Multicenter structural MRI dataset curated from DICOM.\n"
            "Conversion decisions and protected provenance are stored outside this BIDS root.\n",
            encoding="utf-8",
        )


def update_participants(config: ProjectConfig, records: list[SeriesRecord]) -> None:
    root = config.paths.staging_bids_root
    path = root / "participants.tsv"
    existing: dict[str, dict[str, str]] = {}
    fields: list[str] = ["participant_id"]
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            fields = list(reader.fieldnames or fields)
            for row in reader:
                participant_id = row.get("participant_id", "")
                if participant_id:
                    existing[participant_id] = dict(row)
    if "site_id" not in fields:
        fields.append("site_id")

    centers_by_subject: dict[str, set[str]] = {}
    for record in records:
        centers_by_subject.setdefault(record.subject_id, set()).add(record.center)
    for subject_id, centers in centers_by_subject.items():
        if len(centers) != 1:
            raise ValueError(
                f"subject sub-{subject_id} maps to multiple centers: {sorted(centers)}"
            )
        participant_id = f"sub-{subject_id}"
        row = existing.setdefault(participant_id, {field: "n/a" for field in fields})
        row["participant_id"] = participant_id
        center = next(iter(centers))
        site_digest = hashlib.sha256(center.encode("utf-8")).hexdigest()[:8]
        row["site_id"] = f"site-{site_digest}"

    _write_tsv(path, fields, [existing[key] for key in sorted(existing)])
    participants_json = root / "participants.json"
    metadata: dict[str, Any] = {}
    if participants_json.exists():
        metadata = json.loads(participants_json.read_text(encoding="utf-8"))
    metadata["site_id"] = {
        "LongName": "De-identified acquisition site identifier",
        "Description": "Stable one-way hash label derived from the local acquisition site name.",
    }
    participants_json.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def update_scans(
    config: ProjectConfig,
    record: SeriesRecord,
    selection: SelectionRow,
    installed_nifti: Path,
) -> None:
    subject_root = config.paths.staging_bids_root / f"sub-{record.subject_id}"
    path = subject_root / f"sub-{record.subject_id}_scans.tsv"
    fields = ["filename", "protocol_id", "source_plane", "source_kind", "selection_qc"]
    existing: dict[str, dict[str, str]] = {}
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            prior_fields = list(reader.fieldnames or [])
            fields = prior_fields + [field for field in fields if field not in prior_fields]
            for row in reader:
                if row.get("filename"):
                    existing[row["filename"]] = dict(row)
    filename = installed_nifti.relative_to(subject_root).as_posix()
    existing[filename] = {
        **existing.get(filename, {}),
        "filename": filename,
        "protocol_id": record.protocol_id,
        "source_plane": record.plane,
        "source_kind": record.source_kind,
        "selection_qc": selection.reason,
    }
    _write_tsv(path, fields, [existing[key] for key in sorted(existing)])

    sidecar = path.with_suffix(".json")
    metadata: dict[str, Any] = {}
    if sidecar.exists():
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    metadata.update(
        {
            "protocol_id": {"LongName": "Curated acquisition protocol identifier"},
            "source_plane": {"LongName": "DICOM geometry-derived acquisition plane"},
            "source_kind": {
                "LongName": "Original or scanner-derived source classification"
            },
            "selection_qc": {"LongName": "Reason for curated sequence selection"},
        }
    )
    sidecar.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_tsv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "n/a") or "n/a" for field in fields})
    temporary.replace(path)
