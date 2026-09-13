"""Offline acceptance for whole-round technical skipping using newly generated phantoms."""

from __future__ import annotations

import argparse
import secrets
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import nibabel as nib
import numpy as np
from make_assist_demo import make_demo
from playwright.sync_api import expect, sync_playwright

from gb_dicom2bids.config import load_config
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
                "phantom01": ["contrast-A", "contrast-B"],
                "phantom02": ["contrast-A", "contrast-B"],
            },
        )
    )
    broken = sorted(
        (config.nifti_import.source_root / "synthetic_site/phantom01").rglob("*.nii.gz")
    )
    for path in broken:
        nib.save(nib.Nifti1Image(np.ones((8, 8, 1)), np.eye(4)), path)
    before = {p: digest(p) for p in config.nifti_import.source_root.rglob("*.nii.gz")}
    errors, external = [], []
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
                page = browser.new_page(viewport={"width": 1550, "height": 1300})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "request",
                    lambda r: (
                        external.append(r.url) if not r.url.startswith((url, "blob:")) else None
                    ),
                )
                page.goto(url, wait_until="networkidle")
                expect(page.locator("#subject-title")).to_have_text("sub-phantom02", timeout=20000)
                expect(page.locator("#default-summary")).to_contain_text(
                    "全部待选不可读 T1 1 / FLAIR 1"
                )
                expect(page.locator(".frame img[src]")).to_have_count(2)
                assert all(digest(p) == value for p, value in before.items())
                page.screenshot(path=str(output / "automatically-skipped.png"), full_page=True)
                page.reload(wait_until="networkidle")
                expect(page.locator("#subject-title")).to_have_text("sub-phantom02")
                page.locator("#assist-queue").select_option("unreadable")
                expect(page.locator("#subject-title")).to_have_text("sub-phantom01")
                expect(page.get_by_role("button", name="重试预览")).to_have_count(2)
                expect(page.locator("#protocol-content")).to_contain_text("已技术跳过")
                page.screenshot(path=str(output / "recoverable-errors.png"), full_page=True)
                # Repair only a generated phantom, then exercise the visible retry button.
                for path in broken:
                    nib.save(nib.Nifti1Image(np.ones((16, 16, 16)), np.eye(4)), path)
                page.get_by_role("button", name="重试预览").first.click()
                expect(page.locator(".frame img[src]")).to_have_count(1, timeout=20000)
                expect(page.locator("#default-summary")).to_contain_text(
                    "全部待选不可读 T1 0 / FLAIR 0"
                )
                page.locator("#assist-queue").select_option("protocol")
                expect(page.locator("#subject-title")).to_have_text("sub-phantom01")
                expect(page.get_by_role("button", name="重试预览")).to_have_count(1)
                page.get_by_role("button", name="重试预览").click()
                expect(page.locator(".frame img[src]")).to_have_count(2)
                page.screenshot(path=str(output / "retry-restored.png"), full_page=True)
                assert not errors, errors
                assert not external, external
                identify = service.assistance().identification
                assert not identify.state["templates"]
                assert not identify.state["absent"]
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
                            "automatic skip",
                            "next patient",
                            "refresh persistence",
                            "error recovery queue",
                            "retry restores",
                            "no copied quality",
                            "no BIDS writes",
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
