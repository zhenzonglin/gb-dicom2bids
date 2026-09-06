from __future__ import annotations

import copy
import csv
import io
import json
import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import nibabel as nib
import numpy as np
import pytest
from PIL import Image
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from gb_dicom2bids.config import (
    ConversionConfig,
    DatasetConfig,
    PathsConfig,
    ProjectConfig,
    SelectionConfig,
    ToolsConfig,
)
from gb_dicom2bids.convert import (
    ConversionError,
    _convert_subject,
    _install_selected,
    _resume_result,
)
from gb_dicom2bids.manifest import load_selection, write_inventory, write_selection
from gb_dicom2bids.models import ConversionResult, SelectionRow
from gb_dicom2bids.qc_images import VolumeCache
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_server import handler_class
from gb_dicom2bids.qc_state import (
    BusyError,
    ConflictError,
    authorized_choice,
    candidate_id,
    digest,
    file_lock,
    read_decision,
    record_digest,
)
from gb_dicom2bids.runtime import atomic_write_json, read_json, series_state_path


@pytest.fixture
def review(tmp_path, record_factory):
    config = ProjectConfig(
        PathsConfig(*(tmp_path / name for name in ("dicom", "old", "staging", "audit", "scratch"))),
        DatasetConfig(),
        SelectionConfig(),
        ConversionConfig(seed_from_existing_bids=False),
        ToolsConfig(),
    )
    for path in vars(config.paths).values():
        path.mkdir(parents=True)
    records = []
    for index, modality in enumerate(("t1", "t1", "flair", "other", "t1")):
        record = record_factory(
            series_uid_hash=f"synthetic{index}",
            candidate_type=modality,
            image_orientation_patient=[1, 0, 0, 0, 1, 0],
            source_relpaths=[f"synthetic{index}.dcm"],
            source_kind="derived_mpr" if index == 1 else "original",
            plane="sagittal" if index == 4 else "axial",
        )
        if index == 4:
            record.image_orientation_patient = [0, 1, 0, 0, 0, 1]
        header = Dataset()
        header.file_meta = FileMetaDataset()
        header.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        header.StudyDate = "20000101"
        header.StudyTime = "120000"
        header.save_as(config.paths.dicom_root / record.source_relpaths[0])
        records.append(record)
    rows = [
        SelectionRow(
            r.center,
            r.subject_id,
            r.study_uid_hash,
            r.series_uid_hash,
            r.candidate_type,
            ["selected", "review", "excluded", "excluded", "excluded"][i],
            1,
            "synthetic_reason",
            r.plane,
            r.source_kind,
            r.protocol_id,
            f"sub-{r.subject_id}_T1w" if i == 0 else "",
        )
        for i, r in enumerate(records)
    ]
    write_inventory(config.paths.audit_root, records, [])
    write_selection(config.paths.audit_root, rows, records)
    service = ReviewService(config)
    for index, record in enumerate(records):
        uid = candidate_id(record)
        folder = service.root / "test_images" / uid
        folder.mkdir(parents=True)
        image = folder / "candidate.nii.gz"
        affine = np.diag([1.0, 1.0, 2.0, 1.0])
        if record.plane == "sagittal":
            affine = np.array([[0, 0, 2, 0], [1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 0, 1.0]])
        data = np.indices((16, 18, 12)).sum(axis=0).astype(np.float32) + index
        nifti = nib.Nifti1Image(data, affine)
        nifti.set_qform(affine, 1)
        nib.save(nifti, image)
        sidecar = folder / "candidate.json"
        atomic_write_json(
            sidecar, {"ImageOrientationPatientDICOM": record.image_orientation_patient}
        )
        atomic_write_json(
            service.root / "artifacts" / f"{uid}.json",
            {
                "id": uid,
                "record_digest": record_digest(record),
                "image": str(image),
                "sidecar": str(sidecar),
                "image_sha256": digest(image),
                "sidecar_sha256": digest(sidecar),
            },
        )
    yield service, records, rows
    service.close()


