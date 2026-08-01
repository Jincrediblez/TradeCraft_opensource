#!/usr/bin/env python3
"""Clean-install browser smoke for locale persistence and demo mode."""

import os
import re
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright


BASE_URL = os.environ.get("TRADECRAFT_BASE_URL", "http://127.0.0.1:8888").rstrip("/")


def wait_for_server(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/api/demo/status", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"TradeCraft did not start within {timeout}s: {last_error}")


def main() -> None:
    wait_for_server()
    with sync_playwright() as playwright:
        system_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        executable = os.environ.get("TRADECRAFT_BROWSER_EXECUTABLE")
        if not executable and system_chrome.is_file():
            executable = str(system_chrome)
        browser = playwright.chromium.launch(executable_path=executable)
        page = browser.new_page(locale="fr-FR")
        external_requests = []
        page.on(
            "request",
            lambda request: external_requests.append(request.url)
            if not request.url.startswith(BASE_URL)
            else None,
        )
        reset = page.request.post(f"{BASE_URL}/api/settings", data={"locale": "en"})
        assert reset.ok
        demo_reset = page.request.post(f"{BASE_URL}/api/demo/reset")
        assert demo_reset.ok
        page.goto(BASE_URL, wait_until="networkidle")

        page.locator("#demoBanner").wait_for(state="visible")
        assert page.locator("#demoBanner .demo-badge").inner_text() == "DEMO DATA"
        assert page.locator("#tabHome").inner_text() == "Home"
        assert page.evaluate("document.documentElement.lang") == "en"

        with page.expect_response(lambda response: "/api/settings" in response.url and response.request.method == "GET"):
            page.locator("#tabSettings").click()
        page.locator("#settingsLocale").wait_for(state="visible")
        page.select_option("#settingsLocale", "zh-CN")
        with page.expect_response(lambda response: response.url.endswith("/api/settings") and response.request.method == "POST"):
            page.locator("#settingsSave").click()
        page.wait_for_function("document.documentElement.lang === 'zh-CN'")
        page.locator("#settingsSave").wait_for(state="visible")
        page.wait_for_function("!document.querySelector('#settingsSave').disabled")
        assert page.locator("#tabHome").inner_text() == "\u9996\u9875"

        page.reload(wait_until="networkidle")
        assert page.locator("#tabHome").inner_text() == "\u9996\u9875"
        with page.expect_response(lambda response: "/api/settings" in response.url and response.request.method == "GET"):
            page.locator("#tabSettings").click()
        assert page.locator("#settingsLocale").input_value() == "zh-CN"

        page.select_option("#settingsLocale", "en")
        with page.expect_response(lambda response: response.url.endswith("/api/settings") and response.request.method == "POST"):
            page.locator("#settingsSave").click()
        page.wait_for_function("document.documentElement.lang === 'en'")
        page.wait_for_function("!document.querySelector('#settingsSave').disabled")
        assert page.locator("#tabHome").inner_text() == "Home"

        for tab in ("Home", "Replay", "Data", "Trades", "Market", "Watchlist", "Performance", "Audit", "Settings"):
            page.locator(f"#tab{tab}").click()
            page.wait_for_timeout(250)
            visible = page.locator("body").inner_text()
            assert not re.search(r"[\u3400-\u9fff]", visible), f"Untranslated text on {tab}: {visible}"

        fallback = page.request.get(f"{BASE_URL}/api/demo/status?lang=fr-FR")
        assert fallback.headers["content-language"] == "en"
        assert fallback.json()["messageKey"] == "api.demoActive"
        chinese = page.request.get(
            f"{BASE_URL}/api/demo/status?lang=zh-CN",
            headers={"Accept-Language": "zh-CN"},
        )
        assert chinese.headers["content-language"] == "en"
        assert chinese.json()["message"] == "Demo mode is active."
        unexpected = [
            url for url in external_requests
            if not url.startswith("https://s3.tradingview.com/")
            and not url.startswith("https://www.tradingview-widget.com/")
        ]
        assert not unexpected, f"Unexpected external browser requests: {unexpected}"
        browser.close()


if __name__ == "__main__":
    main()
