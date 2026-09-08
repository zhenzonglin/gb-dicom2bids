"""Synthetic headless check of name defaults, rule recall and cross-tab conflicts."""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from make_assist_demo import make_demo
from playwright.sync_api import expect, sync_playwright

from gb_dicom2bids.config import load_config
from gb_dicom2bids.qc_identify import Identification
from gb_dicom2bids.qc_protocols import ProtocolIndex
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_server import handler_class
from gb_dicom2bids.qc_startup import StartupProgress
from gb_dicom2bids.qc_state import digest


def check(output: Path, channel: str | None):
    output.mkdir(parents=True, exist_ok=True)
    errors, external = [], []
    with tempfile.TemporaryDirectory(prefix="synthetic-rule-review-") as folder:
        config = load_config(
            make_demo(
                Path(folder),
                sequences={
                    "phantom01": ["T1_AX_FLAIR", "T1-SE", "T2_FLAIR", "CT", "unknown-contrast"],
                    "phantom02": ["T1_AX_FLAIR", "T1-SE", "T2_FLAIR", "CT", "unknown-contrast"],
                    "phantom03": ["CT", "TOF", "MRA1", "DWI", "b0", "b1000"],
                },
            )
        )
        index = ProtocolIndex(config)
        identify = Identification(index)
        index.identification = identify
        identify.enable()
        originals = {p: digest(p) for p in config.nifti_import.source_root.rglob("*.nii.gz")}
        uids = {index.records[u].series_description: u for u in index.subjects["phantom01"]}
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
                page = browser.new_page(viewport={"width": 1500, "height": 1150})
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on(
                    "request",
                    lambda r: (
                        external.append(r.url) if not r.url.startswith((url, "blob:")) else None
                    ),
                )
                page.goto(url, wait_until="networkidle")
                panel = page.locator("#protocol-content")
                expect(page.locator("#workflow-status")).to_contain_text("T1 待识别 1 组 / 2 人")
                expect(panel.get_by_label("待定 CT", exact=True)).to_have_count(0)
                panel.locator("#identify-show-all").check()
                page.locator(".pane").nth(1).locator("select").first.select_option(uids["CT"])
                expect(
                    page.locator(".pane").nth(1).locator(".badge").filter(has_text="默认非目标")
                ).to_be_visible()

                def confirm_t1():
                    panel.locator(".protocol-row").filter(has_text="T1-SE").get_by_label(
                        "序列归属"
                    ).select_option("other")
                    panel.get_by_role("button", name="预览同类影响").click()
                    expect(panel.locator("#identify-publish")).to_be_enabled()
                    panel.locator("#identify-publish").click()
                    expect(page.locator("#workflow-status")).to_contain_text(
                        "T1 待识别 0 组 / 0 人"
                    )

                confirm_t1()
                page.locator("#open-rule-review").click()
                modal = page.locator("#rule-review")
                expect(modal.locator(".rule-row")).to_have_count(2)
                page.screenshot(path=str(output / "rules-active.png"), full_page=True)
                modal.get_by_label("撤销 t1-ax-flair", exact=True).check()
                modal.locator("#rule-recheck").check()
                modal.locator("#rule-preview").click()
                expect(modal.locator("#rule-report")).to_contain_text("重新进入识别 2")
                # Preview alone must not change the authoritative state.
                assert (
                    identify.families[uids["T1_AX_FLAIR"]]
                    in service.assistance().identification.state["templates"]
                )
                modal.locator("#rule-publish").click()
                expect(modal.locator("#rule-status")).to_contain_text("已撤销")
                expect(page.locator("#workflow-status")).to_contain_text("T1 待识别 1 组 / 2 人")

                second = browser.new_page()
                second.on("pageerror", lambda e: errors.append(str(e)))
                second.goto(url, wait_until="networkidle")
                second.locator("#open-rule-review").click()
                expect(second.locator("#rule-review .rule-row")).to_have_count(3)
                second.get_by_label("撤销 t1-se", exact=True).check()
                # Clear the two patient-specific recheck holds on the first tab.
                modal.locator("#rule-kind").select_option("recheck")
                expect(modal.locator(".rule-row")).to_have_count(2)
                for box in modal.locator(".rule-row input[type=checkbox]").all():
                    box.check()
                modal.locator("#rule-recheck").uncheck()
                modal.locator("#rule-preview").click()
                modal.locator("#rule-publish").click()
                expect(modal.locator("#rule-status")).to_contain_text("已撤销")
                expect(page.locator("#workflow-status")).to_contain_text("T1 待识别 0 组 / 0 人")
                second.locator("#rule-preview").click()
                expect(second.locator("#rule-status")).to_contain_text("规则已变化")
                expect(second.locator("#rule-publish")).to_be_disabled()
                second.close()

                modal.locator("#rule-kind").select_option("exclude")
                expect(modal.locator(".rule-row")).to_have_count(1)
                modal.get_by_label("撤销 t1-se", exact=True).check()
                modal.locator("#rule-preview").click()
                modal.locator("#rule-publish").click()
                expect(modal.locator("#rule-status")).to_contain_text("已撤销")
                expect(page.locator("#workflow-status")).to_contain_text("T1 待识别 1 组 / 2 人")
                modal.locator("#close-rule-review").click()
                page.reload(wait_until="networkidle")
                expect(page.locator("#workflow-status")).to_contain_text("T1 待识别 1 组 / 2 人")
                confirm_t1()
                page.locator("#next-stage").click()
                expect(page.locator("#workflow-status")).to_contain_text("阶段 2")
                expect(
                    page.locator("#subjects").get_by_role("button", name="phantom03")
                ).to_have_count(0)
                page.locator("#open-rule-review").click()
                modal.locator("#rule-kind").select_option("exclude")
                expect(modal.locator(".rule-row")).to_have_count(1)
                modal.get_by_label("撤销 t1-se", exact=True).check()
                modal.locator("#rule-preview").click()
                expect(modal.locator("#rule-publish")).to_be_disabled()
                expect(modal.locator("#rule-report")).to_contain_text("暂停质量授权")
                modal.locator("#rule-return").check()
                modal.locator("#rule-preview").click()
                expect(modal.locator("#rule-publish")).to_be_enabled()
                page.screenshot(path=str(output / "rules-quality-revoke.png"), full_page=True)
                modal.locator("#rule-publish").click()
                expect(modal.locator("#rule-status")).to_contain_text("已撤销")
                expect(page.locator("#workflow-status")).to_contain_text("阶段 1")
                modal.locator("#rule-view").select_option("history")
                expect(modal.locator(".rule-row").filter(has_text="历史记录").first).to_be_visible()
                page.screenshot(path=str(output / "rules-history.png"), full_page=True)
                assert not list((service.root / "subjects").glob("*.json"))
                assert not list(config.paths.staging_bids_root.rglob("*.nii.gz"))
                assert all(digest(p) == d for p, d in originals.items())
                assert not errors, errors
                assert not external, external
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            service.close()
    result = {"passed": True, "page_errors": errors, "external_requests": external}
    (output / "browser_check.json").write_text(json.dumps(result), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--channel", default=None)
    args = parser.parse_args()
    check(args.output, args.channel)
