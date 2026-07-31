import io
import json
from pathlib import Path

import app.main as main
from app import watchlist_manager as wm


def patch_watchlist_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(wm, "ROOT", tmp_path)
    monkeypatch.setattr(wm, "STATE_DIR", tmp_path / "data" / "state")
    monkeypatch.setattr(wm, "DB_PATH", tmp_path / "data" / "state" / "watchlist.db")
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setattr(main, "STATE_DIR", tmp_path / "data" / "state")
    monkeypatch.setattr(main, "IBKR_OPEN_POSITIONS_FILE", tmp_path / "data" / "state" / "ibkr_open_positions.json")
    monkeypatch.setattr(main, "STATE_METADATA_FILE", tmp_path / "data" / "state" / "_metadata.json")
    monkeypatch.setattr(main, "SYMBOL_NAMES_FILE", tmp_path / "data" / "state" / "symbol_names.json")


def test_watchlist_crud_batch_and_color_validation(monkeypatch, tmp_path):
    patch_watchlist_paths(monkeypatch, tmp_path)
    wm.init_db()

    group = wm.create_group({"name": "AI Infra"})
    item = wm.create_item({"symbol": "nvda", "groupId": group["id"], "color": "green", "priority": 1})
    assert item["symbol"] == "NVDA.US"
    assert item["color"] == "green"
    assert wm.list_groups()[0]["itemCount"] == 1

    updated = wm.update_item(item["id"], {"color": "yellow", "note": "leader"})
    assert updated["color"] == "yellow"
    assert updated["note"] == "leader"

    result = wm.batch_items({"action": "add", "items": "AMD\n700.HK", "groupId": group["id"], "color": "blue"})
    assert result["saved"] == 2
    assert {row["symbol"] for row in wm.list_items()} == {"NVDA.US", "AMD.US", "700.HK"}

    ids = [row["id"] for row in wm.list_items() if row["symbol"] != "NVDA.US"]
    result = wm.batch_items({"action": "delete", "ids": ids})
    assert result["deleted"] == 2
    assert [row["symbol"] for row in wm.list_items()] == ["NVDA.US"]

    try:
        wm.create_item({"symbol": "tsm", "color": "orange"})
        assert False, "invalid color should fail"
    except ValueError as exc:
        assert "invalid color" in str(exc)


def test_parse_tradingview_text_sections_and_exchanges():
    text = "###Semi,NASDAQ:NVDA,NYSE:TSM\n###HK,HKEX:700,HKEX:9988\nTVC:US10Y,AMD"
    entries, skipped = wm.parse_tradingview_text(text, default_group="Macro")
    assert entries == [
        {"symbol": "NVDA.US", "group": "Semi"},
        {"symbol": "TSM.US", "group": "Semi"},
        {"symbol": "700.HK", "group": "HK"},
        {"symbol": "9988.HK", "group": "HK"},
        {"symbol": "AMD.US", "group": "HK"},
    ]
    assert skipped == ["TVC:US10Y"]


def test_parse_tradingview_text_no_sections_uses_default_group():
    entries, skipped = wm.parse_tradingview_text("NASDAQ:NVDA, NASDAQ:NVDA, SSE:600519", default_group="Macro")
    assert entries == [
        {"symbol": "NVDA.US", "group": "Macro"},
        {"symbol": "600519.SH", "group": "Macro"},
    ]
    assert skipped == []


def test_parse_tradingview_text_skips_ratios_futures_and_data_feeds():
    text = "###US INDEX,SP:SPX/AMEX:RSP,CME_MINI:ES1!,FRED:M2SL,ECONOMICS:USGDPYY,NYSE:BRK.B,KRX:005930,TSE:4062,AMEX:SPY"
    entries, skipped = wm.parse_tradingview_text(text)
    assert entries == [
        {"symbol": "BRK.B.US", "group": "US INDEX"},
        {"symbol": "SPY.US", "group": "US INDEX"},
    ]
    assert skipped == [
        "SP:SPX/AMEX:RSP",
        "CME_MINI:ES1!",
        "FRED:M2SL",
        "ECONOMICS:USGDPYY",
        "KRX:005930",
        "TSE:4062",
    ]


def test_import_tradingview_creates_groups_and_items(monkeypatch, tmp_path):
    patch_watchlist_paths(monkeypatch, tmp_path)
    wm.init_db()
    result = wm.import_tradingview({
        "name": "🌐Macro_b2542.txt",
        "text": "AAPL,###Semi,NASDAQ:NVDA,HKEX:981",
    })
    assert result["imported"] == 3
    assert result["errors"] == []
    items = wm.list_items()
    by_symbol = {item["symbol"]: item for item in items}
    assert by_symbol["AAPL.US"]["groupName"] == "🌐Macro"
    assert by_symbol["NVDA.US"]["groupName"] == "Semi"
    assert by_symbol["981.HK"]["groupName"] == "Semi"


