"""Generate synthetic phantoms and inventories for local UI verification, never patient data."""

from __future__ import annotations

import argparse
from pathlib import Path

import nibabel as nib
import numpy as np
import yaml
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from gb_dicom2bids.config import load_config
from gb_dicom2bids.manifest import write_inventory, write_selection
from gb_dicom2bids.models import SelectionRow, SeriesRecord
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_state import candidate_id, digest, record_digest
from gb_dicom2bids.runtime import atomic_write_json


def make_demo(root: Path) -> Path:
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("demo destination must be new or empty")
    root.mkdir(parents=True, exist_ok=True)
    paths = {
        name: str(root / folder)
        for name, folder in (
            ("dicom_root", "dicom"),
            ("existing_bids_root", "old"),
            ("staging_bids_root", "staging"),
            ("audit_root", "audit"),
            ("work_root", "scratch"),
        )
    }
    for path in paths.values():
        Path(path).mkdir()
    config_file = root / "demo.yaml"
    config_file.write_text(
        yaml.safe_dump({"paths": paths, "conversion": {"seed_from_existing_bids": False}}),
        encoding="utf-8",
    )
    config = load_config(config_file)
    records, rows = [], []
    for subject in ("phantom01", "phantom02"):
        for i, (kind, status, label) in enumerate(
            (
                ("t1", "selected", "Synthetic T1 axial"),
                ("t1", "review", "Synthetic T1 axial repeat"),
                ("flair", "excluded", "Synthetic FLAIR"),
                ("other", "excluded", "Synthetic unknown contrast"),
            )
        ):
            record = SeriesRecord(
                center="synthetic_site",
                subject_id=subject,
                study_uid_hash="synthetic_study",
                series_uid_hash=f"{subject}_series{i}",
                modality="MR",
                series_number=str(i + 1),
                series_description=label,
                protocol_name=label,
                protocol_id=f"synthetic_{kind}",
                manufacturer="SYNTHETIC",
                model_name="PHANTOM",
                candidate_type=kind,
                acquisition_type="3D",
                plane="axial",
                nearest_plane="axial",
                plane_angle_deg=0,
                source_kind="original",
                image_orientation_patient=[1, 0, 0, 0, 1, 0],
                pixel_spacing_mm=[2, 2],
                slice_thickness_mm=2.5,
                coverage_mm=160,
                repetition_time_ms=9000 if kind == "flair" else 2000,
                echo_time_ms=90 if kind == "flair" else 3,
                inversion_time_ms=2500 if kind == "flair" else None,
                source_relpaths=[f"{subject}_{i}.dcm"],
            )
            header = Dataset()
            header.file_meta = FileMetaDataset()
            header.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
            header.StudyDate, header.StudyTime = "20000101", "120000"
            header.save_as(config.paths.dicom_root / record.source_relpaths[0])
            records.append(record)
            rows.append(
                SelectionRow(
                    record.center,
                    subject,
                    record.study_uid_hash,
                    record.series_uid_hash,
                    kind,
                    status,
                    90 - i * 3,
                    "synthetic_preview_only",
                    "axial",
                    "original",
                    record.protocol_id,
                )
            )
    write_inventory(config.paths.audit_root, records, [])
    write_selection(config.paths.audit_root, rows, records)
    service = ReviewService(config)
    x, y, z = np.indices((72, 80, 64), dtype=np.float32)
    radius = ((x - 36) / 28) ** 2 + ((y - 40) / 34) ** 2 + ((z - 32) / 27) ** 2
    tissue = (radius < 1) * (80 + 20 * np.cos(radius * 24) + 7 * np.sin(x * 0.7) * np.cos(y * 0.5))
    ventricles = (((x - 30) / 3) ** 2 + ((y - 40) / 10) ** 2 + ((z - 34) / 7) ** 2 < 1) | (
        ((x - 42) / 3) ** 2 + ((y - 40) / 10) ** 2 + ((z - 34) / 7) ** 2 < 1
    )
    tissue[ventricles] = 15
    tissue[(x > 47) & (x < 53) & (y > 38) & (y < 46) & (z > 28) & (z < 37)] = 140
    for record in records:
        uid = candidate_id(record)
        folder = service.root / "demo_cache" / uid
        folder.mkdir(parents=True)
        image, sidecar = folder / "candidate.nii.gz", folder / "candidate.json"
        data = tissue.copy()
        if "repeat" in record.series_description:
            data = 0.6 * data + 0.4 * np.roll(data, 4, axis=0)
        if record.candidate_type == "flair":
            data[ventricles] = 3
        affine = np.diag([2.0, 2.0, 2.5, 1.0])
        affine[:3, 3] = [-72, -80, -80]
        nifti = nib.Nifti1Image(data.astype(np.float32), affine)
        nifti.set_qform(affine, 1)
        nib.save(nifti, image)
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
    service.close()
    return config_file


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(make_demo(parser.parse_args().output))
