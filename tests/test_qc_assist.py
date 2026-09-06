from __future__ import annotations

import shutil
from dataclasses import replace

import nibabel as nib
import numpy as np
import pytest
from conftest import write_nifti
from test_nifti_import import make_config, make_image

from gb_dicom2bids.models import SeriesRecord
from gb_dicom2bids.nifti_import import inventory_preconverted
from gb_dicom2bids.qc_assist import effective_decision, evidence_valid, feedback, propose
from gb_dicom2bids.qc_features import FEATURE_NAMES, FEATURE_VERSION, extract, run_features
from gb_dicom2bids.qc_learning import (
    calibrate,
    feature_rows,
    human_label,
    load_model,
    partition,
    upper_error_bound,
)
from gb_dicom2bids.qc_protocols import ProtocolIndex, fingerprint, normalize_name, template
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_state import (
    ConflictError,
    authorized_choice,
    candidate_id,
    digest,
    read_decision,
    record_digest,
    save_decision,
)
from gb_dicom2bids.runtime import atomic_write_json, read_json


def catalogue(index):
    value = index.catalogue()
    atomic_write_json(index.root / "catalogue.json", value)
    return value


def test_protocol_templates_conflicts_preview_and_revoke(tmp_path):
    source = tmp_path / "source"
    for subject, date in (("phantom01", "200001011200"), ("phantom02", "200001021200")):
        for name in ("0602__eT1W-SE", "0603__T1-local", "0702__eFLAIR-longTR-CLEAR"):
            make_image(source / f"site/{subject}/{date}__MR__{name}/image.nii.gz")
    make_image(source / "elsewhere/phantom03/200001031200__MR__0602__eT1W-SE/image.nii.gz")
    config = make_config(tmp_path, source)
    inventory_preconverted(config)
    index = ProtocolIndex(config)
    value = catalogue(index)
    group = next(g for g in value["groups"] if g["count"] == 2)
    assert group["needs_protocol"]
    assert normalize_name("200001011200__MR__0602__eT1W-SE") == "et1w-se"
    assert normalize_name("3D_T1_2mm") == "3d-t1-2mm"
    assert normalize_name("99999999_MR_0602_T1") == "99999999-mr-0602-t1"
    entries = {
        e["id"]: {"modality": e["modality"], "priority": 1 if e["name"] == "et1w-se" else 2}
        for e in group["templates"]
    }
    payload = {
        "group": group["id"],
        "revision": 0,
        "inventory_digest": index.inventory_digest,
        "templates": entries,
        "reviewer": "zhenzong",
        "reason": "synthetic protocol review",
    }
    preview = index.preview(payload)
    assert preview["affected_subjects"] == 2 and not preview["conflicts"]
    index.publish(dict(payload, preview_digest=preview["preview_digest"]))
    assert index.choices("phantom02")["t1"]["choice"]
    assert read_decision(index.root.parent, "phantom02")["candidates"] == {}
    with pytest.raises(ConflictError):
        index.publish(dict(payload, preview_digest=preview["preview_digest"]))
    index.revoke({"group": group["id"], "revision": 1})
    assert index.choices("phantom02")["t1"]["choice"] is None
    uid = index.subjects["phantom02"][0]
    raw = read_decision(index.root.parent, "phantom02")
    raw["candidates"][uid] = {"quality": "defer", "modality": "other", "reason": "wrong name"}
    save_decision(index.root.parent, "phantom02", raw)
    index = ProtocolIndex(config)
    payload["revision"] = 2
    preview = index.preview(payload)
    assert preview["conflicts"]
    with pytest.raises(ConflictError, match="conflict"):
        index.publish(dict(payload, preview_digest=preview["preview_digest"]))
    record = next(iter(index.records.values()))
    assert template(record)["id"] != template(replace(record, center="different"))["id"]
    assert template(record)["id"] != template(replace(record, instance_count=42))["id"]


def test_true_repeats_are_not_resolved_by_a_shared_template(tmp_path):
    source = tmp_path / "source"
    make_image(source / "site/phantom01/T1/repeat-a.nii.gz")
    make_image(source / "site/phantom01/T1/repeat-b.nii.gz")
    config = make_config(tmp_path, source)
    inventory_preconverted(config)
    index = ProtocolIndex(config)
    assert index.choices("phantom01")["t1"]["top_count"] == 2
    assert index.choices("phantom01")["t1"]["choice"] is None


