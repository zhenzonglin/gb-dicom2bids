"""Synthetic subject-local T1 TRA preference; no quality decisions are propagated."""

import copy

import pytest
from test_nifti_import import make_image
from test_qc_exclusions import make_missing, refreshed_inventory
from test_qc_identify import payload_for, publish

from gb_dicom2bids.classify import CLASSIFICATION_VERSION, named_t1_plane
from gb_dicom2bids.qc_protocols import ProtocolIndex
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_state import digest, record_digest


@pytest.mark.parametrize(
    "name,plane",
    [
        ("T1 SAG", "sag"),
        ("t1__se__sag__320-extra", "sag"),
        ("eT1W-3D-SAG320-extra", "sag"),
        ("T1sag", "sag"),
        ("T1-se-sag-320--PosDisp-t2-tse-tra-384", "sag"),
        ("T1 TRA", "tra"),
        ("T1__SE--TRA__320", "tra"),
        ("Ｔ１ ＴＲＡ", "tra"),
        ("tra320-T1W-extra", "tra"),
        ("T1-FLAIR_TRA", "tra"),
        ("T1 sagittal", "sag"),
        ("T1 transverse", "tra"),
        ("T1 contrast", None),
        ("T1-SAG-TRA", None),
        ("T2-SAG", None),
        ("MRA", None),
    ],
)
def test_plane_tokens_allow_intervening_text_but_not_substrings(record_factory, name, plane):
    record = record_factory(series_description=name, protocol_name="", sequence_name="")
    assert named_t1_plane(record) == plane


def test_no_cross_field_or_patient_identity_plane_inference(record_factory):
    record = record_factory(
        series_description="T1",
        protocol_name="SAG",
        sequence_name="",
        center="TRA",
        subject_id="T1SAG",
    )
    assert named_t1_plane(record) is None
    record.series_description, record.protocol_name = "T1 SAG", "T1 TRA"
    assert named_t1_plane(record) is None


def test_unique_tra_finishes_identification_preserving_sag_and_source(tmp_path):
    index, identify = make_missing(
        tmp_path,
        {
            "phantom01": ["T1-se-sag-320--PosDisp-t2-tse-tra-384", "T1-SE-TRA320", "FLAIR"],
            "phantom02": ["T1 SAG", "T1 TRA", "CT", "unknown-contrast", "FLAIR"],
            "phantom03": ["T1 SAG", "T1-SAG-repeat", "FLAIR"],
        },
    )
    records_before = {u: record_digest(r) for u, r in index.records.items()}
    sources = {p: digest(p) for p in index.config.nifti_import.source_root.rglob("*.nii.gz")}
    assert identify.summary()["counts"]["t1"]["automatic_unique"] == 2
    assert identify.summary()["counts"]["t1"]["pending_subjects"] == 1
    for subject in ("phantom01", "phantom02"):
        choices = index.choices(subject)["t1"]
        assert choices["count"] == 2 and choices["top_count"] == 1
        assert identify.name_planes[choices["choice"]] == "tra"
        assert choices["selection_reason"] == "t1_tra_over_sag"
        group = next(g for g in identify.subject_groups(subject) if g["modality"] == "t1")
        assert group["default_selection_reason"] == "t1_tra_over_sag"
        assert sorted(t["priority"] for t in group["templates"]) == [0, 100]
        assert all(
            index.assignment(u)["modality"] == "t1"
            for u in index.subjects[subject]
            if identify.name_planes[u] == "sag"
        )
    service = ReviewService(index.config)
    try:
        assert (
            service.subject("phantom01")["sequence_choices"]["t1"]
            == index.choices("phantom01")["t1"]["choice"]
        )
    finally:
        service.close()
    assert not identify.state["templates"]
    assert not identify.state.get("negative_scopes")
    assert not list((index.root.parent / "subjects").glob("*.json"))
    assert not list(index.config.paths.staging_bids_root.rglob("*.nii*"))
    assert all(digest(p) == value for p, value in sources.items())
    assert records_before == {u: record_digest(r) for u, r in index.records.items()}


