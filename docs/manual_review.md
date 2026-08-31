# Manual review contract

`manual_review.tsv` contains only cases that cannot be selected safely by metadata rules.

Allowed decisions are:

- `accept_original`: accept an original/primary T1 candidate;
- `accept_mpr`: accept an axial derived MPR T1 candidate;
- `accept_sag_fallback`: explicitly accept a non-axial T1 after visual review;
- `accept_flair`: accept a FLAIR candidate;
- `exclude`: exclude the candidate.

Enter the decision and reviewer columns without changing the candidate key. Rerun `convert` after
saving the file. Automated selection never uses `accept_sag_fallback`; that decision is always
attributed to a human reviewer.

Review the generated montage together with protocol metadata, full-brain coverage, voxel size,
slice thickness/gap, source kind, and conversion warnings. Registration QC is supporting evidence,
not a substitute for checking contrast and artifacts.
