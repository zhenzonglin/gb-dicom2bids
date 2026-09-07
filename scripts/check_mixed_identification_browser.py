"""Synthetic browser check: retain a target while excluding one failed false candidate."""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import nibabel as nib
import numpy as np
from make_assist_demo import make_demo
from playwright.sync_api import expect, sync_playwright

from gb_dicom2bids.config import load_config
from gb_dicom2bids.qc_identify import Identification
from gb_dicom2bids.qc_protocols import ProtocolIndex
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_server import handler_class
from gb_dicom2bids.qc_startup import StartupProgress
from gb_dicom2bids.qc_state import digest
from gb_dicom2bids.runtime import read_json


def check(output: Path, channel: str | None):
    output.mkdir(parents=True, exist_ok=True)
    errors, external = [], []
    with tempfile.TemporaryDirectory(prefix="synthetic-mixed-identification-") as folder:
        config = load_config(make_demo(Path(folder)))
        index = ProtocolIndex(config)
        identify = Identification(index)
        index.identification = identify
        identify.enable()
        uids = {index.records[u].series_description: u for u in index.subjects["phantom01"]}
        good, bad, optional = uids["eT1W-SE"], uids["T1-repeat"], uids["unknown-contrast"]
        source = config.nifti_import.source_root / index.records[bad].source_relpaths[0]
        # Only synthetic bytes: emulate a mislabeled, unsupported projection after inventory.
        nib.save(nib.Nifti1Image(np.zeros((8, 8, 1), dtype=np.float32), np.eye(4)), source)
        originals = {p: digest(p) for p in config.nifti_import.source_root.rglob("*.nii.gz")}
        service = ReviewService(config)
        service.warmup(StartupProgress())
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(service, "synthetic-token"))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch(headless=True, channel=channel)
                page = browser.new_page(viewport={"width": 1500, "height": 1300})
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("request", lambda r: external.append(r.url)
                        if not r.url.startswith((url, "blob:")) else None)
                page.goto(url, wait_until="networkidle")
                panel = page.locator("#protocol-content")
                page.locator(".pane").first.locator("select").first.select_option(good)
                page.locator(".pane").nth(1).locator("select").first.select_option(bad)
                expect(page.locator(".candidate-state").filter(
                    has_text="unsupported image dimensions"
                )).to_be_visible(timeout=15000)
                expect(panel.get_by_label("待定 T1-repeat", exact=True)).not_to_be_checked()
                bad_row = panel.locator(".protocol-row").filter(has_text="T1-repeat")
                bad_row.get_by_label("序列归属").select_option("other")
                good_row = panel.locator(".protocol-row").filter(has_text="eT1W-SE")
                good_row.get_by_label("协议优先级").fill("0")
                preview = panel.get_by_role("button", name="预览同类影响")
                publish = panel.get_by_role("button", name="确认识别并应用同类")
                preview.click()
                expect(publish).to_be_enabled()
                expect(panel.locator("pre")).to_contain_text(
                    '"negative_source_policy": "identity_only"'
                )
                assert not service.assistance().identification.state.get("negative_scopes")
                # An explicit defer still prevents exclusion, even if the dropdown says other.
                box = panel.get_by_label("待定 T1-repeat", exact=True)
                box.check()
                preview.click()
                expect(page.locator("#message")).to_contain_text("待定")
                expect(publish).to_be_disabled()
                box.uncheck()
                panel.get_by_label("待定 unknown-contrast", exact=True).check()
                preview.click()
                expect(publish).to_be_enabled()
                expect(panel.locator("pre")).to_contain_text('"quality_copied": false')
                expect(panel.locator("pre")).to_contain_text("T1-repeat")
                page.screenshot(path=str(output / "mixed-preview.png"), full_page=True)
                error_path = service.root / "errors" / f"{bad}.json"
                error_before = digest(error_path)
                publish.click()
                expect(page.locator("#workflow-status")).to_contain_text("T1 待识别 0 组 / 0 人")
                expect(page.locator("#list-status")).to_contain_text("无待识别组")
                page.reload(wait_until="networkidle")
                expect(page.locator("#list-status")).to_contain_text("无待识别组")
                expect(page.locator("#decision-bar")).to_be_hidden()
                page.screenshot(path=str(output / "mixed-complete.png"), full_page=True)
                assert digest(error_path) == error_before
                assert not errors, errors
                assert not external, external
                browser.close()
            fresh = ProtocolIndex(config)
            scope = next(iter(fresh.identification.state["negative_scopes"].values()))
            assert set(scope["templates"]) == {identify.families[bad]}
            assert scope["templates"][identify.families[bad]]["source_policy"] == "identity_only"
            assert scope["deferred"] == [optional]
            assert fresh.choices("phantom01")["t1"]["choice"] == good
            assert fresh.choices("phantom02")["t1"]["count"] == 1
            assert fresh.choices("phantom01")["flair"]["count"] == 1
            history = sorted((fresh.root / "identification_history").glob("*.json"))[-1]
            assert read_json(history)["request"]["negative_failed_previews"] == [bad]
            assert not list((service.root / "subjects").glob("*.json"))
            assert all(digest(p) == value for p, value in originals.items())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            service.close()
    report = {"passed": True, "page_errors": errors, "external_requests": external}
    (output / "browser_check.json").write_text(json.dumps(report), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--channel", default=None)
    args = parser.parse_args()
    check(args.output, args.channel)
