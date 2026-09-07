# gb-dicom2bids

`gb-dicom2bids` is an auditable DICOM-to-BIDS curation tool for multicenter structural MRI.
It inventories every DICOM series before conversion, selects one analysis-ready T1 and FLAIR
per participant/session, preserves an explicit decision trail, and writes into a staging BIDS
dataset without modifying source DICOM data.

Author: **zhenzong**

## NIfTI-only visual curation patch

An independent workflow can inventory an already converted
`center/subject/series/.../*.nii.gz` tree, review candidates in the same local viewer, and
physically copy only approved T1/FLAIR images into a new empty BIDS dataset. It does not use an
existing BIDS dataset or run a DICOM converter. See the
[preconverted NIfTI workflow](docs/preconverted_nifti_qc.md).

Automatic NIfTI suggestions use case-insensitive substring matching of series-folder and file
names (`eFLAIR` is FLAIR and `eT1W` is T1). Existing inventories can be updated without a source
rescan using `python nifti_qc.py --config <local-config> --refresh-classification`. Both viewer
panes can independently select any folder-named sequence and manually designate it as T1, FLAIR,
or other before the quality and final-candidate decisions are saved.

## Optional visual QC patch for v0.2.0

The optional [protocol-assisted QC patch](docs/protocol_assisted_qc.md) adds center/template
classification rules, native-slice quality features, patient-separated quality models and
independent acceptance audits. Start with `python qc_assist.py catalog --config <local-config>`.
The workflow now separates sequence identification from image quality: identify T1/FLAIR
protocols first, then explicitly enter quality review. Other sequences are optional correction
sources, not required grouping fields. Existing human QC is preserved.
Sequence identification needs no free-text justification. After a human confirms a round's
templates are not the target modality, a frozen group reuses that negative evidence and shows
only new templates. Failed/deferred items remain pending; exclusions can be viewed and revoked.
Rules never copy quality labels. Automatic acceptance is disabled until its independent audit
passes; manual decisions take precedence and BIDS installation still requires explicit apply.

Run `python qc_viewer.py` in the existing environment for a loopback-only, offline reviewer.
Startup only prints the local URL; it does not launch a browser. Open that URL manually.
Browser launch requires explicit `--open-browser`; the older `--no-browser` flag remains valid.
Startup reports inventory bytes/records and index preparation to the terminal. Wait for
`Patient index ready` before opening the URL. Loading has no fixed timeout or CPU/disk-busy gate.
Compare all T1/FLAIR candidates, including excluded series, save per-candidate quality and one
final choice per modality, then explicitly apply with `python qc_viewer.py --apply --dry-run`
and `python qc_viewer.py --apply`. No re-inventory or environment rebuild is required.
Once enabled, ordinary conversion cannot install unreviewed images or bypass the explicit apply
step. Copied legacy images remain uncertified until reviewed. See the
[installation, review and rollback guide](docs/visual_qc.md). Synthetic tests are not real-cohort
validation; continue to run BIDS validation and downstream smoke tests before promotion.

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
gb-dicom2bids doctor --config config/config.local.yaml
bash scripts/start_run.sh --config config/config.local.yaml --mode inventory-pilot
gb-dicom2bids status --config config/config.local.yaml --watch 5 --processes
```

The combined run performs the parallel inventory, selects a deterministic stratified pilot,
converts it with the configured pilot worker count, runs pilot QC and BIDS validation, and stops.
It never advances to the full cohort. Review `protocol_catalog.tsv`, `selection_manifest.tsv`,
`manual_review.tsv`, `pilot_manifest.tsv`, and `pilot_summary.json` before continuing. See
[workstation deployment](docs/workstation.md) and [manual review](docs/manual_review.md).

After the pilot and manual review pass, the full conversion is explicitly started with:

```bash
gb-dicom2bids convert --config config/config.local.yaml --workers 24 --resume
```

Use `--subjects-file FILE` for a controlled subset and `--retry-failed` only after the cause of a
recorded failure has been addressed. `scripts/stop_run.sh` sends `TERM` only to the recorded
pipeline process group and never shuts down the workstation.

## Parallel and resumable execution

- Inventory parallelism is by participant directory; the parent process writes deterministic
  series and protocol manifests.
- Conversion parallelism is by participant. T1, FLAIR, and review candidates for one participant
  always run sequentially in one worker.
- `dcm2niix` writes an uncompressed NIfTI and `pigz` performs bounded compression using the
  configured thread count.
- Every series has an atomic state record with its phase, worker PID, child PID, output checksum,
  log, and terminal result. Dead workers are recovered as interrupted work on resume.
- Staging seeding is file-level resumable and records completed bytes, speed, and ETA. The source
  BIDS tree is never modified.
- Resource thresholds pause submission of new conversion work. System load is reported but is not
  an automatic delay or priority gate.
- Pilot validation records both the existing and staging datasets and fails acceptance only when
  staging introduces a new validator error or the candidate validator result is unreadable.

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

Version 0.2.0 is validated with synthetic metadata and images. Full-data conversion and downstream
DWI-to-T1/T1-to-MNI smoke testing must be completed on the workstation before production promotion.

## Copyright

Copyright (c) 2026 zhenzong. All rights reserved. No license is granted. See
[COPYRIGHT.md](COPYRIGHT.md).
