"""Synthetic sequence-negative reuse; never promote an image's quality."""

from dataclasses import replace

import pytest
from test_nifti_import import make_config, make_image
from test_qc_identify import payload_for, publish

from gb_dicom2bids.manifest import load_private_records, write_inventory
from gb_dicom2bids.nifti_import import inventory_preconverted
from gb_dicom2bids.qc_identify import Identification
from gb_dicom2bids.qc_protocols import ProtocolIndex
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_state import ConflictError, digest, read_decision
from gb_dicom2bids.runtime import atomic_write_json, read_json


def make_missing(tmp_path, names=None):
    names = names or {
        "phantom01": ["T2-A", "DWI-B", "ADC-C"],
        "phantom02": ["T2-A", "DWI-B", "ADC-C"],
        "phantom03": ["T2-A", "DWI-B", "ADC-C", "contrast-D"],
        "phantom04": ["T2-A", "contrast-E"],
    }
    source = tmp_path / "source"
    for subject, sequences in names.items():
        for name in sequences:
            make_image(source / f"site/{subject}/{name}/image.nii.gz")
    config = make_config(tmp_path, source)
    inventory_preconverted(config)
    index = ProtocolIndex(config)
    identify = Identification(index)
    index.identification = identify
    identify.enable()
    return index, identify


def negative(identify, modality="t1", *, only=None):
    p = payload_for(identify, modality)
    p.pop("reason")
    p["templates"] = {}
    uids = identify.exclusions().round(p["subject"], modality)
    p["negative_templates"] = sorted(
        {
            identify.families[u]
            for u in uids
            if only is None or identify.index.records[u].series_description in only
        }
    )
    return p


def target_group(identify, modality="t1"):
    return next(g for g in identify.catalogue()["groups"] if g["modality"] == modality)


def refreshed_inventory(index):
    """Simulate a separately rebuilt upstream inventory, never overwrite via import CLI."""
    paths = index.config.paths
    config = replace(
        index.config,
        paths=replace(
            paths,
            audit_root=paths.audit_root.with_name("new-audit"),
            staging_bids_root=paths.staging_bids_root.with_name("new-staging"),
        ),
    )
    inventory_preconverted(config)
    write_inventory(paths.audit_root, load_private_records(config.paths.audit_root), [])


def test_iterative_new_sequences_skip_duplicates_and_finish_absent(tmp_path):
    index, identify = make_missing(tmp_path)
    before = {p: digest(p) for p in index.config.nifti_import.source_root.rglob("*.nii.gz")}
    publish(identify, negative(identify))
    group = target_group(identify)
    assert group["representative"] == "phantom03"
    assert group["count"] == 4 and group["pending_count"] == 2
    assert group["auto_skipped_subjects"] == 1
    assert group["new_template_count"] == 1
    assert {index.records[u].series_description for u in group["new_candidate_ids"]} == {
        "contrast-D"
    }
    publish(identify, negative(identify))
    assert target_group(identify)["representative"] == "phantom04"
    publish(identify, negative(identify))
    group = target_group(identify)
    assert group["pending_count"] == 0 and group["excluded_template_count"] == 5
    assert identify.summary()["counts"]["flair"]["pending_subjects"] == 4
    assert all(not read_decision(index.root.parent, s)["groups"] for s in index.subjects)
    assert all(digest(p) == d for p, d in before.items())
    assert not identify.state["absent"]  # Derived absence is not a sticky manual decision.


def test_discover_target_after_negative_round_and_retain_its_class(tmp_path):
    index, identify = make_missing(tmp_path)
    publish(identify, negative(identify))
    group = target_group(identify)
    p = payload_for(identify, "t1")
    p.pop("reason")
    uid = group["new_candidate_ids"][0]
    p["templates"][identify.families[uid]] = {"modality": "t1", "priority": 0}
    publish(identify, p)
    assert index.choices("phantom03")["t1"]["choice"] == uid
    assert target_group(identify)["representative"] == "phantom04"
    assert not read_decision(index.root.parent, "phantom03")["candidates"]


