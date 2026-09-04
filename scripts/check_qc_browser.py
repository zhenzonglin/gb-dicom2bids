"""Optional development check: Playwright is not required on the workstation."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def check(url: str, output: Path, channel: str | None):
    output.mkdir(parents=True, exist_ok=True)
    errors, external = [], []
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel=channel)
        context = browser.new_context(viewport={"width": 1600, "height": 1440})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda req: (
                external.append(req.url) if not req.url.startswith((url, "blob:")) else None
            ),
        )
        page.goto(url, wait_until="networkidle")
        expect(page.locator(".pane")).to_have_count(2)
        expect(page.locator(".frame img[src]")).to_have_count(6)
        expect(page.locator("#subject-title")).to_have_text("sub-phantom01")
        expect(
            page.locator(".pane-head select").first.locator("option:checked")
        ).to_contain_text("T1")
        expect(
            page.locator(".pane-head select").nth(1).locator("option:checked")
        ).to_contain_text("FLAIR")
        second = context.new_page()
        second.goto(url, wait_until="networkidle")
        # Slice coordinates and CSS transforms must actually change on user interaction.
        slider = page.locator(".view input[type=range]").first
        prior = slider.input_value()
        slider.fill("10")
        slider.dispatch_event("input")
        assert slider.input_value() != prior
        frame = page.locator(".frame").first
        frame.hover()
        page.keyboard.down("Control")
        page.mouse.wheel(0, -100)
        page.keyboard.up("Control")
        expect(frame.locator("img")).to_have_attribute("style", re.compile("scale"))
        for pane in page.locator(".pane").all():
            pane.get_by_role("button", name="通过", exact=True).click()
        page.locator(".pane").first.get_by_role("button", name="设为最终候选").click()
        page.locator("#save").click()
        expect(page.locator("#revision")).to_have_text("记录版本 1")
        expect(page.locator("#message")).to_contain_text("staging尚未改变")
        second.locator("#save").click()
        expect(second.locator("#message")).to_contain_text("another window")
        page.reload(wait_until="networkidle")
        expect(page.locator("#revision")).to_have_text("记录版本 1")
        expect(page.locator(".quality button[data-value=pass].chosen")).to_have_count(2)
        # T1 and FLAIR are the default comparison pair; all other candidates remain selectable.
        expect(page.locator("#others")).to_be_checked()
        select = page.locator(".pane-head select").nth(1)
        options = select.locator("option").all_text_contents()
        flair = next(text for text in options if "FLAIR" in text)
        select.select_option(label=flair)
        expect(page.locator(".pane").nth(1).locator(".badge.excluded")).to_be_visible()
        expect(page.locator(".frame img[src]")).to_have_count(6)
        expect(page.locator(".pane-head select").first.locator("option")).to_have_count(4)
        expect(page.locator(".frame img[src]")).to_have_count(6)
        page.screenshot(path=str(output / "viewer.png"), full_page=True)
        assert not errors, errors
        assert not external, external
        (output / "browser_check.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "page_errors": errors,
                    "external_requests": external,
                    "checks": [
                        "six views",
                        "slice",
                        "zoom",
                        "two passes one choice",
                        "save",
                        "restart persistence",
                        "version conflict",
                        "excluded",
                        "other",
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--channel", default=None)
    args = parser.parse_args()
    check(args.url, args.output, args.channel)
