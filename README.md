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
Names containing both `t1` and `flair`, in either order and with arbitrary intervening text,
default to T1. A single name containing both `t1` and `dark` also defaults to T1, including
`T1 dark fluid` and `dark_extra_T1`. Other FLAIR names default to FLAIR. Without `t1`,
a single name containing `t2`, `dark` and
`fluid`, with intervening text allowed and in any order, also defaults to FLAIR; separate names
are never joined to construct a match. XA (DSA in this project's exclusion category),
CT, TOF, MRA, DWI, b0 and b1000 default to
non-target, but remain available for manual correction. Existing human decisions take priority.
If every remaining candidate in a patient's modality-specific round has a verified image-read
failure, that round is technically skipped. Untried files, temporary I/O failures and human
holds do not qualify. The recovery queue retains all images and logs for retry; no quality
decision or cross-patient exclusion rule is created.
For T1 and FLAIR independently, an available axial candidate finishes sequence identification
without resolving other candidates first. AX/OAx/TRA name hints are axial. SAG/OSag and COR/OCor
direction tokens default to excluded, even if AX also occurs; exclusion wins. These images stay
available under all sequences, not in the default review/quality queue. Ordinary words such as
correction, cortex and relax are not direction tokens; PosDisp reference suffixes are ignored.
AX alone does not invent a T1/FLAIR modality or override CT/XA/DWI exclusions.
Multiple axial images are ordered naturally by source folder,
then filename, and the last one is selected (image10 follows image2). This is deterministic
ordering, not acquisition chronology or quality ranking. Existing human final choices, explicit
rankings, exclusions and holds remain authoritative. Other images remain available for correction.
Without saved human rules or decisions, coexisting eT2(W) FLAIR and T2(W) FLAIR candidates
prefer eT2 FLAIR. Matching is case-insensitive and accepts spaces, hyphens and underscores.
For both T1 and FLAIR, two or three same-name images also use natural-last order without
requiring an axial name hint. Names use the existing normalized sequence family, not geometry;
different protocols stay distinct. The eT2 preference runs first, then same-name/axial selection
within its preferred pool. No source image is removed and no quality pass is written.
One normalized name field having **four or more candidate images in that patient** still overrides
the automatic eT2, axial and same-name preferences. Automatically excluded SAG/COR images do not count toward it.
At this fixed, inclusive limit, only the triggering modality is skipped, including its other
fields; the other modality remains independent. Different fields and patients are not summed.
Saved human final decisions are preserved. This workload exclusion is not a quality failure.
The viewer's count-exclusion queue shows the field and count without automatically opening images.
Run `catalog` after updating to enable this rule and back up the prior private identification state.
This never grants image quality. The persistent rule-review panel can preview and revoke current
inclusion, exclusion, absence and recheck rules without undoing image-quality decisions or BIDS.
Sequence identification needs no free-text justification or separate preview click. Direct
confirmation durably queues the request and opens the next group while serial saving continues.
The status panel distinguishes pending, saved and failed requests; failures can return to their
group for review. Queued requests survive restart; conflicting edits cannot overwrite current
rules. Quality/archiving remains blocked until all pending requests finish.
Independent T1 and FLAIR saves no longer invalidate each other; identical shared-template
updates can converge, but a changed priority or manual decision remains protected. Prefetched
pages are revalidated before opening. Equal-priority and known inclusion/exclusion conflicts
are shown before navigating away. Recent failures are grouped and superseded by later successful
submissions, not automatically reopened in the main queue. They remain available for explicit
review and are never counted as identification complete; all original result files are retained.
After a human confirms a round's
templates are not the target modality, a frozen group reuses that negative evidence and shows
only new templates. Manually deferred items remain pending. Preview failures alone never exclude
an image; a deliberate sequence-only exclusion can still be published. Exclusions remain reversible.
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