def choice(service, record, *, modality=None):
    raw = copy.deepcopy(read_decision(service.root, record.subject_id))
    uid = candidate_id(record)
    modality = modality or record.candidate_type
    raw["candidates"][uid] = {"quality": "pass", "modality": modality, "reason": "synthetic review"}
    raw["groups"][modality] = {"choice": uid}
    return raw


def table(path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def test_all_auto_states_and_other_search(review):
    service, records, _ = review
    candidates = service.subject("001")["candidates"]
    assert len(candidates) == 5
    assert {c["auto_status"] for c in candidates} == {"selected", "review", "excluded"}
    for record in records:
        assert service.prepare(candidate_id(record))["state"] == "ready"
    assert service.list_subjects({"status": "review"})["subjects"][0]["t1"] == 3
    assert service.list_subjects({"q": "nonexistent"})["total"] == 0


def test_multiple_pass_one_choice_versions_withdrawal(review):
    service, records, _ = review
    raw = choice(service, records[0])
    second = candidate_id(records[1])
    raw["candidates"][second] = {"quality": "pass", "modality": "t1"}
    saved = service.save("001", raw)
    assert saved["revision"] == 1
    assert sum(v["quality"] == "pass" for v in saved["candidates"].values()) == 2
    assert saved["groups"]["t1"]["choice"] == candidate_id(records[0])
    assert not list(service.config.paths.staging_bids_root.rglob("*.nii.gz"))
    with pytest.raises(ConflictError):
        service.save("001", raw)
    withdrawn = copy.deepcopy(saved)
    withdrawn["groups"] = {}
    withdrawn["candidates"][candidate_id(records[0])]["quality"] = "unreviewed"
    service.save("001", withdrawn)
    (service.root / "subjects/001.json").unlink()
    assert read_decision(service.root, "001")["revision"] == 2
    assert not read_decision(service.root, "001")["groups"]["t1"]["choice"]


@pytest.mark.parametrize("change", ["fail", "none", "reclassify"])
def test_reason_required(review, change):
    service, records, _ = review
    raw = choice(service, records[0])
    uid = candidate_id(records[0])
    if change == "none":
        raw["groups"]["t1"] = {"none": True}
    elif change == "fail":
        raw["candidates"][uid] = {"quality": "fail"}
    else:
        raw["candidates"][uid] = {"modality": "flair"}
    with pytest.raises(ValueError, match="reason"):
        service.save("001", raw)


def test_reclassify_other_install_and_sagittal_block(review):
    service, records, _ = review
    with pytest.raises(ValueError, match="T1 must"):
        service.save("001", choice(service, records[4]))
    service.save("001", choice(service, records[3], modality="flair"))
    service.apply(dry_run=False)
    rows = load_selection(service.config.paths.audit_root / "selection_manifest.tsv")
    changed = next(r for r in rows if r.series_uid_hash == records[3].series_uid_hash)
    assert changed.candidate_type == "flair" and changed.decision_status == "selected"


def test_distinct_study_does_not_block_final_choices(review):
    service, records, _ = review
    uid = candidate_id(records[2])
    # Simulate distinct study metadata while keeping the test's candidate lookup fixed.
    service.records[uid].study_uid_hash = "repeatstudy"
    artifact = service.root / "artifacts" / f"{uid}.json"
    value = read_json(artifact)
    value["record_digest"] = record_digest(service.records[uid])
    atomic_write_json(artifact, value)
    raw = choice(service, records[0])
    raw["candidates"][uid] = {"quality": "pass", "modality": "flair"}
    raw["groups"]["flair"] = {"choice": uid}
    raw.update(episode_confirmed=False, episode_reason="")  # ignored legacy fields
    saved = service.save("001", raw)
    assert saved["groups"]["t1"]["choice"] == candidate_id(records[0])
    assert saved["groups"]["flair"]["choice"] == uid
    assert "episode_confirmed" not in saved and "episode_reason" not in saved


def test_time_conflict_does_not_block_final_choices(review):
    service, records, _ = review
    raw = choice(service, records[0])
    uid = candidate_id(records[2])
    service._times[uid] = {"StudyDate": "20000102"}
    raw["candidates"][uid] = {"quality": "pass", "modality": "flair"}
    raw["groups"]["flair"] = {"choice": uid}
    assert service.save("001", raw)["groups"]["flair"]["choice"] == uid


def test_apply_new_subject_replace_quarantine_and_source_unchanged(review):
    service, records, _ = review
    source_hashes = {
        p: digest(p) for p in service.config.paths.dicom_root.rglob("*") if p.is_file()
    }
    old = service.config.paths.existing_bids_root / "sub-009/anat"
    old.mkdir(parents=True)
    artifact = service.artifact(candidate_id(records[0]))
    old_image = old / "sub-009_T1w.nii.gz"
    old_image.write_bytes(Path(artifact["image"]).read_bytes())
    old_hash = digest(old_image)
    service.save("001", choice(service, records[0]))
    assert len(service.apply()) == 1
    assert not list(service.config.paths.staging_bids_root.rglob("*.nii.gz"))
    service.apply(dry_run=False)
    accepted = table(service.root / "accepted_manifest.tsv")
    assert len(accepted) == 1 and accepted[0]["subject_id"] == "001"
    assert (
        next(
            r
            for r in table(service.root / "coverage.tsv")
            if r["subject_id"] == "001" and r["modality"] == "t1"
        )["newly_added"]
        == "True"
    )
    assert service.apply() == []  # checksum-verified, idempotent replay
    original = Path(accepted[0]["image"])
    original_hash = digest(original)
    service.save("001", choice(service, records[1]))
    assert table(service.root / "accepted_manifest.tsv") == []
    service.apply(dry_run=False)
    assert not original.exists()
    mpr = service.config.paths.staging_bids_root / "sub-001/anat/sub-001_rec-axialmpr_T1w.nii.gz"
    assert mpr.exists()
    assert digest(service.root / "backups/001/2_t1/sub-001_T1w.nii.gz") == original_hash
    raw = read_decision(service.root, "001")
    raw["groups"]["t1"] = {"none": True, "reason": "unusable coverage"}
    service.save("001", raw)
    service.apply(dry_run=False)
    assert not mpr.exists()
    assert table(service.root / "accepted_manifest.tsv") == []
    assert not table(mpr.parent.parent / "sub-001_scans.tsv")
    assert digest(old_image) == old_hash
    assert all(digest(p) == value for p, value in source_hashes.items())


def test_failed_install_is_not_certified_and_replays(review, monkeypatch):
    import gb_dicom2bids.qc_review as module

    service, records, _ = review
    service.save("001", choice(service, records[0]))
    original = module.write_selection
    monkeypatch.setattr(
        module,
        "write_selection",
        lambda *a, **k: (_ for _ in ()).throw(OSError("NFS test interruption")),
    )
    with pytest.raises(OSError, match="NFS test"):
        service.apply(dry_run=False)
    assert service.write_accepted() == []
    assert read_json(service.root / "transactions/001_t1_1.json")["state"] == "files_ready"
    monkeypatch.setattr(module, "write_selection", original)
    service.apply(dry_run=False)
    assert len(service.write_accepted()) == 1


def test_checksum_damage_and_resume_target_matching(review):
    service, records, rows = review
    uid = candidate_id(records[0])
    artifact = service.artifact(uid)
    service.save("001", choice(service, records[0]))
    image = Path(artifact["image"])
    with image.open("ab") as handle:
        handle.write(b"damage")
    assert service.artifact(uid, deep=True) == {}
    with pytest.raises(ValueError, match="readable"):
        service.apply()
    prior = {
        "stage": "review_ready",
        "subject_id": "001",
        "series_uid_hash": records[0].series_uid_hash,
        "output_path": str(image),
        "output_sha256": digest(image),
        "sidecar_path": artifact["sidecar"],
        "sidecar_sha256": artifact["sidecar_sha256"],
    }
    assert _resume_result(prior, records[0], rows[0], True, False) is None
    assert (
        _resume_result(
            prior, records[0], replace(rows[0], decision_status="review"), True, False
        ).status
        == "skipped"
    )
    prior["stage"] = "converted"
    assert (
        _resume_result(
            prior,
            records[0],
            rows[0],
            True,
            False,
            expected_output=image.parent / "different.nii.gz",
        )
        is None
    )


def test_ordinary_resume_cannot_install_saved_choice(review, monkeypatch):
    import gb_dicom2bids.convert as module

    service, records, rows = review
    service.save("001", choice(service, records[0]))
    artifact = service.artifact(candidate_id(records[0]))
    with pytest.raises(ConversionError, match="--apply"):
        _install_selected(
            service.config, records[0], rows[0], Path(artifact["image"]), Path(artifact["sidecar"])
        )
    captured = []

    def fake(config, record, row):
        captured.append(row.decision_status)
        return ConversionResult(
            record.subject_id,
            record.series_uid_hash,
            record.candidate_type,
            "review_ready",
            "review",
        ), []

    monkeypatch.setattr(module, "_convert_one", fake)
    _convert_subject(service.config, records[:1], rows[:1], True, False)
    assert captured == ["review"]
    assert not list(service.config.paths.staging_bids_root.rglob("*.nii.gz"))


def test_damaged_installed_output_repaired_from_reviewed_cache(review):
    service, records, _ = review
    service.save("001", choice(service, records[0]))
    service.apply(dry_run=False)
    cert = service.write_accepted()[0]
    target = Path(cert["image"])
    target.write_bytes(b"broken")
    assert service.write_accepted() == []
    assert len(service.apply()) == 1
    service.apply(dry_run=False)
    assert digest(target) == cert["image_sha256"]


def test_withdrawal_quarantines_without_declaring_all_candidates_failed(review):
    service, records, _ = review
    service.save("001", choice(service, records[0]))
    service.apply(dry_run=False)
    raw = read_decision(service.root, "001")
    raw["groups"] = {}
    raw["candidates"][candidate_id(records[0])]["quality"] = "defer"
    service.save("001", raw)
    service.apply(dry_run=False)
    assert not list(service.config.paths.staging_bids_root.rglob("*.nii.gz"))
    assert all(
        r.decision_status == "review"
        for r in load_selection(service.config.paths.audit_root / "selection_manifest.tsv")
        if r.candidate_type == "t1"
    )


def test_source_geometry_blocks_bypass(review):
    service, records, _ = review
    service.save("001", choice(service, records[0]))
    assert authorized_choice(service.config, records[0])
    assert not authorized_choice(service.config, replace(records[0], image_orientation_patient=[]))
    assert not authorized_choice(service.config, replace(records[0], source_kind="derived_other"))


def test_resource_guard_and_pipeline_mutual_exclusion(review, monkeypatch):
    import gb_dicom2bids.qc_review as module
    import gb_dicom2bids.qc_state as state

    service, records, _ = review
    service.save("001", choice(service, records[0]))
    monkeypatch.setattr(module, "resource_blockers", lambda *args: ["disk low"])
    with pytest.raises(ValueError, match="resource guard"):
        service.apply(dry_run=False)
    monkeypatch.setattr(state.psutil, "pid_exists", lambda pid: True)
    atomic_write_json(service.config.paths.audit_root / "run_status.json", {"pid": 12345678})
    with pytest.raises(BusyError, match="alive"):
        service.apply()
    with (
        file_lock(service.root / "test.lock"),
        pytest.raises(BusyError),
        file_lock(service.root / "test.lock"),
    ):
        pass


def test_preview_isolated_for_excluded_other_and_failure_retained(review, monkeypatch):
    import gb_dicom2bids.convert as module

    service, records, _ = review
    record = records[3]
    uid = candidate_id(record)
    value = read_json(service.root / "artifacts" / f"{uid}.json")
    (service.root / "artifacts" / f"{uid}.json").unlink()

    def fake(config, actual, row, *, preview_only):
        assert config.paths.audit_root == service.root / "jobs" / uid
        assert preview_only and row.decision_status == "review"
        assert config.conversion.compression_threads == 2
        return ConversionResult(
            "001",
            actual.series_uid_hash,
            "other",
            "review_ready",
            "review",
            output_path=value["image"],
            sidecar_path=value["sidecar"],
            output_sha256=value["image_sha256"],
            sidecar_sha256=value["sidecar_sha256"],
        ), []

    monkeypatch.setattr(module, "_convert_one", fake)
    service._prepare_job(uid)
    assert service.jobs[uid]["state"] == "ready"
    assert not series_state_path(service.config.paths.audit_root, record.series_uid_hash).exists()
    monkeypatch.setattr(
        module,
        "_convert_one",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic decoder error")),
    )
    service._prepare_job(uid)
    assert service.jobs[uid]["state"] == "failed"
    assert "decoder error" in service.log_text(uid)
    assert len(service.subject("001")["candidates"]) == 5


def test_source_voxel_slice_is_exact_and_affine_independent(tmp_path):
    data = np.arange(16 * 20 * 12, dtype=np.float32).reshape(16, 20, 12)
    image = tmp_path / "source-grid.nii.gz"
    affine = np.diag([-2.0, 1.0, 3.0, 1.0])
    nib.save(nib.Nifti1Image(data, affine), image)
    initial = digest(image)
    volume = VolumeCache()
    high = float(data.max())
    png = volume.slice_png(image, 7, 0.0, high)
    pixels = np.array(Image.open(io.BytesIO(png)))
    expected_values = np.flipud(data[:, :, 7].T)
    expected = np.round(expected_values / high * 255).astype(np.uint8)
    np.testing.assert_array_equal(pixels, expected)
    assert pixels.shape == (20, 16)
    meta = volume.metadata(image)
    assert meta["display_mode"] == "source_voxel_slices"
    assert meta["slice_count"] == 12
    assert meta["initial_slice"] == 6

    theta = np.deg2rad(35)
    affine[:3, :3] = [
        [1, 0, 0],
        [0, np.cos(theta), -np.sin(theta)],
        [0, np.sin(theta), np.cos(theta)],
    ]
    oblique = tmp_path / "oblique.nii.gz"
    nib.save(nib.Nifti1Image(data, affine), oblique)
    oblique_initial = digest(oblique)
    assert volume.slice_png(oblique, 7, 0.0, high) == png
    assert digest(image) == initial
    assert digest(oblique) == oblique_initial
    with pytest.raises(ValueError, match="slice index"):
        volume.slice_png(image, -1, 0.0, high)
    with pytest.raises(ValueError, match="slice index"):
        volume.slice_png(image, 12, 0.0, high)
    with pytest.raises(ValueError, match="integer"):
        volume.slice_png(image, 1.5, 0.0, high)


def test_invalid_multivolume_cannot_pass(review):
    service, records, _ = review
    uid = candidate_id(records[0])
    entry = service.root / "artifacts" / f"{uid}.json"
    artifact = read_json(entry)
    image = Path(artifact["image"])
    nib.save(nib.Nifti1Image(np.ones((12, 12, 12, 2), dtype=np.float32), np.eye(4)), image)
    artifact["image_sha256"] = digest(image)
    atomic_write_json(entry, artifact)
    assert service.prepare(uid)["state"] == "ready"
    with pytest.raises(ValueError, match="spatially valid"):
        service.save("001", choice(service, records[0]))


def test_legacy_identity_checksum_reuse_and_freeze(review):
    service, records, _ = review
    record = records[0]
    uid = candidate_id(record)
    value = service.artifact(uid)
    folder = service.config.paths.staging_bids_root / "sub-001/anat"
    folder.mkdir(parents=True)
    image, sidecar = folder / "sub-001_T1w.nii.gz", folder / "sub-001_T1w.json"
    image.write_bytes(Path(value["image"]).read_bytes())
    sidecar.write_bytes(Path(value["sidecar"]).read_bytes())
    (service.root / "artifacts" / f"{uid}.json").unlink()
    assert service.artifact(uid) == {}  # a matching old BIDS filename alone proves nothing
    state_path = series_state_path(service.config.paths.audit_root, record.series_uid_hash)
    state = {
        "stage": "converted",
        "subject_id": "wrong",
        "series_uid_hash": record.series_uid_hash,
        "output_path": str(image),
        "sidecar_path": str(sidecar),
        "output_sha256": digest(image),
        "sidecar_sha256": digest(sidecar),
    }
    atomic_write_json(state_path, state)
    assert service.artifact(uid) == {}
    state["subject_id"] = "001"
    atomic_write_json(state_path, state)
    frozen = service.artifact(uid)
    assert Path(frozen["image"]).is_relative_to(service.root / "frozen")
    image.write_bytes(b"later pipeline replacement")
    assert digest(Path(frozen["image"])) == frozen["image_sha256"]


def test_preview_resource_pause_can_exit_without_conversion(review, monkeypatch):
    import gb_dicom2bids.qc_review as module

    service, records, _ = review
    uid = candidate_id(records[0])
    monkeypatch.setattr(module, "resource_blockers", lambda *args: ["temporary storage low"])

    def stop_wait(_seconds):
        assert service.jobs[uid]["state"] == "paused_resources"
        service.stop.set()

    monkeypatch.setattr(service.stop, "wait", stop_wait)
    service._prepare_job(uid)
    assert service.jobs[uid]["state"] == "interrupted"


def test_source_scan_inconsistency_blocks_final_choice(review):
    service, records, _ = review
    uid = candidate_id(records[0])
    service.records[uid].orientation_consistent = False
    entry = service.root / "artifacts" / f"{uid}.json"
    artifact = read_json(entry)
    artifact["record_digest"] = record_digest(service.records[uid])
    atomic_write_json(entry, artifact)
    with pytest.raises(ValueError, match="source geometry"):
        service.save("001", choice(service, records[0]))


def test_http_token_origin_version_and_local_assets(review):
    service, records, _ = review
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(service, "test-token"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"

    def request(path, data=None, *, token="test-token", source=origin):
        req = Request(
            origin + path,
            data=json.dumps(data).encode() if data else None,
            headers={"X-QC-Token": token, "Origin": source, "Content-Type": "application/json"},
        )
        return urlopen(req, timeout=10)

    try:
        html = request("/").read()
        script = request("/app.js").read()
        assert b"test-token" in html
        assert b'id="others" type="checkbox"' in html
        assert b'id="others" type="checkbox" checked' not in html
        assert request("/identify.js").status == 200
        assert b'id="episode"' not in html
        assert "按文件夹名独立选择序列".encode() in html
        assert "原始NIfTI第三维体素切片".encode() in html
        assert b"comparisonCandidates" in script
        assert b"episode_confirmed" not in script
        assert b"const planes" not in script
        assert b"meta.bounds" not in script
        assert b"index:slider.value" in script
        assert "展示序列".encode() in script
        assert "指定为 FLAIR".encode() in script
        with pytest.raises(HTTPError) as error:
            request("/api/subjects", token="wrong")
        assert error.value.code == 403
        payload = {"subject": "001", "decision": choice(service, records[0])}
        with pytest.raises(HTTPError) as error:
            request("/api/save", payload, source="http://evil.invalid")
        assert error.value.code == 403
        assert request("/api/save", payload).status == 200
        with pytest.raises(HTTPError) as error:
            request("/api/save", payload)
        assert error.value.code == 409
        with pytest.raises(HTTPError) as error:
            request("/../config/config.local.yaml")
        assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
