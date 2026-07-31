#!/usr/bin/env python3
"""Phase 1 stability tests: atomic state, parsers, health, frontend syntax."""

import csv
import json
import re
import shutil
import subprocess
import sys
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.main as main
from app import state_store
from app import trade_plans
from app import watchlist_triggers
from app.scoring_engine import compute_discipline_summary, compute_exit_quality, compute_setup_performance


def test_write_json_atomic_and_metadata():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        original_root = main.ROOT
        original_metadata = main.STATE_METADATA_FILE
        try:
            main.ROOT = root
            main.STATE_METADATA_FILE = root / "data" / "state" / "_metadata.json"
            target = root / "data" / "state" / "example.json"
            main.write_json(target, [{"symbol": "INTC.US"}])
            assert json.loads(target.read_text(encoding="utf-8")) == [{"symbol": "INTC.US"}]
            metadata = json.loads(main.STATE_METADATA_FILE.read_text(encoding="utf-8"))
            entry = metadata["files"]["data/state/example.json"]
            assert entry["schemaVersion"] == 1
            assert entry["recordCount"] == 1
            assert entry["payloadType"] == "list"
        finally:
            main.ROOT = original_root
            main.STATE_METADATA_FILE = original_metadata


def test_state_store_module_write_json():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "cache" / "kline" / "TEST.US_yahoo_day.json"
        metadata_file = root / "data" / "state" / "_metadata.json"
        state_store.write_json(target, [{"time": "2026-06-03", "close": 1}], root=root, metadata_file=metadata_file)
        assert json.loads(target.read_text(encoding="utf-8"))[0]["close"] == 1
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        assert metadata["files"]["cache/kline/TEST.US_yahoo_day.json"]["recordCount"] == 1


def test_parse_tlg_stock_trade():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.tlg"
        parts = [
            "STK_TRD",
            "DEMOACCOUNT",
            "INTC",
            "",
            "NASDAQ",
            "BUY",
            "",
            "20260603",
            "10:12:30",
            "USD",
            "100",
            "",
            "31.5",
            "3150",
            "1.25",
            "",
        ]
        path.write_text("|".join(parts), encoding="utf-8")
        trades = main.parse_tlg(path)
        assert len(trades) == 1
        assert trades[0].symbol_lb == "INTC.US"
        assert trades[0].side == "BUY"
        assert trades[0].quantity == 100
        assert trades[0].price == 31.5


def test_parse_csv_transaction_and_mtm():
    with TemporaryDirectory() as tmp:
        tx_path = Path(tmp) / "tx.csv"
        with tx_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Transaction History",
                "Data",
                "2026-06-03",
                "",
                "",
                "Sell",
                "NVDA",
                "2",
                "120",
                "USD",
                "240",
                "0.5",
                "",
            ])
        trades = main.parse_csv_transactions(tx_path)
        assert len(trades) == 1
        assert trades[0].symbol_lb == "NVDA.US"
        assert trades[0].side == "SELL"

        mtm_path = Path(tmp) / "mtm.csv"
        with mtm_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Mark-to-Market Performance Summary", "Data", "Stocks", "AMD", "", "", "", "", "", "", "", "", "123.45"])
            writer.writerow(["Mark-to-Market Performance Summary", "Data", "Equity and Index Options", "AMD 19JUN26 100 C", "", "", "", "", "", "", "", "", "-12.5"])
        stocks, other = main.parse_mtm_file(mtm_path)
        assert stocks["AMD.US"]["mtmTotal"] == 123.45
        assert other[0].asset_category == "Equity and Index Options"
        assert other[0].mtm_total == -12.5


