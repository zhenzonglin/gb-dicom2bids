"""Offline, checksum-verified code patch with per-file backup and conservative rollback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import psutil
import yaml


def checksum(path: Path, *, normalize: bool = False) -> str:
    content = path.read_bytes()
    if normalize:
        content = content.replace(b"\r\n", b"\n")
    return hashlib.sha256(content).hexdigest()


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def target(project: Path, name: str) -> Path:
    part = PurePosixPath(name)
    if part.is_absolute() or ".." in part.parts or "\\" in name or ":" in name:
        raise ValueError("invalid patch path")
    allowed = name in {
        "qc_viewer.py",
        "pyproject.toml",
        "README.md",
        ".gitignore",
    } or name.startswith(("src/gb_dicom2bids/", "docs/", "scripts/", "tests/"))
    if not allowed or name.endswith(("monitor.py", "test_monitor.py")):
        raise ValueError(f"protected/non-code path in patch: {name}")
    result = project / name
    if not result.resolve().is_relative_to(project) or result.is_symlink():
        raise ValueError(f"symlink/escaping patch target: {name}")
    return result


def assert_idle(project: Path, audit: Path) -> None:
    pids = [read_json(audit / "run_status.json").get("pid")]
    launch = audit / "run.launch"
    if launch.exists():
        pids.extend(
            line.partition("=")[2]
            for line in launch.read_text().splitlines()
            if line.startswith("PID=")
        )
    active = {"linking", "dcm2niix", "compressing", "validating", "installing", "running"}
    for path in (audit / "status").glob("*.json"):
        state = read_json(path)
        if state.get("stage") in active:
            pids.extend((state.get("worker_pid"), state.get("child_pid")))
    for raw in pids:
        try:
            pid = int(raw)
        except (ValueError, TypeError):
            continue
        if pid > 0 and pid != os.getpid() and psutil.pid_exists(pid):
            raise RuntimeError(f"recorded pipeline PID {pid} is alive; safely stop/finish it first")
    for process in psutil.process_iter(["pid", "cmdline", "cwd"]):
        if process.pid == os.getpid():
            continue
        try:
            args = process.info["cmdline"] or []
            names = {Path(arg).name.lower() for arg in args}
            if names & {
                "qc_viewer.py",
                "gb-dicom2bids",
                "gb-dicom2bids.exe",
                "gb_dicom2bids",
                "gb_dicom2bids.cli",
            }:
                cwd = Path(process.info["cwd"] or "").resolve()
                if cwd == project or str(project) in " ".join(args):
                    raise RuntimeError(
                        f"project process {process.pid} is alive; close it before patching"
                    )
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue


@contextmanager
def exclusive(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("another pipeline/patch operation holds the writer lock") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_copy(source: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".patch-tmp")
    shutil.copy2(source, temporary)
    temporary.replace(destination)


def install(bundle: Path, project: Path, audit: Path, *, check_only=False):
    manifest = read_json(bundle / "PATCH_MANIFEST.json")
    if manifest.get("patch") != "visual-qc-1" or not manifest.get("files"):
        raise ValueError("invalid or missing patch manifest")
    assert_idle(project, audit)
    changes = []
    for entry in manifest["files"]:
        name = entry["path"]
        destination = target(project, name)
        source = target(bundle / "payload", name)
        if checksum(source) != entry["sha256"]:
            raise ValueError(f"payload checksum mismatch: {name}")
        if destination.exists() and checksum(destination) == entry["sha256"]:
            continue
        observed = checksum(destination, normalize=True) if destination.exists() else None
        if observed != entry["base_sha256"]:
            raise ValueError(
                f"local version differs: {name}; no files changed, do not force overwrite"
            )
        if name.endswith(".py"):
            compile(source.read_text(encoding="utf-8"), name, "exec")
        changes.append(entry)
    # Verify the existing editable environment before changing any code.
    check = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pathlib,gb_dicom2bids; print(pathlib.Path(gb_dicom2bids.__file__).resolve())",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    )
    module = Path(check.stdout.strip()).resolve()
    if not module.is_relative_to(project / "src"):
        raise RuntimeError("environment imports a different checkout; activate its environment")
    if check_only:
        return {"check": "passed", "files_to_update": len(changes)}
    if not changes:
        marker = audit / "visual_qc" / "enabled.json"
        if not marker.exists():
            write_json(
                marker,
                {
                    "enabled_at": datetime.now(UTC).isoformat(),
                    "patch": manifest["patch"],
                    "version": 1,
                },
            )
        return {"status": "already_installed"}
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = project / "work" / "patch_backups" / stamp
    backup.mkdir(parents=True)
    journal = {"patch": manifest["patch"], "status": "installing", "files": changes}
    # All originals are backed up before the first code file is replaced.
    for entry in changes:
        destination = target(project, entry["path"])
        if destination.exists():
            original = backup / "files" / entry["path"]
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, original)
            entry["original_sha256"] = checksum(original)
        else:
            entry["original_sha256"] = None
    write_json(backup / "backup.json", journal)
    try:
        for entry in changes:
            atomic_copy(bundle / "payload" / entry["path"], target(project, entry["path"]))
        marker = audit / "visual_qc" / "enabled.json"
        if not marker.exists():
            write_json(marker, {"enabled_at": stamp, "patch": manifest["patch"], "version": 1})
        journal["status"] = "completed"
        write_json(backup / "backup.json", journal)
    except Exception as exc:
        journal.update(status="failed", error=str(exc))
        write_json(backup / "backup.json", journal)
        raise RuntimeError(f"patch interrupted; recover with --rollback {backup.name}") from exc
    return {
        "status": "installed",
        "files": len(changes),
        "backup": str(backup),
        "rollback_id": backup.name,
    }


def rollback(project: Path, audit: Path, backup_id: str):
    if not backup_id.isalnum():
        raise ValueError("rollback id must be the recorded timestamp")
    backup = (project / "work" / "patch_backups" / backup_id).resolve()
    if not backup.is_relative_to(project / "work" / "patch_backups"):
        raise ValueError("invalid backup directory")
    journal = read_json(backup / "backup.json")
    if journal.get("patch") != "visual-qc-1":
        raise ValueError("not a visual QC patch backup")
    assert_idle(project, audit)
    for entry in journal["files"]:
        destination = target(project, entry["path"])
        observed = checksum(destination) if destination.exists() else None
        if observed not in {entry["sha256"], entry.get("original_sha256")}:
            raise ValueError(f"file changed after patch: {entry['path']}; rollback refused")
        if (
            entry.get("original_sha256")
            and checksum(backup / "files" / entry["path"]) != entry["original_sha256"]
        ):
            raise ValueError("backup checksum mismatch")
    for entry in reversed(journal["files"]):
        destination = target(project, entry["path"])
        if entry.get("original_sha256"):
            atomic_copy(backup / "files" / entry["path"], destination)
        elif destination.exists():
            destination.unlink()  # only this verified, patch-created code file
    journal["status"] = "rolled_back"
    write_json(backup / "backup.json", journal)
    return {
        "status": "rolled_back",
        "notice": "code only; QC audit, image backups and staging unchanged. "
        "Old code has no visual gate: do not resume production curation until patch restored.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config/config.local.yaml"))
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--rollback")
    args = parser.parse_args()
    project = args.project.resolve()
    config_path = args.config if args.config.is_absolute() else project / args.config
    from gb_dicom2bids.config import load_config

    load_config(config_path)  # Validate source/destination separation before any writes.
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    audit = Path(raw["paths"]["audit_root"]).resolve()
    with exclusive(audit / ".bids-writer.lock"), exclusive(project / ".qc-patch.lock"):
        result = (
            rollback(project, audit, args.rollback)
            if args.rollback
            else install(Path(__file__).resolve().parent, project, audit, check_only=args.check)
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error