def test_batch_add_accepts_tradingview_tokens(monkeypatch, tmp_path):
    patch_watchlist_paths(monkeypatch, tmp_path)
    wm.init_db()
    result = wm.batch_items({"action": "add", "items": "NASDAQ:TSLA\n700"})
    assert result["saved"] == 2
    assert {row["symbol"] for row in wm.list_items()} == {"TSLA.US", "700.HK"}


def test_watchlist_http_api(monkeypatch, tmp_path):
    patch_watchlist_paths(monkeypatch, tmp_path)
    wm.init_db()
    monkeypatch.setattr(main, "watchlist_symbol_payload", lambda symbol, refresh=False: {"ok": True, "symbol": "NVDA.US", "chart": {"candles": []}})
    monkeypatch.setattr(main, "load_kline_cache", lambda symbol: [
        {"time": "2026-07-08", "close": 100.0},
        {"time": "2026-07-09", "close": 105.0},
    ] if symbol == "NVDA.US" else [])

    def call(method, path, payload=None):
        responses = []
        handler = main.Handler.__new__(main.Handler)
        handler.path = path
        body = json.dumps(payload or {}).encode("utf-8") if payload is not None else b""
        handler.headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._json = lambda data, code=200: responses.append((code, data))
        getattr(main.Handler, method)(handler)
        assert responses
        return responses[-1]

    code, data = call("do_POST", "/api/watchlist/items/batch", {"action": "add", "items": "NVDA\nAMD"})
    assert code == 200
    assert data["ok"] is True
    assert data["saved"] == 2

    code, data = call("do_POST", "/api/watchlist/import/tradingview", {"name": "Macro.txt", "text": "NASDAQ:TSLA"})
    assert code == 200
    assert data["ok"] is True
    assert data["imported"] == 1
    snapshot_path = tmp_path / "data" / "state" / "statement_snapshot.json"
    snapshot_path.write_text(json.dumps({
        "NVDA.US": {"symbolLb": "NVDA.US", "description": "NVIDIA CORP"},
        "AMD.US": {"symbolLb": "AMD.US", "description": "ADVANCED MICRO DEVICES INC"},
    }), encoding="utf-8")

    code, data = call("do_GET", "/api/watchlist")
    assert code == 200
    assert {item["symbol"] for item in data["items"]} == {"NVDA.US", "AMD.US", "TSLA.US"}
    nvda = next(item for item in data["items"] if item["symbol"] == "NVDA.US")
    amd = next(item for item in data["items"] if item["symbol"] == "AMD.US")
    assert nvda["latestDate"] == "2026-07-09"
    assert nvda["latestClose"] == 105.0
    assert nvda["changePct"] == 5.0
    assert nvda["companyName"] == "NVIDIA CORP"
    assert amd["companyName"] == "ADVANCED MICRO DEVICES INC"
    assert amd["changePct"] is None

    code, data = call("do_GET", "/api/watchlist/symbol?symbol=nvda")
    assert code == 200
    assert data["symbol"] == "NVDA.US"


def test_yahoo_symbol_maps_us_hk_and_a_shares():
    assert main.yahoo_symbol("NVDA.US") == "NVDA"
    assert main.yahoo_symbol("700.HK") == "0700.HK"
    assert main.yahoo_symbol("600519.SH") == "600519.SS"
    assert main.yahoo_symbol("000333.SZ") == "000333.SZ"


def test_refresh_watchlist_company_names_fetches_and_caches(monkeypatch, tmp_path):
    patch_watchlist_paths(monkeypatch, tmp_path)
    (tmp_path / "data" / "state").mkdir(parents=True)

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "quoteResponse": {
                    "result": [
                        {"symbol": "ARKK", "shortName": "ARK Innovation ETF"},
                        {"symbol": "0700.HK", "longName": "Tencent Holdings Limited"},
                        {"symbol": "600519.SS", "shortName": "Kweichow Moutai Co., Ltd."},
                        {"symbol": "000333.SZ", "shortName": "Midea Group Co., Ltd."},
                    ]
                }
            }

    requests_seen = []

    def fake_get(url, params=None, headers=None, timeout=None):
        requests_seen.append(params["symbols"])
        return FakeResponse()

    monkeypatch.setattr(main.requests, "get", fake_get)

    result = main.refresh_watchlist_company_names(["ARKK.US", "700.HK", "600519.SH", "000333.SZ"], force=True)

    assert result["requested"] == 4
    assert result["fetched"] == 4
    assert requests_seen == ["000333.SZ,0700.HK,600519.SS,ARKK"]
    names = main.watchlist_company_name_map()
    assert names["ARKK.US"] == "ARK Innovation ETF"
    assert names["700.HK"] == "Tencent Holdings Limited"
    assert names["600519.SH"] == "Kweichow Moutai Co., Ltd."
    assert names["000333.SZ"] == "Midea Group Co., Ltd."