def test_native_quality_blur_oblique_and_bad_inputs(tmp_path):
    from scipy.ndimage import gaussian_filter

    shape = (64, 60, 20)
    x, y, z = np.indices(shape, dtype=float)
    mask = ((x - 32) / 23) ** 2 + ((y - 30) / 22) ** 2 + ((z - 10) / 9) ** 2 < 1
    data = ((0.6 + 0.2 * np.sin(x * 0.8) + 0.15 * np.cos(y * 0.7)) * mask).astype(np.float32)
    path = tmp_path / "sharp.nii.gz"
    nib.save(nib.Nifti1Image(data, np.diag([1, 1, 6, 1])), path)
    initial = digest(path)
    sharp = extract(path)
    blurred = tmp_path / "blurred.nii.gz"
    nib.save(nib.Nifti1Image(gaussian_filter(data, [1.8, 1.8, 0]), np.diag([1, 1, 6, 1])), blurred)
    blur = extract(blurred)
    assert blur["features"]["sharpness_p50"] < sharp["features"]["sharpness_p50"]
    assert blur["features"]["hf_p50"] < sharp["features"]["hf_p50"]
    assert sharp["features"]["voxel_k"] == 6
    affine = np.array([[1, 0, 0, 0], [0, 0.8, -3.6, 0], [0, 0.6, 4.8, 0], [0, 0, 0, 1.0]])
    oblique = tmp_path / "oblique.nii.gz"
    nib.save(nib.Nifti1Image(data, affine), oblique)
    assert extract(oblique)["features"] == pytest.approx(sharp["features"])
    assert digest(path) == initial
    for name, bad in (
        ("empty", np.zeros(shape)),
        ("nan", data * np.nan),
        ("four", np.stack([data, data], axis=3)),
    ):
        target = tmp_path / f"{name}.nii.gz"
        nib.save(nib.Nifti1Image(bad, np.eye(4)), target)
        with pytest.raises(ValueError):
            extract(target)


def test_features_resume_checksum_and_source_unchanged(tmp_path):
    source = tmp_path / "source"
    path = make_image(source / "site/phantom01/T1/image.nii.gz")
    config = make_config(tmp_path, source)
    inventory_preconverted(config)
    index = ProtocolIndex(config)
    initial = digest(path)
    result = run_features(index, workers=2)
    assert result["completed"] == 1
    assert run_features(index, workers=2)["cached"] == 1
    assert digest(path) == initial
    nib.save(nib.Nifti1Image(np.zeros((24, 24, 24)), np.eye(4)), path)
    assert run_features(index, workers=2)["failed"] == 1


@pytest.fixture
def learning_cohort(tmp_path, request):
    modality = getattr(request, "param", "t1")
    source = tmp_path / "source"
    source.mkdir()
    seed = write_nifti(tmp_path / "seed.nii.gz")
    subjects = {"train": [], "calibration": [], "audit": []}
    for i in range(5000):
        s = f"phantom{i:04}"
        split = partition(s)
        target = {"train": 45, "calibration": 25, "audit": 65}[split]
        if len(subjects[split]) < target:
            subjects[split].append(s)
        if sum(map(len, subjects.values())) == 135:
            break
    for group in subjects.values():
        for s in group:
            name = "T1" if modality == "t1" else "FLAIR"
            path = source / f"site/{s}/{name}/image.nii.gz"
            path.parent.mkdir(parents=True)
            shutil.copyfile(seed, path)
    config = make_config(tmp_path, source)
    inventory_preconverted(config)
    index = ProtocolIndex(config)
    catalogue(index)
    checksum = digest(seed)
    sidecar = index.root.parent / "sidecars/synthetic.json"
    atomic_write_json(
        sidecar, {"SourceFormat": "preconverted_nifti", "SourceMetadataAvailable": False}
    )
    sidecar_sha = digest(sidecar)
    # These synthetic vectors exercise split/acceptance logic, not clinical accuracy.
    for uid, record in index.records.items():
        subject = record.subject_id
        split = partition(subject)
        j = subjects[split].index(subject)
        good = split == "audit" or j % 2 == 0
        path = source / record.source_relpaths[0]
        stat = path.stat()
        atomic_write_json(
            index.root.parent / "artifacts" / f"{uid}.json",
            {
                "id": uid,
                "record_digest": record_digest(record),
                "image": str(path),
                "sidecar": str(sidecar),
                "image_sha256": checksum,
                "sidecar_sha256": sidecar_sha,
            },
        )
        values = {name: float(good) for name in FEATURE_NAMES}
        values.update(voxel_i=1.0, voxel_j=1.0, voxel_k=1.0)
        atomic_write_json(
            index.root / "features" / f"{uid}.json",
            {
                "id": uid,
                "state": "completed",
                "version": FEATURE_VERSION,
                "features": values,
                "record_digest": record_digest(record),
                "image_sha256": checksum,
                "source_stats": [stat.st_size, stat.st_mtime_ns],
                "suspect_slices": [10],
            },
        )
        if split != "audit" and j < len(subjects[split]) - 1:
            raw = read_decision(index.root.parent, subject)
            raw["candidates"][uid] = {
                "quality": "pass" if good else "fail",
                "modality": modality,
                "failure_category": "motion_blur" if not good else "",
                "reason": "synthetic quality",
                "record_digest": record_digest(record),
                "image_sha256": checksum,
                "sidecar_sha256": sidecar_sha,
            }
            save_decision(index.root.parent, subject, raw)
    return config, subjects


