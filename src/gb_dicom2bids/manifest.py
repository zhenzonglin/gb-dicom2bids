from __future__ import annotations

import csv
import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import ConversionResult, SelectionRow, SeriesRecord

INVENTORY_FIELDS = list(SeriesRecord("", "", "", "").public_dict().keys())
SELECTION_FIELDS = list(SelectionRow("", "", "", "", "", "", 0.0, "", "", "", "").to_dict().keys())
CONVERSION_FIELDS = list(ConversionResult("", "", "", "", "").to_dict().keys())


def _write_tsv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    temporary.replace(path)


def write_inventory(
    audit_root: Path,
    records: list[SeriesRecord],
    unreadable_relpaths: list[str],
    *,
    error_status: str = "unreadable_or_non_dicom",
) -> None:
    audit_root.mkdir(parents=True, exist_ok=True)
    _write_tsv(
        audit_root / "series_inventory.tsv",
        (record.public_dict() for record in records),
        INVENTORY_FIELDS,
    )
    private_path = audit_root / "series_sources.json"
    temporary = private_path.with_suffix(private_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps([record.private_dict() for record in records], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(private_path)
    _write_tsv(
        audit_root / "inventory_errors.tsv",
        (
            {"source_relpath": path, "status": error_status}
            for path in unreadable_relpaths
        ),
        ["source_relpath", "status"],
    )
    write_protocol_catalog(audit_root / "protocol_catalog.tsv", records)


def load_private_records(audit_root: Path) -> list[SeriesRecord]:
    path = audit_root / "series_sources.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"run inventory first; missing {path}") from exc
    return [SeriesRecord.from_private_dict(item) for item in raw]


def write_protocol_catalog(path: Path, records: list[SeriesRecord]) -> None:
    groups: dict[str, list[SeriesRecord]] = defaultdict(list)
    for record in records:
        groups[record.protocol_id].append(record)
    rows: list[dict[str, Any]] = []
    for protocol_id, members in sorted(groups.items()):
        first = members[0]
        rows.append(
            {
                "protocol_id": protocol_id,
                "candidate_type": first.candidate_type,
                "manufacturer": first.manufacturer,
                "model_name": first.model_name,
                "software_versions": first.software_versions,
                "acquisition_type": first.acquisition_type,
                "repetition_time_ms": first.repetition_time_ms,
                "echo_time_ms": first.echo_time_ms,
                "inversion_time_ms": first.inversion_time_ms,
                "slice_thickness_mm": first.slice_thickness_mm,
                "pixel_spacing_mm": "\\".join(str(x) for x in first.pixel_spacing_mm),
                "series_count": len(members),
                "subject_count": len({member.subject_id for member in members}),
                "centers": ";".join(sorted({member.center for member in members})),
            }
        )
    fields = [
        "protocol_id",
        "candidate_type",
        "manufacturer",
        "model_name",
        "software_versions",
        "acquisition_type",
        "repetition_time_ms",
        "echo_time_ms",
        "inversion_time_ms",
        "slice_thickness_mm",
        "pixel_spacing_mm",
        "series_count",
        "subject_count",
        "centers",
    ]
    _write_tsv(path, rows, fields)


def write_selection(
    audit_root: Path,
    rows: list[SelectionRow],
    records: list[SeriesRecord],
    *,
    preserve_manual: bool = True,
) -> None:
    _write_tsv(
        audit_root / "selection_manifest.tsv", (row.to_dict() for row in rows), SELECTION_FIELDS
    )
    by_hash = {record.series_uid_hash: record for record in records}
    manual_rows: list[dict[str, Any]] = []
    for row in rows:
        if row.decision_status != "review" and not row.manual_decision:
            continue
        record = by_hash[row.series_uid_hash]
        manual_rows.append(
            {
                "center": row.center,
                "subject_id": row.subject_id,
                "study_uid_hash": row.study_uid_hash,
                "series_uid_hash": row.series_uid_hash,
                "candidate_type": row.candidate_type,
                "series_description": record.series_description,
                "protocol_name": record.protocol_name,
                "source_plane": row.source_plane,
                "plane_angle_deg": record.plane_angle_deg,
                "source_kind": row.source_kind,
                "protocol_id": row.protocol_id,
                "score": f"{row.score:.6f}",
                "reason": row.reason,
                "decision": row.manual_decision,
                "reviewer": row.reviewer,
                "comments": "",
            }
        )
    manual_path = audit_root / "manual_review.tsv"
    fields = [
        "center",
        "subject_id",
        "study_uid_hash",
        "series_uid_hash",
        "candidate_type",
        "series_description",
        "protocol_name",
        "source_plane",
        "plane_angle_deg",
        "source_kind",
        "protocol_id",
        "score",
        "reason",
        "decision",
        "reviewer",
        "comments",
    ]
    existing_decisions = _existing_manual_values(manual_path) if preserve_manual else {}
    for item in manual_rows:
        key = (item["subject_id"], item["study_uid_hash"], item["series_uid_hash"])
        if key in existing_decisions and not str(item["reason"]).startswith("visual_qc_"):
            item.update(existing_decisions[key])
    _write_tsv(manual_path, manual_rows, fields)


def _existing_manual_values(path: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    if not path.exists():
        return {}
    values: dict[tuple[str, str, str], dict[str, str]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            key = (
                row.get("subject_id", ""),
                row.get("study_uid_hash", ""),
                row.get("series_uid_hash", ""),
            )
            values[key] = {
                "decision": row.get("decision", ""),
                "reviewer": row.get("reviewer", ""),
                "comments": row.get("comments", ""),
            }
    return values


def load_selection(path: Path) -> list[SelectionRow]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = []
        for raw in csv.DictReader(handle, delimiter="\t"):
            raw["score"] = float(raw.get("score") or 0.0)
            rows.append(SelectionRow(**{field: raw.get(field, "") for field in SELECTION_FIELDS}))
    return rows


def write_conversion_results(path: Path, rows: list[ConversionResult]) -> None:
    _write_tsv(path, (row.to_dict() for row in rows), CONVERSION_FIELDS)
