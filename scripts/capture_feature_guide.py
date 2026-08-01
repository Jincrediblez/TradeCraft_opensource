#!/usr/bin/env python3
"""Capture the English feature-guide gallery in a reproducible Retina layout."""

from __future__ import annotations

import os
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "images" / "feature-guide"
BASE_URL = os.environ.get("TRADECRAFT_BASE_URL", "http://127.0.0.1:8888").rstrip("/")
VIEWPORT = {"width": 1726, "height": 1179}
SYSTEM_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def wait_for_server(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/api/health", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"TradeCraft did not become ready at {BASE_URL}")


def capture(page: Page, filename: str, settle_ms: int = 500) -> None:
    page.wait_for_timeout(settle_ms)
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=str(OUTPUT / filename),
        type="png",
        full_page=False,
    )


def click_tab(page: Page, name: str) -> None:
    page.locator(f"#tab{name}").click()
    page.wait_for_timeout(400)


def main() -> None:
    wait_for_server()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        executable = os.environ.get("TRADECRAFT_BROWSER_EXECUTABLE")
        if not executable and SYSTEM_CHROME.is_file():
            executable = str(SYSTEM_CHROME)
        browser = playwright.chromium.launch(executable_path=executable)
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=2,
            locale="en-US",
            color_scheme="dark",
        )
        page = context.new_page()
        page.add_init_script("localStorage.setItem('ibkr-theme', 'dark')")
        assert page.request.post(f"{BASE_URL}/api/settings", data={"locale": "en"}).ok
        assert page.request.post(f"{BASE_URL}/api/demo/reset").ok
        page.goto(BASE_URL, wait_until="networkidle")
        page.locator("#demoBanner").wait_for(state="visible")
        page.wait_for_function("document.body.classList.contains('night')")

        capture(page, "01-home.png")
        click_tab(page, "Replay"); capture(page, "02-replay.png", 1200)
        click_tab(page, "Trades"); capture(page, "03-trades.png")
        click_tab(page, "Watchlist"); capture(page, "04-watchlist.png", 900)
        page.locator("#watchlistAddToggle").click(); capture(page, "05-watchlist-add.png")
        click_tab(page, "Performance"); capture(page, "06-performance.png")
        page.locator("#perfYtdBtn").click(); capture(page, "07-performance-ytd.png")
        click_tab(page, "Settings"); capture(page, "08-settings.png")

        click_tab(page, "Data")
        for filename, mode in (
            ("09-data-rank.png", "rank"),
            ("10-data-pnl.png", "pnl"),
            ("11-data-amount.png", "amount"),
            ("12-data-activity.png", "activity"),
        ):
            page.locator(f'.data-sub-tabs [data-mode="{mode}"]').click()
            capture(page, filename)

        click_tab(page, "Market")
        for filename, value in (
            ("13-market-nasdaq.png", "NASDAQ100|change"),
            ("14-market-sp500.png", "SPX500|change"),
            ("15-market-nasdaq-ytd.png", "NASDAQ100|Perf.YTD"),
            ("16-market-sp500-ytd.png", "SPX500|Perf.YTD"),
        ):
            page.locator(f'#marketHeatmapTabs [data-value="{value}"]').click()
            capture(page, filename, 2500)

        with page.expect_popup() as popup_info:
            page.locator('#marketHeatmapTabs [data-value="FINVIZ"]').click()
        finviz = popup_info.value
        try:
            finviz.wait_for_load_state("domcontentloaded", timeout=15000)
            finviz.wait_for_timeout(2500)
            verification = "security verification" in finviz.locator("body").inner_text().lower()
            destination = OUTPUT / "17-market-finviz.png"
            if verification:
                if not destination.is_file():
                    raise RuntimeError(
                        "Finviz blocked the automated capture and no reviewed fallback exists."
                    )
            else:
                capture(finviz, destination.name)
        finally:
            finviz.close()

        click_tab(page, "Audit")
        for filename, pane in (
            ("18-audit-overview.png", "overview"),
            ("19-audit-outcome.png", "outcome"),
            ("20-audit-process.png", "process"),
            ("21-audit-behavior.png", "behavior"),
        ):
            page.locator(f'#auditSubnav [data-audit-pane="{pane}"]').click()
            capture(page, filename)

        page.locator('#auditSubnav [data-audit-pane="process"]').click()
        page.locator('#auditPaneProcess [data-audit-evidence]').first.click()
        page.locator("#auditPaneEvidence .audit-evidence-row").first.wait_for(state="visible")
        page.locator("#auditPaneEvidence .audit-evidence-head").first.click()
        capture(page, "22-audit-evidence.png")

        page.locator('#auditSubnav [data-audit-pane="process"]').click()
        page.locator('#auditPaneProcess [data-audit-feedback][data-decision="confirmed"]').first.click()
        page.wait_for_timeout(900)
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator('#auditPaneProcess [data-audit-rule]').first.click()
        page.wait_for_timeout(900)
        page.locator('#auditSubnav [data-audit-pane="improvement"]').click()
        capture(page, "23-audit-improvement.png")

        page.locator('#auditSubnav [data-audit-pane="ai"]').click()
        capture(page, "24-audit-ai.png", 900)
        browser.close()


if __name__ == "__main__":
    main()
