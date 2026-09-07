# Protocol-assisted QC: verification record

Date: 2026-09-07. Author: zhenzong. Branch: `feat/protocol-assisted-qc`.

## Verified locally

- Python 3.11; Ruff and compilation passed.
- 132 repository tests passed. The existing unrelated local monitor tests are not part of this
  patch. Warnings concern deprecated pydicom fixture flags, not failed checks.
- Eleven new synthetic tests cover center/name/geometry grouping, numeric name preservation,
  preview conflicts, rule withdrawal, real duplicate ties, native blur and local bad slices,
  ghost-related feature finiteness, oblique and thick-slice geometry, unreadable files, cache
  resume and resource pausing, quality-label eligibility, patient-disjoint frozen models,
  independent audit failure, domain suspension and fresh audit allocation, modality separation,
  corrupt model rejection, manual override, physical installation and recoverable withdrawal.
  The viewer also has a regression check for sparse manual records, concurrent initialization
  and deferred full-inventory hashing without changing the publication digest.
- The 59-patient zero-error audit fixture passes the exact upper-bound criterion; a 58-patient
  zero-error fixture and a 59-patient one-error fixture do not. These are program-logic tests,
  not estimates of clinical performance.
- Headless local-browser checks passed for the existing reviewer and the new protocol panel:
  native images rendered, rules previewed/published/revoked, human decisions saved and persisted,
  quality was not copied across subjects, structured failure reasons persisted. No page errors
  or external resource requests were observed.
- CLI smoke checks completed catalog, 8-worker native features, insufficient-label calibration,
  manual-only proposals, status and an empty dry-run without installing unapproved images.
- Source package and wheel built; the allow-listed offline patch and SHA256 manifest built.
- Public release gate passed for staged code, documentation and synthetic tests.

## Patient-list startup repair

The original first list request redundantly read a decision/history path for every participant
and hashed every inventory record before returning the list. The viewer now reuses its loaded
human decisions, initializes one shared protocol index, and defers the unchanged full digest
until it is needed for protocol publication or model checks. No saved decisions are rewritten.

An in-memory benchmark with 20,834 synthetic participants and 333,344 synthetic records reduced
index initialization from 14.25 to 3.56 seconds on the local development machine. Per-participant
decision reads during this request decreased from 20,834 to zero. The baseline bypassed disk
reads; these measurements are not workstation/NFS throughput claims.

The sidebar now reports loading time, visible list errors and a retry button. A local browser
test injected a list API failure and verified that retry restored the patient list and native
images. An unresolved request reports a timeout after 60 seconds instead of an unexplained
empty panel. Workstation-specific failures still require the displayed error to diagnose.

## Sequence-first workflow repair

The default `catalog` now creates a metadata-only, two-stage identification workflow. T1 and
FLAIR have separate center/name groups; unrelated sequences do not split groups. Layer counts
and voxel dimensions remain quality-domain metadata, not mandatory sequence-name grouping keys.
Generic names without protocol meaning remain individual. Existing whole-combination rules and
manual QC files are retained, but the new workflow does not silently adopt old group rules.

Eleven additional synthetic tests cover independent modality groups, extra DWI, geometry variants,
optional missed-sequence corrections, no copied quality, source/manual checksum preservation,
per-patient absence, repeat scans, version/preview protection, manual conflicts, generic names,
metadata-only cataloguing, changed inventories and backend quality-stage gates.

The separate browser check exercises optional correction, group-wide publication, disabled quality
controls, explicit stage transition, persisted human quality and reopening identification. Only
synthetic images were used. The pending counts are not measurements of the workstation cohort.

## Negative-template reuse patch

Fourteen additional synthetic tests verify iterative new-template review, duplicate skipping,
finding a missed target after a negative round, frozen membership, independent target modalities,
manual positive protection, generic-name isolation, deferred items, unreadable files, persistent
preview errors and successful retry, inventory changes, source changes between preview/publication,
withdrawal, and removal of false-positive rows without silently rejecting unseen optional sequences.
Prior per-patient absence remains compatible and is not promoted into group-wide evidence.

The 132-test suite passed with 75% total coverage (negative-rule module 93%, identification 89%).
Ruff, Python compilation, JavaScript syntax checks and CLI smoke checks passed. Source and wheel
builds passed using the project's normal isolated build; the older local environment's build-only
dependencies were insufficient for a no-isolation build and were not changed.

Three headless Edge browser suites passed: the new negative-template workflow, existing positive
identification/quality saving, and the legacy assistance interface. New checks verified removal of
the reason field, native images, automatic next representative, novel-only and full-list toggles,
persisted deferral, refresh/resume, withdrawal and no copied quality. There were no JavaScript page
errors or external asset requests. Browser checks used generated phantoms, never patient images.

The private negative-rule scopes and history are version-checked. Successful retries retain the
original error text while resolving the error status. Model authorizations remain revision-gated.
No source images, existing human quality decisions, local configuration or monitor files were
changed by the patch. Actual cohort savings and workstation runtime have not been measured.

## Workstation validation still required

No patient data, manual labels or trained model were available to the local verification run.
Actual label counts, rule coverage, false-acceptance rate and reduction in manual review are
therefore **not measured**, not zero. Runtime and throughput on the workstation/NFS also remain
unmeasured. Models and thresholds are intentionally not shipped in Git.

Before automatic authorization, run the documented independent audit separately for T1 and
FLAIR. Inspect heavy lesion burden, infarction, atrophy and low-resolution acquisitions for
confounding. Report errors and the one-sided 95% upper bound in each modality's accepted pool;
report center/template coverage separately without claiming rare-domain error guarantees.

The private `calibration_summary.json`, model `validation.json`, `triage.json`, `recheck.json`
and `accepted_manifest.tsv` provide the field evidence. Final BIDS validation and downstream
registration checks remain required before research use.
