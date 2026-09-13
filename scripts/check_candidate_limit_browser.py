"""Offline browser acceptance of the inclusive four-image modality limit."""

from __future__ import annotations

import argparse
import secrets
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from make_assist_demo import make_demo
from playwright.sync_api import expect, sync_playwright

from gb_dicom2bids.config import load_config
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_server import handler_class
from gb_dicom2bids.qc_state import digest, read_decision
from gb_dicom2bids.runtime import atomic_write_json


def repeated(name: str, count: int) -> list[str]:
    return [f"202001011200__MR__{i:04d}__{name}" for i in range(count)]


def check(output: Path, channel: str | None) -> None:
    config = load_config(
        make_demo(
            output / "demo",
            negative=True,
            sequences={
                "phantom01": [*repeated("T1 tra", 3), "FLAIR"],
                "phantom02": [*repeated("T1 tra", 4), "FLAIR"],
                "phantom03": ["T1 tra", *repeated("FLAIR", 4)],
            },
        )
    )
    before = {p: digest(p) for p in config.nifti_import.source_root.rglob("*.nii.gz")}
    errors, external, prepares = [], [], []
    service = ReviewService(config)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(service, secrets.token_hex(16)))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True, channel=channel)
            try:
                page = browser.new_page(viewport={"width": 1500, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "request",
                    lambda r: (
                        external.append(r.url) if not r.url.startswith((url, "blob:")) else None
                    ),
                )
                page.on(
                    "request",
                    lambda r: prepares.append(r.url) if r.url.endswith("/api/prepare") else None,
                )
                page.goto(url, wait_until="networkidle")
                expect(page.locator("#total")).to_have_text("0")
                expect(page.locator("#next-stage")).to_be_enabled()
                expect(page.locator("#default-summary")).to_contain_text(
                    "同字段 ≥4 自动跳过 T1 1 / FLAIR 1"
                )
                expect(page.locator(".frame img[src]")).to_have_count(0)
                initial_prepares = len(prepares)
                page.locator("#assist-queue").select_option("candidate_limit")
                expect(page.locator("#total")).to_have_text("2")
                expect(page.locator("#protocol-content")).to_contain_text("4 个候选影像")
                expect(page.locator("#protocol-content")).to_contain_text("不会自动准备预览")
                expect(page.locator(".frame img[src]")).to_have_count(0)
                assert len(prepares) == initial_prepares
                page.screenshot(path=str(output / "count-exclusions.png"), full_page=True)
                page.reload(wait_until="networkidle")
                expect(page.locator("#default-summary")).to_contain_text(
                    "同字段 ≥4 自动跳过 T1 1 / FLAIR 1"
                )
                # Three axial images now finish identification without any confirmation.
                page.locator("#assist-queue").select_option("identified")
                page.locator("#subjects .subject").filter(has_text="phantom01").first.click()
                expect(page.locator("#subject-title")).to_have_text("sub-phantom01")
                selected = service.subject("phantom01")["sequence_choices"]["t1"]
                assert "__0002__" in service.records[selected].source_relpaths[0]
                expect(page.locator(".frame img[src]")).to_have_count(2)
                assert selected in page.locator(".pane-head select").evaluate_all(
                    "nodes => nodes.map(node => node.value)"
                )
                page.screenshot(path=str(output / "axial-last-selected.png"), full_page=True)
                expect(page.locator("#next-stage")).to_be_enabled()
                page.locator("#next-stage").click()
                expect(page.locator("#workflow-status")).to_contain_text("阶段 2")
                second = page.locator("#subjects .subject").filter(has_text="phantom02")
                second.click()
                expect(page.locator("#subject-title")).to_have_text("sub-phantom02")
                expect(page.locator(".pane-head select")).to_have_count(1)
                expect(page.locator(".pane-head select option")).to_have_count(1)
                expect(page.locator(".pane-head select")).to_contain_text("FLAIR")
                expect(page.locator(".frame img[src]")).to_have_count(1)
                page.screenshot(path=str(output / "flair-preserved.png"), full_page=True)
                assert not errors, errors
                assert not external, external
                assert all(digest(p) == value for p, value in before.items())
                assert all(
                    not read_decision(service.root, s)["candidates"] for s in service.by_subject
                )
                assert not list(config.paths.staging_bids_root.rglob("*.nii*"))
                atomic_write_json(
                    output / "browser_check.json",
                    {
                        "passed": True,
                        "page_errors": errors,
                        "external_requests": external,
                        "checks": [
                            "3 selects last axial without identification review",
                            "4 skips",
                            "modality isolation",
                            "audit queue",
                            "no automatic preview",
                            "reload",
                            "quality queue isolation",
                            "no quality decision",
                            "no source or BIDS writes",
                        ],
                    },
                )
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--channel", default=None)
    args = parser.parse_args()
    check(args.output.resolve(), args.channel)
