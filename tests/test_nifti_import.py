from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from conftest import write_nifti

from gb_dicom2bids.config import load_config
from gb_dicom2bids.manifest import load_private_records, load_selection
from gb_dicom2bids.nifti_import import inventory_preconverted
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_state import candidate_id, digest, read_decision


def make_config(tmp_path: Path, source: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "paths:\n"
        f"  staging_bids_root: {tmp_path / 'bids'}\n"
        f"  audit_root: {tmp_path / 'audit'}\n"
        f"  work_root: {tmp_path / 'work'}\n"
        "nifti_import:\n"
        f"  source_root: {source}\n"
        "conversion:\n"
        "  seed_from_existing_bids: false\n"
        "inventory:\n"
        "  workers: 2\n",
        encoding="utf-8",
    )
    return load_config(config_path)


def make_image(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_nifti(path)


def test_nifti_inventory_review_and_physical_bids_copy(tmp_path) -> None:
    source = tmp_path / "source"
    t1 = make_image(source / "center-a/p001/T1_MPRAGE/image.nii.gz")
    make_image(source / "center-a/p001/FLAIR/image.nii.gz")
    make_image(source / "center-a/p002/Scout/image.nii.gz")
    source_hash = digest(t1)
    config = make_config(tmp_path, source)

    summary = inventory_preconverted(config)
    assert summary == {
        **summary,
        "status": "completed",
        "source_files": 3,
        "candidates": 3,
        "subjects": 2,
        "t1_candidates": 1,
        "flair_candidates": 1,
        "other_candidates": 1,
        "errors": 0,
    }
    records = load_private_records(config.paths.audit_root)
    assert {record.candidate_type for record in records} == {"t1", "flair", "other"}
    assert all(record.source_kind == "preconverted_nifti" for record in records)
    assert all(record.plane == "unknown" for record in records)
    assert all(row.decision_status == "review" for row in load_selection(
        config.paths.audit_root / "selection_manifest.tsv"
    ))
    assert not list(config.paths.staging_bids_root.rglob("*.nii.gz"))

    service = ReviewService(config)
    try:
        record = next(record for record in records if record.candidate_type == "t1")
        uid = candidate_id(record)
        service._prepare_job(uid)
        artifact = service.artifact(uid, deep=True)
        assert Path(artifact["image"]) == t1
        sidecar = json.loads(Path(artifact["sidecar"]).read_text(encoding="utf-8"))
        assert sidecar["SourceMetadataAvailable"] is False
        raw = copy.deepcopy(read_decision(service.root, "p001"))
        raw["candidates"][uid] = {
            "quality": "pass",
            "modality": "t1",
            "reason": "full-volume visual review passed",
        }
        raw["groups"]["t1"] = {"choice": uid}
        service.save("p001", raw)
        assert service.apply() == [
            {
                "subject_id": "p001",
                "modality": "t1",
                "action": "install",
                "candidate_id": uid,
                "revision": 1,
            }
        ]
        service.apply(dry_run=False)
        installed = config.paths.staging_bids_root / "sub-p001/anat/sub-p001_T1w.nii.gz"
        assert installed.is_file() and not installed.is_symlink()
        assert installed.resolve() != t1.resolve()
        assert digest(installed) == source_hash == digest(t1)
        accepted = service.write_accepted()
        assert len(accepted) == 1 and accepted[0]["modality"] == "t1"
        participants = (config.paths.staging_bids_root / "participants.tsv").read_text(
            encoding="utf-8"
        )
        assert "sub-p001" in participants and "sub-p002" not in participants
        coverage = (service.root / "coverage.tsv").read_text(encoding="utf-8")
        assert "source_candidates" in coverage and "not_used" in coverage
        assert not config.paths.existing_bids_root.exists()
    finally:
        service.close()


def test_nifti_import_refuses_overwrite_and_center_collision(tmp_path) -> None:
    source = tmp_path / "source"
    make_image(source / "center-a/p001/T1/image.nii.gz")
    make_image(source / "center-b/p001/T1/image.nii.gz")
    config = make_config(tmp_path, source)
    with pytest.raises(ValueError, match="multiple centers"):
        inventory_preconverted(config)
    source.joinpath("center-b/p001/T1/image.nii.gz").unlink()
    inventory_preconverted(config)
    with pytest.raises(ValueError, match="already exists"):
        inventory_preconverted(config)
