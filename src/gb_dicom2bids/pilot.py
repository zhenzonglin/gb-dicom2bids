from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .models import SelectionRow, SeriesRecord
from .runtime import atomic_write_json


@dataclass(frozen=True)
class PilotEntry:
    subject_id: str
    center: str
    reason: str
    stratum: str
    protocol_id: str
    stratum_available: int
    stratum_target: int
    shortfall: int


def select_pilot_subjects(
    records: list[SeriesRecord],
    selections: list[SelectionRow],
    *,
    per_stratum: int = 2,
) -> list[PilotEntry]:
    if per_stratum < 1:
        raise ValueError("per_stratum must be at least 1")
    by_hash = {record.series_uid_hash: record for record in records}
    selected_by_subject: dict[str, dict[str, SelectionRow]] = defaultdict(dict)
    for row in selections:
        if row.decision_status == "selected" and row.candidate_type in {"t1", "flair"}:
            selected_by_subject[row.subject_id][row.candidate_type] = row

    strata: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)
    for subject_id, modalities in selected_by_subject.items():
        if set(modalities) != {"t1", "flair"}:
            continue
        flair = by_hash.get(modalities["flair"].series_uid_hash)
        if flair is None:
            continue
        key = (
            flair.center,
            flair.manufacturer or "unknown",
            flair.model_name or "unknown",
            flair.protocol_id,
        )
        strata[key].append(subject_id)

    entries: list[PilotEntry] = []
    for key, subjects in sorted(strata.items()):
        candidates = sorted(set(subjects))
        chosen = candidates[:per_stratum]
        stratum = "|".join(key)
        for subject_id in chosen:
            entries.append(
                PilotEntry(
                    subject_id=subject_id,
                    center=key[0],
                    reason="flair_protocol_stratum",
                    stratum=stratum,
                    protocol_id=key[-1],
                    stratum_available=len(candidates),
                    stratum_target=per_stratum,
                    shortfall=max(0, per_stratum - len(candidates)),
                )
            )

    exceptional: dict[str, list[str]] = defaultdict(list)
    for row in selections:
        record = by_hash.get(row.series_uid_hash)
        if record is None:
            continue
        if (
            row.candidate_type == "t1"
            and record.source_kind == "derived_mpr"
            and record.plane == "axial"
        ):
            exceptional["axial_mpr"].append(row.subject_id)
        if row.decision_status == "review" and (
            record.plane in {"unknown", "inconsistent"}
            or record.classification_confidence != "high"
        ):
            exceptional["review_or_low_confidence"].append(row.subject_id)
    for reason, subjects in sorted(exceptional.items()):
        for subject_id in sorted(set(subjects))[:per_stratum]:
            record = next(record for record in records if record.subject_id == subject_id)
            entries.append(
                PilotEntry(
                    subject_id=subject_id,
                    center=record.center,
                    reason=reason,
                    stratum=reason,
                    protocol_id="",
                    stratum_available=len(set(subjects)),
                    stratum_target=per_stratum,
                    shortfall=max(0, per_stratum - len(set(subjects))),
                )
            )
    return sorted(
        entries,
        key=lambda entry: (entry.center, entry.subject_id, entry.reason, entry.stratum),
    )


def write_pilot_manifest(path: Path, entries: list[PilotEntry]) -> None:
    fields = list(asdict(PilotEntry("", "", "", "", "", 0, 0, 0)))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(asdict(entry) for entry in entries)
    temporary.replace(path)


def write_pilot_summary(
    path: Path,
    entries: list[PilotEntry],
    *,
    conversion_counts: dict[str, int] | None = None,
    qc_counts: dict[str, int] | None = None,
    validation: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "subjects": len({entry.subject_id for entry in entries}),
        "strata": len(
            {entry.stratum for entry in entries if entry.reason == "flair_protocol_stratum"}
        ),
        "strata_with_shortfall": len(
            {
                entry.stratum
                for entry in entries
                if entry.reason == "flair_protocol_stratum" and entry.shortfall > 0
            }
        ),
        "entries": [asdict(entry) for entry in entries],
        "conversion_counts": conversion_counts or {},
        "qc_counts": qc_counts or {},
        "validation": validation or {},
    }
    atomic_write_json(path, payload)