def test_period_files_prefer_tlg_trades_and_activity_mtm():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        tlg_path = root / "sample.tlg"
        tlg_path.write_text(
            "|".join([
                "STK_TRD",
                "DEMOACCOUNT",
                "INTC",
                "",
                "NASDAQ",
                "BUY",
                "",
                "20260603",
                "10:12:30",
                "USD",
                "100",
                "",
                "31.5",
                "3150",
                "1.25",
                "",
            ]),
            encoding="utf-8",
        )

        tx_path = root / "tx.csv"
        with tx_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Transaction History",
                "Data",
                "2026-06-03",
                "",
                "",
                "Buy",
                "INTC",
                "999",
                "99",
                "USD",
                "98901",
                "-9",
                "",
            ])

        activity_path = root / "activity.csv"
        with activity_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Statement", "Data", "Title", "Activity Statement"])
            writer.writerow(["Statement", "Data", "Period", "January 1, 2026 - June 3, 2026"])
            writer.writerow(["Mark-to-Market Performance Summary", "Data", "Stocks", "INTC", "", "", "", "", "", "", "", "", "456.78"])

        trades, mtm_payload, manifest, snapshot = main.parse_period_files([tlg_path, tx_path, activity_path])
        assert len(trades) == 1
        assert trades[0].source == "tlg"
        assert trades[0].trade_time == "10:12:30"
        assert trades[0].quantity == 100
        assert mtm_payload["stocks"]["INTC.US"]["mtmTotal"] == 456.78
        assert manifest["parsedTlgRows"] == 1
        assert manifest["parsedCsvRows"] == 0


def make_trade(symbol_lb: str, trade_date: str = "20260501") -> main.Trade:
    return main.Trade(
        symbol=symbol_lb.split(".", 1)[0],
        symbol_lb=symbol_lb,
        side="BUY",
        quantity=1,
        price=10,
        gross_amount=10,
        commission=0,
        currency="USD",
        exchange="NASDAQ",
        trade_date=trade_date,
        trade_time="10:00:00",
        source="test",
    )


def test_refresh_prefetch_symbols_cover_current_openable_universe(monkeypatch):
    monkeypatch.setattr(main, "load_settings", lambda: {"defaultSymbol": "nvda"})
    overview = {
        "defaultSymbol": "nvda",
        "availableSymbols": [
            {"symbol": "INTC.US"},
            {"symbol": "AMD.US"},
            {"symbol": "INTC.US"},
        ],
        "recentlyTraded": [
            {"symbol": "MRNA.US"},
        ],
    }
    trades = [make_trade("TSM.US"), make_trade("AMD.US")]
    mtm_stocks = {"MRVL.US": {"mtmTotal": 10}, "TSM.US": {"mtmTotal": -1}}
    symbols = main.build_refresh_prefetch_symbols(
        overview,
        trades,
        mtm_stocks,
        recent=["qcom", "AMD.US"],
    )
    assert symbols == ["QCOM.US", "AMD.US", "NVDA.US", "MRNA.US", "INTC.US", "TSM.US", "MRVL.US"]


def test_refresh_prefetch_symbols_include_settings_default_and_recent_symbols(monkeypatch):
    monkeypatch.setattr(main, "load_settings", lambda: {"defaultSymbol": "goog"})
    monkeypatch.setattr(main, "recent_symbols", lambda: ["asml", "AMD.US"])
    symbols = main.build_refresh_prefetch_symbols(
        {"availableSymbols": [], "recentlyTraded": []},
        [],
        {},
    )
    assert symbols == ["ASML.US", "AMD.US", "GOOG.US"]


def test_refresh_prefetch_symbols_prioritize_recent_then_defaults(monkeypatch):
    monkeypatch.setattr(main, "load_settings", lambda: {"defaultSymbol": "QQQ.US"})
    symbols = main.build_refresh_prefetch_symbols(
        {
            "defaultSymbol": "SPY.US",
            "recentlyTraded": [{"symbol": "AMD.US"}],
            "availableSymbols": [{"symbol": "GOOG.US"}, {"symbol": "AMD.US"}],
        },
        [make_trade("NVDA.US")],
        {"TSM.US": {}},
        recent=["GOOG.US", "INTC.US"],
    )
    assert symbols == ["GOOG.US", "INTC.US", "SPY.US", "QQQ.US", "AMD.US", "NVDA.US", "TSM.US"]


