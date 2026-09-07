"""Synthetic browser verification of sequence identification before image-quality review."""

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
        page = browser.new_page(viewport={"width": 1600, "height": 1250})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on(
            "request",
            lambda r: external.append(r.url) if not r.url.startswith((url, "blob:")) else None,
        )
        page.goto(url, wait_until="networkidle")
        expect(page.locator("#workflow-status")).to_contain_text("阶段 1")
        expect(page.locator("#list-status")).to_contain_text("1 个序列组")
        expect(page.locator("#decision-bar")).to_be_hidden()
        expect(page.locator(".quality").first).to_be_hidden()
        expect(page.locator("#next-stage")).to_be_disabled()
        panel = page.locator("#protocol-content")
        expect(panel).to_contain_text("同类 2 人")
        expect(panel.locator(".protocol-row").first).not_to_contain_text("unknown")
        # Optional correction source, not a required classification row.
        optional = panel.get_by_label("纠错备选序列")
        option = optional.locator("option").filter(has_text="unknown-contrast")
        optional.select_option(option.get_attribute("value"))
        panel.get_by_role("button", name="将备选加入 T1 识别").click()
        for i, rank in enumerate(panel.get_by_label("协议优先级").all(), start=1):
            rank.fill(str(i))
        expect(panel.locator(".protocol-reason")).to_have_count(0)
        panel.get_by_role("button", name="预览同类影响").click()
        expect(panel.locator("pre")).to_contain_text('"affected_subjects": 2')
        panel.get_by_role("button", name="确认识别并应用同类").click()
        expect(page.locator("#list-status")).to_contain_text("无待识别组")
        expect(page.locator("#next-stage")).to_be_enabled()
        page.screenshot(path=str(output / "sequence-complete.png"), full_page=True)
        page.locator("#next-stage").click()
        expect(page.locator("#workflow-status")).to_contain_text("阶段 2")
        expect(page.locator("#decision-bar")).to_be_visible()
        expect(page.locator(".quality").first).to_be_visible()
        expect(page.locator("#revision")).to_have_text("记录版本 0")
        expect(page.locator(".quality button[data-value=pass].chosen")).to_have_count(0)
        expect(page.locator(".frame img[src]")).to_have_count(2)
        pane = page.locator(".pane").first
        pane.get_by_role("button", name="通过", exact=True).click()
        pane.get_by_role("button", name="设为最终候选").click()
        page.locator("#save").click()
        expect(page.locator("#revision")).to_have_text("记录版本 1")
        page.reload(wait_until="networkidle")
        expect(page.locator(".quality button[data-value=pass].chosen")).to_have_count(1)
        page.wait_for_function(
            "Array.from(document.querySelectorAll('.frame img'))"
            ".every(i=>i.complete && i.naturalWidth>0)"
        )
        page.screenshot(path=str(output / "quality-stage.png"), full_page=True)
        page.on("dialog", lambda dialog: dialog.accept())
        page.locator("#next-stage").click()
        expect(page.locator("#workflow-status")).to_contain_text("阶段 1")
        expect(page.locator("#decision-bar")).to_be_hidden()
        assert not errors, errors
        assert not external, external
        (output / "browser_check.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "page_errors": errors,
                    "external_requests": external,
                    "checks": [
                        "separate sequence group",
                        "optional correction",
                        "batch propagation",
                        "quality hidden and locked",
                        "explicit stage transition",
                        "no copied quality",
                        "manual save persists",
                        "native images",
                        "reopen sequence stage",
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8892")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--channel", default=None)
    args = parser.parse_args()
    check(args.url, args.output, args.channel)
