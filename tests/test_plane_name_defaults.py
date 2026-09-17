"""SAG/COR exclusions and AX identification are defaults, not quality decisions."""

import copy

import pytest
from test_qc_candidate_limit import make_count_index
from test_qc_exclusions import make_missing
from test_qc_identify import payload_for, publish

from gb_dicom2bids.classify import default_classification, named_target_plane
from gb_dicom2bids.qc_identify import require_quality
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_state import digest, read_decision, record_digest, save_decision


@pytest.mark.parametrize(
    "name,reason",
    [
        ("T1 SAG", "sagittal_name"),
        ("FLAIR_OSag", "sagittal_name"),
        ("eT1Wsag320", "sagittal_name"),
        ("t2 dark extra fluid sagittal", "sagittal_name"),
        ("T1__COR", "coronal_name"),
        ("FLAIR OCor", "coronal_name"),
        ("FLAIRcor320", "coronal_name"),
        ("Ｔ１ ＣＯＲＯＮＡＬ", "coronal_name"),
        ("T1 AX SAG", "sagittal_name"),
        ("FLAIR COR AX", "coronal_name"),
        ("T1_AX_PosDisp_FLAIR_COR", None),
    ],
)
def test_plane_rejection_precedes_axial_inclusion(record_factory, name, reason):
    record = record_factory(series_description=name, protocol_name="", sequence_name="")
    before = record_digest(record)
    result = default_classification(record)
    assert result["excluded"] == bool(reason)
    if reason:
        assert result["modality"] == "other"
        assert result["reason"] == "non_target_" + reason
    assert record_digest(record) == before


@pytest.mark.parametrize("marker", ["AX", "axial", "OAx", "TRA", "Ax320"])
@pytest.mark.parametrize("name,modality", [("T1", "t1"), ("T2 dark extra fluid", "flair")])
def test_axial_spellings(record_factory, marker, name, modality):
    record = record_factory(
        series_description=f"{name}__{marker}", protocol_name="", sequence_name=""
    )
    assert named_target_plane(record, modality) == "tra"
    assert default_classification(record)["modality"] == modality


@pytest.mark.parametrize(
    "name", ["T1 correction", "T1 cortex", "T1 sagacity", "T1 contrast", "T1 relax", "T1 coaxial"]
)
def test_direction_substrings_inside_ordinary_words_are_not_markers(record_factory, name):
    record = record_factory(
        center="SAG",
        subject_id="CORAX",
        series_description=name,
        protocol_name="",
        sequence_name="",
    )
    assert default_classification(record)["modality"] == "t1"
    assert not default_classification(record)["excluded"]
    assert named_target_plane(record, "t1") is None


@pytest.mark.parametrize(
    "name", ["CT AX T1", "XA AX FLAIR", "MRA AX", "DWI AX", "b0 AX", "b1000 AX"]
)
def test_ax_does_not_undo_non_target_exclusions(record_factory, name):
    record = record_factory(series_description=name, protocol_name="", sequence_name="")
    assert default_classification(record)["excluded"]


def test_ax_without_modality_is_not_invented_as_t1_or_flair(record_factory):
    record = record_factory(series_description="AX", protocol_name="", sequence_name="")
    assert default_classification(record)["modality"] == "other"


def test_only_sag_cor_are_skipped_but_retained_for_correction(tmp_path):
    index, identify = make_missing(tmp_path, {"phantom01": ["T1 SAG", "FLAIR COR"]})
    before = {p: digest(p) for p in index.config.nifti_import.source_root.rglob("*.nii.gz")}
    assert identify.summary()["pending_groups"] == 0
    assert identify.summary()["default_excluded_series"] == 2
    assert all(c["choice"] is None for c in index.choices("phantom01").values())
    viewer = ReviewService(index.config)
    try:
        visible = viewer.subject("phantom01")["candidates"]
        assert len(visible) == 2 and all(c["default_excluded"] for c in visible)
    finally:
        viewer.close()
    assert all(digest(p) == value for p, value in before.items())
    assert not read_decision(index.root.parent, "phantom01")["candidates"]
    assert not list(index.config.paths.staging_bids_root.rglob("*.nii*"))


def test_sag_cor_do_not_count_against_surviving_axial_candidates(tmp_path):
    index, identify = make_count_index(
        tmp_path, {"phantom01": {"T1 AX": 3, "T1 SAG": 5, "FLAIR AX": 1, "FLAIR COR": 4}}
    )
    assert not identify.candidate_limits("phantom01")
    assert identify.summary()["pending_groups"] == 0
    for modality in ("t1", "flair"):
        choice = index.choices("phantom01")[modality]["choice"]
        assert " AX/" in index.records[choice].source_relpaths[0]
    t1 = index.choices("phantom01")["t1"]["choice"]
    assert index.records[t1].source_relpaths[0].endswith("image2.nii.gz")


def test_v6_migration_preserves_manual_rules_and_quality_records(tmp_path):
    index, identify = make_missing(tmp_path, {"phantom01": ["T1 SAG", "T1 AX", "FLAIR AX"]})
    state = copy.deepcopy(identify.state)
    state.update(defaults_version="sequence-defaults-6", phase="identification")
    identify._save(state, "synthetic_previous_version")
    sag = next(u for u, r in index.records.items() if "SAG" in r.series_description)
    assert identify.assignment(sag)["modality"] == "t1"
    payload = payload_for(identify, "t1")
    for family, value in payload["templates"].items():
        value["priority"] = 0 if family == identify.families[sag] else 100
    publish(identify, payload)
    saved = read_decision(index.root.parent, "phantom01")
    saved["candidates"][sag] = {"modality": "t1", "quality": "pass", "reason": "synthetic review"}
    save_decision(index.root.parent, "phantom01", saved)
    sources = {p: digest(p) for p in index.config.nifti_import.source_root.rglob("*.nii.gz")}
    quality_before = read_decision(index.root.parent, "phantom01")
    rules_before = copy.deepcopy(identify.state["templates"])
    identify._save(dict(identify.state, phase="quality"), "synthetic_old_quality_stage")
    with pytest.raises(ValueError, match="catalog"):
        require_quality(index.root)
    identify.enable()
    assert index.choices("phantom01")["t1"]["choice"] == sag
    assert identify.state["templates"] == rules_before
    assert not identify.default_excluded(sag)
    assert read_decision(index.root.parent, "phantom01") == quality_before
    assert list((index.root / "identification_backups").glob("*.json"))
    assert all(digest(p) == value for p, value in sources.items())
