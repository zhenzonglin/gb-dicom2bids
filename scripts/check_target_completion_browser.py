"""Synthetic browser check: revoke mistaken exclusion, identify T1, stop non-target review."""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

from make_assist_demo import make_demo
from playwright.sync_api import expect, sync_playwright

from gb_dicom2bids.config import load_config
from gb_dicom2bids.manifest import load_private_records, write_inventory
from gb_dicom2bids.qc_identify import Identification
from gb_dicom2bids.qc_protocols import ProtocolIndex
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_server import handler_class
from gb_dicom2bids.qc_startup import StartupProgress
from gb_dicom2bids.qc_state import digest


def check(output: Path, channel: str | None):
    output.mkdir(parents=True, exist_ok=True)
    errors, external = [], []
    with tempfile.TemporaryDirectory(prefix="synthetic-target-completion-") as folder:
        config = load_config(make_demo(Path(folder)))
        # This synthetic target deliberately requires an explicit protocol publication.
        records = load_private_records(config.paths.audit_root)
        records = [
            replace(r, classification_confidence="low") if r.candidate_type == "t1" else r
            for r in records
        ]
        write_inventory(config.paths.audit_root, records, [])
        index = ProtocolIndex(config)
        identify = Identification(index)
        index.identification = identify
        identify.enable()
        group = next(g for g in identify.catalogue()["groups"] if g["modality"] == "t1")
        uids = {index.records[u].series_description: u for u in index.subjects["phantom01"]}
        uid, deferred = uids["eT1W-SE"], uids["unknown-contrast"]
        payload = {
            "subject": "phantom01", "group": group["id"], "revision": identify.state["revision"],
            "reviewer": "zhenzong", "templates": {}, "negative_source_policy": "identity_only",
            "negative_templates": [identify.families[uids[n]] for n in ("eT1W-SE", "T1-repeat")],
            "deferred_candidates": [deferred],
        }
        preview = identify.preview(payload)
        identify.publish(dict(payload, preview_digest=preview["preview_digest"]))
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
                page = browser.new_page(viewport={"width": 1500, "height": 1250})
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("request", lambda r: external.append(r.url)
                        if not r.url.startswith((url, "blob:")) else None)
                page.goto(url, wait_until="networkidle")
                panel = page.locator("#protocol-content")
                expect(panel.locator(".list-error")).to_contain_text("eT1W-SE")
                expect(panel.locator(".list-error")).to_contain_text("不是有效候选")
                page.locator("#identify-show-all").check()
                pane = page.locator(".pane").first
                pane.locator("select").first.select_option(uid)
                expect(pane.locator(".badge.excluded")).to_have_text("已排除于 T1")
                expect(pane.locator("select").first.locator("option:checked")).to_contain_text(
                    "已排除于 T1"
                )
                expect(page.locator(".frame img[src]")).to_have_count(2, timeout=15000)
                page.screenshot(path=str(output / "excluded-label.png"), full_page=True)
                panel.get_by_text("已排除模板 / 撤回排除", exact=True).click()
                panel.get_by_label("撤回 et1w-se", exact=True).check()
                panel.get_by_role("button", name="预览撤回排除").click()
                publish = panel.get_by_role("button", name="确认识别并应用同类")
                expect(publish).to_be_enabled()
                publish.click()
                expect(panel.get_by_label("序列归属")).to_have_count(1)
                expect(panel.get_by_label("待定 unknown-contrast", exact=True)).to_be_checked()
                expect(panel.locator(".protocol-row").first).to_contain_text("eT1W-SE")
                panel.get_by_role("button", name="预览同类影响").click()
                expect(publish).to_be_enabled()
                publish.click()
                expect(page.locator("#workflow-status")).to_contain_text("T1 待识别 0 组 / 0 人")
                expect(page.locator("#list-status")).to_contain_text("无待识别组")
                expect(page.locator("#next-stage")).to_be_enabled()
                page.reload(wait_until="networkidle")
                expect(page.locator("#list-status")).to_contain_text("无待识别组")
                expect(page.locator("#decision-bar")).to_be_hidden()
                page.screenshot(path=str(output / "target-complete.png"), full_page=True)
                assert not errors, errors
                assert not external, external
                browser.close()
            state = ProtocolIndex(config).identification.state
            scope = next(iter(state["negative_scopes"].values()))
            assert scope["deferred"] == [deferred]
            assert identify.families[uid] not in scope["templates"]
            assert identify.families[uids["T1-repeat"]] in scope["templates"]
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