def test_frozen_model_patient_isolation_and_independent_audit(learning_cohort):
    config, _ = learning_cohort
    index = ProtocolIndex(config)
    fitted = calibrate(index)
    assert fitted["t1"]["state"] == "awaiting_independent_audit"
    model = load_model(index.root, "t1")
    assert not set(model["training_subjects"]) & set(model["calibration_subjects"])
    initial = propose(index)
    assert initial["counts"].get("auto_pass", 0) == 0
    audit = read_json(index.root / "models" / model["version"] / "audit.json")
    assert len(audit["entries"]) == 59
    assert all(partition(e["subject"]) == "audit" for e in audit["entries"])
    assert all(
        read_decision(index.root.parent, e["subject"])["revision"] == 0 for e in audit["entries"]
    )
    for entry in audit["entries"]:
        uid, subject = entry["id"], entry["subject"]
        feature = read_json(index.root / "features" / f"{uid}.json")
        raw = read_decision(index.root.parent, subject)
        raw["candidates"][uid] = {
            "quality": "pass",
            "modality": "t1",
            "reason": "independent review",
            "record_digest": feature["record_digest"],
            "image_sha256": feature["image_sha256"],
            "sidecar_sha256": read_json(index.root.parent / "artifacts" / f"{uid}.json")[
                "sidecar_sha256"
            ],
        }
        save_decision(index.root.parent, subject, raw)
    index = ProtocolIndex(config)
    assert calibrate(index)["t1"]["version"] == model["version"]
    result = propose(index)
    assert result["validation"]["t1"]["valid"]
    assert upper_error_bound(0, 58) > 0.05 > upper_error_bound(0, 59)
    assert upper_error_bound(1, 59) > 0.05
    assert result["counts"]["auto_pass"] > 0
    proposal_path = next(
        p for p in (index.root / "proposals").glob("*.json") if read_json(p)["groups"]
    )
    subject = proposal_path.stem
    proof = read_json(proposal_path)["groups"]["t1"]
    assert evidence_valid(index.root.parent, proof)
    model_path = index.root / "models" / model["version"] / "model.json"
    original_model = read_json(model_path)
    atomic_write_json(model_path, dict(original_model, intercept=999))
    assert not evidence_valid(index.root.parent, proof)
    atomic_write_json(model_path, original_model)
    assert evidence_valid(index.root.parent, proof)
    assert (
        effective_decision(index.root.parent, subject)["groups"]["t1"]["choice"]
        == proof["candidate_id"]
    )
    assert read_decision(index.root.parent, subject)["groups"] == {}
    record = index.records[proof["candidate_id"]]
    assert authorized_choice(config, record)
    service = ReviewService(config)
    try:
        other_proposal = next(
            p
            for p in (index.root / "proposals").glob("*.json")
            if p != proposal_path and read_json(p)["groups"]
        )
        other = read_json(other_proposal)["groups"]["t1"]
        manual = read_decision(index.root.parent, other_proposal.stem)
        manual["candidates"][other["candidate_id"]] = {
            "quality": "defer",
            "modality": "t1",
            "reason": "manual override",
        }
        service.save(other_proposal.stem, manual)
        assert not effective_decision(index.root.parent, other_proposal.stem)["groups"]["t1"].get(
            "choice"
        )
        actions = service.apply(dry_run=True)
        assert any(a.get("decision_source") == "automatic" for a in actions)
        service.apply(dry_run=False)
        assert service.apply(dry_run=True) == []
        propose(ProtocolIndex(config))
        assert service.apply(dry_run=True) == []  # Re-proposing must not reinstall unchanged files.
        cert = next(c for c in service.write_accepted() if c["subject_id"] == subject)
        assert cert["reviewer"] == "automatic" and cert["decision_source"] == "automatic"
        assert (
            digest(config.paths.staging_bids_root / f"sub-{subject}/anat/sub-{subject}_T1w.nii.gz")
            == proof["image_sha256"]
        )
        # Rule withdrawal invalidates authorization and offers recoverable quarantine.
        atomic_write_json(index.root / "rules.json", {"revision": 1, "groups": {}})
        assert not authorized_choice(config, record)
        assert not evidence_valid(index.root.parent, proof)
        assert service.write_accepted() == []
        assert any(a["action"] == "quarantine" for a in service.apply(dry_run=True))
        service.apply(dry_run=False)
        assert not list(config.paths.staging_bids_root.glob("sub-*/anat/*.nii.gz"))
        assert list((index.root.parent / "backups").glob("*/*/*.nii.gz"))
    finally:
        service.close()


