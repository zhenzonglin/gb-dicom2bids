# Protocol-assisted QC: verification record

Date: 2026-09-06. Author: zhenzong. Branch: `feat/protocol-assisted-qc`.

## Verified locally

- Python 3.11; Ruff and compilation passed.
- 106 repository tests passed. The existing unrelated local monitor tests are not part of this
  patch. Warnings concern deprecated pydicom fixture flags, not failed checks.
- Ten new synthetic tests cover center/name/geometry grouping, numeric name preservation,
  preview conflicts, rule withdrawal, real duplicate ties, native blur and local bad slices,
  ghost-related feature finiteness, oblique and thick-slice geometry, unreadable files, cache
  resume and resource pausing, quality-label eligibility, patient-disjoint frozen models,
  independent audit failure, domain suspension and fresh audit allocation, modality separation,
  corrupt model rejection, manual override, physical installation and recoverable withdrawal.
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

## Not yet verified on the workstation

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
