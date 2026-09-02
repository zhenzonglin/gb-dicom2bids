from __future__ import annotations

from gb_dicom2bids.models import SelectionRow
from gb_dicom2bids.pilot import select_pilot_subjects


def _selection(record, status: str = "selected") -> SelectionRow:
    suffix = "T1w" if record.candidate_type == "t1" else "FLAIR"
    return SelectionRow(
        center=record.center,
        subject_id=record.subject_id,
        study_uid_hash=record.study_uid_hash,
        series_uid_hash=record.series_uid_hash,
        candidate_type=record.candidate_type,
        decision_status=status,
        score=100.0,
        reason="synthetic",
        source_plane=record.plane,
        source_kind=record.source_kind,
        protocol_id=record.protocol_id,
        output_basename=f"sub-{record.subject_id}_{suffix}",
    )


def test_pilot_covers_strata_shortfall_and_review(record_factory) -> None:
    records = []
    selections = []
    pairs = (
        ("001", "flair-common"),
        ("002", "flair-common"),
        ("003", "flair-common"),
        ("004", "flair-rare"),
    )
    for subject, protocol in pairs:
        t1 = record_factory(subject_id=subject, series_uid_hash=f"t1-{subject}")
        flair = record_factory(
            subject_id=subject,
            series_uid_hash=f"flair-{subject}",
            candidate_type="flair",
            protocol_id=protocol,
            series_description="FLAIR",
        )
        records.extend((t1, flair))
        selections.extend((_selection(t1), _selection(flair)))
    uncertain = record_factory(
        subject_id="005",
        series_uid_hash="review-005",
        candidate_type="flair",
        protocol_id="flair-review",
        plane="unknown",
        classification_confidence="medium",
    )
    records.append(uncertain)
    selections.append(_selection(uncertain, status="review"))

    entries = select_pilot_subjects(records, selections, per_stratum=2)
    subjects = {entry.subject_id for entry in entries}
    assert {"001", "002", "004", "005"}.issubset(subjects)
    rare = next(entry for entry in entries if entry.subject_id == "004")
    assert rare.shortfall == 1
    review = next(entry for entry in entries if entry.subject_id == "005")
    assert review.reason == "review_or_low_confidence"