def test_label_filtering_does_not_turn_not_selected_into_quality_failure(learning_cohort):
    config, _ = learning_cohort
    index = ProtocolIndex(config)
    row = next(r for r in feature_rows(index) if human_label(index, r) == 0)
    rating = index.decisions[row["subject"]]["candidates"][row["id"]]
    rating.update(failure_category="not_selected", reason="未选择")
    assert human_label(index, row) is None
    rating.update(failure_category="classification", reason="模态误分类")
    assert human_label(index, row) is None
    rating.update(failure_category="motion_blur", reason="模糊")
    assert human_label(index, row) == 0
    rating["image_sha256"] = "changed"
    assert human_label(index, row) is None


def test_audit_error_suspends_domain_and_new_model_never_reuses_audit(learning_cohort):
    config, _ = learning_cohort
    index = ProtocolIndex(config)
    assert calibrate(index)["flair"]["state"] == "needs_labels"
    propose(index)
    model = load_model(index.root, "t1")
    manifest = read_json(index.root / "models" / model["version"] / "audit.json")
    for i, entry in enumerate(manifest["entries"]):
        raw = read_decision(index.root.parent, entry["subject"])
        f = read_json(index.root / "features" / f"{entry['id']}.json")
        raw["candidates"][entry["id"]] = {
            "quality": "fail" if i == 0 else "pass",
            "modality": "t1",
            "failure_category": "motion_blur",
            "reason": "independent phantom rating",
            "record_digest": f["record_digest"],
            "image_sha256": f["image_sha256"],
        }
        save_decision(index.root.parent, entry["subject"], raw)
    index = ProtocolIndex(config)
    feedback(index, manifest["entries"][0]["subject"])
    assert read_json(index.root / "suspensions.json")
    assert read_json(index.root / "recheck.json")["rows"]
    result = propose(index)
    assert result["validation"]["t1"]["errors"] == 1
    assert not result["validation"]["t1"]["valid"]
    assert not result["counts"].get("auto_pass")
    calibrate(index, new_model=True, audit_size=100)
    new = load_model(index.root, "t1")
    assert new["version"] != model["version"]
    propose(index)
    fresh = read_json(index.root / "models" / new["version"] / "audit.json")
    assert fresh["random_required"] == 100
    assert not (
        {e["subject"] for e in fresh["entries"]} & {e["subject"] for e in manifest["entries"]}
    )
    # An explicit retrain must not silently retain the old active model if labels vanish.
    for decision in index.decisions.values():
        decision["candidates"] = {}
    assert calibrate(index, new_model=True)["t1"]["state"] == "needs_labels"
    assert not load_model(index.root, "t1")


