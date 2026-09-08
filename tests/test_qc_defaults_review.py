"""Synthetic name defaults, manual precedence, scoped review and reversible removal."""

import copy
from dataclasses import replace
from unittest.mock import Mock

import pytest
from test_qc_exclusions import make_missing, negative, refreshed_inventory
from test_qc_identify import payload_for, publish

from gb_dicom2bids.classify import CLASSIFICATION_VERSION, classify_record, default_classification
from gb_dicom2bids.qc_identify import Identification, require_quality
from gb_dicom2bids.qc_protocols import ProtocolIndex
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_state import (
    ConflictError,
    digest,
    read_decision,
    record_digest,
    save_decision,
)
from gb_dicom2bids.runtime import read_json


@pytest.mark.parametrize(
    "name, expected",
    [
        ("T1-FLAIR", "t1"),
        ("T1__FLAIR", "t1"),
        ("T1 AX FLAIR", "t1"),
        ("FLAIR_T1", "t1"),
        ("OAx--T1___FLAIR", "t1"),
        ("eT1W-FLAIR", "t1"),
        ("T1/AX/FLAIR", "t1"),
        ("t1--AX--fLaIr", "t1"),
        ("Ｔ１ ＦＬＡＩＲ", "t1"),
        ("T2 FLAIR", "flair"),
        ("T2__FLAIR", "flair"),
        ("eFLAIR-longTR-CLEAR", "flair"),
        ("T1 MRA", "other"),
        ("CT T1 FLAIR", "other"),
        ("3D-TOF", "other"),
        ("3DTOF", "other"),
        ("MRA1", "other"),
        ("DWI", "other"),
        ("dDWI-SENSE", "other"),
        ("isoDWI", "other"),
        ("s-b0", "other"),
        ("b__1000", "other"),
        ("CT", "other"),
        ("synthetic__CT__0001", "other"),
        ("artifact-T1", "t1"),
        ("direction-T1", "t1"),
        ("T1-b10000", "t1"),
        ("T1-ab0x", "t1"),
        ("TOFollow-T1", "t1"),
        ("MPRAGE", "t1"),
    ],
)
def test_current_name_defaults(record_factory, name, expected):
    record = record_factory(series_description=name, protocol_name="", sequence_name="")
    source_digest = record_digest(record)
    result = default_classification(record)
    assert result["modality"] == expected
    assert record_digest(record) == source_digest
    assert classify_record(record).candidate_type == expected


def test_short_markers_ignore_identity_and_names_are_not_concatenated(record_factory):
    record = record_factory(
        center="CT", subject_id="b1000", series_description="FLAIR", protocol_name=""
    )
    assert default_classification(record)["modality"] == "flair"
    record.series_description, record.protocol_name = "t1", "flair"
    assert default_classification(record)["reason"] != "name_t1_and_flair"
    record.series_description, record.protocol_name = "t", "1"
    assert default_classification(record)["modality"] == "other"


def revoke(identify, kind, *, recheck=False, **filters):
    rows = identify.review_rules(dict(kind=kind, **filters))["rules"]
    assert rows
    p = {
        "revision": identify.state["revision"],
        "rule_ids": [rows[0]["id"]],
        "reviewer": "zhenzong",
        "recheck": recheck,
        "return_to_identification": True,
    }
    preview = identify.revoke_preview(p)
    identify.revoke_publish(dict(p, preview_digest=preview["preview_digest"]))
    return preview


def test_defaults_unique_multiple_and_only_non_target_do_not_grant_quality(tmp_path):
    index, identify = make_missing(
        tmp_path,
        {
            "phantom01": ["T1 AX FLAIR", "T2_FLAIR", "CT", "TOF", "b0", "b1000"],
            "phantom02": ["T1 AX FLAIR", "T1-SE", "FLAIR"],
            "phantom03": ["MRA1", "isoDWI", "CT"],
            "phantom04": ["unknown-contrast", "CT"],
        },
    )
    pending = {
        (s, g["modality"]) for g in identify.catalogue()["groups"] for s in g["pending_subjects"]
    }
    assert pending == {("phantom02", "t1"), ("phantom04", "t1"), ("phantom04", "flair")}
    assert index.choices("phantom01")["t1"]["count"] == 1
    assert index.choices("phantom02")["t1"]["choice"] is None
    assert identify.summary()["counts"]["t1"]["automatic_unique"] == 1
    assert not identify.exclusions().round("phantom03", "t1")
    with pytest.raises(ValueError, match="序列识别"):
        require_quality(index.root)
    assert not list((index.root.parent / "subjects").glob("*.json"))
    service = ReviewService(index.config)
    try:
        ct = next(
            c for c in service.subject("phantom01")["candidates"] if c["series_description"] == "CT"
        )
        assert ct["default_excluded"] and ct["default_classification"]["reason"] == "non_target_ct"
    finally:
        service.close()


