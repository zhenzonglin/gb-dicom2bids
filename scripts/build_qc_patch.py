"""Build an allow-listed, data-free offline patch for the v0.2.0 source checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

BASE = "6f1e44b1ead066c71c3646e634bf02330decdd5e"
FILES = [
    ".gitignore",
    "README.md",
    "pyproject.toml",
    "nifti_qc.py",
    "qc_viewer.py",
    "docs/preconverted_nifti_qc.md",
    "docs/visual_qc.md",
    "src/gb_dicom2bids/classify.py",
    "src/gb_dicom2bids/cli.py",
    "src/gb_dicom2bids/convert.py",
    "src/gb_dicom2bids/manifest.py",
    "src/gb_dicom2bids/nifti_import.py",
    "src/gb_dicom2bids/qc_state.py",
    "src/gb_dicom2bids/qc_images.py",
    "src/gb_dicom2bids/qc_review.py",
    "src/gb_dicom2bids/qc_server.py",
    "src/gb_dicom2bids/qc_web/index.html",
    "src/gb_dicom2bids/qc_web/app.js",
    "src/gb_dicom2bids/qc_web/style.css",
    "tests/test_visual_qc.py",
    "tests/test_classify.py",
    "tests/test_nifti_import.py",
    "tests/test_qc_patch.py",
    "scripts/make_qc_demo.py",
    "scripts/check_qc_browser.py",
    "scripts/build_qc_patch.py",
    "scripts/install_qc_patch.py",
]


def build(root: Path, output: Path) -> Path:
    from gb_dicom2bids.security import FORBIDDEN_PATH_FRAGMENTS, FORBIDDEN_TOKEN, SECRET_MARKERS

    entries, contents = [], {}
    for name in FILES:
        content = (root / name).read_bytes().replace(b"\r\n", b"\n")
        text = content.decode("utf-8")
        if FORBIDDEN_TOKEN in text.lower() or any(
            marker.lower() in text.lower()
            for marker in (*SECRET_MARKERS, *FORBIDDEN_PATH_FRAGMENTS)
        ):
            raise ValueError(f"public release guard rejected {name}")
        prior = subprocess.run(
            ["git", "show", f"{BASE}:{name}"], cwd=root, capture_output=True, check=False
        )
        entries.append(
            {
                "path": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "base_sha256": hashlib.sha256(prior.stdout.replace(b"\r\n", b"\n")).hexdigest()
                if prior.returncode == 0
                else None,
            }
        )
        contents[f"payload/{name}"] = content
    manifest = {
        "patch": "visual-qc-1",
        "base_version": "0.2.0",
        "base_commit": BASE,
        "author": "zhenzong",
        "files": entries,
    }
    contents["PATCH_MANIFEST.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    contents["install_qc_patch.py"] = contents["payload/scripts/install_qc_patch.py"]
    contents["README_INSTALL.md"] = contents["payload/docs/visual_qc.md"]
    sums = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {name}\n"
        for name, content in sorted(contents.items())
    )
    contents["CHECKSUMS.sha256"] = sums.encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(contents.items()):
            archive.writestr(name, content)
    output.with_suffix(output.suffix + ".sha256").write_text(
        f"{hashlib.sha256(output.read_bytes()).hexdigest()}  {output.name}\n", encoding="utf-8"
    )
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("dist/gb-dicom2bids-v0.2.0-visual-qc-1.zip")
    )
    args = parser.parse_args()
    print(build(Path(__file__).resolve().parents[1], args.output.resolve()))
