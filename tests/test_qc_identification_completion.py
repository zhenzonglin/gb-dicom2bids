"""Sequence completion depends on target candidates, not optional non-target images."""

import pytest
from test_qc_exclusions import make_missing, negative, target_group
from test_qc_identify import payload_for, publish

from gb_dicom2bids.qc_identify import require_quality
from gb_dicom2bids.qc_protocols import ProtocolIndex
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_state import ConflictError, digest, read_decision, save_decision
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


@pytest.mark.parametrize("modality", ["t1", "flair"])
@pytest.mark.parametrize("manual_conflict", [False, True])
def test_mixed_identity_only_decision_preserves_target_and_existing_quality(
    tmp_path, modality, manual_conflict
):
    names = [f"{modality}-keep", f"{modality}-projection", "ADC-C"]
    index, identify = make_missing(tmp_path, {"phantom01": names, "phantom02": names})
    uids = {index.records[u].series_description: u for u in index.subjects["phantom01"]}
    good, bad, optional = uids[names[0]], uids[names[1]], uids[names[2]]
    error = index.root.parent / "errors" / f"{bad}.json"
    atomic_write_json(error, {"state": "failed", "error": "synthetic projection preview error"})
    identify.preview_outcome(bad, True)
    if manual_conflict:
        other_bad = next(
            u
            for u in index.subjects["phantom02"]
            if index.records[u].series_description == names[1]
        )
        decision = read_decision(index.root.parent, "phantom02")
        decision["candidates"][other_bad] = {
            "modality": modality, "quality": "pass", "reason": "synthetic manual classification"
        }
        index.decisions["phantom02"] = save_decision(index.root.parent, "phantom02", decision)
        identify.invalidate()
    protected = [*index.config.nifti_import.source_root.rglob("*.nii.gz"), error]
    protected += list((index.root.parent / "subjects").glob("*.json"))
    before = {path: digest(path) for path in protected}
    p = payload_for(identify, modality)
    p.update(
        subject="phantom01",
        templates={
            identify.families[good]: {"modality": modality, "priority": 0},
            identify.families[bad]: {"modality": "other", "priority": 100},
        },
        negative_source_policy="identity_only",
        negative_templates=[identify.families[bad]],
        deferred_candidates=[optional],
    )
    old_state = digest(index.root / "identification.json")
    preview = identify.preview(p)
    assert preview["failed_preview_candidates"] == [bad]
    assert not preview["quality_copied"]
    assert digest(index.root / "identification.json") == old_state
    if manual_conflict:
        with pytest.raises(ConflictError, match="人工分类冲突"):
            identify.publish(dict(p, preview_digest=preview["preview_digest"]))
        assert digest(index.root / "identification.json") == old_state
    else:
        identify.publish(dict(p, preview_digest=preview["preview_digest"]))
        fresh = ProtocolIndex(index.config)
        assert target_group(fresh.identification, modality)["pending_count"] == 0
        scope = next(iter(fresh.identification.state["negative_scopes"].values()))
        assert set(scope["templates"]) == {identify.families[bad]}
        assert scope["deferred"] == [optional]
        assert fresh.choices("phantom01")[modality]["choice"] == good
        assert fresh.choices("phantom02")[modality]["count"] == 1
        assert fresh.assignment(bad)["excluded_modalities"] == [modality]
        assert fresh.assignment(optional)["excluded_modalities"] == []
        assert bad in fresh.identification.failed_previews
        assert not list((index.root.parent / "subjects").glob("*.json"))
        with pytest.raises(ValueError, match="序列识别"):
            require_quality(index.root)  # Completion alone cannot begin quality or archival.
    assert all(digest(path) == value for path, value in before.items())
