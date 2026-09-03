from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def patcher(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "patch_installer", Path(__file__).parents[1] / "scripts/install_qc_patch.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project, bundle, audit = (tmp_path / name for name in ("project", "bundle", "audit"))
    for path in (project, bundle, audit):
        path.mkdir()
    original = b"# synthetic original\r\n"
    (project / "qc_viewer.py").write_bytes(original)
    (project / "monitor.py").write_text("preserve monitor", encoding="utf-8")
    (project / "config").mkdir()
    (project / "config/config.local.yaml").write_text("preserve config", encoding="utf-8")
    (audit / "staging_seed.json").write_text(
        '{"status": "completed", "bytes_completed": 123}', encoding="utf-8"
    )
    entries = []
    for name, content, base in (
        ("qc_viewer.py", b"# synthetic replacement\n", original),
        ("src/gb_dicom2bids/qc_state.py", b"# new module\n", None),
    ):
        path = bundle / "payload" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        entries.append(
            {
                "path": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "base_sha256": hashlib.sha256(base.replace(b"\r\n", b"\n")).hexdigest()
                if base
                else None,
            }
        )
    module.write_json(bundle / "PATCH_MANIFEST.json", {"patch": "visual-qc-1", "files": entries})
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [], 0, stdout=str(project / "src/gb_dicom2bids/__init__.py") + "\n"
        ),
    )
    yield module, project, bundle, audit


def test_offline_install_backup_repeat_rollback_preserves_runtime(patcher):
    module, project, bundle, audit = patcher
    snapshot = {
        p: p.read_bytes()
        for p in (
            project / "monitor.py",
            project / "config/config.local.yaml",
            audit / "staging_seed.json",
        )
    }
    assert module.install(bundle, project, audit, check_only=True)["files_to_update"] == 2
    assert (project / "qc_viewer.py").read_bytes() == b"# synthetic original\r\n"
    result = module.install(bundle, project, audit)
    assert result["status"] == "installed"
    assert (audit / "visual_qc/enabled.json").exists()
    assert module.install(bundle, project, audit)["status"] == "already_installed"
    module.rollback(project, audit, result["rollback_id"])
    assert (project / "qc_viewer.py").read_bytes() == b"# synthetic original\r\n"
    assert not (project / "src/gb_dicom2bids/qc_state.py").exists()
    assert all(p.read_bytes() == content for p, content in snapshot.items())
    assert (audit / "visual_qc/enabled.json").exists()  # audit is never deleted


def test_modified_code_and_corrupt_payload_refuse_before_mutation(patcher):
    module, project, bundle, audit = patcher
    (project / "qc_viewer.py").write_text("# local edit", encoding="utf-8")
    with pytest.raises(ValueError, match="local version differs"):
        module.install(bundle, project, audit)
    assert not (project / "work/patch_backups").exists()
    (bundle / "payload/qc_viewer.py").write_text("# corrupt", encoding="utf-8")
    with pytest.raises(ValueError, match="payload checksum mismatch"):
        module.install(bundle, project, audit)


def test_rollback_protects_post_patch_edits(patcher):
    module, project, bundle, audit = patcher
    result = module.install(bundle, project, audit)
    (project / "qc_viewer.py").write_text("# new user edit", encoding="utf-8")
    with pytest.raises(ValueError, match="file changed"):
        module.rollback(project, audit, result["rollback_id"])
    assert (project / "qc_viewer.py").read_text() == "# new user edit"


def test_patch_path_and_runtime_guards(patcher, monkeypatch):
    module, project, bundle, audit = patcher
    for name in ("../escape.py", "config/config.local.yaml", "monitor.py", "src/../other.py"):
        with pytest.raises(ValueError):
            module.target(project, name)
    (audit / "run_status.json").write_text(json.dumps({"pid": 1234567}), encoding="utf-8")
    monkeypatch.setattr(module.psutil, "pid_exists", lambda pid: True)
    with pytest.raises(RuntimeError, match="is alive"):
        module.install(bundle, project, audit)
