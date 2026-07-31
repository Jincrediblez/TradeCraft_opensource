#!/usr/bin/env python3
"""Phase 4: shared in-process K-line cache (app.kline_cache)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.kline_cache as kc
import app.main as main


class _FakeYahooResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "chart": {
                "result": [{
                    "timestamp": [1767225600, 1767312000],
                    "indicators": {
                        "quote": [{
                            "open": [10.0, 11.0],
                            "high": [12.0, 12.5],
                            "low": [9.5, 10.5],
                            "close": [11.0, 12.0],
                            "volume": [1000, 2000],
                        }],
                        "adjclose": [{"adjclose": [10.8, 11.9]}],
                    },
                }],
                "error": None,
            }
        }


def test_fetch_yahoo_kline_maps_chart_payload(monkeypatch):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "timeout": timeout})
        return _FakeYahooResponse()

    monkeypatch.setattr(main.requests, "get", fake_get)

    rows = main.fetch_yahoo_kline("NVDA.US", "2026-01-01", "2026-01-02")

    assert calls[0]["url"].endswith("/NVDA")
    assert calls[0]["params"]["interval"] == "1d"
    assert rows == [
        {
            "time": "2026-01-01",
            "open": 9.818182,
            "high": 11.781818,
            "low": 9.327273,
            "close": 10.8,
            "adjusted_close": 10.8,
            "volume": 1000,
            "turnover": 10800.0,
            "source": "yahoo",
        },
        {
            "time": "2026-01-02",
            "open": 10.908333,
            "high": 12.395833,
            "low": 10.4125,
            "close": 11.9,
            "adjusted_close": 11.9,
            "volume": 2000,
            "turnover": 23800.0,
            "source": "yahoo",
        },
    ]


def test_memoizes_and_clear_forces_reload(monkeypatch):
    calls = []

    def fake_load(symbol):
        calls.append(symbol)
        return [{"time": "2026-01-01", "close": 1.0}]

    monkeypatch.setattr(main, "load_kline_cache", fake_load)
    kc.clear()

    first = kc.get_kline("NVDA.US")
    second = kc.get_kline("NVDA.US")
    assert first is second            # served from cache
    assert calls == ["NVDA.US"]       # loaded only once

    kc.clear()
    kc.get_kline("NVDA.US")
    assert calls == ["NVDA.US", "NVDA.US"]  # reloaded after clear


def test_returns_empty_list_on_loader_error(monkeypatch):
    def boom(symbol):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(main, "load_kline_cache", boom)
    kc.clear()
    assert kc.get_kline("X.US") == []


def test_force_refresh_invalidates_replay_and_memory_caches(monkeypatch, tmp_path):
    root = tmp_path
    kline_dir = root / "cache" / "kline"
    state_dir = root / "data" / "state"
    kline_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)

    originals = {
        "ROOT": main.ROOT,
        "KLINE_CACHE_DIR": main.KLINE_CACHE_DIR,
        "KLINE_MANIFEST_FILE": main.KLINE_MANIFEST_FILE,
        "STATE_METADATA_FILE": main.STATE_METADATA_FILE,
    }
    try:
        main.ROOT = root
        main.KLINE_CACHE_DIR = kline_dir
        main.KLINE_MANIFEST_FILE = kline_dir / "_manifest.json"
        main.STATE_METADATA_FILE = state_dir / "_metadata.json"
        main.REPLAY_PAYLOAD_CACHE.clear()
        main.REPLAY_PAYLOAD_CACHE[("TEST.US", "old")] = {"symbol": "TEST.US", "candles": []}
        kc._CACHE["TEST.US"] = [{"time": "2026-01-03", "close": 1.0}]

        main.save_kline_cache("TEST.US", [{"time": "2026-01-03", "close": 1.0}])

        def fake_fetch(symbol, start, end):
            assert symbol == "TEST.US"
            return [{"time": "2026-01-10", "open": 2, "high": 2, "low": 2, "close": 2}]

        monkeypatch.setattr(main, "fetch_yahoo_kline", fake_fetch)
        monkeypatch.setattr(main, "load_settings", lambda: {"yahooRefreshSeconds": 300})

        result = main.fetch_kline_history(
            "TEST.US",
            "2026-01-01",
            "2026-01-10",
            force_refresh=True,
        )
        replay_cache_cleared = not any(key[0] == "TEST.US" for key in main.REPLAY_PAYLOAD_CACHE)
        memory_cache_cleared = kc._CACHE == {}
    finally:
        for key, value in originals.items():
            setattr(main, key, value)
        main.REPLAY_PAYLOAD_CACHE.clear()
        kc.clear()

    assert result.rows[-1]["time"] == "2026-01-10"
    assert replay_cache_cleared
    assert memory_cache_cleared


def test_fresh_completed_market_day_does_not_refetch(monkeypatch, tmp_path):
    root = tmp_path
    kline_dir = root / "cache" / "kline"
    state_dir = root / "data" / "state"
    kline_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    originals = {
        "ROOT": main.ROOT,
        "KLINE_CACHE_DIR": main.KLINE_CACHE_DIR,
        "KLINE_MANIFEST_FILE": main.KLINE_MANIFEST_FILE,
        "STATE_METADATA_FILE": main.STATE_METADATA_FILE,
    }
    calls = []
    try:
        main.ROOT = root
        main.KLINE_CACHE_DIR = kline_dir
        main.KLINE_MANIFEST_FILE = kline_dir / "_manifest.json"
        main.STATE_METADATA_FILE = state_dir / "_metadata.json"
        main.save_kline_cache("GOOG.US", [
            {"time": "2026-07-15", "open": 100, "high": 101, "low": 99, "close": 100},
            {"time": "2026-07-16", "open": 101, "high": 102, "low": 100, "close": 101},
        ])
        monkeypatch.setattr(main, "expected_kline_end_date", lambda end, symbol="", now=None: "2026-07-16")
        monkeypatch.setattr(main, "load_settings", lambda: {"yahooRefreshSeconds": 300})
        monkeypatch.setattr(main, "fetch_yahoo_kline", lambda *args: calls.append(args) or [])

        result = main.fetch_kline_history("GOOG.US", "2026-07-15", "2026-07-17")
    finally:
        for key, value in originals.items():
            setattr(main, key, value)
        kc.clear()

    assert calls == []
    assert result.rows[-1]["time"] == "2026-07-16"
    assert result.freshness["stale"] is False


def test_historical_backfill_error_does_not_block_latest_refresh(monkeypatch, tmp_path):
    root = tmp_path
    kline_dir = root / "cache" / "kline"
    state_dir = root / "data" / "state"
    kline_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)

    originals = {
        "ROOT": main.ROOT,
        "KLINE_CACHE_DIR": main.KLINE_CACHE_DIR,
        "KLINE_MANIFEST_FILE": main.KLINE_MANIFEST_FILE,
        "STATE_METADATA_FILE": main.STATE_METADATA_FILE,
    }
    calls = []
    try:
        main.ROOT = root
        main.KLINE_CACHE_DIR = kline_dir
        main.KLINE_MANIFEST_FILE = kline_dir / "_manifest.json"
        main.STATE_METADATA_FILE = state_dir / "_metadata.json"

        main.save_kline_cache(
            "SPCX.US",
            [
                {"time": "2026-06-12", "open": 100, "high": 100, "low": 100, "close": 100},
                {"time": "2026-07-06", "open": 162, "high": 163, "low": 161, "close": 162},
            ],
        )

        def fake_fetch(symbol, start, end):
            calls.append((start, end))
            if end < "2026-06-12":
                raise RuntimeError("pre-listing range rejected")
            return [
                {"time": "2026-07-07", "open": 170, "high": 171, "low": 169, "close": 170},
                {"time": "2026-07-09", "open": 180, "high": 181, "low": 179, "close": 180},
            ]

        monkeypatch.setattr(main, "fetch_yahoo_kline", fake_fetch)
        monkeypatch.setattr(main, "load_settings", lambda: {"yahooRefreshSeconds": 300})

        result = main.fetch_kline_history("SPCX.US", "2022-05-07", "2026-07-09")
    finally:
        for key, value in originals.items():
            setattr(main, key, value)
        kc.clear()

    assert calls == [("2026-06-29", "2026-07-09"), ("2022-05-07", "2026-06-11")]
    assert result.rows[-1]["time"] == "2026-07-09"
    assert result.freshness["stale"] is False
    assert result.freshness["error"] == ""


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
