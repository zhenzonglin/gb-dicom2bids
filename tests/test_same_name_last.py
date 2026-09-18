"""Two or three same-name targets use natural-last order without quality approval."""

import copy

import pytest
from test_nifti_import import make_config, make_image
from test_qc_candidate_limit import make_count_index
from test_qc_identify import payload_for, publish

from gb_dicom2bids.nifti_import import inventory_preconverted
from gb_dicom2bids.qc_identify import Identification
from gb_dicom2bids.qc_protocols import ProtocolIndex
from gb_dicom2bids.qc_state import digest, read_decision


@pytest.mark.parametrize(
    "name,modality",
    [
        ("T1 SE", "t1"),
        ("T1 FLAIR", "t1"),
        ("MPRAGE", "t1"),
        ("FLAIR", "flair"),
        ("T2 dark extra fluid", "flair"),
        ("eT2 FLAIR", "flair"),
    ],
)
@pytest.mark.parametrize("count", [2, 3, 4])
def test_same_name_without_axial_hint_for_both_modalities(tmp_path, name, modality, count):
    other = "FLAIR" if modality == "t1" else "T1"
    index, identify = make_count_index(tmp_path, {"phantom01": {name: count, other: 1}})
    before = {p: digest(p) for p in index.config.nifti_import.source_root.rglob("*.nii.gz")}
    selected = index.choices("phantom01")[modality]
    assert identify.summary()["counts"][modality]["pending_subjects"] == 0
    if count < 4:
        assert selected["count"] == count and selected["top_count"] == 1
        assert selected["selection_reason"] == f"{modality}_same_name_last"
        assert (
            index.records[selected["choice"]]
            .source_relpaths[0]
            .endswith(f"image{count - 1}.nii.gz")
        )
    else:
        assert selected["choice"] is None and selected["count"] == 0
        assert modality in identify.candidate_limits("phantom01")
    assert all(digest(p) == h for p, h in before.items())
    assert not read_decision(index.root.parent, "phantom01")["candidates"]
    assert not list(index.config.paths.staging_bids_root.rglob("*.nii*"))


@pytest.mark.parametrize("name,modality", [("T1 SE", "t1"), ("T2 FLAIR", "flair")])
def test_name_normalization_and_folder_then_file_natural_order(tmp_path, name, modality):
    source = tmp_path / "source"
    paths = [
        f"site/phantom01/202001011200__MR__0002__{name}/image99.nii.gz",
        f"site/phantom01/202001011200__MR__0010__{name}/image2.nii.gz",
        f"site/phantom01/202001011200__MR__0010__{name}/image10.nii.gz",
    ]
    for path in paths:
        make_image(source / path)
    config = make_config(tmp_path, source)
    inventory_preconverted(config)
    index = ProtocolIndex(config)
    index.identification = Identification(index)
    index.identification.enable()
    assert len(set(index.identification.families.values())) == 1
    uid = index.choices("phantom01")[modality]["choice"]
    assert index.records[uid].source_relpaths == [paths[-1]]
    index.subjects["phantom01"].reverse()
    index.identification.invalidate()
    assert index.choices("phantom01")[modality]["choice"] == uid


def test_different_protocols_remain_ambiguous_and_patients_are_isolated(tmp_path):
    index, identify = make_count_index(
        tmp_path,
        {
            "phantom01": {"T1 A": 2, "T1 B": 3, "FLAIR": 1},
            "phantom02": {"T1 A": 2, "FLAIR": 1},
        },
    )
    first = index.choices("phantom01")["t1"]
    assert first["count"] == 5 and first["top_count"] == 2 and first["choice"] is None
    assert identify.summary()["counts"]["t1"]["pending_subjects"] == 1
    second = index.choices("phantom02")["t1"]["choice"]
    assert index.records[second].subject_id == "phantom02"


@pytest.mark.parametrize("protection", ["template", "image", "choice", "defer", "recheck"])
def test_same_name_repeat_rule_does_not_override_human_decisions(tmp_path, protection):
    index, identify = make_count_index(tmp_path, {"phantom01": {"T1 SE": 3, "FLAIR": 1}})
    first = next(
        u for u, r in index.records.items() if r.source_relpaths[0].endswith("T1 SE/image0.nii.gz")
    )
    if protection in {"template", "defer"}:
        payload = payload_for(identify, "t1")
        if protection == "defer":
            payload.update(templates={}, deferred_candidates=[first])
        publish(identify, payload)
    elif protection in {"image", "choice"}:
        decision = index.decisions["phantom01"]
        if protection == "image":
            decision["candidates"][first] = {"quality": "pass", "modality": "t1"}
        else:
            decision["groups"]["t1"] = {"choice": first}
    else:
        state = copy.deepcopy(identify.state)
        state["recheck"] = {"phantom01:t1": {"candidates": [first]}}
        identify._save(state, "synthetic_hold")
    before = copy.deepcopy(identify.state), copy.deepcopy(index.decisions)
    assert not identify.preferred_candidates("phantom01", "t1", identify.state)[0]
    assert before == (identify.state, index.decisions)


def test_et2_preference_then_same_name_last_and_no_t1_flair_confusion(tmp_path):
    index, identify = make_count_index(
        tmp_path,
        {
            "phantom01": {"T1 FLAIR": 2, "eT2 FLAIR": 3, "T2 FLAIR": 2},
        },
    )
    choices = index.choices("phantom01")
    assert identify.summary()["pending_groups"] == 0
    assert (
        index.records[choices["t1"]["choice"]].source_relpaths[0].endswith("T1 FLAIR/image1.nii.gz")
    )
    assert (
        index.records[choices["flair"]["choice"]]
        .source_relpaths[0]
        .endswith("eT2 FLAIR/image2.nii.gz")
    )
    assert choices["flair"]["selection_reason"] == "flair_et2_over_t2"
