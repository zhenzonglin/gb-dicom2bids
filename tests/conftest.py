from __future__ import annotations

from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np
import pytest

from gb_dicom2bids.models import SeriesRecord


@pytest.fixture
def record_factory():
    def factory(**overrides: Any) -> SeriesRecord:
        values: dict[str, Any] = {
            "center": "site01",
            "subject_id": "001",
            "study_uid_hash": "studyhash",
            "series_uid_hash": "serieshash",
            "modality": "MR",
            "series_description": "T1 AX",
            "protocol_name": "T1",
            "image_type": ["ORIGINAL", "PRIMARY", "M"],
            "acquisition_type": "2D",
            "pixel_spacing_mm": [1.0, 1.0],
            "slice_thickness_mm": 1.0,
            "spacing_between_slices_mm": 1.0,
            "plane": "axial",
            "nearest_plane": "axial",
            "plane_angle_deg": 0.0,
            "coverage_mm": 160.0,
            "source_kind": "original",
            "candidate_type": "t1",
            "classification_confidence": "high",
            "protocol_id": "t1-test",
            "instance_count": 160,
        }
        values.update(overrides)
        return SeriesRecord(**values)

    return factory


def write_nifti(path: Path, shape: tuple[int, int, int] = (24, 24, 24)) -> Path:
    data = np.zeros(shape, dtype=np.float32)
    data[5:-5, 5:-5, 5:-5] = 100.0
    image = nib.Nifti1Image(data, np.diag([1.0, 1.0, 1.0, 1.0]))
    image.set_qform(image.affine, code=1)
    image.set_sform(image.affine, code=1)
    nib.save(image, path)
    return path