def test_prefetch_symbols_uses_market_end_and_keeps_failures():
    originals = {
        "get_symbol_trades": main.get_symbol_trades,
        "get_market_data_end_date": main.get_market_data_end_date,
        "fetch_kline_history": main.fetch_kline_history,
    }
    calls = []

    def fake_get_symbol_trades(symbol):
        if symbol == "OLD.US":
            return [make_trade("OLD.US", "20260605")]
        return []

    def fake_market_end():
        return date(2026, 6, 6)

    def fake_fetch(symbol, start, end, cache_only=False, force_refresh=False):
        calls.append({"symbol": symbol, "start": start, "end": end, "forceRefresh": force_refresh})
        if symbol == "FAIL.US":
            raise RuntimeError("yahoo unavailable")
        return main.KlineResult(
            rows=[{"time": end, "close": "10"}],
            freshness={"symbol": symbol, "cacheEnd": end},
        )

    try:
        main.get_symbol_trades = fake_get_symbol_trades
        main.get_market_data_end_date = fake_market_end
        main.fetch_kline_history = fake_fetch
        results = main.prefetch_symbols_for_refresh(["OK.US", "FAIL.US", "OLD.US"], force_refresh=True)
    finally:
        for key, value in originals.items():
            setattr(main, key, value)

    assert {call["symbol"]: call["end"] for call in calls} == {
        "FAIL.US": "2026-06-06",
        "OK.US": "2026-06-06",
        "OLD.US": "2026-06-06",
    }
    old_call = next(call for call in calls if call["symbol"] == "OLD.US")
    assert old_call["end"] != "2026-06-15"
    assert all(call["forceRefresh"] is True for call in calls)
    assert any(item["symbol"] == "FAIL.US" and not item["ok"] for item in results)
    assert any(item["symbol"] == "OK.US" and item["ok"] for item in results)
    assert any(item["symbol"] == "OLD.US" and item["ok"] for item in results)


def test_prefetch_stops_network_calls_after_yahoo_throttle(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "get_symbol_trades", lambda symbol: [])
    monkeypatch.setattr(main, "get_market_data_end_date", lambda: date(2026, 7, 17))

    def fake_fetch(symbol, start, end, cache_only=False, force_refresh=False):
        calls.append(symbol)
        return main.KlineResult(
            rows=[{"time": "2026-07-15", "close": 100}],
            freshness={
                "symbol": symbol,
                "cacheEnd": "2026-07-15",
                "expectedEnd": "2026-07-16",
                "stale": True,
                "error": "Yahoo Finance throttled the request (429)",
            },
        )

    monkeypatch.setattr(main, "fetch_kline_history", fake_fetch)
    results = main.prefetch_symbols_for_refresh(["GOOG.US", "NVDA.US", "AMD.US"])

    assert calls == ["GOOG.US"]
    assert results[0]["ok"] is False
    assert [item.get("skipped", False) for item in results] == [False, True, True]


def test_yahoo_retry_cooldown_expires_instead_of_locking_for_the_day():
    assert main.yahoo_retry_allowed({"lastError": ""}) is True
    assert main.yahoo_retry_allowed({
        "lastError": "Yahoo Finance throttled the request (429)",
        "lastAttemptAt": main.now_iso_text(),
    }) is False
    assert main.yahoo_retry_allowed({
        "lastError": "Yahoo Finance throttled the request (429)",
        "lastAttemptAt": "2026-01-01T00:00:00",
    }) is True
    assert main.yahoo_retry_allowed({
        "lastError": "Yahoo Finance throttled the request (429)",
    }) is True


def test_expected_kline_end_date_never_requires_future_data():
    expected = main.expected_kline_end_date("2999-01-08")
    assert expected <= main.today_text()


def test_expected_kline_end_date_respects_us_market_close_and_weekend():
    assert main.expected_kline_end_date(
        "2026-07-17", "GOOG.US", datetime(2026, 7, 17, 9, 30)
    ) == "2026-07-16"
    assert main.expected_kline_end_date(
        "2026-07-17", "GOOG.US", datetime(2026, 7, 17, 15, 59)
    ) == "2026-07-16"
    assert main.expected_kline_end_date(
        "2026-07-17", "GOOG.US", datetime(2026, 7, 17, 16, 0)
    ) == "2026-07-17"
    assert main.expected_kline_end_date(
        "2026-07-18", "GOOG.US", datetime(2026, 7, 18, 12, 0)
    ) == "2026-07-17"


def test_health_payload_shape():
    payload = main.build_health_payload()
    assert payload["ok"] is True
    assert "service" in payload
    assert "port" in payload["service"]
    assert "ibkr" in payload
    assert "sourceFileCounts" in payload["ibkr"]
    assert "kline" in payload
    assert "cacheIssueCount" in payload["kline"]
    assert "ai" in payload
    assert "cache" in payload["ai"]
    assert "performance" in payload
    assert "state" in payload