def test_migration_keeps_manual_completions_and_artifact_identity(tmp_path, monkeypatch):
    index, identify = make_missing(
        tmp_path,
        {
            "phantom01": ["T1-SE", "T1_AX_FLAIR"],
            "phantom02": ["T1-SE", "T1_AX_FLAIR"],
        },
    )
    # A legacy inventory classified T1-FLAIR as FLAIR. It is never rewritten in place here.
    for uid, record in list(index.records.items()):
        if "FLAIR" in record.series_description:
            index.records[uid] = replace(record, candidate_type="flair")
    identify = Identification(index)
    index.identification = identify
    state = copy.deepcopy(identify.state)
    state.pop("defaults_version")
    identify._save(state, "synthetic_legacy_state")
    publish(identify, payload_for(identify, "t1"))
    legacy = copy.deepcopy(identify.state)
    legacy.pop("manual_completed", None)  # Old releases did not persist this field.
    identify._save(legacy, "synthetic_legacy_state")
    uid = index.subjects["phantom01"][0]
    image_decision = read_decision(index.root.parent, "phantom01")
    image_decision["candidates"][uid] = {
        "quality": "fail",
        "modality": "t1",
        "reason": "synthetic blur",
    }
    save_decision(index.root.parent, "phantom01", image_decision)
    raw = {u: record_digest(r) for u, r in index.records.items()}
    protected = [*index.config.nifti_import.source_root.rglob("*.nii.gz")]
    protected += list((index.root.parent / "subjects").glob("*.json"))
    hashes = {p: digest(p) for p in protected}
    monkeypatch.setattr(
        "gb_dicom2bids.qc_identify.check_image", Mock(side_effect=AssertionError("no images"))
    )
    identify.enable()
    assert identify.state["defaults_version"] == CLASSIFICATION_VERSION
    assert list((index.root / "identification_backups").glob("*.json"))
    assert identify.summary()["counts"]["t1"]["pending_subjects"] == 0
    assert all(record_digest(index.records[u]) == d for u, d in raw.items())
    assert all(digest(p) == d for p, d in hashes.items())
    revision = identify.state["revision"]
    identify.enable()
    assert identify.state["revision"] == revision
    revoke(identify, "include")
    assert identify.summary()["counts"]["t1"]["pending_subjects"] == 2


def test_partial_exclusion_is_not_migrated_as_whole_group_completion(tmp_path):
    _, identify = make_missing(tmp_path, {"phantom01": ["contrast-A", "contrast-B"]})
    state = copy.deepcopy(identify.state)
    state.pop("defaults_version")
    identify._save(state, "synthetic_legacy_state")
    publish(identify, negative(identify, only={"contrast-A"}))
    identify.enable()
    assert identify.summary()["counts"]["t1"]["pending_subjects"] == 1
    assert "phantom01:t1" not in identify.state.get("manual_completed", {})


