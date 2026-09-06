from __future__ import annotations

from dataclasses import replace

import pytest
from test_nifti_import import make_config, make_image

from gb_dicom2bids.nifti_import import inventory_preconverted
from gb_dicom2bids.qc_assist import evidence_valid, main
from gb_dicom2bids.qc_identify import Identification, require_quality
from gb_dicom2bids.qc_protocols import ProtocolIndex
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_state import ConflictError, digest, read_decision, save_decision


def make_index(tmp_path, *, missing=False):
    source = tmp_path / "source"
    for subject in ("phantom01", "phantom02"):
        for name in ("T1-A", "T1-B", "unknown-contrast" if missing else "FLAIR"):
            make_image(source / f"site/{subject}/{name}/image.nii.gz")
    make_image(source / "site/phantom02/DWI/image.nii.gz")
    config = make_config(tmp_path, source)
    inventory_preconverted(config)
    index = ProtocolIndex(config)
    # Geometry variations must not change sequence-classification families.
    for uid, record in list(index.records.items()):
        if record.subject_id == "phantom02" and record.candidate_type == "t1":
            index.records[uid] = replace(record, instance_count=33, slice_thickness_mm=4.5)
    identify = Identification(index)
    index.identification = identify
    identify.enable()
    return index, identify


def payload_for(identify, modality):
    group = next(g for g in identify.catalogue()["groups"] if g["modality"] == modality)
    return {
        "group": group["id"],
        "subject": group["representative"],
        "revision": identify.state["revision"],
        "reviewer": "zhenzong",
        "reason": "synthetic sequence identification",
        "templates": {
            e["id"]: {"modality": modality, "priority": i} for i, e in enumerate(group["templates"])
        },
    }


def publish(identify, payload):
    preview = identify.preview(payload)
    return identify.publish(dict(payload, preview_digest=preview["preview_digest"]))


def test_independent_modality_groups_ignore_other_sequences_and_geometry(tmp_path):
    index, identify = make_index(tmp_path)
    groups = identify.catalogue()["groups"]
    assert len(groups) == 2
    assert all(g["count"] == 2 for g in groups)
    assert identify.summary()["counts"]["t1"]["pending_groups"] == 1
    assert identify.summary()["counts"]["flair"]["pending_groups"] == 0
    assert all("dwi" not in e["name"] for g in groups for e in g["templates"])
    preview = identify.preview(payload_for(identify, "t1"))
    assert preview["affected_subjects"] == 2 and not preview["quality_copied"]
    publish(identify, payload_for(identify, "t1"))
    assert identify.summary()["pending_groups"] == 0
    assert all(index.choices(s)["t1"]["choice"] for s in index.subjects)
    assert not list((index.root.parent / "subjects").glob("*.json"))


def test_optional_other_correction_propagates_without_whole_subject_match(tmp_path):
    index, identify = make_index(tmp_path, missing=True)
    p = payload_for(identify, "flair")
    uid = next(
        u
        for u in index.subjects[p["subject"]]
        if index.records[u].series_description == "unknown-contrast"
    )
    p["templates"][identify.families[uid]] = {"modality": "flair", "priority": 0}
    result = publish(identify, p)
    assert result["affected_subjects"] == 2
    assert all(index.choices(s)["flair"]["count"] == 1 for s in index.subjects)
    assert all(index.choices(s)["flair"]["choice"] for s in index.subjects)
    assert identify.summary()["counts"]["flair"]["pending_groups"] == 0


def test_stage_blocks_quality_save_apply_and_cli_until_identification_finished(tmp_path):
    index, identify = make_index(tmp_path)
    config = index.config
    assert main(["features", "--config", str(tmp_path / "config.yaml")]) == 2
    with pytest.raises(ValueError, match="待识别"):
        identify.transition({"revision": identify.state["revision"], "phase": "quality"})
    with pytest.raises(ValueError, match="序列识别"):
        require_quality(index.root)
    service = ReviewService(config)
    try:
        with pytest.raises(ValueError, match="序列识别"):
            service.save("phantom01", {})
        with pytest.raises(ValueError, match="序列识别"):
            service.apply(dry_run=True)
        assert service.list_subjects({"queue": "protocol"})["total"] == 1
    finally:
        service.close()
    publish(identify, payload_for(identify, "t1"))
    identify.transition({"revision": identify.state["revision"], "phase": "quality"})
    require_quality(index.root)
    assert ProtocolIndex(config).identification.state["phase"] == "quality"
    identify.transition({"revision": identify.state["revision"], "phase": "identification"})
    with pytest.raises(ValueError, match="序列识别"):
        require_quality(index.root)
    assert not evidence_valid(index.root.parent, {"identification_revision": 0})


