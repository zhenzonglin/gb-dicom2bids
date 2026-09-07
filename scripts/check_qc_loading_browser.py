"""Headless, synthetic checks of slow startup requests, retry and cancellation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def check(output: Path, channel: str | None):
    assets = Path(__file__).resolve().parents[1] / "src/gb_dicom2bids/qc_web"
    output.mkdir(parents=True, exist_ok=True)
    errors, external, pending = [], [], []
    seen = {"identify": 0, "subjects": 0, "assist": 0}
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel=channel)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        origin = "http://127.0.0.1:9876"

        def route(request):
            url = request.request.url
            if not url.startswith(origin + "/"):
                external.append(url)
                request.abort()
                return
            path = url[len(origin) :].split("?", 1)[0]
            if path == "/api/identify":
                seen["identify"] += 1
                pending.append(request)
            elif path == "/api/subjects":
                seen["subjects"] += 1
                request.fulfill(json={"subjects": [], "total": 0})
            elif path == "/api/assist/status":
                seen["assist"] += 1
                request.fulfill(json={"features": {}, "queues": {}})
            elif path in {"/", "/app.js", "/identify.js", "/assist.js", "/style.css"}:
                file = assets / ("index.html" if path == "/" else path[1:])
                text = file.read_text(encoding="utf-8").replace("__QC_TOKEN__", "synthetic-token")
                mime = (
                    "text/html"
                    if path == "/"
                    else "text/css"
                    if path.endswith(".css")
                    else "text/javascript"
                )
                request.fulfill(body=text, content_type=mime)
            else:
                request.fulfill(status=404, body="not found")

        page.route("**/*", route)
        # Deliberately hold the initialization request. Virtual time tests >60s
        # without delaying a real server or opening a user's browser.
        page.clock.install()
        page.goto(origin, wait_until="domcontentloaded")
        expect(page.locator("#list-status")).to_contain_text("正在加载序列识别状态")
        page.clock.fast_forward(125_000)
        expect(page.locator("#list-status")).to_contain_text("125 秒")
        expect(page.locator("#retry-list")).to_be_hidden()
        assert seen == {"identify": 1, "subjects": 0, "assist": 0}, seen
        page.screenshot(path=str(output / "slow-identification.png"), full_page=True)
        pending.pop().fulfill(json={"enabled": False})
        expect(page.locator("#list-status")).to_contain_text("当前筛选无匹配患者")

        # Surface an actual server error, then retry the same load without re-scanning.
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#list-status")).to_contain_text("正在加载序列识别状态")
        pending.pop().fulfill(status=400, json={"error": "synthetic inventory error"})
        expect(page.locator("#list-status")).to_contain_text("synthetic inventory error")
        expect(page.locator("#retry-list")).to_be_visible()
        page.locator("#retry-list").click()
        expect(page.locator("#list-status")).to_contain_text("正在加载序列识别状态")
        page.clock.fast_forward(65_000)
        pending.pop().fulfill(json={"enabled": False})
        expect(page.locator("#list-status")).to_contain_text("当前筛选无匹配患者")

        # A new filter cancels the old request; its late answer must not replace state.
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#list-status")).to_contain_text("正在加载序列识别状态")
        stale = pending.pop()
        previous = seen["identify"]
        page.locator("#center").fill("synthetic")
        page.clock.fast_forward(500)
        page.wait_for_function("listGeneration >= 2")
        assert seen["identify"] == previous + 1
        stale.fulfill(json={"enabled": False})
        assert page.evaluate("workflow") is None
        pending.pop().fulfill(json={"enabled": False})
        expect(page.locator("#list-status")).to_contain_text("当前筛选无匹配患者")
        assert not errors, errors
        assert not external, external
        browser.close()
    report = {"passed": True, "errors": errors, "external_requests": external, "requests": seen}
    (output / "browser_check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--channel", default=None)
    args = parser.parse_args()
    check(args.output, args.channel)
