"""Explicit axial-first, natural-last sequence choice; never quality approval."""

import copy

import pytest
from test_nifti_import import make_config, make_image
from test_qc_exclusions import make_missing

from gb_dicom2bids.nifti_import import inventory_preconverted
from gb_dicom2bids.qc_identify import Identification, axial_candidate_order
from gb_dicom2bids.qc_protocols import ProtocolIndex
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_state import digest, read_decision, record_digest


@pytest.mark.parametrize(
    "modality,base,other", [("t1", "T1", "FLAIR"), ("flair", "T2 dark extra fluid", "T1")]
)
@pytest.mark.parametrize("extra", ["OSag", "COR", "unspecified", "OAx OSag"])
def test_existing_axial_finishes_without_identifying_other_targets(
    tmp_path, modality, base, other, extra
):
    index, identify = make_missing(
        tmp_path, {"phantom01": [f"{base} OAx", f"{base} {extra}", other]}
    )
    choice = index.choices("phantom01")[modality]
    assert index.records[choice["choice"]].series_description == f"{base} OAx"
    assert choice["selection_reason"] == f"{modality}_axial_last"
    assert identify.summary()["counts"][modality]["pending_subjects"] == 0
    assert not read_decision(index.root.parent, "phantom01")["candidates"]


@pytest.mark.parametrize("modality,base,other", [("t1", "T1", "FLAIR"), ("flair", "FLAIR", "T1")])
def test_folder_then_filename_natural_last_is_stable_and_visible(tmp_path, modality, base, other):
    source = tmp_path / "source"
    # Folder order dominates filename order; within folder 10, image 10 beats image 2.
    paths = [
        f"site/phantom01/{base} OAx2/image99.nii.gz",
        f"site/phantom01/{base} TRA10/image2.nii.gz",
        f"site/phantom01/{base} TRA10/image10.nii.gz",
        f"site/phantom01/{base} OSag99/image99.nii.gz",
        f"site/phantom01/{other}/image.nii.gz",
    ]
    # Use the same axial spelling so the test isolates natural numeric ordering.
    paths[0] = paths[0].replace("OAx2", "TRA2")
    for path in paths:
        make_image(source / path)
    config = make_config(tmp_path, source)
    inventory_preconverted(config)
    index = ProtocolIndex(config)
    index.identification = Identification(index)
    index.identification.enable()
    records_before = {u: record_digest(r) for u, r in index.records.items()}
    images_before = {p: digest(source / p) for p in paths}
    chosen = index.choices("phantom01")[modality]
    uid = chosen["choice"]
    assert index.records[uid].source_relpaths == [paths[2]]
    assert chosen["top_count"] == 1 and chosen["count"] == 4
    assert index.identification.summary()["counts"][modality]["pending_subjects"] == 0
    index.subjects["phantom01"].reverse()
    index.identification.invalidate()
    assert index.choices("phantom01")[modality]["choice"] == uid
    viewer = ReviewService(config)
    try:
        assert viewer.subject("phantom01")["sequence_choices"][modality] == uid
    finally:
        viewer.close()
    assert records_before == {u: record_digest(r) for u, r in index.records.items()}
    assert all(digest(source / p) == value for p, value in images_before.items())
    assert not read_decision(index.root.parent, "phantom01")["candidates"]
    assert not list(config.paths.staging_bids_root.rglob("*.nii*"))


def test_order_unicode_case_and_path_separator_ties_are_stable(record_factory):
    records = [
        record_factory(source_relpaths=["site/p/T1 TRA/scan２.nii.gz"], series_uid_hash="a"),
        record_factory(source_relpaths=["site/p/T1 TRA/SCAN10.nii.gz"], series_uid_hash="b"),
    ]
    assert max(records, key=axial_candidate_order).series_uid_hash == "b"
    records[1].source_relpaths = ["site\\p\\T1 TRA\\SCAN10.nii.gz"]
    assert max(records, key=axial_candidate_order).series_uid_hash == "b"


def test_v5_catalog_migration_backs_up_before_enabling_axial_last(tmp_path):
    index, identify = make_missing(
        tmp_path, {"phantom01": ["T1 TRA2", "T1 TRA10", "T1 COR", "FLAIR"]}
    )
    state = copy.deepcopy(identify.state)
    state["defaults_version"] = "sequence-defaults-5"
    identify._save(state, "synthetic_previous_policy")
    assert identify.summary()["counts"]["t1"]["pending_subjects"] == 1
    identify.enable()
    assert identify.summary()["counts"]["t1"]["pending_subjects"] == 0
    uid = index.choices("phantom01")["t1"]["choice"]
    assert index.records[uid].series_description == "T1 TRA10"
    assert list((identify.root / "identification_backups").glob("*.json"))
    assert not read_decision(index.root.parent, "phantom01")["candidates"]
