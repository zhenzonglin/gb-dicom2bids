# Workstation deployment

## 1. Clone and create the environment

```bash
git clone https://github.com/zhenzonglin/gb-dicom2bids.git
cd gb-dicom2bids
/path/to/conda env create -f environment.yml
/path/to/conda activate gb-dicom2bids
cp config/config.example.yaml config/config.local.yaml
```

Use the workstation's existing Conda installation. Do not install dependencies into the system
Python environment.

## 2. Configure local paths

Set four distinct absolute paths in `config/config.local.yaml`:

- read-only DICOM root;
- current BIDS root;
- versioned staging BIDS root;
- private audit root.

The program refuses overlapping source/destination roots. Participant mappings and audit outputs
must remain outside the Git repository.

## 3. Inventory first

```bash
gb-dicom2bids inventory --config config/config.local.yaml
```

Confirm that every center, scanner, and FLAIR protocol class is represented in the generated
inventory. Resolve cross-center subject-label collisions before conversion.

## 4. Pilot conversion

Select at least two participants from every center/scanner/FLAIR-protocol combination, plus every
rare protocol and conversion-repair case.

```bash
gb-dicom2bids convert --config config/config.local.yaml --subjects sub001 sub002 --dry-run
gb-dicom2bids convert --config config/config.local.yaml --subjects sub001 sub002
gb-dicom2bids qc --config config/config.local.yaml
gb-dicom2bids validate --config config/config.local.yaml
```

Do not run a full cohort while manual-review rows remain unresolved.

## 5. Full run and production promotion

After the pilot passes, run the full conversion and repeat QC/validation. Separately execute the
existing DWI-to-T1 and T1-to-MNI smoke tests on representative cases.

This package intentionally does not promote staging data. Before a manual production switch:

1. verify that no downstream job is using the current dataset;
2. preserve the current dataset under a dated backup name;
3. move the validated staging dataset into the production location on the same filesystem;
4. retain the old dataset and a promotion record for rollback.
