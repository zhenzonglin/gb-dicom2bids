from __future__ import annotations

import subprocess

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


def test_multiple_commits_with_expected_identity_pass(tmp_path) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "zhenzong"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "config",
            "user.email",
            "linzhenzong1@163.com",
        ],
        check=True,
    )
    for index in range(2):
        (tmp_path / "safe.txt").write_text(f"safe {index}", encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp_path), "add", "safe.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", f"test: safe {index}"],
            check=True,
            capture_output=True,
        )
    assert check_public_release(tmp_path) == []
