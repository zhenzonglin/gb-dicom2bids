"""Offline browser acceptance for the synthetic --negative assistance demo."""

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
        page = browser.new_page(viewport={"width": 1550, "height": 1300})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on(
            "request",
            lambda r: external.append(r.url) if not r.url.startswith((url, "blob:")) else None,
        )
        page.goto(url, wait_until="networkidle")
        panel = page.locator("#protocol-content")
        expect(panel).to_contain_text("同类 3 人")
        expect(panel.locator(".protocol-reason")).to_have_count(0)
        expect(panel.get_by_label("纠错备选序列").locator("option")).to_have_count(3)
        expect(page.locator("#decision-bar")).to_be_hidden()
        expect(page.locator(".frame img[src]")).to_have_count(2)
        page.screenshot(path=str(output / "first-round.png"), full_page=True)

        def exclude():
            panel.get_by_role("button", name="本轮序列均不是 T1").click()
            expect(panel.locator("pre")).to_contain_text('"quality_copied": false')
            panel.get_by_role("button", name="确认识别并应用同类").click()

        exclude()
        expect(page.locator("#subject-title")).to_have_text("sub-phantom03")
        expect(panel).to_contain_text("本轮新序列 1 种")
        expect(panel).to_contain_text("自动跳过 1 人")
        optional = panel.get_by_label("纠错备选序列")
        expect(optional.locator("option")).to_have_count(1)
        expect(optional).to_contain_text("unknown-contrast")
        expect(page.locator(".pane")).to_have_count(1)
        expect(page.locator(".frame img[src]")).to_have_count(1)
        page.screenshot(path=str(output / "only-new-sequence.png"), full_page=True)
        page.reload(wait_until="networkidle")
        expect(page.locator("#subject-title")).to_have_text("sub-phantom03")
        expect(optional.locator("option")).to_have_count(1)
        panel.get_by_label("查看全部序列（含已排除）").check()
        expect(optional.locator("option")).to_have_count(4)
        panel.get_by_label("查看全部序列（含已排除）").uncheck()
        expect(optional.locator("option")).to_have_count(1)

        # A deferred new template stays pending across publication and reload.
        panel.get_by_label("待定 unknown-contrast", exact=True).check()
        exclude()
        expect(panel).to_contain_text("剩余待识别 1 人")
        page.reload(wait_until="networkidle")
        expect(panel.get_by_label("待定 unknown-contrast", exact=True)).to_be_checked()
        expect(page.locator("#next-stage")).to_be_disabled()
        panel.get_by_label("待定 unknown-contrast", exact=True).uncheck()

        # Revoke a covered template and confirm it again, without touching quality.
        panel.get_by_text("已排除模板 / 撤回排除", exact=True).click()
        panel.get_by_label("撤回 t2-a", exact=True).check()
        panel.get_by_role("button", name="预览撤回排除").click()
        expect(panel.get_by_role("button", name="确认识别并应用同类")).to_be_enabled()
        panel.get_by_role("button", name="确认识别并应用同类").click()
        expect(page.locator("#subject-title")).to_have_text("sub-phantom01")
        expect(optional.locator("option")).to_have_count(1)
        expect(optional).to_contain_text("T2-A")
        exclude()
        expect(page.locator("#subject-title")).to_have_text("sub-phantom03")
        # Unchecking before a different action did not accidentally save that draft.
        panel.get_by_label("待定 unknown-contrast", exact=True).uncheck()
        exclude()
        expect(page.locator("#list-status")).to_contain_text("无待识别组")
        expect(page.locator("#next-stage")).to_be_enabled()
        page.locator("#next-stage").click()
        expect(page.locator("#workflow-status")).to_contain_text("阶段 2")
        expect(page.locator(".quality button[data-value=pass].chosen")).to_have_count(0)
        expect(page.locator("#revision")).to_have_text("记录版本 0")
        page.screenshot(path=str(output / "no-quality-copied.png"), full_page=True)
        assert not errors, errors
        assert not external, external
        (output / "browser_check.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "page_errors": errors,
                    "external_requests": external,
                    "checks": [
                        "no reason required",
                        "skip duplicates",
                        "only new sequences",
                        "restart persistence",
                        "show all",
                        "defer persists",
                        "revoke exclusion",
                        "automatic next representative",
                        "no copied quality",
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8894")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--channel", default=None)
    args = parser.parse_args()
    check(args.url, args.output, args.channel)
