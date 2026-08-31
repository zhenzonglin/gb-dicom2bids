from __future__ import annotations

import json

import nibabel as nib
import numpy as np
import pytest
from conftest import write_nifti

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
    _install_selected,
    _validate_converted_pair,
    decoder_command,
)
from gb_dicom2bids.models import SelectionRow


def test_decoder_routing(tmp_path) -> None:
    source = tmp_path / "in.dcm"
    output = tmp_path / "out.dcm"
    assert decoder_command("1.2.840.10008.1.2.5", source, output)[0] == "dcmdrle"
    assert decoder_command("1.2.840.10008.1.2.4.80", source, output)[0] == "dcmdjpls"
    assert decoder_command("1.2.840.10008.1.2.4.90", source, output) is None
    assert decoder_command("1.2.840.10008.1.2.4.50", source, output)[0] == "dcmdjpeg"
    assert decoder_command("1.2.3", source, output) is None


def test_converted_pair_geometry(record_factory, tmp_path) -> None:
    nifti = write_nifti(tmp_path / "image.nii.gz")
    sidecar = tmp_path / "image.json"
    sidecar.write_text(
        json.dumps({"ImageOrientationPatientDICOM": [1, 0, 0, 0, 1, 0]}),
        encoding="utf-8",
    )
    record = record_factory(image_orientation_patient=[1, 0, 0, 0, 1, 0])
    _validate_converted_pair(nifti, sidecar, record)


def test_converted_pair_rejects_affine_plane_mismatch(record_factory, tmp_path) -> None:
    data = np.zeros((24, 24, 24), dtype=np.float32)
    affine = np.array(
        [[0.0, 0.0, 1.0, 0.0], [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0, 0, 0, 1]]
    )
    nifti = tmp_path / "sagittal.nii.gz"
    nib.save(nib.Nifti1Image(data, affine), nifti)
    sidecar = tmp_path / "sagittal.json"
    sidecar.write_text("{}", encoding="utf-8")
    record = record_factory(image_orientation_patient=[1, 0, 0, 0, 1, 0])
    with pytest.raises(ConversionError, match="affine plane mismatch"):
        _validate_converted_pair(nifti, sidecar, record)


def test_install_selected_backs_up_and_replaces_pair(record_factory, tmp_path) -> None:
    config = ProjectConfig(
        PathsConfig(
            tmp_path / "dicom",
            tmp_path / "existing",
            tmp_path / "staging",
            tmp_path / "audit",
        ),
        DatasetConfig(),
        SelectionConfig(),
        ConversionConfig(),
        ToolsConfig(),
    )
    record = record_factory()
    selection = SelectionRow(
        center=record.center,
        subject_id=record.subject_id,
        study_uid_hash=record.study_uid_hash,
        series_uid_hash=record.series_uid_hash,
        candidate_type="t1",
        decision_status="selected",
        score=100,
        reason="best_original_axial_t1",
        source_plane="axial",
        source_kind="original",
        protocol_id=record.protocol_id,
        output_basename="sub-001_T1w",
    )
    anat = config.paths.staging_bids_root / "sub-001" / "anat"
    anat.mkdir(parents=True)
    old_nifti = write_nifti(anat / "sub-001_T1w.nii.gz")
    old_json = anat / "sub-001_T1w.json"
    old_json.write_text('{"old": true}', encoding="utf-8")
    new_nifti = write_nifti(tmp_path / "new.nii.gz")
    new_json = tmp_path / "new.json"
    new_json.write_text('{"new": true}', encoding="utf-8")

    installed, diffs = _install_selected(
        config, record, selection, new_nifti, new_json
    )

    assert installed == old_nifti
    assert json.loads(old_json.read_text()) == {"new": True}
    assert (config.paths.audit_root / "replaced/sub-001/anat/sub-001_T1w.json").exists()
    assert {row["action"] for row in diffs} == {"replaced"}
