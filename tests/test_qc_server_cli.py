"""Viewer startup is headless by default; tests never launch a real browser."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gb_dicom2bids import qc_server


@pytest.fixture
def startup(monkeypatch, tmp_path):
    service = Mock(root=tmp_path)
    service.apply.return_value = []
    server = Mock()
    server.serve_forever.side_effect = KeyboardInterrupt
    factory = Mock(return_value=server)
    opening = Mock()
    monkeypatch.setattr(qc_server, "load_config", Mock(return_value=object()))
    monkeypatch.setattr(qc_server, "ReviewService", Mock(return_value=service))
    monkeypatch.setattr(qc_server, "file_lock", lambda path: nullcontext())
    monkeypatch.setattr(qc_server, "ThreadingHTTPServer", factory)
    monkeypatch.setattr(qc_server.webbrowser, "open", opening)
    return SimpleNamespace(service=service, server=server, factory=factory, opening=opening)


@pytest.mark.parametrize("flags", [[], ["--no-browser"]])
def test_default_and_legacy_flag_never_launch_browser(startup, capsys, flags):
    startup.factory.side_effect = lambda *a: (
        startup.service.warmup.assert_called_once() or startup.server
    )
    assert qc_server.main(["--config", "synthetic.yaml", *flags]) == 0
    startup.opening.assert_not_called()
    assert startup.factory.call_args.args[0] == ("127.0.0.1", 8765)
    assert "http://127.0.0.1:8765" in capsys.readouterr().out
    startup.server.server_close.assert_called_once()
    startup.service.close.assert_called_once()


def test_browser_is_opt_in_and_uses_selected_port(startup):
    assert qc_server.main(["--open-browser", "--port", "8899"]) == 0
    startup.opening.assert_called_once_with("http://127.0.0.1:8899")


@pytest.mark.parametrize("flags", [["--apply"], ["--apply", "--dry-run", "--open-browser"]])
def test_apply_never_starts_server_or_browser(startup, flags):
    assert qc_server.main(flags) == 0
    startup.opening.assert_not_called()
    startup.factory.assert_not_called()
    startup.service.apply.assert_called_once_with(dry_run="--dry-run" in flags)
    startup.service.close.assert_called_once()
    startup.service.warmup.assert_not_called()


def test_conflicting_browser_flags_are_rejected_before_startup(startup):
    with pytest.raises(SystemExit) as error:
        qc_server.main(["--open-browser", "--no-browser"])
    assert error.value.code == 2
    startup.factory.assert_not_called()
    startup.opening.assert_not_called()


def test_warmup_failure_never_prints_ready_url(startup, capsys):
    startup.service.warmup.side_effect = ValueError("synthetic invalid inventory")
    assert qc_server.main([]) == 2
    assert "Visual QC:" not in capsys.readouterr().out
    startup.factory.assert_not_called()
    startup.service.close.assert_called_once()


def test_interrupt_during_warmup_closes_service(startup):
    startup.service.warmup.side_effect = KeyboardInterrupt
    assert qc_server.main([]) == 130
    startup.factory.assert_not_called()
    startup.service.close.assert_called_once()


@pytest.mark.parametrize("error", [BrokenPipeError, ConnectionResetError])
def test_disconnected_tab_does_not_trigger_a_second_write(error):
    handler = object.__new__(qc_server.handler_class(Mock(), "synthetic-token"))
    handler._send_response = Mock(side_effect=error())
    handler.json_response(200, {"ready": True})
    handler._send_response.assert_called_once()
    assert handler.close_connection


def test_other_io_errors_are_not_hidden():
    handler = object.__new__(qc_server.handler_class(Mock(), "synthetic-token"))
    handler._send_response = Mock(side_effect=OSError("synthetic storage error"))
    with pytest.raises(OSError, match="storage"):
        handler.json_response(200, {})
