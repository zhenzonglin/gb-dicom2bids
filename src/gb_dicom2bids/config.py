from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when a local configuration is unsafe or incomplete."""


@dataclass(frozen=True)
class PathsConfig:
    dicom_root: Path
    existing_bids_root: Path
    staging_bids_root: Path
    audit_root: Path
    work_root: Path | None = None


@dataclass(frozen=True)
class DatasetConfig:
    name: str = "Multicenter structural MRI"
    bids_version: str = "1.11.1"
    single_session: bool = True


@dataclass(frozen=True)
class SelectionConfig:
    axial_max_angle_deg: float = 20.0
    orientation_consistency_deg: float = 3.0
    manual_review_margin: float = 5.0
    allow_axial_mpr_fallback: bool = True
    minimum_brain_coverage_mm: float = 100.0
    flair_prefer_3d: bool = True


@dataclass(frozen=True)
class ConversionConfig:
    seed_from_existing_bids: bool = True
    convert_review_candidates: bool = True
    anonymize_sidecars: bool = True
    compression: str = "y"
    workers: int = 2
    pilot_workers: int = 2
    compression_threads: int = 2
    resume: bool = True


@dataclass(frozen=True)
class InventoryConfig:
    workers: int = 1


@dataclass(frozen=True)
class RuntimeConfig:
    status_interval_seconds: float = 5.0
    resource_interval_seconds: float = 30.0
    minimum_work_free_gb: float = 0.0
    minimum_staging_free_gb: float = 0.0
    minimum_available_memory_gb: float = 0.0


@dataclass(frozen=True)
class ToolsConfig:
    dcm2niix: str = "dcm2niix"
    deno: str = "deno"
    validator_spec: str = "jsr:@bids/validator@3.0.1"
    pigz: str = "pigz"


@dataclass(frozen=True)
class ProjectConfig:
    paths: PathsConfig
    dataset: DatasetConfig
    selection: SelectionConfig
    conversion: ConversionConfig
    tools: ToolsConfig
    inventory: InventoryConfig = field(default_factory=InventoryConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

    @property
    def work_root(self) -> Path:
        return self.paths.work_root or self.paths.audit_root / "work"


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"configuration section {name!r} must be a mapping")
    return value


def _absolute_path(value: Any, key: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{key} must be absolute: {value}")
    return path


def load_config(path: str | Path) -> ProjectConfig:
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a mapping")

    paths_raw = _section(raw, "paths")
    required = ("dicom_root", "existing_bids_root", "staging_bids_root", "audit_root")
    missing = [key for key in required if key not in paths_raw]
    if missing:
        raise ConfigError(f"missing path keys: {', '.join(missing)}")
    paths = PathsConfig(
        dicom_root=_absolute_path(paths_raw["dicom_root"], "paths.dicom_root"),
        existing_bids_root=_absolute_path(
            paths_raw["existing_bids_root"], "paths.existing_bids_root"
        ),
        staging_bids_root=_absolute_path(paths_raw["staging_bids_root"], "paths.staging_bids_root"),
        audit_root=_absolute_path(paths_raw["audit_root"], "paths.audit_root"),
        work_root=(
            _absolute_path(paths_raw["work_root"], "paths.work_root")
            if "work_root" in paths_raw
            else None
        ),
    )

    dataset = DatasetConfig(**_section(raw, "dataset"))
    selection = SelectionConfig(**_section(raw, "selection"))
    conversion = ConversionConfig(**_section(raw, "conversion"))
    tools = ToolsConfig(**_section(raw, "tools"))
    inventory = InventoryConfig(**_section(raw, "inventory"))
    runtime = RuntimeConfig(**_section(raw, "runtime"))
    config = ProjectConfig(paths, dataset, selection, conversion, tools, inventory, runtime)
    validate_config(config)
    return config


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def validate_config(config: ProjectConfig) -> None:
    paths = config.paths
    all_paths = {
        "dicom_root": paths.dicom_root,
        "existing_bids_root": paths.existing_bids_root,
        "staging_bids_root": paths.staging_bids_root,
        "audit_root": paths.audit_root,
    }
    if paths.work_root is not None:
        all_paths["work_root"] = paths.work_root
    normalized = [path.resolve(strict=False) for path in all_paths.values()]
    if len(set(normalized)) != len(normalized):
        raise ConfigError("DICOM, existing BIDS, staging BIDS, and audit roots must be distinct")

    sources = (paths.dicom_root, paths.existing_bids_root)
    destinations = (paths.staging_bids_root, paths.audit_root, config.work_root)
    for destination in destinations:
        for source in sources:
            if _is_within(destination, source) or _is_within(source, destination):
                raise ConfigError(
                    f"unsafe overlapping source/destination roots: {source} and {destination}"
                )

    if not 0.0 < config.selection.axial_max_angle_deg < 45.0:
        raise ConfigError("selection.axial_max_angle_deg must be between 0 and 45")
    if not 0.0 < config.selection.orientation_consistency_deg < 30.0:
        raise ConfigError("selection.orientation_consistency_deg must be between 0 and 30")
    if config.conversion.compression not in {"y", "pigz"}:
        raise ConfigError("conversion.compression must be 'y' or 'pigz'")
    if config.conversion.workers < 1:
        raise ConfigError("conversion.workers must be at least 1")
    if config.conversion.pilot_workers < 1:
        raise ConfigError("conversion.pilot_workers must be at least 1")
    if config.conversion.compression_threads < 1:
        raise ConfigError("conversion.compression_threads must be at least 1")
    if config.inventory.workers < 1:
        raise ConfigError("inventory.workers must be at least 1")
    if config.runtime.status_interval_seconds <= 0:
        raise ConfigError("runtime.status_interval_seconds must be positive")
    if config.runtime.resource_interval_seconds <= 0:
        raise ConfigError("runtime.resource_interval_seconds must be positive")
    thresholds = {
        "minimum_work_free_gb": config.runtime.minimum_work_free_gb,
        "minimum_staging_free_gb": config.runtime.minimum_staging_free_gb,
        "minimum_available_memory_gb": config.runtime.minimum_available_memory_gb,
    }
    if any(value < 0 for value in thresholds.values()):
        raise ConfigError("runtime resource thresholds must be non-negative")
    if not config.dataset.single_session:
        raise ConfigError("version 0.2 supports single-session BIDS datasets only")


def require_inputs(config: ProjectConfig, *, need_existing_bids: bool = False) -> None:
    if not config.paths.dicom_root.is_dir():
        raise ConfigError(f"DICOM root does not exist: {config.paths.dicom_root}")
    if need_existing_bids and not config.paths.existing_bids_root.is_dir():
        raise ConfigError(f"existing BIDS root does not exist: {config.paths.existing_bids_root}")
