from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path

from .classify import normalized_text
from .config import SelectionConfig
from .models import SelectionRow, SeriesRecord

ALLOWED_MANUAL_DECISIONS = {
    "",
    "accept_original",
    "accept_mpr",
    "accept_sag_fallback",
    "accept_flair",
    "exclude",
}


def _voxel_volume(record: SeriesRecord) -> float | None:
    if len(record.pixel_spacing_mm) != 2 or record.slice_thickness_mm is None:
        return None
    values = [*record.pixel_spacing_mm, record.slice_thickness_mm]
    if any(value <= 0 or not math.isfinite(value) for value in values):
        return None
    return values[0] * values[1] * values[2]


def score_t1(record: SeriesRecord, config: SelectionConfig) -> float:
    score = {"original": 100.0, "derived_mpr": 65.0, "unknown": 25.0}.get(
        record.source_kind, 5.0
    )
    if record.acquisition_type.upper() == "3D":
        score += 15.0
    if record.coverage_mm is not None:
        score += min(30.0, record.coverage_mm / 5.0)
        if record.coverage_mm < config.minimum_brain_coverage_mm:
            score -= 30.0
    voxel_volume = _voxel_volume(record)
    if voxel_volume is not None:
        score += min(25.0, 20.0 / max(voxel_volume, 0.1))
    if not record.orientation_consistent:
        score -= 100.0
    return score


def score_flair(record: SeriesRecord, config: SelectionConfig) -> float:
    score = {"original": 60.0, "unknown": 25.0, "derived": 5.0}.get(
        record.source_kind, 10.0
    )
    if config.flair_prefer_3d and record.acquisition_type.upper() == "3D":
        score += 35.0
    elif record.acquisition_type.upper() == "2D" and record.plane == "axial":
        score += 15.0
    if record.coverage_mm is not None:
        if record.coverage_mm >= config.minimum_brain_coverage_mm:
            score += 30.0
        else:
            score -= 40.0
    voxel_volume = _voxel_volume(record)
    if voxel_volume is not None:
        score += min(35.0, 28.0 / max(voxel_volume, 0.1))
    thickness = record.slice_thickness_mm
    if thickness is not None:
        if thickness <= 1.5:
            score += 20.0
        elif thickness <= 3.0:
            score += 12.0
        elif thickness <= 5.0:
            score += 4.0
        else:
            score -= 15.0
    spacing = record.spacing_between_slices_mm
    if thickness is not None and spacing is not None:
        gap = spacing - thickness
        score += 8.0 if gap <= 0.5 else -min(20.0, gap * 4.0)
    if record.classification_confidence == "medium":
        score -= 5.0
    artifact_text = normalized_text(
        record.series_description,
        record.protocol_name,
        record.sequence_name,
        " ".join(record.image_type),
    )
    if any(
        token in artifact_text
        for token in (" motion ", " repeat ", " failed ", " artifact ", " moco ")
    ):
        score -= 40.0
    if not record.orientation_consistent:
        score -= 100.0
    return score


def _row(record: SeriesRecord, status: str, score: float, reason: str) -> SelectionRow:
    output = ""
    if status == "selected":
        if record.candidate_type == "t1":
            entity = "_rec-axialmpr" if record.source_kind == "derived_mpr" else ""
            output = f"sub-{record.subject_id}{entity}_T1w"
        elif record.candidate_type == "flair":
            output = f"sub-{record.subject_id}_FLAIR"
    return SelectionRow(
        center=record.center,
        subject_id=record.subject_id,
        study_uid_hash=record.study_uid_hash,
        series_uid_hash=record.series_uid_hash,
        candidate_type=record.candidate_type,
        decision_status=status,
        score=score,
        reason=reason,
        source_plane=record.plane,
        source_kind=record.source_kind,
        protocol_id=record.protocol_id,
        output_basename=output,
    )


