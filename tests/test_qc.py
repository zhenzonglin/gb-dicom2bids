from __future__ import annotations

from conftest import write_nifti

from gb_dicom2bids.qc import create_montage, inspect_nifti


def test_nifti_qc_and_montage(tmp_path) -> None:
    nifti = write_nifti(tmp_path / "synthetic.nii.gz")
    status, detail = inspect_nifti(nifti)
    assert status == "pass"
    assert "shape=" in detail
    montage = tmp_path / "montage.png"
    create_montage(nifti, montage, "synthetic")
    assert montage.is_file() and montage.stat().st_size > 0


def test_missing_nifti_fails(tmp_path) -> None:
    status, _ = inspect_nifti(tmp_path / "missing.nii.gz")
    assert status == "fail"