def test_sequence_rules_preserve_manual_quality_and_source_files(tmp_path):
    index, identify = make_index(tmp_path)
    uid = next(u for u in index.subjects["phantom01"] if index.records[u].candidate_type == "t1")
    manual = read_decision(index.root.parent, "phantom01")
    manual["candidates"][uid] = {"quality": "fail", "modality": "t1", "reason": "blur"}
    saved = save_decision(index.root.parent, "phantom01", manual)
    index.decisions["phantom01"] = saved
    identify.invalidate()
    path = index.root.parent / "subjects/phantom01.json"
    source = index.config.nifti_import.source_root
    before = {p: digest(p) for p in [path, *source.rglob("*.nii.gz")]}
    publish(identify, payload_for(identify, "t1"))
    assert all(digest(p) == value for p, value in before.items())
    assert read_decision(index.root.parent, "phantom01")["candidates"][uid]["quality"] == "fail"


def test_missing_is_confirmed_per_subject_not_propagated_as_quality_failure(tmp_path):
    index, identify = make_index(tmp_path, missing=True)
    p = payload_for(identify, "flair")
    p["absent"] = True
    result = publish(identify, p)
    assert result["affected_subjects"] == 1
    assert result["counts"]["flair"]["pending_subjects"] == 1
    group = next(g for g in identify.catalogue()["groups"] if g["modality"] == "flair")
    assert group["representative"] != p["subject"]
    assert not read_decision(index.root.parent, p["subject"])["groups"]


def test_duplicate_same_protocol_remains_quality_comparison(tmp_path):
    index, identify = make_index(tmp_path)
    original = next(r for r in index.records.values() if r.candidate_type == "flair")
    other = replace(original, series_uid_hash="repeat")
    from gb_dicom2bids.qc_state import candidate_id

    uid = candidate_id(other)
    index.records[uid] = other
    index.templates[uid] = next(
        t for u, t in index.templates.items() if index.records[u] is original
    )
    index.subjects[original.subject_id].append(uid)
    identify = Identification(index)
    index.identification = identify
    group = next(g for g in identify.catalogue()["groups"] if g["modality"] == "flair")
    assert not group["needs_protocol"] and group["repeat_subjects"] == 1
    assert index.choices(original.subject_id)["flair"]["top_count"] == 2
    assert index.choices(original.subject_id)["flair"]["choice"] is None


def test_versions_and_preview_cannot_be_bypassed(tmp_path):
    _, identify = make_index(tmp_path)
    payload = payload_for(identify, "t1")
    with pytest.raises(ConflictError, match="预览"):
        identify.publish(payload)
    publish(identify, payload)
    with pytest.raises(ConflictError):
        identify.preview(payload)
    p = payload_for(identify, "t1")
    for e in p["templates"].values():
        e["priority"] = 1
    with pytest.raises(ValueError, match="同优先级"):
        identify.preview(p)
    p["compare_in_quality"] = True
    publish(identify, p)


def test_catalog_uses_only_existing_inventory_not_nifti_headers(tmp_path, monkeypatch):
    index, _ = make_index(tmp_path)
    import gb_dicom2bids.qc_images as images

    def forbidden(*args, **kwargs):
        raise AssertionError("must not scan source headers")

    monkeypatch.setattr(images, "check_image", forbidden)
    assert main(["catalog", "--config", str(tmp_path / "config.yaml")]) == 0
    assert (index.root / "identification_catalogue.json").exists()


def test_inventory_change_blocks_quality_until_recatalogued(tmp_path):
    index, identify = make_index(tmp_path)
    publish(identify, payload_for(identify, "t1"))
    identify.transition({"revision": identify.state["revision"], "phase": "quality"})
    path = index.config.paths.audit_root / "series_sources.json"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="清单已变化"):
        require_quality(index.root)
    with pytest.raises(ConflictError, match="清单在本次会话"):
        identify.transition({"revision": identify.state["revision"], "phase": "quality"})
    fresh = ProtocolIndex(index.config).identification
    fresh.enable()
    assert fresh.state["phase"] == "identification"


def test_generic_names_are_not_broad_classification_rules(tmp_path):
    index, _ = make_index(tmp_path, missing=True)
    for uid, record in list(index.records.items()):
        if record.series_description == "unknown-contrast":
            index.records[uid] = replace(record, series_description="image")
    identify = Identification(index)
    families = {
        identify.families[u] for u, r in index.records.items() if r.series_description == "image"
    }
    assert len(families) == 2


def test_manual_modality_conflict_is_visible_and_not_overwritten(tmp_path):
    index, identify = make_index(tmp_path)
    uid = next(u for u in index.subjects["phantom01"] if index.records[u].candidate_type == "t1")
    index.decisions["phantom01"]["candidates"][uid] = {
        "modality": "t1",
        "quality": "pass",
        "reason": "existing manual identification",
    }
    p = payload_for(identify, "t1")
    p["templates"][identify.families[uid]]["modality"] = "other"
    result = identify.preview(p)
    assert result["conflicts"][0]["reason"] == "manual_modality_conflict"
    with pytest.raises(ConflictError, match="人工分类冲突"):
        identify.publish(dict(p, preview_digest=result["preview_digest"]))