def build_selection(records: list[SeriesRecord], config: SelectionConfig) -> list[SelectionRow]:
    grouped: dict[str, list[SeriesRecord]] = defaultdict(list)
    for record in records:
        grouped[record.subject_id].append(record)

    rows: list[SelectionRow] = []
    for _subject_id, group in sorted(grouped.items()):
        multiple_studies = len({record.study_uid_hash for record in group}) > 1
        t1_records = [record for record in group if record.candidate_type == "t1"]
        flair_records = [record for record in group if record.candidate_type == "flair"]
        other_records = [record for record in group if record.candidate_type == "other"]
        rows.extend(_select_t1(t1_records, config, multiple_studies))
        rows.extend(_select_flair(flair_records, config, multiple_studies))
        rows.extend(_row(record, "excluded", 0.0, "not_t1_or_flair") for record in other_records)

    rows.sort(
        key=lambda row: (
            row.center,
            row.subject_id,
            row.study_uid_hash,
            row.candidate_type,
            -row.score,
            row.series_uid_hash,
        )
    )
    return rows


def _select_t1(
    records: list[SeriesRecord], config: SelectionConfig, multiple_studies: bool
) -> list[SelectionRow]:
    if not records:
        return []
    scores = {record.series_uid_hash: score_t1(record, config) for record in records}
    if multiple_studies:
        return [
            _row(record, "review", scores[record.series_uid_hash], "multiple_studies")
            for record in records
        ]

    axial = [record for record in records if record.plane == "axial"]
    originals = [record for record in axial if record.source_kind == "original"]
    mpr = [record for record in axial if record.source_kind == "derived_mpr"]
    pool = originals or (mpr if config.allow_axial_mpr_fallback else [])
    if not pool:
        return [
            _row(record, "review", scores[record.series_uid_hash], "no_reliable_axial_t1")
            for record in records
        ]

    ranked = sorted(
        pool,
        key=lambda record: (-scores[record.series_uid_hash], record.series_uid_hash),
    )
    top_score = scores[ranked[0].series_uid_hash]
    tied = [
        record
        for record in ranked
        if top_score - scores[record.series_uid_hash] <= config.manual_review_margin
    ]
    if len(tied) > 1:
        review_hashes = {record.series_uid_hash for record in tied}
        return [
            _row(
                record,
                "review" if record.series_uid_hash in review_hashes else "excluded",
                scores[record.series_uid_hash],
                "t1_near_tie" if record.series_uid_hash in review_hashes else "lower_ranked_t1",
            )
            for record in records
        ]

    selected_hash = ranked[0].series_uid_hash
    return [
        _row(
            record,
            "selected" if record.series_uid_hash == selected_hash else "excluded",
            scores[record.series_uid_hash],
            "best_original_axial_t1"
            if record.series_uid_hash == selected_hash and record.source_kind == "original"
            else (
                "axial_mpr_fallback"
                if record.series_uid_hash == selected_hash
                else "lower_ranked_t1"
            ),
        )
        for record in records
    ]


def _select_flair(
    records: list[SeriesRecord], config: SelectionConfig, multiple_studies: bool
) -> list[SelectionRow]:
    if not records:
        return []
    scores = {record.series_uid_hash: score_flair(record, config) for record in records}
    if multiple_studies:
        return [
            _row(record, "review", scores[record.series_uid_hash], "multiple_studies")
            for record in records
        ]
    ranked = sorted(
        records,
        key=lambda record: (-scores[record.series_uid_hash], record.series_uid_hash),
    )
    top = ranked[0]
    if top.classification_confidence != "high" or not top.orientation_consistent:
        return [
            _row(
                record,
                "review" if record.series_uid_hash == top.series_uid_hash else "excluded",
                scores[record.series_uid_hash],
                (
                    "flair_low_confidence"
                    if record.series_uid_hash == top.series_uid_hash
                    else "lower_ranked_flair"
                ),
            )
            for record in records
        ]
    near_tie = (
        len(ranked) > 1
        and scores[top.series_uid_hash] - scores[ranked[1].series_uid_hash]
        <= config.manual_review_margin
    )
    if near_tie:
        review_hashes = {top.series_uid_hash, ranked[1].series_uid_hash}
        return [
            _row(
                record,
                "review" if record.series_uid_hash in review_hashes else "excluded",
                scores[record.series_uid_hash],
                (
                    "flair_near_tie"
                    if record.series_uid_hash in review_hashes
                    else "lower_ranked_flair"
                ),
            )
            for record in records
        ]
    return [
        _row(
            record,
            "selected" if record.series_uid_hash == top.series_uid_hash else "excluded",
            scores[record.series_uid_hash],
            (
                "best_wmh_flair"
                if record.series_uid_hash == top.series_uid_hash
                else "lower_ranked_flair"
            ),
        )
        for record in records
    ]


