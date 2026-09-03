#!/usr/bin/env python3
"""Workstation-local visual QC entry point; use the existing project environment."""

from gb_dicom2bids.qc_server import main

if __name__ == "__main__":
    raise SystemExit(main())
