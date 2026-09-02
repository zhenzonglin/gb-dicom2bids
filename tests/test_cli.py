from __future__ import annotations

import pytest

from gb_dicom2bids.cli import build_parser, main


def test_cli_help(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "inventory" in capsys.readouterr().out


def test_cli_missing_config(capsys, tmp_path) -> None:
    code = main(["inventory", "--config", str(tmp_path / "missing.yaml")])
    assert code == 2
    assert "configuration file not found" in capsys.readouterr().err


def test_cli_exposes_parallel_monitoring_commands() -> None:
    help_text = build_parser().format_help()
    for command in ("doctor", "pilot", "run", "status"):
        assert command in help_text
