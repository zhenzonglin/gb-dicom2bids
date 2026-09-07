# Changelog

## Protocol-assisted QC branch patch

- Stream legacy private JSON inventories without retaining a second complete decoded list;
  report startup stages, read bytes and record counts while preparing the identification index.
- Patient-list initialization has no fixed timeout or CPU/disk utilization gate. Delay auxiliary
  progress polling until workflow initialization finishes, and prevent overlapping polls.
- Ignore disconnected browser sockets without retrying an error response on the same connection.
- Viewer startup prints the local URL without launching a browser; browser launch is opt-in
  with `--open-browser`, and `--no-browser` remains compatible.
- Removed the required sequence-identification justification; quality-failure reasons remain.
- Added human-confirmed, modality-specific negative templates with frozen group membership,
  iterative novel-sequence representatives, deferred/read-failure protection and reversible exclusions.
- Preserved existing human QC, template identities and source data. Sequence rules never grant quality.
- Added offline browser checks and synthetic regression tests; real-cohort workload remains unmeasured.

## v0.2.0

- Added participant-level parallel conversion and directory-level parallel DICOM inventory.
- Added atomic per-series lifecycle state, checksummed resume, failed-task retry control, and stale
  worker recovery.
- Added file-level resumable staging copy with byte progress, speed, and ETA.
- Added bounded two-thread `pigz` compression after uncompressed `dcm2niix` output.
- Added `doctor`, stratified `pilot`, combined `run --mode inventory-pilot`, and live `status`
  commands.
- Added targeted process-group launch and graceful stop scripts.
- Added resource sampling and free-space/memory submission gates without load-based delay.

Synthetic tests pass for this release. Workstation inventory, stratified pilot conversion, full-data
QC, BIDS validation, and downstream registration smoke tests remain site validation tasks.
