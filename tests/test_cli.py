from __future__ import annotations

import pytest

from gb_dicom2bids.cli import main


def test_cli_help(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "inventory" in capsys.readouterr().out


def test_cli_missing_config(capsys, tmp_path) -> None:
    code = main(["inventory", "--config", str(tmp_path / "missing.yaml")])
    assert code == 2
    assert "configuration file not found" in capsys.readouterr().err