def test_health_payload_counts_only_active_kline_adjust_mode():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        manifest_file = root / "data" / "state" / "manifest.json"
        metadata_file = root / "data" / "state" / "_metadata.json"
        log_file = root / "logs" / "app.log"
        main.write_json(manifest_file, {})
        main.write_json(metadata_file, {})
        from app.health_service import build_health_payload as build_health

        payload = build_health(
            root=root,
            manifest_file=manifest_file,
            state_metadata_file=metadata_file,
            app_log_file=log_file,
            kline_state={
                "LEGACY.US": {
                    "cacheEnd": "2026-05-29",
                    "requestedEnd": "2026-06-05",
                    "lastError": "legacy failed",
                },
                "OK.US|yahoo": {
                    "cacheEnd": "2026-06-05",
                    "requestedEnd": "2026-06-05",
                    "lastError": "",
                },
                "STALE.US|yahoo": {
                    "cacheEnd": "2026-06-03",
                    "requestedEnd": "2026-06-05",
                    "lastError": "active failed",
                },
            },
            expected_kline_end_date=lambda value, symbol: value,
            adjust_mode="yahoo",
            app_log=lambda *args, **kwargs: None,
        )
        assert payload["kline"]["symbolCount"] == 2
        assert payload["kline"]["staleCount"] == 1
        assert payload["kline"]["errorCount"] == 1
        assert payload["kline"]["lastErrors"][0]["symbol"] == "STALE.US"


def test_kline_cache_health_detects_empty_cache():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        original_cache_dir = main.KLINE_CACHE_DIR
        try:
            main.KLINE_CACHE_DIR = root / "cache" / "kline"
            main.KLINE_CACHE_DIR.mkdir(parents=True)
            target = main.KLINE_CACHE_DIR / "BROKEN.US_yahoo_day.json"
            target.write_text("[]", encoding="utf-8")
            issues = main.kline_cache_health_issues()
            assert issues[0]["symbol"] == "BROKEN.US"
            assert issues[0]["type"] == "empty"
        finally:
            main.KLINE_CACHE_DIR = original_cache_dir


def test_backup_latest_good_state_copies_core_files():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        originals = {
            "ROOT": main.ROOT,
            "STATE_DIR": main.STATE_DIR,
            "PERIODS_STATE_DIR": main.PERIODS_STATE_DIR,
            "TRADES_STATE_FILE": main.TRADES_STATE_FILE,
            "TRADES_ALL_STATE_FILE": main.TRADES_ALL_STATE_FILE,
            "MTM_STATE_FILE": main.MTM_STATE_FILE,
            "MANIFEST_STATE_FILE": main.MANIFEST_STATE_FILE,
            "STATE_METADATA_FILE": main.STATE_METADATA_FILE,
            "RECENT_SYMBOLS_FILE": main.RECENT_SYMBOLS_FILE,
            "KLINE_MANIFEST_FILE": main.KLINE_MANIFEST_FILE,
        }
        try:
            main.ROOT = root
            main.STATE_DIR = root / "data" / "state"
            main.PERIODS_STATE_DIR = main.STATE_DIR / "periods"
            main.TRADES_STATE_FILE = main.STATE_DIR / "trades.json"
            main.TRADES_ALL_STATE_FILE = main.STATE_DIR / "trades_all.json"
            main.MTM_STATE_FILE = main.STATE_DIR / "mtm.json"
            main.MANIFEST_STATE_FILE = main.STATE_DIR / "manifest.json"
            main.STATE_METADATA_FILE = main.STATE_DIR / "_metadata.json"
            main.RECENT_SYMBOLS_FILE = main.STATE_DIR / "recent_symbols.json"
            main.KLINE_MANIFEST_FILE = root / "cache" / "kline" / "_manifest.json"
            main.write_json(main.TRADES_STATE_FILE, [{"symbol_lb": "INTC.US"}])
            main.write_json(main.MANIFEST_STATE_FILE, {"tradeCount": 1})
            result = main.backup_latest_good_state()
            assert result["fileCount"] >= 2
            assert (main.STATE_DIR / "backups" / "latest_good" / "data" / "state" / "trades.json").exists()
        finally:
            for key, value in originals.items():
                setattr(main, key, value)


def test_trade_plan_storage_normalizes_payload():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        original_file = trade_plans.TRADE_PLANS_FILE
        try:
            trade_plans.TRADE_PLANS_FILE = root / "data" / "state" / "trade_plans.json"
            plan = trade_plans.save_trade_plan(
                "nvda",
                {
                    "setupType": "突破",
                    "thesis": "earnings breakout",
                    "invalidationPrice": "115.5",
                    "targetHoldingDays": "20",
                    "addCondition": "close above high",
                    "reduceCondition": "extended",
                    "stopCondition": "break invalidation",
                },
            )
            assert plan["symbol"] == "NVDA.US"
            assert plan["invalidationPrice"] == 115.5
            assert plan["targetHoldingDays"] == 20
            loaded = trade_plans.get_trade_plan("NVDA.US")
            assert loaded["setupType"] == "突破"
        finally:
            trade_plans.TRADE_PLANS_FILE = original_file


