"""Name hints classify protocols; they never certify image quality or physical orientation."""

import pytest
from test_qc_candidate_limit import make_count_index
from test_qc_exclusions import make_missing

from gb_dicom2bids.classify import default_classification, named_target_plane


@pytest.mark.parametrize(
    "name,modality,plane",
    [
        ("OAx-T1-FLAIR", "t1", "tra"),
        ("T1__OAx__SE", "t1", "tra"),
        ("OSag-extra-T1W", "t1", "sag"),
        ("T1-OSag-320-PosDisp-T1-OAx", "t1", "sag"),
        ("OAx eFLAIR", "flair", "tra"),
        ("FLAIR__OSag", "flair", "sag"),
        ("T2 dark fluid TRA", "flair", "tra"),
        ("sag_t2_dark_fluid", "flair", "sag"),
        ("FLAIR sagittal", "flair", "sag"),
        ("OAx_BRAVO", "t1", "tra"),
        ("T1 OAx OSag", "t1", None),
        ("T1 coaxial", "t1", None),
        ("T1-FLAIR_OAx", "flair", None),
        ("OAx", "t1", None),
    ],
)
def test_names_and_conflicting_plane_hints(record_factory, name, modality, plane):
    record = record_factory(series_description=name, protocol_name="", sequence_name="")
    assert named_target_plane(record, modality) == plane


@pytest.mark.parametrize(
    "name",
    [
        "T2 dark fluid",
        "T2__DARK_FLUID",
        "t2-dark--fluid",
        "t2_darkfluid_TRA",
        "t2_tse_dark_extra_fluid_tra",
        "t2_320-dark-protocol123-fluid",
        "FLUID_extra_T2_more_DARK",
    ],
)
def test_t2_dark_fluid_explicit_classification(record_factory, name):
    record = record_factory(series_description=name, protocol_name="", sequence_name="")
    result = default_classification(record)
    assert (result["modality"], result["confidence"], result["reason"]) == (
        "flair",
        "high",
        "name_t2_dark_fluid",
    )
    record.series_description = "T1 FLAIR T2 dark fluid"
    assert default_classification(record)["modality"] == "t1"


def test_dark_fluid_keywords_never_join_different_names(record_factory):
    record = record_factory(series_description="T2 dark", protocol_name="fluid", sequence_name="")
    assert default_classification(record)["modality"] != "flair"
    record.series_description, record.protocol_name = "T2", "dark extra fluid"
    assert default_classification(record)["modality"] != "flair"
    record.series_description = "T2 dark extra fluid OAx"
    assert named_target_plane(record, "flair") == "tra"


@pytest.mark.parametrize(
    "axial,sag,modality",
    [
        ("OAx-T1-FLAIR", "OSag T1 SE", "t1"),
        ("T1 tra", "T1 additional characters sag", "t1"),
        ("OAx-T2-FLAIR", "OSag T2 FLAIR", "flair"),
        ("T2 dark fluid tra", "T2 dark fluid sag", "flair"),
    ],
)
def test_unique_axial_finishes_only_identification(tmp_path, axial, sag, modality):
    other = "FLAIR" if modality == "t1" else "T1"
    index, identify = make_missing(tmp_path, {"phantom01": [axial, sag, other]})
    choice = index.choices("phantom01")[modality]
    assert index.records[choice["choice"]].series_description == axial
    assert choice["count"] == 2
    assert identify.summary()["counts"][modality]["automatic_unique"] == 1
    assert not identify.state["templates"]
    assert not list((index.root.parent / "subjects").glob("*.json"))


@pytest.mark.parametrize("count", [2, 3, 4])
def test_axial_repeats_keep_comparison_or_fixed_limit(tmp_path, count):
    index, identify = make_count_index(
        tmp_path,
        {
            "phantom01": {
                "OAx T1 FLAIR": count,
                "OSag T1": 1,
                "FLAIR": 1,
            }
        },
    )
    assert index.choices("phantom01")["t1"]["choice"] is None
    assert identify.summary()["counts"]["t1"]["candidate_limit_skipped"] == int(count >= 4)
    assert identify.summary()["counts"]["t1"]["pending_subjects"] == int(count < 4)
