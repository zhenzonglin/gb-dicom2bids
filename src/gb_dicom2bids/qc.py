from __future__ import annotations

import csv
import html
from pathlib import Path
from typing import Any

import matplotlib
import nibabel as nib
import numpy as np
import SimpleITK as sitk

from .config import ProjectConfig
from .models import SelectionRow, SeriesRecord

matplotlib.use("Agg")
from matplotlib import pyplot as plt  # noqa: E402


def run_qc(
    config: ProjectConfig,
    records: list[SeriesRecord],
    selections: list[SelectionRow],
    *,
    subjects: set[str] | None = None,
    output_prefix: str = "",
) -> list[dict[str, Any]]:
    audit = config.paths.audit_root
    montage_root = audit / f"{output_prefix}qc_montages"
    montage_root.mkdir(parents=True, exist_ok=True)
    selected = [
        row
        for row in selections
        if row.decision_status == "selected" and (subjects is None or row.subject_id in subjects)
    ]
    by_hash = {record.series_uid_hash: record for record in records}
    rows: list[dict[str, Any]] = []
    selected_by_subject: dict[str, list[SelectionRow]] = {}
    for row in selected:
        selected_by_subject.setdefault(row.subject_id, []).append(row)
        record = by_hash[row.series_uid_hash]
        nifti = _selected_path(config, row)
        status, detail = inspect_nifti(nifti)
        if (
            row.candidate_type == "t1"
            and record.plane != "axial"
            and row.manual_decision != "accept_sag_fallback"
        ):
            status = "fail"
            detail = "non-axial T1 lacks explicit manual fallback decision"
        montage = ""
        if nifti.exists() and status != "fail":
            montage_path = montage_root / f"sub-{row.subject_id}_{row.candidate_type}.png"
            create_montage(nifti, montage_path, f"sub-{row.subject_id} {row.candidate_type}")
            montage = str(montage_path)
        rows.append(
            {
                "subject_id": row.subject_id,
                "series_uid_hash": row.series_uid_hash,
                "candidate_type": row.candidate_type,
                "status": status,
                "detail": detail,
                "nifti": str(nifti),
                "montage": montage,
                "registration_metric": "",
            }
        )

    for subject_id, subject_rows in selected_by_subject.items():
        counts = {
            candidate: sum(row.candidate_type == candidate for row in subject_rows)
            for candidate in ("t1", "flair")
        }
        for candidate, count in counts.items():
            if count > 1:
                rows.append(
                    {
                        "subject_id": subject_id,
                        "series_uid_hash": "",
                        "candidate_type": candidate,
                        "status": "fail",
                        "detail": f"multiple selected {candidate} series: {count}",
                        "nifti": "",
                        "montage": "",
                        "registration_metric": "",
                    }
                )
        t1 = next((row for row in subject_rows if row.candidate_type == "t1"), None)
        flair = next((row for row in subject_rows if row.candidate_type == "flair"), None)
        if t1 and flair:
            registration_status, metric = pairwise_registration_check(
                _selected_path(config, t1), _selected_path(config, flair)
            )
            rows.append(
                {
                    "subject_id": subject_id,
                    "series_uid_hash": f"{t1.series_uid_hash}+{flair.series_uid_hash}",
                    "candidate_type": "t1_flair_registration",
                    "status": registration_status,
                    "detail": "rigid mutual-information registration check",
                    "nifti": "",
                    "montage": "",
                    "registration_metric": metric,
                }
            )

    for row in selections:
        if row.decision_status == "review" and (subjects is None or row.subject_id in subjects):
            rows.append(
                {
                    "subject_id": row.subject_id,
                    "series_uid_hash": row.series_uid_hash,
                    "candidate_type": row.candidate_type,
                    "status": "review",
                    "detail": row.reason,
                    "nifti": "",
                    "montage": "",
                    "registration_metric": "",
                }
            )
    _write_qc(audit / f"{output_prefix}qc_summary.tsv", rows)
    _write_html(audit / f"{output_prefix}qc_report.html", rows)
    return rows


def _selected_path(config: ProjectConfig, row: SelectionRow) -> Path:
    return (
        config.paths.staging_bids_root
        / f"sub-{row.subject_id}"
        / "anat"
        / f"{row.output_basename}.nii.gz"
    )


