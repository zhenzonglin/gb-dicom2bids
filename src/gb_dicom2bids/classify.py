from __future__ import annotations

import hashlib
import re

from .models import SeriesRecord

T1_TOKENS = (
    " t1 ",
    " t1w ",
    " et1w ",
    " mprage ",
    " bravo ",
    " spgr ",
    " tfl3d ",
    " t1seg ",
)
FLAIR_TOKENS = (
    " flair ",
    " t2flair ",
    " t2 flair ",
    " darkfluid ",
    " dark fluid ",
    " fluid attenuated ",
)
EXCLUDED_TOKENS = (
    " localizer ",
    " scout ",
    " survey ",
    " screensave ",
    " screen save ",
    " mip ",
    " minip ",
    " phoenixzip ",
)


def normalized_text(*values: str) -> str:
    text = " ".join(value or "" for value in values).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return f" {text.strip()} "


def source_kind(image_type: list[str], description: str, protocol: str) -> str:
    tokens = {str(value).upper() for value in image_type}
    text = normalized_text(description, protocol)
    is_mpr = any(token in text for token in (" mpr ", " reformat ", " reformatted "))
    if "ORIGINAL" in tokens and "PRIMARY" in tokens and not is_mpr:
        return "original"
    if is_mpr:
        return "derived_mpr"
    if "DERIVED" in tokens or "SECONDARY" in tokens:
        return "derived"
    return "unknown"


def _plausible_flair_timing(record: SeriesRecord) -> bool:
    tr = record.repetition_time_ms
    te = record.echo_time_ms
    ti = record.inversion_time_ms
    return bool(
        tr is not None
        and te is not None
        and ti is not None
        and tr >= 4_000.0
        and te >= 60.0
        and ti >= 1_200.0
    )


def classify_record(record: SeriesRecord) -> SeriesRecord:
    text = normalized_text(
        record.series_description,
        record.protocol_name,
        record.sequence_name,
        " ".join(record.image_type),
    )
    excluded = any(token in text for token in EXCLUDED_TOKENS)
    has_flair_name = any(token in text for token in FLAIR_TOKENS)
    has_t1_name = any(token in text for token in T1_TOKENS)
    t1_flair = " t1 flair " in text or " t1flair " in text

    if record.modality.upper() != "MR" or excluded:
        record.candidate_type = "other"
        record.classification_confidence = "high" if excluded else "low"
    elif has_flair_name and not t1_flair:
        record.candidate_type = "flair"
        record.classification_confidence = "high"
    elif _plausible_flair_timing(record) and not has_t1_name:
        record.candidate_type = "flair"
        record.classification_confidence = "medium"
    elif has_t1_name and not has_flair_name:
        record.candidate_type = "t1"
        record.classification_confidence = "high"
    else:
        record.candidate_type = "other"
        record.classification_confidence = "low"

    record.source_kind = source_kind(
        record.image_type, record.series_description, record.protocol_name
    )
    record.protocol_id = protocol_identifier(record)
    return record


def protocol_identifier(record: SeriesRecord) -> str:
    values = (
        record.candidate_type,
        record.manufacturer.lower().strip(),
        record.model_name.lower().strip(),
        record.software_versions.lower().strip(),
        record.acquisition_type.upper().strip(),
        _rounded(record.repetition_time_ms),
        _rounded(record.echo_time_ms),
        _rounded(record.inversion_time_ms),
        _rounded(record.flip_angle_deg),
        _rounded(record.slice_thickness_mm),
        _rounded(record.spacing_between_slices_mm),
        "x".join(_rounded(value) for value in record.pixel_spacing_mm),
    )
    digest = hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()[:10]
    prefix = record.candidate_type if record.candidate_type in {"t1", "flair"} else "other"
    return f"{prefix}-{digest}"


def _rounded(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"
