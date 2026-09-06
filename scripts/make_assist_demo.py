"""Create native NIfTI phantoms for the protocol-assistance browser check."""

from __future__ import annotations

import argparse
from pathlib import Path

import nibabel as nib
import numpy as np
import yaml

from gb_dicom2bids.config import load_config
from gb_dicom2bids.nifti_import import inventory_preconverted
from gb_dicom2bids.qc_protocols import ProtocolIndex
from gb_dicom2bids.runtime import atomic_write_json


def make_demo(root: Path) -> Path:
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("demo destination must be new or empty")
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "demo.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "paths": {
                    "staging_bids_root": str(root / "bids"),
                    "audit_root": str(root / "audit"),
                    "work_root": str(root / "scratch"),
                },
                "nifti_import": {"source_root": str(root / "source")},
                "conversion": {"seed_from_existing_bids": False},
            }
        ),
        encoding="utf-8",
    )
    x, y, z = np.indices((64, 64, 32), dtype=np.float32)
    radius = ((x - 32) / 23) ** 2 + ((y - 32) / 27) ** 2 + ((z - 16) / 14) ** 2
    data = (radius < 1) * (80 + 20 * np.cos(radius * 24) + 7 * np.sin(x * 0.7))
    for subject in ("phantom01", "phantom02"):
        for name in ("eT1W-SE", "T1-repeat", "eFLAIR-longTR-CLEAR", "unknown-contrast"):
            folder = root / "source" / "synthetic_site" / subject / name
            folder.mkdir(parents=True)
            nib.save(nib.Nifti1Image(data, np.diag([2.0, 2.0, 4.0, 1.0])), folder / "image.nii.gz")
    config = load_config(config_path)
    inventory_preconverted(config)
    index = ProtocolIndex(config)
    atomic_write_json(index.root / "catalogue.json", index.catalogue())
    return config_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(make_demo(parser.parse_args().output))
