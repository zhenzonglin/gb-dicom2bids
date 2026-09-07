from gb_dicom2bids.qc_startup import StartupProgress


def test_progress_reports_to_stderr_and_stops_even_on_error(capsys):
    reporter = StartupProgress()
    try:
        with reporter:
            reporter.stage("synthetic stage")
            reporter.inventory(1024, 2048, 17)
            reporter._print()
            raise ValueError("synthetic failure")
    except ValueError:
        pass
    output = capsys.readouterr()
    assert not output.out
    assert "synthetic stage" in output.err
    assert "17" in output.err
    assert reporter.stopped.is_set()
    assert not reporter.thread.is_alive()