def test_watchlist_triggers_storage_normalizes_and_dedupes():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        original_file = watchlist_triggers.WATCHLIST_TRIGGERS_FILE
        try:
            watchlist_triggers.WATCHLIST_TRIGGERS_FILE = root / "data" / "state" / "watchlist_triggers.json"
            rows = watchlist_triggers.save_triggers([
                {"symbol": "nvda", "watchPrice": "120", "breakoutPrice": "130", "note": "leader"},
                {"symbol": "NVDA.US", "watchPrice": "125"},
                {"symbol": "1810", "pullbackZone": "50-52"},
            ])
            assert [row["symbol"] for row in rows] == ["NVDA.US", "1810.HK"]
            assert rows[0]["watchPrice"] == "120"
            loaded = watchlist_triggers.load_triggers()
            assert loaded[1]["pullbackZone"] == "50-52"
        finally:
            watchlist_triggers.WATCHLIST_TRIGGERS_FILE = original_file


def test_setup_performance_summary():
    rows = compute_setup_performance([
        {"setup_type": "突破", "realized_pnl": 100, "r_multiple": 1.0, "risk_status": "planned"},
        {"setup_type": "突破", "realized_pnl": -50, "r_multiple": -0.5, "risk_status": "planned"},
        {"setup_type": "", "realized_pnl": 20, "r_multiple": None, "risk_status": "risk_missing"},
    ])
    by_setup = {row["setupType"]: row for row in rows}
    assert by_setup["突破"]["closedTradeCount"] == 2
    assert by_setup["突破"]["winRate"] == 50.0
    assert by_setup["突破"]["avgR"] == 0.25
    assert by_setup["未计划"]["riskMissingCount"] == 1


def test_exit_quality_empty_shape():
    payload = compute_exit_quality([])
    assert payload["evaluatedCount"] == 0
    assert payload["topMissed20d"] == []


def test_discipline_summary_flags_missing_risk():
    payload = compute_discipline_summary(
        {"risk_missing_count": 3},
        {"alerts": []},
        [],
        {"sellTooEarlyCount20d": 0, "trendUnbrokenExitCount": 0},
    )
    titles = [i["title"] for i in payload["topIssues"]]
    assert "初始风险缺失" in titles
    assert payload["hardRules"]


class ScriptCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_script = False
        self.current = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "script" and "src" not in dict(attrs):
            self.in_script = True
            self.current = []

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self.in_script:
            self.scripts.append("".join(self.current))
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.current.append(data)


