#!/usr/bin/env python3
"""Capture the English feature-guide gallery at a reproducible 16:9 size."""

from __future__ import annotations

import os
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "images" / "feature-guide"
BASE_URL = os.environ.get("TRADECRAFT_BASE_URL", "http://127.0.0.1:8888").rstrip("/")
VIEWPORT = {"width": 1920, "height": 1080}


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
    page.screenshot(
        path=str(OUTPUT / filename),
        type="jpeg",
        quality=94,
        full_page=False,
    )


def click_tab(page: Page, name: str) -> None:
    page.locator(f"#tab{name}").click()
    page.wait_for_timeout(400)


def main() -> None:
    wait_for_server()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=2,
            locale="en-US",
            color_scheme="light",
        )
        page = context.new_page()
        assert page.request.post(f"{BASE_URL}/api/settings", data={"locale": "en"}).ok
        assert page.request.post(f"{BASE_URL}/api/demo/reset").ok
        page.goto(BASE_URL, wait_until="networkidle")
        page.locator("#demoBanner").wait_for(state="visible")

        capture(page, "01-home.jpg")
        click_tab(page, "Replay"); capture(page, "02-replay.jpg", 900)
        click_tab(page, "Trades"); capture(page, "03-trades.jpg")
        click_tab(page, "Watchlist"); capture(page, "04-watchlist.jpg", 900)
        page.locator("#watchlistAddToggle").click(); capture(page, "05-watchlist-add.jpg")
        click_tab(page, "Performance"); capture(page, "06-performance.jpg")
        page.locator("#perfYtdBtn").click(); capture(page, "07-performance-ytd.jpg")
        click_tab(page, "Settings"); capture(page, "08-settings.jpg")

        click_tab(page, "Data")
        for filename, mode in (
            ("09-data-rank.jpg", "rank"),
            ("10-data-pnl.jpg", "pnl"),
            ("11-data-amount.jpg", "amount"),
            ("12-data-activity.jpg", "activity"),
        ):
            page.locator(f'.data-sub-tabs [data-mode="{mode}"]').click()
            capture(page, filename)

        click_tab(page, "Market")
        for filename, value in (
            ("13-market-nasdaq.jpg", "NASDAQ100|change"),
            ("14-market-sp500.jpg", "SPX500|change"),
            ("15-market-nasdaq-ytd.jpg", "NASDAQ100|Perf.YTD"),
            ("16-market-sp500-ytd.jpg", "SPX500|Perf.YTD"),
            ("17-market-finviz.jpg", "FINVIZ"),
        ):
            page.locator(f'#marketHeatmapTabs [data-value="{value}"]').click()
            capture(page, filename, 2500)

        click_tab(page, "Audit")
        for filename, pane in (
            ("18-audit-overview.jpg", "overview"),
            ("19-audit-outcome.jpg", "outcome"),
            ("20-audit-process.jpg", "process"),
            ("21-audit-behavior.jpg", "behavior"),
            ("22-audit-evidence.jpg", "evidence"),
            ("23-audit-improvement.jpg", "improvement"),
            ("24-audit-ai.jpg", "ai"),
        ):
            page.locator(f'#auditSubnav [data-audit-pane="{pane}"]').click()
            capture(page, filename)
        browser.close()


if __name__ == "__main__":
    main()
