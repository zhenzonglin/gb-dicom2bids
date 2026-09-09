from __future__ import annotations

import hashlib
import re
import unicodedata

from .models import SeriesRecord

CLASSIFICATION_VERSION = "sequence-defaults-3"

T1_KEYWORDS = (
    "t1",
    "mprage",
    "bravo",
    "spgr",
    "tfl3d",
)
FLAIR_KEYWORDS = (
    "flair",
    "darkfluid",
    "fluidattenuated",
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


def compact_text(*values: str) -> str:
    """Normalize names for case-insensitive substring classification."""
    text = " ".join(value or "" for value in values).lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def named_t1_plane(record: SeriesRecord) -> str | None:
    """Protocol-name hint only, never evidence of physical acquisition orientation."""
    hints = set()
    for value in (record.series_description, record.protocol_name, record.sequence_name):
        name = unicodedata.normalize("NFKC", value or "").lower()
        # Exported PosDisp suffixes describe another positioning/reference series.
        name = name.split("posdisp", 1)[0]
        if "t1" not in compact_text(name):
            continue
        name = re.sub(r"t1w?", "t1 ", name)
        for plane, pattern in (
            ("sag", r"(?<![a-z])sag(?:ittal)?(?![a-z])"),
            ("tra", r"(?<![a-z])tra(?:nsverse)?(?![a-z])"),
        ):
            if re.search(pattern, name):
                hints.add(plane)
    return next(iter(hints)) if len(hints) == 1 else None


def default_classification(record: SeriesRecord) -> dict:
    """Pure name/timing suggestion. Never modifies source identity or manual decisions."""
    names = [
        unicodedata.normalize("NFKC", value or "").lower()
        for value in (record.series_description, record.protocol_name, record.sequence_name)
    ]
    compact = [compact_text(name) for name in names]
    tokens = [normalized_text(name) for name in names]
    modality, confidence, reason, excluded = "other", "low", "unrecognized", False
    non_target = (
        ("ct", r"(?<![a-z0-9])ct(?![a-z0-9])"),
        ("tof", r"(?<![a-z0-9])(?:[23]d[ _-]*)?tof(?:[ _-]*[23]d)?(?![a-z0-9])"),
        ("mra", r"(?<![a-z0-9])(?:[23]d[ _-]*)?mra\d*(?![a-z0-9])"),
        ("dwi", r"(?<![a-z0-9])(?:[dei]*|iso|[23]d[ _-]*)dwi(?![a-z0-9])"),
        ("b0", r"(?<![a-z0-9])(?:[esd])?b[ _-]*0(?![a-z0-9])"),
        ("b1000", r"(?<![a-z0-9])(?:[esd])?b[ _-]*1000(?![a-z0-9])"),
    )
    hit = next(
        (label for label, regex in non_target if any(re.search(regex, n) for n in names)), None
    )
    # Preserve existing localizer/projection guards, without combining two names.
    projection = any(t in name for name in tokens for t in EXCLUDED_TOKENS) or any(
        token in normalized_text(*record.image_type) for token in EXCLUDED_TOKENS
    )
    if record.modality.upper() != "MR" or hit or projection:
        excluded, confidence = True, "high"
        reason = "non_target_" + (hit or ("projection" if projection else record.modality.lower()))
    elif any("t1" in name and "flair" in name for name in compact):
        modality, confidence, reason = "t1", "high", "name_t1_and_flair"
    elif any(k in name for name in compact for k in FLAIR_KEYWORDS):
        modality, confidence, reason = "flair", "high", "name_flair"
    elif _plausible_flair_timing(record) and not any(
        k in name for name in compact for k in T1_KEYWORDS
    ):
        modality, confidence, reason = "flair", "medium", "flair_timing"
    elif any(k in name for name in compact for k in T1_KEYWORDS):
        modality, confidence, reason = "t1", "high", "name_t1"
    return {
        "modality": modality,
        "confidence": confidence,
        "reason": reason,
        "excluded": excluded,
        "version": CLASSIFICATION_VERSION,
    }


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
    suggestion = default_classification(record)
    record.candidate_type = suggestion["modality"]
    record.classification_confidence = suggestion["confidence"]

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