@pytest.mark.parametrize("kind", ["include", "exclude", "absent"])
def test_revoke_preview_is_read_only_and_publish_preserves_unrelated_state(tmp_path, kind):
    index, identify = make_missing(tmp_path, {"phantom01": ["contrast-A", "contrast-B"]})
    if kind == "include":
        p = payload_for(identify, "t1")
        p["templates"][identify.families[index.subjects["phantom01"][0]]] = {
            "modality": "t1",
            "priority": 0,
        }
    elif kind == "exclude":
        p = negative(identify, only={"contrast-A"})
    else:
        p = dict(payload_for(identify, "t1"), absent=True)
    publish(identify, p)
    other = dict(payload_for(identify, "flair"), absent=True)
    publish(identify, other)
    protected = {p: digest(p) for p in index.config.nifti_import.source_root.rglob("*.nii.gz")}
    row = identify.review_rules({"kind": kind, "modality": "t1"})["rules"][0]
    body = {"revision": identify.state["revision"], "rule_ids": [row["id"]], "reviewer": "zhenzong"}
    before = digest(index.root / "identification.json")
    preview = identify.revoke_preview(body)
    assert digest(index.root / "identification.json") == before and not preview["quality_copied"]
    identify.revoke_publish(dict(body, preview_digest=preview["preview_digest"]))
    assert "phantom01:flair" in identify.state["absent"]
    assert identify.summary()["counts"]["t1"]["pending_subjects"] == 1
    fresh = ProtocolIndex(index.config).identification
    assert fresh.review_rules({"kind": kind, "modality": "t1"})["total"] == 0
    assert any(r["action"] == "remove" for r in fresh.review_rules({"view": "history"})["rules"])
    with pytest.raises(ConflictError):
        identify.revoke_publish(dict(body, preview_digest=preview["preview_digest"]))
    assert all(digest(p) == d for p, d in protected.items())
    assert not list((index.root.parent / "subjects").glob("*.json"))


def test_revoke_falls_back_to_default_or_forces_recheck_until_explicit_publish(tmp_path):
    index, identify = make_missing(tmp_path, {"phantom01": ["T1-FLAIR", "FLAIR"]})
    publish(identify, payload_for(identify, "t1"))
    revoke(identify, "include", recheck=True)
    assert identify.summary()["counts"]["t1"]["pending_subjects"] == 1
    publish(identify, payload_for(identify, "t1"))
    assert not identify.state.get("recheck")
    revoke(identify, "include")
    assert identify.summary()["counts"]["t1"]["pending_subjects"] == 0
    assert index.choices("phantom01")["t1"]["count"] == 1


def test_quality_stage_requires_acknowledgement_and_preserves_human_image_override(tmp_path):
    index, identify = make_missing(tmp_path, {"phantom01": ["T1-FLAIR", "FLAIR"]})
    publish(identify, payload_for(identify, "t1"))
    uid = index.choices("phantom01")["t1"]["choice"]
    decision = read_decision(index.root.parent, "phantom01")
    decision["candidates"][uid] = {"quality": "pass", "modality": "t1"}
    index.decisions["phantom01"] = save_decision(index.root.parent, "phantom01", decision)
    path = index.root.parent / "subjects/phantom01.json"
    saved = digest(path)
    identify.transition({"revision": identify.state["revision"], "phase": "quality"})
    row = identify.review_rules({"kind": "include"})["rules"][0]
    body = {"revision": identify.state["revision"], "rule_ids": [row["id"]], "reviewer": "zhenzong"}
    preview = identify.revoke_preview(body)
    assert preview["manual_image_overrides"] == 1 and preview["returns_to_identification"]
    with pytest.raises(ValueError, match="明确确认"):
        identify.revoke_publish(dict(body, preview_digest=preview["preview_digest"]))
    assert identify.state["phase"] == "quality"
    revoke(identify, "include")
    assert identify.state["phase"] == "identification" and digest(path) == saved
    assert index.assignment(uid)["classification_source"] == "manual_image"


def test_negative_revoke_never_expands_scope_and_defaults_can_be_overridden(tmp_path):
    index, identify = make_missing(tmp_path, {"phantom01": ["CT", "contrast-A"]})
    publish(identify, negative(identify, only={"contrast-A"}))
    sid = next(iter(identify.state["negative_scopes"]))
    members = identify.state["negative_scopes"][sid]["subjects"].copy()
    from test_nifti_import import make_image

    make_image(index.config.nifti_import.source_root / "site/phantom02/contrast-A/image.nii.gz")
    refreshed_inventory(index)
    fresh = ProtocolIndex(index.config).identification
    fresh.enable()
    preview = revoke(fresh, "exclude")
    assert preview["affected_subjects"] == 1
    assert fresh.state["negative_scopes"][sid]["subjects"] == members
    ct = next(u for u, r in fresh.index.records.items() if r.series_description == "CT")
    p = payload_for(fresh, "t1")
    p["subject"] = "phantom01"
    p["group"] = next(g["id"] for g in fresh.subject_groups("phantom01") if g["modality"] == "t1")
    p["templates"] = {fresh.families[ct]: {"modality": "t1", "priority": 0}}
    publish(fresh, p)
    assert fresh.assignment(ct)["modality"] == "t1" and not fresh.default_excluded(ct)


