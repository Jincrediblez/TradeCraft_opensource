#!/usr/bin/env python3
"""Clean-install browser smoke for locale persistence and demo mode."""

import os
import re
import time
import urllib.request

from playwright.sync_api import sync_playwright


BASE_URL = os.environ.get("TRADECRAFT_BASE_URL", "http://127.0.0.1:8787").rstrip("/")


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
        browser = playwright.chromium.launch()
        page = browser.new_page(locale="fr-FR")
        external_requests = []
        page.on(
            "request",
            lambda request: external_requests.append(request.url)
            if not request.url.startswith(BASE_URL)
            else None,
        )
        page.goto(BASE_URL, wait_until="networkidle")

        page.locator("#demoBanner").wait_for(state="visible")
        assert page.locator("#demoBanner .demo-badge").inner_text() == "DEMO DATA"
        assert page.locator("#tabHome").inner_text() == "Home"
        assert page.evaluate("document.documentElement.lang") == "en"

        page.locator("#tabSettings").click()
        page.locator("#settingsLocale").wait_for(state="visible")
        page.select_option("#settingsLocale", "zh-CN")
        page.locator("#settingsSave").click()
        page.wait_for_function("document.documentElement.lang === 'zh-CN'")
        page.locator("#settingsSave").wait_for(state="visible")
        page.wait_for_function("!document.querySelector('#settingsSave').disabled")
        assert page.locator("#tabHome").inner_text() == "首页"

        page.reload(wait_until="networkidle")
        assert page.locator("#tabHome").inner_text() == "首页"
        page.locator("#tabSettings").click()
        assert page.locator("#settingsLocale").input_value() == "zh-CN"

        page.select_option("#settingsLocale", "en")
        page.locator("#settingsSave").click()
        page.wait_for_function("document.documentElement.lang === 'en'")
        page.wait_for_function("!document.querySelector('#settingsSave').disabled")
        assert page.locator("#tabHome").inner_text() == "Home"

        for tab in ("Home", "Replay", "Data", "Trades", "Market", "Watchlist", "Performance", "Audit", "Settings"):
            page.locator(f"#tab{tab}").click()
            page.wait_for_timeout(250)
            visible = page.locator("body").inner_text().replace("简体中文", "")
            assert not re.search(r"[\u3400-\u9fff]", visible), f"Untranslated text on {tab}: {visible}"

        fallback = page.request.get(f"{BASE_URL}/api/demo/status?lang=fr-FR")
        assert fallback.headers["content-language"] == "en"
        assert fallback.json()["messageKey"] == "api.demoActive"
        chinese = page.request.get(f"{BASE_URL}/api/demo/status?lang=zh-CN")
        assert chinese.headers["content-language"] == "zh-CN"
        assert chinese.json()["message"] == "演示模式已启用。"
        assert not external_requests, f"Demo mode made external browser requests: {external_requests}"
        browser.close()


if __name__ == "__main__":
    main()