def test_revoke_refresh_and_stale_preview(tmp_path):
    index, identify = make_missing(tmp_path)
    p = negative(identify)
    first = p["negative_templates"][0]
    publish(identify, p)
    fresh = ProtocolIndex(index.config).identification
    assert target_group(fresh)["representative"] == "phantom03"
    p = payload_for(fresh, "t1")
    p.update(templates={}, revoke_negative=[first])
    preview = fresh.preview(p)
    publish(fresh, p)
    assert target_group(fresh)["pending_count"] >= 3
    with pytest.raises(ConflictError):
        fresh.publish(dict(p, preview_digest=preview["preview_digest"]))


def test_scope_never_expands_to_later_subject_and_generic_names_do_not_merge(tmp_path):
    index, identify = make_missing(tmp_path, {"phantom01": ["image"], "phantom02": ["image"]})
    publish(identify, negative(identify))
    assert target_group(identify)["pending_count"] == 1
    make_image(index.config.nifti_import.source_root / "site/phantom03/image/image.nii.gz")
    refreshed_inventory(index)
    fresh = ProtocolIndex(index.config).identification
    fresh.enable()
    assert fresh.summary()["counts"]["t1"]["pending_subjects"] == 2
    assert "phantom03" not in next(iter(fresh.state["negative_scopes"].values()))["subjects"]


def test_new_subject_with_matching_named_template_does_not_inherit_scope(tmp_path):
    index, identify = make_missing(tmp_path, {"phantom01": ["T2-A"], "phantom02": ["T2-A"]})
    publish(identify, negative(identify))
    make_image(index.config.nifti_import.source_root / "site/phantom03/T2-A/image.nii.gz")
    refreshed_inventory(index)
    fresh = ProtocolIndex(index.config).identification
    fresh.enable()
    pending = [
        g for g in fresh.catalogue()["groups"] if g["modality"] == "t1" and g["needs_protocol"]
    ]
    assert len(pending) == 1 and pending[0]["subjects"] == ["phantom03"]


def test_manual_positive_blocks_negative_but_other_modality_is_preserved(tmp_path):
    index, identify = make_missing(tmp_path, {"phantom01": ["FLAIR"], "phantom02": ["FLAIR"]})
    uid = index.subjects["phantom02"][0]
    index.decisions["phantom02"]["candidates"][uid] = {"modality": "flair", "quality": "pass"}
    publish(identify, negative(identify))
    assert index.assignment(uid)["modality"] == "flair"
    assert index.choices("phantom02")["flair"]["choice"] == uid
    # Later explicit T1 evidence overrides the negative and reopens the participant.
    index.decisions["phantom02"]["candidates"][uid]["modality"] = "t1"
    identify.invalidate()
    assert "t1" not in index.assignment(uid)["excluded_modalities"]
    p = negative(identify, only={"FLAIR"})
    with pytest.raises(ConflictError, match="人工分类冲突"):
        publish(identify, p)


def test_deferred_does_not_disappear_and_can_be_resolved(tmp_path):
    index, identify = make_missing(tmp_path)
    p = negative(identify, only={"T2-A", "DWI-B"})
    uid = next(
        u for u in index.subjects[p["subject"]] if index.records[u].series_description == "ADC-C"
    )
    p["deferred_candidates"] = [uid]
    publish(identify, p)
    assert target_group(identify)["representative"] == "phantom02"
    assert target_group(identify)["pending_count"] == 4
    p = negative(identify)
    p["subject"] = "phantom01"
    with pytest.raises(ValueError, match="待定"):
        publish(identify, p)
    p["deferred_candidates"] = []
    publish(identify, p)
    assert target_group(identify)["representative"] == "phantom03"


