# Workstation deployment

## 1. Clone the tagged release and create an isolated environment

```bash
git clone --branch v0.2.0 https://github.com/zhenzonglin/gb-dicom2bids.git gb-dicom2bids-v0.2.0
cd gb-dicom2bids-v0.2.0
/path/to/conda env create -p /path/to/envs/gb-dicom2bids-v0.2.0 -f environment.yml
conda activate /path/to/envs/gb-dicom2bids-v0.2.0
cp config/config.example.yaml config/config.local.yaml
```

Use the workstation's existing Conda installation. Do not install dependencies into system Python.
The local configuration is ignored by Git.

## 2. Configure paths and parallelism

Set five distinct absolute paths in `config/config.local.yaml`:

- read-only DICOM root;
- current BIDS root;
- versioned staging BIDS root;
- private audit root;
- node-local scratch root.

The recommended 56-core/112-thread profile is 24 inventory workers, 16 pilot conversion workers,
24 full conversion workers, and two `pigz` threads per conversion worker. Keep at least 50 GiB free
on scratch, 500 GiB on the staging filesystem, and 128 GiB available memory. The program reports
system load without delaying a run solely because load is high.

The program refuses overlapping source/destination roots. Participant mappings and audit outputs
must remain outside the Git repository.

## 3. Check the workstation

```bash
gb-dicom2bids doctor --config config/config.local.yaml
```

`doctor` verifies paths, write permissions, required tools, resource thresholds, CPU capacity, and
whether another recorded run is active. Resolve every error before launch.

## 4. Run inventory and the automatic pilot, then stop

```bash
bash scripts/start_run.sh --config config/config.local.yaml --mode inventory-pilot
gb-dicom2bids status --config config/config.local.yaml --watch 5 --processes
```

The launch script creates a separate process group and stores its PID, PGID, log, configuration,
mode, and start time in the private audit directory. The combined mode runs inventory, constructs a
two-per-stratum pilot for every available site/scanner/FLAIR protocol group, includes MPR and
uncertain review representatives, performs pilot conversion/QC/validation, and exits. A shortfall
is recorded when a stratum contains fewer than two eligible participants. Validator errors are
compared with a saved baseline from the existing BIDS tree so pilot acceptance detects newly
introduced errors rather than treating inherited errors as new failures.

Expected audit outputs include:

- `series_inventory.tsv`, `protocol_catalog.tsv`, `selection_manifest.tsv`, and
  `manual_review.tsv`;
- `pilot_manifest.tsv` and `pilot_summary.json`;
- `conversion_status.tsv`, per-series status JSON, process logs, resource history, pilot QC, and
  validator JSON.

To stop only this pipeline:

```bash
bash scripts/stop_run.sh --config config/config.local.yaml
```

The stop script verifies the recorded command and working directory, sends `TERM` to that process
group, waits up to 60 seconds, and does not send `KILL` or affect unrelated processes.

## 5. Resume a subset or the full cohort

After pilot review, use a controlled file containing one participant label per line when needed:

```bash
gb-dicom2bids convert --config config/config.local.yaml \
  --subjects-file pilot-extra.txt --workers 16 --resume
```

Only after pilot acceptance and completion of manual decisions, start the full conversion:

```bash
bash scripts/start_run.sh --config config/config.local.yaml --mode full
```

Failed rows remain failed on resume. Add `--retry-failed` to a direct `convert` command only after
the underlying problem is corrected. Completed outputs are skipped only when their recorded SHA-256
still matches. A dead worker's active row is marked interrupted and becomes eligible for resume.

## 6. Acceptance and production promotion

Require unique status coverage for every series, no automatic sagittal T1 selection, at most one T1
and one FLAIR per eligible participant, no new BIDS validation errors, and representative
DWI-to-T1/T1-to-MNI discovery and registration smoke tests.

This package intentionally does not promote staging data. Before a manual production switch:

1. verify that no downstream job is using the current dataset;
2. preserve the current dataset under a dated backup name;
3. move the validated staging dataset into the production location on the same filesystem;
4. retain the old dataset and a promotion record for rollback.
