from __future__ import annotations

import csv

import pytest

from gb_dicom2bids.config import SelectionConfig
from gb_dicom2bids.select import apply_manual_decisions, build_selection


def test_original_axial_beats_mpr(record_factory) -> None:
    original = record_factory(series_uid_hash="original")
    mpr = record_factory(
        series_uid_hash="mpr", source_kind="derived_mpr", series_description="Axial MPR"
    )
    rows = build_selection([original, mpr], SelectionConfig(manual_review_margin=1.0))
    selected = [row for row in rows if row.decision_status == "selected"]
    assert [row.series_uid_hash for row in selected] == ["original"]


def test_mpr_is_automatic_fallback(record_factory) -> None:
    mpr = record_factory(series_uid_hash="mpr", source_kind="derived_mpr")
    rows = build_selection([mpr], SelectionConfig())
    assert rows[0].decision_status == "selected"
    assert rows[0].output_basename.endswith("_rec-axialmpr_T1w")


def test_non_axial_t1_requires_review(record_factory) -> None:
    sagittal = record_factory(plane="sagittal", nearest_plane="sagittal")
    rows = build_selection([sagittal], SelectionConfig())
    assert rows[0].decision_status == "review"
    assert rows[0].reason == "no_reliable_axial_t1"


def test_near_tie_requires_review(record_factory) -> None:
    first = record_factory(series_uid_hash="a")
    second = record_factory(series_uid_hash="b")
    rows = build_selection([first, second], SelectionConfig(manual_review_margin=5.0))
    assert {row.decision_status for row in rows} == {"review"}


def test_flair_prefers_high_resolution_3d(record_factory) -> None:
    low = record_factory(
        series_uid_hash="low",
        candidate_type="flair",
        acquisition_type="2D",
        slice_thickness_mm=5.0,
        spacing_between_slices_mm=6.0,
        pixel_spacing_mm=[1.0, 1.0],
        protocol_id="flair-low",
    )
    high = record_factory(
        series_uid_hash="high",
        candidate_type="flair",
        acquisition_type="3D",
        slice_thickness_mm=1.0,
        spacing_between_slices_mm=1.0,
        pixel_spacing_mm=[1.0, 1.0],
        protocol_id="flair-high",
    )
    rows = build_selection([low, high], SelectionConfig(manual_review_margin=1.0))
    selected = next(row for row in rows if row.decision_status == "selected")
    assert selected.series_uid_hash == "high"


def test_flair_penalizes_obvious_motion_repeat(record_factory) -> None:
    clean = record_factory(
        series_uid_hash="clean",
        candidate_type="flair",
        series_description="3D FLAIR",
        protocol_id="flair-clean",
    )
    motion = record_factory(
        series_uid_hash="motion",
        candidate_type="flair",
        series_description="3D FLAIR MOTION REPEAT",
        protocol_id="flair-motion",
    )
    rows = build_selection([clean, motion], SelectionConfig(manual_review_margin=1.0))
    selected = next(row for row in rows if row.decision_status == "selected")
    assert selected.series_uid_hash == "clean"


def test_manual_sagittal_acceptance(record_factory, tmp_path) -> None:
    sagittal = record_factory(plane="sagittal", nearest_plane="sagittal")
    rows = build_selection([sagittal], SelectionConfig())
    path = tmp_path / "manual_review.tsv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["subject_id", "study_uid_hash", "series_uid_hash", "decision", "reviewer"],
            delimiter="\t",
        )
        writer.writeheader()
        writer.writerow(
            {
                "subject_id": "001",
                "study_uid_hash": "studyhash",
                "series_uid_hash": "serieshash",
                "decision": "accept_sag_fallback",
                "reviewer": "reviewer1",
            }
        )
    result = apply_manual_decisions(rows, path)
    assert result[0].decision_status == "selected"
    assert result[0].output_basename.endswith("_acq-manualsag_T1w")


def test_invalid_manual_decision_fails(record_factory, tmp_path) -> None:
    rows = build_selection([record_factory(plane="sagittal")], SelectionConfig())
    path = tmp_path / "manual_review.tsv"
    path.write_text(
        "subject_id\tstudy_uid_hash\tseries_uid_hash\tdecision\treviewer\n"
        "001\tstudyhash\tserieshash\tnot_allowed\tx\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid manual decision"):
        apply_manual_decisions(rows, path)


def test_multiple_studies_require_one_subject_level_manual_choice(
    record_factory, tmp_path
) -> None:
    first = record_factory(series_uid_hash="first", study_uid_hash="study-a")
    second = record_factory(series_uid_hash="second", study_uid_hash="study-b")
    rows = build_selection([first, second], SelectionConfig())
    assert {row.decision_status for row in rows} == {"review"}

    path = tmp_path / "manual_review.tsv"
    path.write_text(
        "subject_id\tstudy_uid_hash\tseries_uid_hash\tdecision\treviewer\n"
        "001\tstudy-a\tfirst\taccept_original\treviewer1\n",
        encoding="utf-8",
    )
    resolved = apply_manual_decisions(rows, path)
    selected = [row for row in resolved if row.decision_status == "selected"]
    assert [row.series_uid_hash for row in selected] == ["first"]
    assert next(row for row in resolved if row.series_uid_hash == "second").reason == (
        "superseded_by_manual_selection"
    )