def test_review_cache_pagination_does_not_reread_history(tmp_path, monkeypatch):
    _, identify = make_missing(tmp_path, {"phantom01": ["contrast-A"]})
    state = copy.deepcopy(identify.state)
    for n in range(105):
        state["templates"][f"synthetic-rule-{n}"] = {"modality": "t1", "priority": n}
    identify._save(state, "synthetic_many_rules")
    first = identify.review_rules({})
    assert first["total"] == 105 and len(first["rules"]) == 100
    monkeypatch.setattr(
        "gb_dicom2bids.qc_rule_review.read_json", Mock(side_effect=AssertionError("cached"))
    )
    assert len(identify.review_rules({"offset": "100"})["rules"]) == 5
    assert identify.review_rules({"q": "synthetic-rule-104"})["total"] == 1


def test_uncommitted_history_does_not_revoke_and_retry_is_safe(tmp_path, monkeypatch):
    _, identify = make_missing(tmp_path, {"phantom01": ["T1", "FLAIR"]})
    publish(identify, payload_for(identify, "t1"))
    before = digest(identify.root / "identification.json")
    import gb_dicom2bids.qc_identify as module

    original = module.atomic_write_json

    def interrupt(path, value):
        if path.name == "identification.json":
            raise OSError("synthetic interruption before commit")
        original(path, value)

    monkeypatch.setattr(module, "atomic_write_json", interrupt)
    with pytest.raises(OSError, match="synthetic interruption"):
        revoke(identify, "include")
    assert digest(identify.root / "identification.json") == before
    fresh = ProtocolIndex(identify.index.config).identification
    assert fresh.review_rules({"kind": "include"})["total"] == 1
    monkeypatch.setattr(module, "atomic_write_json", original)
    revoke(fresh, "include")
    assert fresh.review_rules({"kind": "include"})["total"] == 0
    assert read_json(fresh.root / "identification.json")["revision"] == fresh.state["revision"]
    orphan = identify.state["revision"] + 1
    assert all(r["revision"] != orphan for r in fresh.review_rules({"view": "history"})["rules"])


def test_saved_flair_classification_beats_t1_flair_default_until_removed(tmp_path):
    index, identify = make_missing(tmp_path, {"phantom01": ["T1_AX_FLAIR", "unknown-A"]})
    uid = next(u for u, r in index.records.items() if r.series_description == "T1_AX_FLAIR")
    p = payload_for(identify, "flair")
    p["templates"] = {identify.families[uid]: {"modality": "flair", "priority": 0}}
    publish(identify, p)
    identify.enable()
    assert index.assignment(uid)["modality"] == "flair"
    assert index.assignment(uid)["classification_source"] == "manual_rule"
    assert identify.summary()["counts"]["flair"]["pending_subjects"] == 0
    revoke(identify, "include")
    assert index.assignment(uid)["modality"] == "t1"
    assert identify.summary()["counts"]["flair"]["pending_subjects"] == 1


def test_revoking_t1_rule_keeps_unrelated_flair_completion_protection(tmp_path):
    _, identify = make_missing(tmp_path, {"phantom01": ["T1", "FLAIR", "unknown-A"]})
    publish(identify, payload_for(identify, "t1"))
    publish(identify, payload_for(identify, "flair"))
    retained = copy.deepcopy(identify.state["manual_completed"]["phantom01:flair"])
    revoke(identify, "include", modality="t1")
    assert identify.state["manual_completed"]["phantom01:flair"] == retained


def test_saved_completion_does_not_cover_added_target_image(tmp_path):
    index, identify = make_missing(tmp_path, {"phantom01": ["T1", "FLAIR"]})
    publish(identify, payload_for(identify, "t1"))
    from test_nifti_import import make_image

    make_image(index.config.nifti_import.source_root / "site/phantom01/T1-repeat/image.nii.gz")
    refreshed_inventory(index)
    fresh = ProtocolIndex(index.config).identification
    fresh.enable()
    assert fresh.summary()["counts"]["t1"]["pending_subjects"] == 1
