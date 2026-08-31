# Legacy conversion migration

The earlier conversion scripts grouped inputs by directory and relied heavily on textual tokens in
series descriptions. The new workflow changes the unit of work to DICOM Series Instance UID and
separates inventory, selection, conversion, QC, and validation.

The old scripts remain outside this repository as historical provenance. Their absolute paths,
runtime logs, real participant identifiers, and output data must not be copied into the public
project.