def inspect_nifti(path: Path) -> tuple[str, str]:
    if not path.is_file():
        return "fail", "selected NIfTI is missing"
    try:
        image = nib.load(str(path))
    except Exception as exc:
        return "fail", f"NIfTI cannot be read: {exc}"
    if len(image.shape) not in {3, 4} or any(size < 8 for size in image.shape[:3]):
        return "fail", f"unexpected shape {image.shape}"
    affine = np.asarray(image.affine)
    if not np.isfinite(affine).all() or abs(np.linalg.det(affine[:3, :3])) < 1e-8:
        return "fail", "invalid affine"
    qform, qcode = image.header.get_qform(coded=True)
    sform, scode = image.header.get_sform(coded=True)
    if (qcode == 0 or qform is None) and (scode == 0 or sform is None):
        return "fail", "both qform and sform are unset"
    return "pass", f"shape={image.shape};zooms={image.header.get_zooms()[:3]}"


def create_montage(path: Path, output: Path, title: str) -> None:
    image = nib.as_closest_canonical(nib.load(str(path)))
    data = np.asarray(image.dataobj)
    if data.ndim == 4:
        data = data[..., 0]
    centers = [size // 2 for size in data.shape]
    slices = [
        np.rot90(data[centers[0], :, :]),
        np.rot90(data[:, centers[1], :]),
        np.rot90(data[:, :, centers[2]]),
    ]
    finite = data[np.isfinite(data)]
    if finite.size:
        vmin, vmax = np.percentile(finite, [1, 99])
    else:
        vmin, vmax = 0.0, 1.0
    figure, axes = plt.subplots(1, 3, figsize=(9, 3))
    for axis, plane, label in zip(axes, slices, ("sagittal", "coronal", "axial"), strict=True):
        axis.imshow(plane, cmap="gray", vmin=vmin, vmax=vmax)
        axis.set_title(label)
        axis.axis("off")
    figure.suptitle(title)
    figure.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=120)
    plt.close(figure)


def pairwise_registration_check(t1_path: Path, flair_path: Path) -> tuple[str, str]:
    if not t1_path.is_file() or not flair_path.is_file():
        return "fail", "missing input"
    try:
        fixed = sitk.ReadImage(str(t1_path), sitk.sitkFloat32)
        moving = sitk.ReadImage(str(flair_path), sitk.sitkFloat32)
        initial = sitk.CenteredTransformInitializer(
            fixed,
            moving,
            sitk.Euler3DTransform(),
            sitk.CenteredTransformInitializerFilter.GEOMETRY,
        )
        registration = sitk.ImageRegistrationMethod()
        registration.SetMetricAsMattesMutualInformation(numberOfHistogramBins=32)
        registration.SetMetricSamplingStrategy(registration.RANDOM)
        registration.SetMetricSamplingPercentage(0.02, seed=2026)
        registration.SetInterpolator(sitk.sitkLinear)
        registration.SetOptimizerAsRegularStepGradientDescent(
            learningRate=2.0, minStep=0.01, numberOfIterations=80
        )
        registration.SetShrinkFactorsPerLevel([4, 2, 1])
        registration.SetSmoothingSigmasPerLevel([2, 1, 0])
        registration.SmoothingSigmasAreSpecifiedInPhysicalUnitsOn()
        registration.SetInitialTransform(initial, inPlace=False)
        registration.Execute(fixed, moving)
        metric = float(registration.GetMetricValue())
        if not np.isfinite(metric):
            return "fail", "non-finite metric"
        return "pass", f"{metric:.8g}"
    except Exception as exc:
        return "fail", str(exc)


def _write_qc(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "subject_id",
        "series_uid_hash",
        "candidate_type",
        "status",
        "detail",
        "nifti",
        "montage",
        "registration_metric",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def _write_html(path: Path, rows: list[dict[str, Any]]) -> None:
    table_rows = []
    for row in rows:
        montage = row.get("montage") or ""
        montage_link = (
            f'<a href="qc_montages/{html.escape(Path(montage).name)}">montage</a>'
            if montage
            else ""
        )
        values = [
            row.get("subject_id", ""),
            row.get("candidate_type", ""),
            row.get("status", ""),
            row.get("detail", ""),
            row.get("registration_metric", ""),
        ]
        cells = "".join(f"<td>{html.escape(str(value))}</td>" for value in values)
        table_rows.append(f"<tr>{cells}<td>{montage_link}</td></tr>")
    document = (
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>gb-dicom2bids QC</title>
<style>body{font-family:sans-serif}table{border-collapse:collapse}
td,th{border:1px solid #aaa;padding:.4rem}</style>
</head><body><h1>QC summary</h1><table><thead><tr><th>subject</th><th>type</th>
<th>status</th><th>detail</th><th>registration metric</th><th>image</th></tr></thead>
<tbody>"""
        + "\n".join(table_rows)
        + "</tbody></table></body></html>\n"
    )
    path.write_text(document, encoding="utf-8")