@pytest.mark.parametrize(
    "names",
    [
        ["T1 SAG", "T1 TRA", "T1 TRA-repeat"],
        ["T1 SAG", "T1 TRA", "T1-FLAIR"],
        ["T1 SAG", "T1 TRA", "T1 COR"],
        ["T1 SAG-TRA", "T1 TRA"],
    ],
)
def test_extra_target_or_multiple_tra_never_auto_selects(tmp_path, names):
    index, identify = make_missing(tmp_path, {"phantom01": [*names, "FLAIR"]})
    assert identify.summary()["counts"]["t1"]["pending_subjects"] == 1
    assert index.choices("phantom01")["t1"]["choice"] is None


def test_same_name_tra_repeats_remain_multiple_images(tmp_path):
    index, _ = make_missing(tmp_path, {"phantom01": ["T1 SAG", "T1 TRA", "FLAIR"]})
    make_image(index.config.nifti_import.source_root / "site/phantom01/T1 TRA/repeat.nii.gz")
    refreshed_inventory(index)
    fresh = ProtocolIndex(index.config).identification
    fresh.enable()
    assert fresh.index.choices("phantom01")["t1"]["top_count"] == 2
    assert fresh.summary()["counts"]["t1"]["pending_subjects"] == 1


@pytest.mark.parametrize("protection", ["rule", "image", "choice", "defer", "recheck", "failed"])
def test_manual_and_unresolved_target_protection(tmp_path, protection):
    index, identify = make_missing(tmp_path, {"phantom01": ["T1 SAG", "T1 TRA", "FLAIR"]})
    sag = next(u for u in index.subjects["phantom01"] if identify.name_planes[u] == "sag")
    if protection == "rule":
        p = payload_for(identify, "t1")
        for f, entry in p["templates"].items():
            entry["priority"] = 0 if f == identify.families[sag] else 100
        publish(identify, p)
        assert index.choices("phantom01")["t1"]["choice"] == sag
    elif protection in {"image", "choice"}:
        decision = index.decisions["phantom01"]
        if protection == "image":
            decision["candidates"][sag] = {"quality": "pass", "modality": "t1"}
        else:
            decision["groups"]["t1"] = {"choice": sag}
    elif protection == "defer":
        p = payload_for(identify, "t1")
        p.update(templates={}, deferred_candidates=[sag])
        publish(identify, p)
    elif protection == "recheck":
        state = copy.deepcopy(identify.state)
        state["recheck"] = {"phantom01:t1": {"candidates": [sag]}}
        identify._save(state, "synthetic_recheck")
    else:
        identify.preview_outcome(sag, True)
    assert not identify.preferred_t1("phantom01", identify.state)


def test_version_two_migration_keeps_manual_ranking_and_does_not_certify(tmp_path):
    index, identify = make_missing(tmp_path, {"phantom01": ["T1 SAG", "T1 TRA", "FLAIR"]})
    state = copy.deepcopy(identify.state)
    state["defaults_version"] = "sequence-defaults-2"
    identify._save(state, "synthetic_previous_version")
    assert identify.summary()["counts"]["t1"]["pending_subjects"] == 1
    assert index.choices("phantom01")["t1"]["choice"] is None
    identify.enable()
    assert identify.state["defaults_version"] == CLASSIFICATION_VERSION
    assert identify.summary()["counts"]["t1"]["pending_subjects"] == 0
    assert list((identify.root / "identification_backups").glob("*.json"))
    publish(identify, payload_for(identify, "t1"))
    rules = copy.deepcopy(identify.state["templates"])
    state = copy.deepcopy(identify.state)
    state["defaults_version"] = "sequence-defaults-2"
    identify._save(state, "synthetic_previous_manual_version")
    identify.enable()
    assert identify.state["templates"] == rules
    assert identify.summary()["counts"]["t1"]["manual_completed"] == 1
    assert not list((index.root.parent / "subjects").glob("*.json"))
