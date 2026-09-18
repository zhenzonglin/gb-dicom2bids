"""Use synthetic sequences to verify one-click navigation during a deliberately blocked save."""

from __future__ import annotations

import argparse
import re
import secrets
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from make_assist_demo import make_demo
from playwright.sync_api import expect, sync_playwright

from gb_dicom2bids.config import load_config
from gb_dicom2bids.qc_identify import Identification
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_server import handler_class
from gb_dicom2bids.qc_state import digest, read_decision
from gb_dicom2bids.runtime import atomic_write_json


def check(output: Path, channel: str | None) -> None:
    config = load_config(
        make_demo(
            output / "demo",
            negative=True,
            sequences={
                "phantom01": ["T1 A", "T1 B", "FLAIR"],
                "phantom02": ["T1 C", "T1 D", "FLAIR"],
                "phantom03": ["T1 E", "T1 F", "FLAIR"],
            },
        )
    )
    before = {p: digest(p) for p in config.nifti_import.source_root.rglob("*.nii.gz")}
    service = ReviewService(config)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(service, secrets.token_hex(16)))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    entered, release = threading.Event(), threading.Event()
    original = Identification.publish

    def slow(self, payload):
        if payload.get("background_job_id") and not entered.is_set():
            entered.set()
            if not release.wait(40):
                raise TimeoutError("synthetic save delay expired")
        return original(self, payload)

    Identification.publish = slow
    url = f"http://127.0.0.1:{server.server_port}"
    errors, external, requests = [], [], []
    try:
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True, channel=channel)
            try:
                page = browser.new_page(viewport={"width": 1550, "height": 1100})
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("request", lambda r: requests.append(r.url))
                page.on(
                    "request",
                    lambda r: (
                        external.append(r.url) if not r.url.startswith((url, "blob:")) else None
                    ),
                )
                page.goto(url, wait_until="networkidle")
                expect(page.locator("#identify-publish")).to_be_enabled()
                first = page.locator("#subject-title").inner_text()
                # Distinct protocols need an explicit priority, but no preview click.
                page.get_by_role("spinbutton", name="协议优先级").nth(0).fill("0")
                page.get_by_role("spinbutton", name="协议优先级").nth(1).fill("1")
                page.locator("#identify-publish").click()
                expect(page.locator("#subject-title")).not_to_have_text(first)
                assert entered.wait(5)
                assert not release.is_set()
                expect(page.locator(".frame img[src]")).to_have_count(2)
                second = page.locator("#subject-title").inner_text()
                page.get_by_role("spinbutton", name="协议优先级").nth(0).fill("0")
                page.get_by_role("spinbutton", name="协议优先级").nth(1).fill("1")
                page.locator("#identify-publish").click()
                expect(page.locator("#subject-title")).not_to_have_text(second)
                # Navigation briefly clears the title before the next subject arrives.
                expect(page.locator("#subject-title")).to_have_text(re.compile(r"^sub-phantom"))
                third = page.locator("#subject-title").inner_text()
                expect(page.locator("#identification-job-status")).to_contain_text("排队/写入 2")
                page.screenshot(path=str(output / "next-before-write-finishes.png"), full_page=True)
                # The queue survives a browser refresh while the worker is still blocked.
                page.reload(wait_until="networkidle")
                expect(page.locator("#subject-title")).to_have_text(third)
                expect(page.locator("#identification-job-status")).to_contain_text("排队/写入 2")
                release.set()
                expect(page.locator("#identification-job-status")).to_contain_text(
                    "最近已保存 2", timeout=20000
                )
                # An unresolved equal-priority decision fails in the background, never disappears.
                page.locator("#identify-publish").click()
                expect(page.locator("#identification-job-status")).to_contain_text(
                    "最近失败 1", timeout=20000
                )
                page.locator("#identification-jobs details").evaluate("e => e.open = true")
                expect(page.locator("#identification-job-details")).to_contain_text("同优先级")
                page.get_by_role("button", name="返回该组复核").click()
                expect(page.locator("#subject-title")).to_have_text(third)
                page.screenshot(
                    path=str(output / "failed-decision-recoverable.png"), full_page=True
                )
                page.get_by_role("spinbutton", name="协议优先级").nth(0).fill("0")
                page.get_by_role("spinbutton", name="协议优先级").nth(1).fill("1")
                page.locator("#identify-publish").click()
                expect(page.locator("#identification-job-status")).to_contain_text(
                    "最近已保存 3", timeout=20000
                )
                assert not any(r.endswith("/api/identify/preview") for r in requests)
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
                            "one click without preview",
                            "next group while writer blocked",
                            "two queued decisions",
                            "refresh persistence",
                            "disjoint revision rebase",
                            "failure visible and recoverable",
                            "no quality propagation",
                            "no image writes",
                        ],
                    },
                )
            finally:
                browser.close()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        service.close()
        Identification.publish = original


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--channel", default=None)
    args = parser.parse_args()
    check(args.output.resolve(), args.channel)
