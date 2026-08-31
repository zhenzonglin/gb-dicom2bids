from __future__ import annotations

from gb_dicom2bids.security import FORBIDDEN_TOKEN, check_public_release


def test_safe_tree_passes(tmp_path) -> None:
    (tmp_path / "safe.txt").write_text("safe synthetic content", encoding="utf-8")
    assert check_public_release(tmp_path) == []


def test_forbidden_identity_is_detected(tmp_path) -> None:
    (tmp_path / "unsafe.txt").write_text(FORBIDDEN_TOKEN, encoding="utf-8")
    problems = check_public_release(tmp_path)
    assert any("forbidden token" in problem for problem in problems)


def test_medical_image_is_detected(tmp_path) -> None:
    (tmp_path / "image.nii.gz").write_bytes(b"not-an-image")
    problems = check_public_release(tmp_path)
    assert any("prohibited" in problem for problem in problems)
