# Preconverted NIfTI visual QC and clean BIDS archive

This optional patch imports a read-only NIfTI tree organized as
`center/subject/series/.../*.nii.gz`. It does not read DICOM, seed from an existing BIDS
dataset, or run an image converter. Input files remain unchanged. Only a manually approved
T1 and/or FLAIR is physically copied into a new BIDS destination.

No JSON sidecars are required. A minimal sidecar is generated in the private audit directory
when a candidate is first opened. Because a NIfTI affine describes the stored grid rather than
the original acquisition, the acquisition plane and scanner metadata remain `unknown`. Visual
approval therefore certifies image usability, not the original acquisition plane.

## Configure and inventory

Create a new local configuration; do not copy the DICOM-pipeline configuration:

```bash
cp config/config.preconverted.example.yaml config/config.nifti.local.yaml
```

Set `nifti_import.source_root`, an empty `paths.staging_bids_root`, and a new
`paths.audit_root`. Keep `conversion.seed_from_existing_bids: false`. Then run exactly once:

```bash
python nifti_qc.py --config config/config.nifti.local.yaml
```

The command reads NIfTI headers only, writes a deterministic private inventory and leaves the
BIDS destination empty. It refuses a non-empty BIDS destination, an existing QC decision store,
symlinked source entries, uncompressed `.nii`, subject labels shared by multiple centers, and an
attempt to overwrite an existing inventory.

Inspect `nifti_import_status.json`, `series_inventory.tsv`, `inventory_errors.tsv`, and
`selection_manifest.tsv` under the private audit root before review. Filename/folder tokens are
only an initial T1/FLAIR suggestion. Candidates without a recognized token remain `other` and
must be found with the viewer's “include other series” filter.

## Review and copy approved files

```bash
python qc_viewer.py --config config/config.nifti.local.yaml
```

Review the full volume, set the correct modality, record pass/fail/defer, and select at most one
final candidate per modality. Different series folders have no surviving examination identity;
selecting across them requires an explicit confirmation that they belong to the same examination.

Saving records decisions only. Preview preparation reads and hashes the selected source on demand;
it does not copy the whole source collection. Apply a small reviewed batch first:

```bash
python qc_viewer.py --config config/config.nifti.local.yaml --apply --dry-run
python qc_viewer.py --config config/config.nifti.local.yaml --apply
```

Apply uses file copies, verifies source and destination checksums, and writes BIDS metadata only
after files are ready. It never reads or modifies an earlier BIDS dataset. The authoritative
accepted set is `visual_qc/accepted_manifest.tsv`; `visual_qc/coverage.tsv` distinguishes source
candidates, installed images, certification, and missing reasons. Repeating apply is idempotent.

Run the BIDS validator and downstream file-discovery/registration smoke tests before promoting the
new dataset. The software does not rename, delete, or replace a production dataset.
