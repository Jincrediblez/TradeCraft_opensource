import json
import re
from pathlib import Path

from app.demo_data import demo_status, exit_demo, render_demo_audit_report, reset_demo, seed_demo
from app.i18n import load_catalog, normalize_locale, translate
from app.scoring_engine import STYLE_DRIFT_WEIGHTS, weighted_style_drift
from app.trade_setup_manager import SETUP_TYPES


def test_locale_resolution_and_fallback():
    assert normalize_locale("zh-CN") == "zh-CN"
    assert normalize_locale("zh") == "en"
    assert normalize_locale("auto") == "en"
    assert normalize_locale("fr-FR") == "en"


def test_locale_catalogs_share_stable_keys():
    english = load_catalog("en")
    chinese = load_catalog("zh-CN")
    for section in ("nav", "settings", "watchlist", "demo", "api", "ai"):
        assert set(english[section]) == set(chinese[section])
    assert translate("en", "nav.home") == "Home"
    assert translate("zh-CN", "nav.home") == "首页"
    assert translate("en", "api.internalError") == "TradeCraft could not complete the request."
    assert english["translations"]["Home"] == "Home"
    assert chinese["translations"]["Home"] == "首页"
    assert set(SETUP_TYPES).issubset(english["translations"])
    assert set(SETUP_TYPES).issubset(chinese["translations"])


def test_canonical_setup_enum_is_complete_and_ordered():
    assert SETUP_TYPES == [
        "Planned breakout",
        "Impulsive breakout",
        "Pullback entry",
        "Oversold rebound",
        "Fundamental catalyst",
        "Theme rotation",
        "FOMO chase",
        "Stop loss",
        "Take profit",
        "Risk reduction",
        "Trend weakness reduction",
        "Portfolio rotation",
        "Forced liquidation",
        "Unclassified",
        "Unplanned",
    ]


def test_seven_dimension_style_weights_are_normalized():
    assert set(STYLE_DRIFT_WEIGHTS) == {
        "theme_judgment",
        "churn_friction",
        "stock_selection",
        "narrative_hype",
        "entry_quality",
        "asymmetric_exit",
        "tail_risk",
    }
    assert abs(sum(STYLE_DRIFT_WEIGHTS.values()) - 1.0) < 0.001
    assert weighted_style_drift({key: 100 for key in STYLE_DRIFT_WEIGHTS}) == 100


def test_demo_seed_is_synthetic_and_exit_isolated(tmp_path):
    result = seed_demo(tmp_path)
    assert result["active"] is True
    trades_path = tmp_path / "data" / "state" / "trades.json"
    trades = json.loads(trades_path.read_text(encoding="utf-8"))
    assert trades and {row["source"] for row in trades} == {"demo"}
    serialized = json.dumps(trades)
    assert not re.search(r"\bU\d{8}\b", serialized)
    assert result["randomized"] is True
    assert result["generationId"]
    assert result["symbols"]
    assert (tmp_path / "cache" / "kline" / f"{result['symbols'][0]}_yahoo_day.json").exists()

    exited = exit_demo(tmp_path)
    assert exited["active"] is False
    assert demo_status(tmp_path)["active"] is False
    assert not trades_path.exists()
    assert (tmp_path / "data" / "state" / "demo_disabled.json").exists()


def test_demo_reset_replaces_the_entire_random_dataset(tmp_path):
    first_seed = seed_demo(tmp_path)
    first_trades = (tmp_path / "data" / "state" / "trades.json").read_bytes()
    first = reset_demo(tmp_path)
    second_trades = (tmp_path / "data" / "state" / "trades.json").read_bytes()
    second = reset_demo(tmp_path)
    third_trades = (tmp_path / "data" / "state" / "trades.json").read_bytes()
    assert first["seedVersion"] == second["seedVersion"]
    assert len({first_seed["generationId"], first["generationId"], second["generationId"]}) == 3
    assert first_trades != second_trades
    assert second_trades != third_trades
    marker = json.loads((tmp_path / "data" / "state" / "tradecraft_demo.json").read_text(encoding="utf-8"))
    assert len(marker["files"]) == len(set(marker["files"]))


def test_demo_audit_report_is_english_only_explicitly_synthetic_and_offline():
    snapshot = {
        "meta": {"executionCount": 24, "roundTripCount": 11},
        "outcome": {
            "timeWeightedReturnPct": 4.25,
            "alphaVsPrimaryPct": -1.5,
            "primaryBenchmark": {"symbol": "QQQ.US"},
        },
        "scorecards": {
            "process": {"value": 63},
            "confidence": {"value": 72},
        },
        "dataQuality": {
            "initialRiskCoveragePct": 45,
            "tradePlanCoveragePct": 60,
        },
        "findings": [
            {
                "title": "Exploratory allocation exceeds limit",
                "detail": "Exploratory positions represent 100.0% of holdings, above the 20% limit. Core positions: 0.0%.",
            }
        ],
        "advantages": [],
    }
    english = render_demo_audit_report(snapshot)
    assert english.startswith("# Demo AI Trading Audit")
    assert "No external AI was called" in english
    assert "randomized Demo" in english
    assert "Exploratory allocation exceeds limit" in english
    assert "Exploratory positions represent 100.0% of holdings" in english
    assert "Core positions: 0.0%." in english
    assert not re.search(r"[\u3400-\u9fff]", english)


def test_market_widgets_are_available_in_demo_mode():
    html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    market_loader = html.split("function loadMarketWidget(rawValue)", 1)[1].split(
        "function renderHeatmap", 1
    )[0]
    assert "STATE.demoActive" not in market_loader
    assert "Market widgets are disabled in demo mode." not in html
    assert "embed-widget-stock-heatmap.js" in market_loader
