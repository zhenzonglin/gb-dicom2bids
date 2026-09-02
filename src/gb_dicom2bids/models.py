from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SeriesRecord:
    center: str
    subject_id: str
    study_uid_hash: str
    series_uid_hash: str
    series_number: int | None = None
    modality: str = ""
    series_description: str = ""
    protocol_name: str = ""
    sequence_name: str = ""
    image_type: list[str] = field(default_factory=list)
    manufacturer: str = ""
    model_name: str = ""
    software_versions: str = ""
    acquisition_type: str = ""
    repetition_time_ms: float | None = None
    echo_time_ms: float | None = None
    inversion_time_ms: float | None = None
    flip_angle_deg: float | None = None
    rows: int | None = None
    columns: int | None = None
    pixel_spacing_mm: list[float] = field(default_factory=list)
    slice_thickness_mm: float | None = None
    spacing_between_slices_mm: float | None = None
    image_orientation_patient: list[float] = field(default_factory=list)
    nearest_plane: str = "unknown"
    plane: str = "unknown"
    plane_angle_deg: float | None = None
    orientation_consistent: bool = True
    coverage_mm: float | None = None
    source_kind: str = "unknown"
    candidate_type: str = "other"
    classification_confidence: str = "low"
    protocol_id: str = ""
    instance_count: int = 0
    duplicate_instance_count: int = 0
    source_relpaths: list[str] = field(default_factory=list, repr=False)
    inventory_note: str = ""

    def public_dict(self) -> dict[str, Any]:
        """Return fields safe for the human-readable inventory."""
        data = asdict(self)
        data.pop("source_relpaths", None)
        data["image_type"] = "\\".join(self.image_type)
        data["pixel_spacing_mm"] = "\\".join(str(x) for x in self.pixel_spacing_mm)
        data["image_orientation_patient"] = "\\".join(
            f"{x:.8g}" for x in self.image_orientation_patient
        )
        return data

    def private_dict(self) -> dict[str, Any]:
        """Return the resumable representation stored only in the private audit root."""
        return asdict(self)

    @classmethod
    def from_private_dict(cls, data: dict[str, Any]) -> SeriesRecord:
        return cls(**data)


@dataclass
class SelectionRow:
    center: str
    subject_id: str
    study_uid_hash: str
    series_uid_hash: str
    candidate_type: str
    decision_status: str
    score: float
    reason: str
    source_plane: str
    source_kind: str
    protocol_id: str
    output_basename: str = ""
    reviewer: str = ""
    manual_decision: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["score"] = f"{self.score:.6f}"
        return data


@dataclass
class ConversionResult:
    subject_id: str
    series_uid_hash: str
    candidate_type: str
    status: str
    mode: str
    output_path: str = ""
    message: str = ""
    output_sha256: str = ""
    sidecar_path: str = ""
    sidecar_sha256: str = ""
    worker_pid: int | None = None
    child_pid: int | None = None
    started_at: str = ""
    finished_at: str = ""
    elapsed_seconds: float | None = None
    log_path: str = ""

    def to_dict(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items()}
