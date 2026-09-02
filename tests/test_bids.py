from __future__ import annotations

import csv
import json

from gb_dicom2bids.bids import ensure_dataset_metadata, update_participants, update_scans
from gb_dicom2bids.config import (
    ConversionConfig,
    DatasetConfig,
    PathsConfig,
    ProjectConfig,
    SelectionConfig,
    ToolsConfig,
)
from gb_dicom2bids.models import SelectionRow


def _config(tmp_path) -> ProjectConfig:
    return ProjectConfig(
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


def test_dataset_metadata_and_participants(record_factory, tmp_path) -> None:
    config = _config(tmp_path)
    ensure_dataset_metadata(config)
    update_participants(config, [record_factory(center="hospital-a")])
    description = json.loads(
        (config.paths.staging_bids_root / "dataset_description.json").read_text()
    )
    assert description["BIDSVersion"] == "1.11.1"
    with (config.paths.staging_bids_root / "participants.tsv").open() as handle:
        row = next(csv.DictReader(handle, delimiter="\t"))
    assert row["participant_id"] == "sub-001"
    assert row["site_id"].startswith("site-")
    assert row["site_id"] != "hospital-a"


def test_scans_metadata(record_factory, tmp_path) -> None:
    config = _config(tmp_path)
    ensure_dataset_metadata(config)
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
    nifti = config.paths.staging_bids_root / "sub-001" / "anat" / "sub-001_T1w.nii.gz"
    nifti.parent.mkdir(parents=True)
    nifti.touch()
    scans = config.paths.staging_bids_root / "sub-001" / "sub-001_scans.tsv"
    scans.write_text(
        "filename\tprotocol_id\nanat/sub-001_acq-old_T1w.nii.gz\told\n",
        encoding="utf-8",
    )
    update_scans(config, record, selection, nifti)
    content = scans.read_text()
    assert "protocol_id" in content
    assert "acq-old" not in content
