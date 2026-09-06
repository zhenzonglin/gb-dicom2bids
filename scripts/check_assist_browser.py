"""Exercise protocol rules, native slices and manual decisions in a real local browser."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def check(url: str, output: Path, channel: str | None):
    output.mkdir(parents=True, exist_ok=True)
    errors, external = [], []
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel=channel)
        page = browser.new_page(viewport={"width": 1600, "height": 1400})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on(
            "request",
            lambda r: external.append(r.url) if not r.url.startswith((url, "blob:")) else None,
        )
        page.goto(url, wait_until="networkidle")
        expect(page.locator(".frame img[src]")).to_have_count(2)
        expect(page.locator(".frame img").first).to_have_attribute("alt", "原始体素切片")
        page.locator("#protocol-panel summary").click()
        panel = page.locator("#protocol-content")
        expect(panel).to_contain_text("同组合 2 名")
        expect(panel.locator(".protocol-row")).to_have_count(4)
        for row in panel.locator(".protocol-row").all():
            rank = "1" if "eT1W-SE" in row.inner_text() else "2"
            row.get_by_label("协议优先级").fill(rank)
        panel.locator(".protocol-reason").fill("synthetic template preference")
        panel.get_by_role("button", name="预览批量影响").click()
        expect(panel.locator("pre")).to_contain_text('"affected_subjects": 2')
        panel.get_by_role("button", name="发布这组规则").click()
        expect(panel).to_contain_text("已发布规则")
        # No representative quality is copied by publishing a protocol rule.
        expect(page.locator("#revision")).to_have_text("记录版本 0")
        expect(page.locator(".quality button[data-value=pass].chosen")).to_have_count(0)
        for pane in page.locator(".pane").all():
            pane.get_by_role("button", name="通过", exact=True).click()
            pane.get_by_role("button", name="设为最终候选").click()
        page.locator("#save").click()
        expect(page.locator("#revision")).to_have_text("记录版本 1")
        page.reload(wait_until="networkidle")
        expect(page.locator(".quality button[data-value=pass].chosen")).to_have_count(2)
        page.locator("#protocol-panel summary").click()
        panel.get_by_role("button", name="撤回这组规则").click()
        expect(panel).to_contain_text("尚未发布规则")
        expect(page.locator("#revision")).to_have_text("记录版本 1")
        expect(page.locator(".quality button[data-value=pass].chosen")).to_have_count(2)
        # Structure a failure on the second patient and persist it without choosing an output.
        page.locator(".subject").filter(has_text="phantom02").click()
        pane = page.locator(".pane").first
        pane.get_by_role("button", name="不通过", exact=True).click()
        pane.get_by_label("质量原因").select_option("motion_blur")
        page.locator("#save").click()
        expect(page.locator("#revision")).to_have_text("记录版本 1")
        page.reload(wait_until="networkidle")
        page.locator(".subject").filter(has_text="phantom02").click()
        expect(page.locator(".pane").first.get_by_label("质量原因")).to_have_value("motion_blur")
        expect(page.locator(".frame img[src]")).to_have_count(2)
        page.wait_for_function(
            "Array.from(document.querySelectorAll('.frame img'))"
            ".every(i => i.complete && i.naturalWidth > 0)"
        )
        page.locator("#protocol-panel summary").click()
        page.screenshot(path=str(output / "assist.png"), full_page=True)
        assert not errors, errors
        assert not external, external
        (output / "browser_check.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "page_errors": errors,
                    "external_requests": external,
                    "checks": [
                        "native slices",
                        "template preview",
                        "rule publish",
                        "no copied quality",
                        "manual save",
                        "persistence",
                        "rule revoke",
                        "structured failure",
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8877")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--channel", default=None)
    args = parser.parse_args()
    check(args.url, args.output, args.channel)
