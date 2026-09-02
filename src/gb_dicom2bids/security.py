from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

EXCLUDED_DIRECTORIES = {
    ".git",
    ".venv",
    "build",
    "dist",
    "htmlcov",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}
EXCLUDED_FILES = {".coverage"}
FORBIDDEN_TOKEN = "co" + "dex"
FORBIDDEN_EXTENSIONS = {".dcm", ".ima", ".nii", ".bval", ".bvec", ".p12", ".pfx", ".pem", ".key"}
FORBIDDEN_PATH_FRAGMENTS = ("/data/" + "shares/", "/data/" + "usersdir/")
SECRET_MARKERS = ("gh" + "p_", "github_" + "pat_", "BEGIN " + "PRIVATE KEY")
MAX_FILE_BYTES = 5 * 1024 * 1024
EXPECTED_NAME = "zhenzong"
EXPECTED_EMAIL = "linzhenzong1@163.com"


def check_public_release(root: Path) -> list[str]:
    problems: list[str] = []
    for path in _files_to_check(root):
        relative = path.relative_to(root).as_posix()
        if FORBIDDEN_TOKEN in relative.lower():
            problems.append(f"forbidden token in filename: {relative}")
        lowered = relative.lower()
        if lowered.endswith(".nii.gz") or path.suffix.lower() in FORBIDDEN_EXTENSIONS:
            problems.append(f"prohibited data or credential extension: {relative}")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            problems.append(f"file exceeds {MAX_FILE_BYTES} bytes: {relative}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append(f"unexpected binary file: {relative}")
            continue
        lowered_text = text.lower()
        if FORBIDDEN_TOKEN in lowered_text:
            problems.append(f"forbidden token in content: {relative}")
        for fragment in FORBIDDEN_PATH_FRAGMENTS:
            if fragment in text:
                problems.append(f"private absolute path in content: {relative}")
        for marker in SECRET_MARKERS:
            if marker.lower() in lowered_text:
                problems.append(f"possible credential in content: {relative}")
    problems.extend(_check_git_history(root))
    return sorted(set(problems))


def _files_to_check(root: Path) -> list[Path]:
    git = root / ".git"
    if git.exists():
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=False
        )
        if completed.returncode == 0 and completed.stdout:
            names = [name for name in completed.stdout.decode("utf-8").split("\0") if name]
            return [root / name for name in names if (root / name).is_file()]
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in EXCLUDED_DIRECTORIES and not name.endswith(".egg-info")
        )
        for filename in sorted(filenames):
            if filename in EXCLUDED_FILES:
                continue
            files.append(Path(directory) / filename)
    return files


def _check_git_history(root: Path) -> list[str]:
    if not (root / ".git").exists():
        return []
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            "--format=%an%x09%ae%x09%cn%x09%ce%x09%B%x00",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout:
        return []
    problems: list[str] = []
    for entry in completed.stdout.split("\0"):
        entry = entry.lstrip("\r\n")
        if not entry.strip():
            continue
        parts = entry.split("\t", 4)
        if len(parts) != 5:
            problems.append("unable to parse git history identity")
            continue
        author, author_email, committer, committer_email, body = parts
        if (
            author != EXPECTED_NAME
            or author_email != EXPECTED_EMAIL
            or committer != EXPECTED_NAME
            or committer_email != EXPECTED_EMAIL
        ):
            problems.append(
                "unexpected git identity: "
                f"author={author!r} <{author_email}>, "
                f"committer={committer!r} <{committer_email}>"
            )
        if FORBIDDEN_TOKEN in body.lower():
            problems.append("forbidden token in git history")
        if "co-authored-by:" in body.lower():
            problems.append("automatic co-author footer in git history")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that a repository is safe for public release"
    )
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    problems = check_public_release(Path(args.root).resolve())
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}")
        return 1
    print("Public release gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