def test_old_preview_failure_is_not_skipped_and_successful_retry_clears_it(tmp_path):
    index, identify = make_missing(tmp_path, {"phantom01": ["T2-A"], "phantom02": ["T2-A"]})
    uid = index.subjects["phantom02"][0]
    error = index.root.parent / "errors" / f"{uid}.json"
    atomic_write_json(error, {"state": "failed", "error": "synthetic prior read error"})
    identify = ProtocolIndex(index.config).identification
    publish(identify, negative(identify))
    assert target_group(identify)["pending_subjects"] == ["phantom02"]
    fresh = ProtocolIndex(index.config).identification
    assert target_group(fresh)["pending_subjects"] == ["phantom02"]
    service = ReviewService(index.config)
    try:
        service._prepare_job(uid)
        assert service.jobs[uid]["state"] == "ready"
        assert read_json(error)["state"] == "resolved"
        assert read_json(error)["error"] == "synthetic prior read error"
        assert target_group(ProtocolIndex(index.config).identification)["pending_count"] == 0
    finally:
        service.close()


def test_negative_preview_detects_changed_source_before_publish(tmp_path):
    index, identify = make_missing(tmp_path)
    p = negative(identify)
    preview = identify.preview(p)
    uid = index.subjects[p["subject"]][0]
    source = index.config.nifti_import.source_root / index.records[uid].source_relpaths[0]
    import os

    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    with pytest.raises(ConflictError, match="预览"):
        identify.publish(dict(p, preview_digest=preview["preview_digest"]))
    assert not identify.state.get("negative_scopes")


def test_corrupt_image_cannot_be_negative_evidence(tmp_path):
    index, identify = make_missing(tmp_path)
    uid = index.subjects["phantom01"][0]
    source = index.config.nifti_import.source_root / index.records[uid].source_relpaths[0]
    source.write_bytes(b"not a nifti")
    with pytest.raises((ValueError, OSError), match="读取失败"):
        publish(identify, negative(identify))
    assert not identify.state.get("negative_scopes")


def test_inventory_new_template_reopens_previously_skipped_subject(tmp_path):
    index, identify = make_missing(tmp_path, {"phantom01": ["T2-A"], "phantom02": ["T2-A"]})
    publish(identify, negative(identify))
    assert target_group(identify)["pending_count"] == 0
    make_image(index.config.nifti_import.source_root / "site/phantom02/contrast-new/image.nii.gz")
    refreshed_inventory(index)
    fresh = ProtocolIndex(index.config).identification
    fresh.enable()
    assert target_group(fresh)["representative"] == "phantom02"
    assert target_group(fresh)["new_template_count"] == 1


def test_reject_all_false_automatic_candidates_without_global_reclassification(tmp_path):
    index, identify = make_missing(
        tmp_path, {"phantom01": ["T1-A", "T1-B"], "phantom02": ["T1-A", "T1-B"]}
    )
    publish(identify, negative(identify))
    assert target_group(identify)["pending_count"] == 0
    assert index.choices("phantom02")["t1"]["count"] == 0
    assert all(index.assignment(u)["modality"] == "t1" for u in index.subjects["phantom02"])
    assert not identify.state["templates"]


def test_scoped_rule_never_crosses_center_or_original_group(tmp_path):
    index, identify = make_missing(tmp_path)
    # Move one subject to another center without changing the files used by the fixture.
    for uid in index.subjects["phantom04"]:
        index.records[uid] = replace(index.records[uid], center="another_site")
    identify = Identification(index)
    index.identification = identify
    publish(identify, negative(identify))
    assert identify.exclusions().scope("phantom04", "t1")[0] is None
    assert identify.exclusions().round("phantom04", "t1") == sorted(index.subjects["phantom04"])


def test_removing_false_positive_rows_still_requires_unseen_optional_sequences(tmp_path):
    index, identify = make_missing(
        tmp_path,
        {
            "phantom01": ["T1-A", "T1-B", "contrast-C"],
            "phantom02": ["T1-A", "T1-B", "contrast-C"],
        },
    )
    p = payload_for(identify, "t1")
    p.pop("reason")
    for entry in p["templates"].values():
        entry["modality"] = "other"
    publish(identify, p)
    group = target_group(identify)
    assert group["pending_count"] == 2
    assert {index.records[u].series_description for u in group["new_candidate_ids"]} == {
        "contrast-C"
    }