def apply_manual_decisions(
    rows: list[SelectionRow], manual_review_path: Path
) -> list[SelectionRow]:
    if not manual_review_path.exists():
        return rows
    decisions: dict[tuple[str, str, str], tuple[str, str]] = {}
    with manual_review_path.open("r", encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle, delimiter="\t"):
            decision = (raw.get("decision") or "").strip()
            if decision not in ALLOWED_MANUAL_DECISIONS:
                raise ValueError(f"invalid manual decision: {decision!r}")
            if not decision:
                continue
            key = (
                (raw.get("subject_id") or "").strip(),
                (raw.get("study_uid_hash") or "").strip(),
                (raw.get("series_uid_hash") or "").strip(),
            )
            decisions[key] = (decision, (raw.get("reviewer") or "").strip())

    accepted_by_group: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in rows:
        key = (row.subject_id, row.study_uid_hash, row.series_uid_hash)
        if key not in decisions:
            continue
        decision, reviewer = decisions[key]
        row.manual_decision = decision
        row.reviewer = reviewer
        if decision == "exclude":
            row.decision_status = "excluded"
            row.reason = "manual_exclusion"
            row.output_basename = ""
            continue
        _validate_manual_acceptance(row, decision)
        row.decision_status = "selected"
        row.reason = f"manual_{decision}"
        row.output_basename = _manual_output_basename(row, decision)
        accepted_by_group[(row.subject_id, row.candidate_type)].append(row.series_uid_hash)

    for group, accepted in accepted_by_group.items():
        if len(accepted) > 1:
            raise ValueError(f"multiple manually accepted candidates for {group}: {accepted}")
        selected_hash = accepted[0]
        for row in rows:
            row_group = (row.subject_id, row.candidate_type)
            if row_group == group and row.series_uid_hash != selected_hash:
                row.decision_status = "excluded"
                row.reason = "superseded_by_manual_selection"
                row.output_basename = ""
    return rows


def _validate_manual_acceptance(row: SelectionRow, decision: str) -> None:
    if decision == "accept_flair" and row.candidate_type != "flair":
        raise ValueError("accept_flair may only be used for FLAIR candidates")
    t1_decisions = {"accept_original", "accept_mpr", "accept_sag_fallback"}
    if decision in t1_decisions and row.candidate_type != "t1":
        raise ValueError(f"{decision} may only be used for T1 candidates")
    if decision == "accept_original" and row.source_kind != "original":
        raise ValueError("accept_original requires an original/primary source")
    if decision == "accept_mpr" and row.source_kind != "derived_mpr":
        raise ValueError("accept_mpr requires a derived MPR source")


def _manual_output_basename(row: SelectionRow, decision: str) -> str:
    if row.candidate_type == "flair":
        return f"sub-{row.subject_id}_FLAIR"
    if decision == "accept_mpr":
        return f"sub-{row.subject_id}_rec-axialmpr_T1w"
    if decision == "accept_sag_fallback":
        return f"sub-{row.subject_id}_acq-manualsag_T1w"
    return f"sub-{row.subject_id}_T1w"
