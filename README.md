# gb-dicom2bids

`gb-dicom2bids` is an auditable DICOM-to-BIDS curation tool for multicenter structural MRI.
It inventories every DICOM series before conversion, selects one analysis-ready T1 and FLAIR
per participant/session, preserves an explicit decision trail, and writes into a staging BIDS
dataset without modifying source DICOM data.

Author: **zhenzong**

## Safety boundary

- Source DICOM directories are read-only.
- Existing BIDS data are copied to an independent staging directory before targeted changes.
- Real images, participant identifiers, runtime manifests, local paths, and credentials must not
  be committed to this repository.
- Promotion of a staging dataset is a separate, deliberate operation after manual review and
  validation; this package does not rename or delete the production dataset.

## Install on the workstation

```bash
git clone https://github.com/zhenzonglin/gb-dicom2bids.git
cd gb-dicom2bids
/path/to/conda env create -f environment.yml
/path/to/conda run -n gb-dicom2bids gb-dicom2bids --help
cp config/config.example.yaml config/config.local.yaml
```

Edit `config/config.local.yaml` with workstation-local paths. The local file is ignored by Git.

## Required execution order

```bash
gb-dicom2bids inventory --config config/config.local.yaml
gb-dicom2bids convert --config config/config.local.yaml --dry-run
gb-dicom2bids convert --config config/config.local.yaml
gb-dicom2bids qc --config config/config.local.yaml
gb-dicom2bids validate --config config/config.local.yaml
```

Do not start with a full conversion. Review `protocol_catalog.tsv`, `selection_manifest.tsv`,
and `manual_review.tsv` after inventory. See [workstation deployment](docs/workstation.md) and
[manual review](docs/manual_review.md).

## Selection policy

- T1 plane is derived from DICOM geometry, not series-name tokens.
- An acquisition is axial when its slice normal is within 20 degrees of the superior-inferior
  axis. Original/primary axial T1 is preferred; a derived axial MPR is allowed only as fallback.
- If no reliable axial T1 exists, the participant is held for manual review.
- FLAIR classification combines names, image type, inversion timing, acquisition dimension,
  geometry, coverage, and resolution. Selection is tuned for WMH quantification.
- Only one selected T1 and one selected FLAIR are written per participant/session. Every other
  candidate receives an explicit exclusion or review reason.

## Current validation boundary

Version 0.1.0 is validated with synthetic metadata and images. Full-data conversion and downstream
DWI-to-T1/T1-to-MNI smoke testing must be completed on the workstation before production promotion.

## Copyright

Copyright (c) 2026 zhenzong. All rights reserved. No license is granted. See
[COPYRIGHT.md](COPYRIGHT.md).
