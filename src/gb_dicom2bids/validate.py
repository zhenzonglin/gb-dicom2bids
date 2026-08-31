from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import ProjectConfig


def run_bids_validator(config: ProjectConfig) -> dict[str, Any]:
    deno = config.tools.deno
    if shutil.which(deno) is None and not Path(deno).is_file():
        raise RuntimeError(f"Deno not found: {deno}")
    dataset_description = config.paths.staging_bids_root / "dataset_description.json"
    if not dataset_description.is_file():
        raise RuntimeError(f"missing dataset_description.json: {dataset_description}")

    output = config.paths.audit_root / "bids_validator.json"
    command = [
        deno,
        "run",
        "-ERWN",
        config.tools.validator_spec,
        str(config.paths.staging_bids_root),
        "--json",
        "--outfile",
        str(output),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    status = {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "result_file": str(output),
        "passed": completed.returncode == 0,
    }
    status_path = config.paths.audit_root / "validation_status.json"
    status_path.write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return status