def test_failed_feature_cache_and_resource_pause(tmp_path, monkeypatch):
    import gb_dicom2bids.qc_features as module

    source = tmp_path / "source"
    path = make_image(source / "site/phantom01/T1/image.nii.gz")
    config = make_config(tmp_path, source)
    inventory_preconverted(config)
    index = ProtocolIndex(config)
    calls = []

    def blockers(*args):
        calls.append(1)
        return ["synthetic disk guard"] if len(calls) == 1 else []

    monkeypatch.setattr(module, "resource_blockers", blockers)
    assert run_features(index, workers=1)["completed"] == 1
    assert len(calls) >= 2
    path.unlink()  # Synthetic fixture only; verify an unreadable image retains a failed row.
    assert run_features(index, workers=1)["failed"] == 1
    assert read_json(next((index.root / "features").glob("*.running.json")))["pid"] is None


def test_local_bad_layers_and_ghost_features_are_finite(tmp_path):
    from scipy.ndimage import gaussian_filter

    x, y, z = np.indices((64, 64, 24), dtype=float)
    mask = ((x - 32) / 20) ** 2 + ((y - 32) / 22) ** 2 + ((z - 12) / 10) ** 2 < 1
    data = mask * (1 + 0.3 * np.sin(x) + 0.2 * np.cos(y))
    data[:, :, 10:13] = gaussian_filter(data[:, :, 10:13], [3, 3, 0])
    path = tmp_path / "local.nii.gz"
    nib.save(nib.Nifti1Image(data, np.eye(4)), path)
    local = extract(path)
    assert set(local["suspect_slices"]) & {10, 11, 12}
    nib.save(nib.Nifti1Image(data + 0.25 * np.roll(data, 25, axis=1), np.eye(4)), path)
    ghost = extract(path)
    assert all(np.isfinite(list(ghost["features"].values())))
    assert ghost["features"]["background_ratio"] > 0


@pytest.mark.parametrize("learning_cohort", ["flair"], indirect=True)
def test_flair_model_does_not_authorize_t1(learning_cohort):
    config, _ = learning_cohort
    index = ProtocolIndex(config)
    fitted = calibrate(index)
    assert fitted["flair"]["state"] == "awaiting_independent_audit"
    assert fitted["t1"]["state"] == "needs_labels"
    assert not load_model(index.root, "t1")
    result = propose(index)
    assert not result["validation"]["flair"]["valid"]
    assert not result["counts"].get("auto_pass")


def test_viewer_index_reuses_sparse_decisions_and_defers_full_digest(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import Mock

    import gb_dicom2bids.qc_protocols as protocol_module
    import gb_dicom2bids.qc_review as review_module

    config = make_config(tmp_path, tmp_path / "source")
    records = [
        SeriesRecord(
            center="synthetic",
            subject_id=f"phantom{i:04}",
            study_uid_hash="study",
            series_uid_hash=f"series{i}",
            series_description="T1",
            candidate_type="t1",
        )
        for i in range(1000)
    ]
    monkeypatch.setattr(review_module, "load_private_records", lambda root: records)
    monkeypatch.setattr(review_module, "load_selection", lambda path: [])
    no_reads = Mock(side_effect=AssertionError("must not re-read per-patient QC on list request"))
    monkeypatch.setattr(protocol_module, "read_decision", no_reads)
    hashes = Mock(wraps=record_digest)
    monkeypatch.setattr(protocol_module, "record_digest", hashes)
    atomic_write_json(config.paths.audit_root / "visual_qc/assist/catalogue.json", {"groups": []})
    service = ReviewService(config, activate=False)
    uid = candidate_id(records[0])
    manual = {
        "revision": 7,
        "candidates": {
            uid: {"quality": "defer", "modality": "flair", "reason": "synthetic correction"}
        },
        "groups": {},
    }
    service.decisions[records[0].subject_id] = manual
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            indices = list(pool.map(lambda _: service.assistance(), range(2)))
        assert indices[0] is indices[1]
        assert indices[0].decisions[records[0].subject_id] is manual
        listed = service.list_subjects({"others": "1"})
        assert listed["total"] == 1000 and len(listed["subjects"]) == 100
        assert listed["subjects"][0]["flair"] == 1
        assert no_reads.call_count == hashes.call_count == 0
        # The exact old digest remains mandatory for preview/publish and is cached once.
        expected = fingerprint(sorted((candidate_id(r), record_digest(r)) for r in records))
        assert indices[0].inventory_digest == expected
        assert indices[1].inventory_digest == expected
        assert hashes.call_count == 1000
    finally:
        service.close()
