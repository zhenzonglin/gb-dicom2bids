"""Live synthetic preview failure must never check or lock human defer controls."""

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
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_server import handler_class
from gb_dicom2bids.qc_startup import StartupProgress


def check(output: Path, channel: str | None):
    output.mkdir(parents=True, exist_ok=True)
    errors, external = [], []
    with tempfile.TemporaryDirectory(prefix="synthetic-manual-defer-") as folder:
        config = load_config(make_demo(Path(folder), negative=True))
        # Simulate an unsupported projection after inventory without any real source data.
        source = config.nifti_import.source_root / "synthetic_site/phantom01/T2-A/image.nii.gz"
        nib.save(nib.Nifti1Image(np.zeros((8, 8, 1), dtype=np.float32), np.eye(4)), source)
        service = ReviewService(config)
        service.warmup(StartupProgress())
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(service, "synthetic-token"))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        uid = next(
            u
            for u in service.by_subject["phantom01"]
            if service.records[u].series_description == "T2-A"
        )
        try:
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch(headless=True, channel=channel)
                page = browser.new_page(viewport={"width": 1500, "height": 1100})
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on(
                    "request",
                    lambda r: (
                        external.append(r.url) if not r.url.startswith((url, "blob:")) else None
                    ),
                )
                page.goto(url, wait_until="networkidle")
                panel = page.locator("#protocol-content")
                expect(page.locator("#subject-title")).to_have_text("sub-phantom01")
                page.locator(".pane select").first.select_option(uid)
                expect(
                    page.locator(".candidate-state").filter(has_text="unsupported image dimensions")
                ).to_be_visible(timeout=15000)
                box = panel.get_by_label("待定 T2-A", exact=True)
                expect(box).not_to_be_checked()
                expect(box).to_be_enabled()
                panel.get_by_role("button", name="本轮序列均不是 T1").click()
                expect(page.locator("#message")).to_contain_text("手动勾选待定")
                expect(box).not_to_be_checked()

                # Even a fresh preview failure after manual uncheck must not reverse it.
                box.check()
                box.uncheck()
                page.get_by_role("button", name="重试预览").first.click()
                expect(
                    page.locator(".candidate-state").filter(has_text="unsupported image dimensions")
                ).to_be_visible(timeout=15000)
                expect(box).not_to_be_checked()
                expect(box).to_be_enabled()
                page.on("dialog", lambda d: d.accept())
                page.reload(wait_until="networkidle")
                expect(box).not_to_be_checked()
                expect(box).to_be_enabled()

                # Publish a deliberate defer; only this saved decision restores the check.
                box.check()
                panel.get_by_role("button", name="本轮序列均不是 T1").click()
                publish = panel.get_by_role("button", name="确认识别并应用同类")
                expect(publish).to_be_enabled()
                publish.click()
                expect(page.locator("#subject-title")).to_have_text("sub-phantom02")
                # Resolve the other synthetic representatives through normal UI actions.
                # The failed/deferred original remains pending, never auto-excluded.
                for expected in ("sub-phantom03", "sub-phantom01"):
                    panel.get_by_role("button", name="本轮序列均不是 T1").click()
                    expect(publish).to_be_enabled()
                    publish.click()
                    expect(page.locator("#subject-title")).to_have_text(expected)
                page.reload(wait_until="networkidle")
                expect(page.locator("#subject-title")).to_have_text("sub-phantom01")
                expect(box).to_be_checked()
                expect(box).to_be_enabled()
                box.uncheck()
                expect(box).not_to_be_checked()
                page.locator(".pane select").first.select_option(uid)
                expect(
                    page.locator(".candidate-state").filter(has_text="unsupported image dimensions")
                ).to_be_visible(timeout=15000)
                expect(box).not_to_be_checked()
                expect(box).to_be_enabled()
                page.screenshot(path=str(output / "manual-only-defer.png"), full_page=True)
                assert not errors, errors
                assert not external, external
                browser.close()
            assert not list((service.root / "subjects").glob("*.json"))
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
