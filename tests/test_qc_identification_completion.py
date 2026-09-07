"""Sequence completion depends on target candidates, not optional non-target images."""

import pytest
from test_qc_exclusions import make_missing, negative, target_group
from test_qc_identify import payload_for, publish

from gb_dicom2bids.qc_protocols import ProtocolIndex
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_state import digest, read_decision
from gb_dicom2bids.runtime import atomic_write_json


@pytest.mark.parametrize("modality", ["t1", "flair"])
@pytest.mark.parametrize("obstruction", ["deferred", "failed"])
@pytest.mark.parametrize("obstruct_target", [False, True])
def test_confirmed_target_ignores_only_unrelated_pending_items(
    tmp_path, modality, obstruction, obstruct_target
):
    index, identify = make_missing(
        tmp_path,
        {
            "phantom01": ["contrast-target", "DWI-B", "ADC-C"],
            "phantom02": ["contrast-target", "DWI-B", "ADC-C"],
            "phantom03": ["DWI-B", "ADC-C"],
        },
    )
    uids = {index.records[u].series_description: u for u in index.subjects["phantom01"]}
    blocked = uids["contrast-target" if obstruct_target else "ADC-C"]
    p = negative(identify, modality, only={"DWI-B"})
    p["subject"] = "phantom01"
    if obstruction == "deferred":
        p["deferred_candidates"] = [blocked]
    publish(identify, p)
    if obstruction == "failed":
        error = index.root.parent / "errors" / f"{blocked}.json"
        atomic_write_json(error, {"state": "failed", "error": "synthetic preview error"})
        identify.preview_outcome(blocked, True)
    protected = [*index.config.nifti_import.source_root.rglob("*.nii.gz")]
    protected += list((index.root.parent / "errors").glob("*.json"))
    before = {path: digest(path) for path in protected}
    p = payload_for(identify, modality)
    p["subject"] = "phantom01"
    p["templates"][identify.families[uids["contrast-target"]]] = {
        "modality": modality, "priority": 0
    }
    publish(identify, p)
    for current in (identify, ProtocolIndex(index.config).identification):
        group = target_group(current, modality)
        expected = ["phantom01", "phantom03"] if obstruct_target else ["phantom03"]
        assert group["pending_subjects"] == expected
        assert group["representative"] in expected
        assert "phantom02" not in group["pending_subjects"]
        assert current.summary()["counts"]["flair" if modality == "t1" else "t1"][
            "pending_subjects"
        ] == 3
        if obstruction == "deferred":
            assert blocked in next(iter(current.state["negative_scopes"].values()))["deferred"]
        else:
            assert blocked in current.failed_previews
    assert all(digest(path) == value for path, value in before.items())
    assert all(not read_decision(index.root.parent, s)["candidates"] for s in index.subjects)
    assert all(not read_decision(index.root.parent, s)["groups"] for s in index.subjects)


def test_excluded_t1_label_does_not_complete_identification_until_explicit_revoke(tmp_path):
    index, identify = make_missing(
        tmp_path, {"phantom01": ["T1-A", "ADC-C"], "phantom02": ["T1-A", "ADC-C"]}
    )
    p = negative(identify, only={"T1-A"})
    p["negative_source_policy"] = "identity_only"
    publish(identify, p)
    assert target_group(identify)["pending_count"] == 2
    service = ReviewService(index.config)
    try:
        item = next(
            c for c in service.subject("phantom01")["candidates"]
            if c["series_description"] == "T1-A"
        )
        assert item["candidate_type"] == "t1"
        assert item["excluded_modalities"] == ["t1"]
        assert index.choices("phantom01")["t1"]["count"] == 0
    finally:
        service.close()
    family = p["negative_templates"][0]
    p = payload_for(identify, "t1")
    p.update(templates={}, revoke_negative=[family])
    publish(identify, p)
    p = payload_for(identify, "t1")
    p["templates"][family] = {"modality": "t1", "priority": 0}
    publish(identify, p)
    assert target_group(identify)["pending_count"] == 0
    assert identify.summary()["counts"]["flair"]["pending_subjects"] == 2
