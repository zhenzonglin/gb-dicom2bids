"""Synthetic XA/CT non-target categorization and existing-catalogue compatibility."""

import copy

import pytest
from test_qc_exclusions import make_missing
from test_qc_identify import payload_for, publish

from gb_dicom2bids.classify import CLASSIFICATION_VERSION, default_classification
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_state import digest, record_digest


@pytest.mark.parametrize(
    "name,source_modality,category",
    [
        ("XA", "MR", "DSA"),
        ("series__xa__001", "MR", "DSA"),
        ("series XA 001", "MR", "DSA"),
        ("series-XA-001", "MR", "DSA"),
        ("ＸＡ__FLAIR", "MR", "DSA"),
        ("XA_T1_FLAIR", "MR", "DSA"),
        ("FLAIR", "XA", "DSA"),
        ("T1 MIP", " xa ", "DSA"),
        ("CT", "MR", "CT"),
        ("series__ct__002", "MR", "CT"),
        ("series CT 002", "MR", "CT"),
        ("series-CT-002", "MR", "CT"),
        ("ＣＴ_T1", "MR", "CT"),
        ("CT_T1_FLAIR", "MR", "CT"),
        ("FLAIR", "CT", "CT"),
        ("T1 MIP", " ct ", "CT"),
    ],
)
def test_xa_ct_metadata_and_name_markers(record_factory, name, source_modality, category):
    record = record_factory(
        modality=source_modality,
        series_description=name,
        protocol_name="",
        sequence_name="",
    )
    before = record_digest(record)
    result = default_classification(record)
    assert result["modality"] == "other" and result["excluded"]
    assert result["non_target_type"] == category
    assert result["reason"] == ("non_target_xa_dsa" if category == "DSA" else "non_target_ct")
    assert record_digest(record) == before


@pytest.mark.parametrize(
    "name,modality",
    [
        ("EXAM_T1", "t1"),
        ("RELAXATION_FLAIR", "flair"),
        ("artifact_T1", "t1"),
        ("T1_XA123", "t1"),
        ("FLAIR_CT456", "flair"),
    ],
)
def test_short_markers_never_use_substrings_or_subject_identity(record_factory, name, modality):
    record = record_factory(
        center="XA",
        subject_id="CT",
        series_description=name,
        protocol_name="",
        sequence_name="",
    )
    result = default_classification(record)
    assert result["modality"] == modality and not result["excluded"]
    assert result["non_target_type"] == ""


def test_xa_ct_catalogue_upgrade_preserves_manual_and_tra_preference(tmp_path):
    index, identify = make_missing(
        tmp_path,
        {
            "phantom01": ["XA", "CT"],
            "phantom02": ["T1 SAG", "T1 TRA", "FLAIR", "XA"],
            "phantom03": ["manual-XA", "FLAIR"],
        },
    )
    # A previous explicit correction remains authoritative despite the new default.
    xa = next(
        u for u in index.subjects["phantom03"] if index.records[u].series_description == "manual-XA"
    )
    p = payload_for(identify, "t1")
    group = next(g for g in identify.subject_groups("phantom03") if g["modality"] == "t1")
    p.update(
        subject="phantom03",
        group=group["id"],
        templates={
            identify.families[xa]: {"modality": "t1", "priority": 0},
        },
    )
    publish(identify, p)
    rules = copy.deepcopy(identify.state["templates"])
    state = copy.deepcopy(identify.state)
    state["defaults_version"] = "sequence-defaults-3"
    identify._save(state, "synthetic_previous_version")
    before = {u: record_digest(r) for u, r in index.records.items()}
    images = {p: digest(p) for p in index.config.nifti_import.source_root.rglob("*.nii.gz")}
    selected = index.choices("phantom02")["t1"]["choice"]
    assert selected is not None
    assert index.records[selected].series_description == "T1 TRA"
    identify.enable()
    assert identify.state["defaults_version"] == CLASSIFICATION_VERSION
    assert identify.state["templates"] == rules
    assert index.assignment(xa)["modality"] == "t1" and not identify.default_excluded(xa)
    assert index.choices("phantom02")["t1"]["choice"] == selected
    # Exclusion remains visible through all sequences, not a destructive operation.
    service = ReviewService(index.config)
    try:
        candidates = service.subject("phantom01")["candidates"]
        assert {c["series_description"] for c in candidates} == {"XA", "CT"}
        ct = next(c for c in candidates if c["series_description"] == "CT")
        assert ct["default_excluded"] and ct["default_classification"]["non_target_type"] == "CT"
        xa_candidate = next(c for c in candidates if c["series_description"] == "XA")
        assert xa_candidate["default_excluded"]
        assert xa_candidate["default_classification"]["non_target_type"] == "DSA"
    finally:
        service.close()
    assert all(digest(p) == value for p, value in images.items())
    assert before == {u: record_digest(r) for u, r in index.records.items()}
    assert not list((index.root.parent / "subjects").glob("*.json"))
    assert not list(index.config.paths.staging_bids_root.rglob("*.nii*"))


def test_xa_ct_only_subjects_need_no_target_review(tmp_path):
    index, identify = make_missing(tmp_path, {"phantom01": ["series__XA__001", "series__CT__002"]})
    assert identify.summary()["pending_groups"] == 0
    assert identify.summary()["default_excluded_series"] == 2
    for modality in ("t1", "flair"):
        assert not identify.exclusions().round("phantom01", modality)
        assert index.choices("phantom01")[modality]["count"] == 0
    assert identify.state["phase"] == "identification"
