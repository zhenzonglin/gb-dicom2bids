#!/usr/bin/env python3
"""Build a visual-QC inventory from an existing NIfTI-only collection."""

from gb_dicom2bids.nifti_import import main

if __name__ == "__main__":
    raise SystemExit(main())
