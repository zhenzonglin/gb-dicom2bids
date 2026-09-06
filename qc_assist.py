"""Workstation entry point for protocol assistance and quality triage."""

from gb_dicom2bids.qc_assist import main

if __name__ == "__main__":
    raise SystemExit(main())