def test_frontend_inline_scripts_parse():
    node = shutil.which("node")
    if not node:
        return
    html = (main.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    parser = ScriptCollector()
    parser.feed(html)
    assert parser.scripts
    script = "\n".join(f"new Function({source!r});" for source in parser.scripts)
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_frontend_watchlist_tab_structure():
    html = (main.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="tabWatchlist"' in html
    assert 'id="watchlistView"' in html
    assert 'setTab("watchlist")' in html
    assert 'function loadWatchlistView' in html


def test_frontend_replay_refresh_skips_fresh_or_failed_today_cache():
    html = (main.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "function replayNeedsMarketRefresh" in html
    assert "freshness.error && freshness.retryAllowed === false" in html
    assert "replayNeedsMarketRefresh(data)" in html


def test_build_position_reconciliation():
    fifo = [
        {"symbol": "AMD.US", "quantity": 120.0},
        {"symbol": "GOOG.US", "quantity": 475.0},
        {"symbol": "RKLB.US", "quantity": 20.0},
        {"symbol": "SMH.US", "quantity": 340.0},
    ]
    ibkr = [
        {"symbolLb": "AMD.US", "quantity": 120.0},
        {"symbolLb": "GOOG.US", "quantity": 475.0},
        {"symbolLb": "SMH.US", "quantity": 330.0},
    ]
    recon = main.build_position_reconciliation(fifo, ibkr)
    assert recon["status"] == "diff"
    assert recon["diffCount"] == 2
    only_fifo_symbols = {x["symbol"] for x in recon["onlyInFifo"]}
    assert only_fifo_symbols == {"RKLB.US"}
    mismatch_symbols = {x["symbol"] for x in recon["quantityMismatch"]}
    assert mismatch_symbols == {"SMH.US"}


def test_frontend_new_render_functions_parse():
    html_path = Path("static/index.html")
    if not html_path.exists():
        return
    content = html_path.read_text(encoding="utf-8")
    node = shutil.which("node")
    if not node:
        return
    # Extract functions related to new account cards and replay exposure
    snippets = []
    names = (
        "renderHomeAccountCards",
        "positionValueClass",
        "renderIbkrPositionRows",
        "bindIbkrPositionRowClicks",
        "renderReplayExposureCard",
    )
    pattern = r"function\s+(" + "|".join(names) + r")\s*\([^)]*\)\s*\{"
    for match in re.finditer(pattern, content):
        start = match.start()
        brace_count = 0
        i = content.find("{", start)
        if i == -1:
            continue
        for j in range(i, len(content)):
            if content[j] == "{":
                brace_count += 1
            elif content[j] == "}":
                brace_count -= 1
                if brace_count == 0:
                    snippets.append(content[i + 1:j])
                    break
    if not snippets:
        return
    script = "\n".join(f"new Function({source!r});" for source in snippets)
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_frontend_ibkr_positions_render_in_replay():
    html_path = Path("static/index.html")
    if not html_path.exists():
        return
    content = html_path.read_text(encoding="utf-8")
    node = shutil.which("node")
    if not node:
        return

    names = (
        "positionValueClass",
        "renderIbkrPositionRows",
        "bindIbkrPositionRowClicks",
        "renderReplayExposureCard",
    )
    definitions = []
    for name in names:
        match = re.search(rf"function\s+{name}\s*\([^)]*\)\s*\{{", content)
        assert match, f"missing frontend function {name}"
        start = match.start()
        i = content.find("{", start)
        brace_count = 0
        for j in range(i, len(content)):
            if content[j] == "{":
                brace_count += 1
            elif content[j] == "}":
                brace_count -= 1
                if brace_count == 0:
                    definitions.append(content[start:j + 1])
                    break

    script = "\n".join(definitions) + r"""
const roots = {
  replayExposureContent: { innerHTML: "", style: {}, querySelectorAll: () => [] },
  replayExposureCard: { style: {} },
};
global.document = { getElementById: id => roots[id] || null };
global.STATE = {
  currentSymbol: "GOOG.US",
  overview: {
    accountSnapshot: { endingValue: 444557.34462156, stockValue: 417699.3, endingCash: 26864.93462156, endingSettledCash: 26864.93462156 },
    riskSnapshot: { stockExposurePct: 0.939585, cashPct: 0.060431, leverage: 0.939585, fees: 555.57473, financingInterest: -1487.450044, syepCollateral: 185249, syepFeeEarned: 4.13 },
    positionReconciliation: { status: "diff", diffCount: 2, onlyInFifo: [{ symbol: "RKLB.US", fifoQty: 20, ibkrQty: 0 }], onlyInIbkr: [], quantityMismatch: [{ symbol: "SMH.US", fifoQty: 340, ibkrQty: 330 }] },
    ibkrOpenPositions: [
      { symbolLb: "AMD.US", quantity: 120, value: 55965.6, unrealizedPl: 2968.05064, sharesLent: 0 },
      { symbolLb: "GOOG.US", quantity: 475, value: 173736, unrealizedPl: 11284.520235, sharesLent: 0 },
      { symbolLb: "SMH.US", quantity: 330, value: 187997.7, unrealizedPl: 1762.19901, sharesLent: -289 },
    ],
  },
};
global.escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
global.fmtNum = value => Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
global.formatSigned = value => `${Number(value || 0) > 0 ? "+" : ""}${fmtNum(value)}`;
global.normalizeInputSymbol = value => String(value || "").trim().toUpperCase();
global.setTab = () => {};
global.loadSymbol = () => {};
renderReplayExposureCard();
const replay = roots.replayExposureContent.innerHTML;
for (const symbol of ["AMD.US", "GOOG.US", "SMH.US"]) {
  if (!replay.includes(symbol)) throw new Error(`replay missing ${symbol}`);
}
if (!replay.includes("借出 -289")) {
  throw new Error("SMH sharesLent was not rendered");
}
if (!replay.includes("总敞口/NAV")) {
  throw new Error("NAV exposure metrics were not rendered");
}
const rowsHtml = renderIbkrPositionRows(global.STATE.overview.ibkrOpenPositions, "GOOG.US", 5, global.STATE.overview.accountSnapshot.endingValue);
const rowsGoogIndex = rowsHtml.indexOf("GOOG.US");
const rowsSmhIndex = rowsHtml.indexOf("SMH.US");
if (!(rowsSmhIndex >= 0 && rowsGoogIndex > rowsSmhIndex)) {
  throw new Error("current symbol should stay in value-sorted position instead of jumping to the top");
}
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_frontend_ibkr_position_click_stays_in_replay():
    html_path = Path("static/index.html")
    if not html_path.exists():
        return
    content = html_path.read_text(encoding="utf-8")
    node = shutil.which("node")
    if not node:
        return

    match = re.search(r"function\s+bindIbkrPositionRowClicks\s*\([^)]*\)\s*\{", content)
    assert match, "missing frontend function bindIbkrPositionRowClicks"
    start = match.start()
    i = content.find("{", start)
    brace_count = 0
    definition = ""
    for j in range(i, len(content)):
        if content[j] == "{":
            brace_count += 1
        elif content[j] == "}":
            brace_count -= 1
            if brace_count == 0:
                definition = content[start:j + 1]
                break

    script = definition + r"""
let clickHandler = null;
let setTabCalls = 0;
let loadSymbolCalls = [];
let scrollCalls = [];
global.STATE = { currentTab: "replay", currentSymbol: "SMH.US" };
global.normalizeInputSymbol = value => String(value || "").trim().toUpperCase();
global.setTab = () => { setTabCalls += 1; };
global.loadSymbol = (symbol, options) => { loadSymbolCalls.push({ symbol, options }); return Promise.resolve(); };
global.window = {
  scrollX: 11,
  scrollY: 222,
  scrollTo: value => { scrollCalls.push(value); },
};
global.requestAnimationFrame = fn => fn();
global.setTimeout = fn => fn();
const root = {
  querySelectorAll: () => [{
    dataset: { symbol: "AMD.US" },
    blur: () => {},
    addEventListener: (eventName, handler) => {
      if (eventName === "click") clickHandler = handler;
    },
  }],
};
bindIbkrPositionRowClicks(root);
clickHandler({ preventDefault: () => {}, stopPropagation: () => {} });
Promise.resolve().then(() => {
  if (setTabCalls !== 0) throw new Error(`unexpected setTab calls: ${setTabCalls}`);
  if (loadSymbolCalls.length !== 1) throw new Error(`unexpected loadSymbol calls: ${loadSymbolCalls.length}`);
  if (loadSymbolCalls[0].symbol !== "AMD.US") throw new Error("wrong symbol loaded");
  if (!loadSymbolCalls[0].options || loadSymbolCalls[0].options.activateReplay !== false) {
    throw new Error("position click should load without reactivating replay");
  }
  if (!scrollCalls.some(call => call.left === 11 && call.top === 222)) {
    throw new Error("position click should restore the replay scroll position");
  }
});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


if __name__ == "__main__":
    test_write_json_atomic_and_metadata()
    test_state_store_module_write_json()
    test_parse_tlg_stock_trade()
    test_parse_csv_transaction_and_mtm()
    test_period_files_prefer_tlg_trades_and_activity_mtm()
    test_refresh_prefetch_symbols_cover_current_openable_universe()
    test_prefetch_symbols_uses_market_end_and_keeps_failures()
    test_expected_kline_end_date_never_requires_future_data()
    test_health_payload_shape()
    test_health_payload_counts_only_active_kline_adjust_mode()
    test_kline_cache_health_detects_empty_cache()
    test_backup_latest_good_state_copies_core_files()
    test_trade_plan_storage_normalizes_payload()
    test_setup_performance_summary()
    test_exit_quality_empty_shape()
    test_discipline_summary_flags_missing_risk()
    test_frontend_inline_scripts_parse()
    test_parse_activity_statement_snapshot_real_file()
    test_build_position_reconciliation()
    test_frontend_new_render_functions_parse()
    test_frontend_ibkr_positions_render_in_home_and_replay()
    test_frontend_ibkr_position_click_stays_in_replay()
    print("Phase 1 stability tests passed.")
