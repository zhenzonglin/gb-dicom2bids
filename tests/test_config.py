from __future__ import annotations

import pytest

from gb_dicom2bids.config import ConfigError, load_config


def _write_config(path, roots) -> None:
    path.write_text(
        "paths:\n"
        f"  dicom_root: {roots[0]}\n"
        f"  existing_bids_root: {roots[1]}\n"
        f"  staging_bids_root: {roots[2]}\n"
        f"  audit_root: {roots[3]}\n",
        encoding="utf-8",
    )


def test_load_valid_config(tmp_path) -> None:
    roots = [tmp_path / name for name in ("dicom", "existing", "staging", "audit")]
    path = tmp_path / "config.yaml"
    _write_config(path, roots)
    config = load_config(path)
    assert config.selection.axial_max_angle_deg == 20.0
    assert config.paths.audit_root == roots[3]
    assert config.inventory.workers == 1
    assert config.conversion.pilot_workers == 2
    assert config.work_root == roots[3] / "work"


def test_overlapping_paths_are_rejected(tmp_path) -> None:
    roots = [
        tmp_path / "dicom",
        tmp_path / "existing",
        tmp_path / "dicom" / "staging",
        tmp_path / "audit",
    ]
    path = tmp_path / "config.yaml"
    _write_config(path, roots)
    with pytest.raises(ConfigError, match="overlapping"):
        load_config(path)


def test_relative_paths_are_rejected(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "paths:\n  dicom_root: relative\n  existing_bids_root: /b\n"
        "  staging_bids_root: /c\n  audit_root: /d\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="absolute"):
        load_config(path)
