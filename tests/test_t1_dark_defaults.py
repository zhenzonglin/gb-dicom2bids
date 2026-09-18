"""T1 plus dark is T1, without changing exclusion or human-decision precedence."""

import pytest
from test_qc_exclusions import make_missing

from gb_dicom2bids.classify import (
    CLASSIFICATION_VERSION,
    default_classification,
    named_target_plane,
)
from gb_dicom2bids.qc_protocols import ProtocolIndex
from gb_dicom2bids.qc_state import digest, read_decision, save_decision


@pytest.mark.parametrize(
    "name",
    [
        "T1 dark fluid",
        "eT1W__DARK_FLUID",
        "dark-extra-T1",
        "T1_dark_T2_fluid",
        "t1-dark",
        "DARK__fluid__T1W",
        "OAx-T1-dark-fluid",
    ],
)
def test_t1_dark_is_t1(record_factory, name):
    record = record_factory(series_description=name, protocol_name="", sequence_name="")
    result = default_classification(record)
    assert result["modality"] == "t1"
    assert result["reason"] == "name_t1_and_dark"


@pytest.mark.parametrize(
    "name", ["CT T1 dark", "XA T1 dark", "TOF T1 dark", "T1 dark SAG", "T1 dark COR"]
)
def test_non_targets_and_plane_exclusions_still_win(record_factory, name):
    record = record_factory(series_description=name, protocol_name="", sequence_name="")
    assert default_classification(record)["excluded"] is True


def test_dark_does_not_join_names_and_t2_stays_flair(record_factory):
    record = record_factory(series_description="T1", protocol_name="dark fluid", sequence_name="")
    assert default_classification(record)["reason"] != "name_t1_and_dark"
    record.series_description, record.protocol_name = "T2 extra dark extra fluid", ""
    assert default_classification(record)["modality"] == "flair"
    record.series_description = "T1 dark fluid OAx"
    assert named_target_plane(record, "t1") == "tra"
    assert named_target_plane(record, "flair") is None
    assert default_classification(record, version="sequence-defaults-8")["modality"] == "flair"


@pytest.mark.parametrize("manual", [False, True])
def test_v8_catalog_migrates_defaults_but_preserves_manual_quality(tmp_path, manual):
    index, identify = make_missing(tmp_path, {"phantom01": ["T1 dark fluid", "FLAIR"]})
    uid = next(u for u, r in index.records.items() if r.series_description == "T1 dark fluid")
    identify._save(dict(identify.state, defaults_version="sequence-defaults-8"), "synthetic_v8")
    assert identify.assignment(uid)["modality"] == "flair"
    if manual:
        decision = read_decision(index.root.parent, "phantom01")
        decision["candidates"][uid] = {"quality": "pass", "modality": "flair"}
        decision["groups"]["flair"] = {"choice": uid}
        save_decision(index.root.parent, "phantom01", decision)
    before = read_decision(index.root.parent, "phantom01")
    sources = {p: digest(p) for p in index.config.nifti_import.source_root.rglob("*.nii.gz")}
    fresh = ProtocolIndex(index.config)
    fresh.identification.enable()
    assert fresh.identification.state["defaults_version"] == CLASSIFICATION_VERSION
    assert fresh.identification.assignment(uid)["modality"] == ("flair" if manual else "t1")
    assert read_decision(index.root.parent, "phantom01") == before
    assert all(digest(p) == h for p, h in sources.items())
    assert list((index.root / "identification_backups").glob("*.json"))
    assert set(fresh.records) == set(index.records)
    assert not list(index.config.paths.staging_bids_root.rglob("*.nii*"))
