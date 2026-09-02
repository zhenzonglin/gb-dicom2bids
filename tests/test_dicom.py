from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

from gb_dicom2bids.dicom import sanitize_subject_label, scan_dicom_tree


def _write_dicom(
    path: Path, *, description: str, z: float, study_uid: str, series_uid: str
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = MRImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    dataset.is_little_endian = True
    dataset.is_implicit_VR = False
    dataset.SOPClassUID = MRImageStorage
    dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    dataset.StudyInstanceUID = study_uid
    dataset.SeriesInstanceUID = series_uid
    dataset.Modality = "MR"
    dataset.SeriesNumber = 1
    dataset.SeriesDescription = description
    dataset.ProtocolName = description
    dataset.ImageType = ["ORIGINAL", "PRIMARY", "M"]
    dataset.Manufacturer = "Synthetic"
    dataset.ManufacturerModelName = "TestModel"
    dataset.MRAcquisitionType = "2D"
    dataset.RepetitionTime = 500.0
    dataset.EchoTime = 10.0
    dataset.Rows = 32
    dataset.Columns = 32
    dataset.PixelSpacing = [1.0, 1.0]
    dataset.SliceThickness = 5.0
    dataset.SpacingBetweenSlices = 5.0
    dataset.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    dataset.ImagePositionPatient = [0, 0, z]
    dataset.save_as(path, enforce_file_format=True)


def test_scan_groups_by_series_uid(tmp_path) -> None:
    root = tmp_path / "dicom"
    study_uid = generate_uid()
    series_uid = generate_uid()
    for index, z in enumerate((0.0, 5.0, 10.0), start=1):
        _write_dicom(
            root / "siteA" / "P001" / "series" / f"{index}.dcm",
            description="T1 AX",
            z=z,
            study_uid=study_uid,
            series_uid=series_uid,
        )
    result = scan_dicom_tree(root)
    assert len(result.records) == 1
    record = result.records[0]
    assert record.subject_id == "p001"
    assert record.instance_count == 3
    assert record.plane == "axial"
    assert record.coverage_mm == 15.0
    assert record.candidate_type == "t1"


def test_cross_center_subject_collision_is_rejected(tmp_path) -> None:
    root = tmp_path / "dicom"
    for center in ("siteA", "siteB"):
        _write_dicom(
            root / center / "P001" / "series" / "1.dcm",
            description="T1 AX",
            z=0,
            study_uid=generate_uid(),
            series_uid=generate_uid(),
        )
    with pytest.raises(ValueError, match="cross-center"):
        scan_dicom_tree(root)


def test_subject_label_normalization() -> None:
    assert sanitize_subject_label("sub-TMS001") == "tms001"
    assert sanitize_subject_label("Patient_01") == "patient01"


def test_parallel_inventory_is_deterministic_and_deduplicates(tmp_path) -> None:
    root = tmp_path / "dicom"
    for subject in ("P002", "P001"):
        source = root / "siteA" / subject / "series" / "1.dcm"
        _write_dicom(
            source,
            description="T1 AX",
            z=0,
            study_uid=generate_uid(),
            series_uid=generate_uid(),
        )
        shutil.copy2(source, source.with_name("duplicate.dcm"))
    serial = scan_dicom_tree(root, workers=1)
    parallel = scan_dicom_tree(root, workers=2)
    assert [row.private_dict() for row in parallel.records] == [
        row.private_dict() for row in serial.records
    ]
    assert [row.subject_id for row in parallel.records] == ["p001", "p002"]
    assert all(row.instance_count == 1 for row in parallel.records)
    assert all(row.duplicate_instance_count == 1 for row in parallel.records)
    assert parallel.files_seen == 4
