#!/usr/bin/env python3
"""Theme classifier and 3-layer attribution engine.

Layers:
  1. Theme beta      = benchmark return over holding period
  2. Selection alpha = stock buy-and-hold return - benchmark return
  3. Timing alpha    = trader actual return - stock buy-and-hold return
"""

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml


try:  # normal package import (server, cli, pytest)
    from app.state_store import read_json as _store_read_json, atomic_write_text as _atomic_write
    from app.kline_cache import get_kline
except ImportError:  # standalone: `python app/theme_classifier.py` puts app/ on sys.path
    from state_store import read_json as _store_read_json, atomic_write_text as _atomic_write
    from kline_cache import get_kline

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
STATE_DIR = ROOT / "data" / "state"
CLOSED_TRADES_FILE = STATE_DIR / "closed_trades.json"
THEME_ATTRIBUTION_FILE = STATE_DIR / "theme_attribution.json"

THEMES_CONFIG_PATH = CONFIG_DIR / "themes.yaml"


def read_json(path: Path, default):
    return _store_read_json(path, default)


def write_json(path: Path, payload) -> None:
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))


def load_themes_config() -> dict:
    try:
        with open(THEMES_CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing configuration file {THEMES_CONFIG_PATH.name}; verify that config/ is complete.") from exc
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Configuration file {THEMES_CONFIG_PATH.name} could not be parsed: {exc}") from exc


def symbol_code(symbol_lb: str) -> str:
    """NVDA.US -> NVDA"""
    return symbol_lb.split(".", 1)[0].upper()


def classify_symbol(symbol_lb: str, config: dict) -> Tuple[str, str, Optional[str]]:
    """Return (theme_name, tier, benchmark_symbol_lb or None)."""
    code = symbol_code(symbol_lb)
    themes = config.get("themes", {})
    for name, info in themes.items():
        if code in [s.upper() for s in info.get("symbols", [])]:
            bench = info.get("benchmark")
            bench_lb = bench if bench else None
            return name, info.get("tier", "optionality"), bench_lb
    unclass = config.get("unclassified", {})
    bench = unclass.get("benchmark")
    return "unclassified", unclass.get("tier", "optionality"), bench if bench else None


# ------------------------------------------------------------------
# K-line helpers (avoid circular import from main)
# ------------------------------------------------------------------

def load_kline_cache(symbol_lb: str) -> List[dict]:
    # Backward-compatible name; delegates to the shared in-process cache.
    return get_kline(symbol_lb)


def kline_price_on_or_before(rows: List[dict], date_str: str) -> Optional[float]:
    """Find the latest available close price on or before date_str."""
    target = date_str.replace("-", "")
    best = None
    for row in rows:
        day = str(row.get("time", ""))[:10].replace("-", "")
        if day <= target:
            try:
                best = float(row.get("close", 0))
            except Exception:
                pass
        else:
            break
    return best


def kline_price_on(rows: List[dict], date_str: str) -> Optional[float]:
    """Find close price exactly on date_str."""
    target = date_str.replace("-", "")
    for row in rows:
        day = str(row.get("time", ""))[:10].replace("-", "")
        if day == target:
            try:
                return float(row.get("close", 0))
            except Exception:
                return None
        if day > target:
            return None
    return None




def get_price_on(symbol_lb: str, date_str: str) -> Optional[float]:
    rows = get_kline(symbol_lb)
    return kline_price_on(rows, date_str)


def get_price_before(symbol_lb: str, date_str: str) -> Optional[float]:
    rows = get_kline(symbol_lb)
    return kline_price_on_or_before(rows, date_str)


# ------------------------------------------------------------------
# Attribution per closed trade
# ------------------------------------------------------------------

def compute_trade_attribution(
    symbol_lb: str,
    open_date: str,
    close_date: str,
    avg_entry: float,
    avg_exit: float,
    benchmark_lb: Optional[str],
) -> Optional[dict]:
    """Return attribution dict or None if data insufficient."""
    # Need prices: stock close on day before open, stock close on close_date
    # For benchmark: same dates
    stock_open_px = get_price_before(symbol_lb, open_date)
    stock_close_px = get_price_on(symbol_lb, close_date)

    if stock_open_px is None or stock_close_px is None or stock_open_px == 0:
        return None

    stock_bh_return = (stock_close_px - stock_open_px) / stock_open_px
    trader_return = (avg_exit - avg_entry) / avg_entry if avg_entry != 0 else 0

    theme_return = None
    selection_alpha = None

    if benchmark_lb:
        bench_open_px = get_price_before(benchmark_lb, open_date)
        bench_close_px = get_price_on(benchmark_lb, close_date)
        if bench_open_px and bench_close_px and bench_open_px != 0:
            theme_return = (bench_close_px - bench_open_px) / bench_open_px
            selection_alpha = stock_bh_return - theme_return

    timing_alpha = trader_return - stock_bh_return

    return {
        "symbol": symbol_lb,
        "open_date": open_date,
        "close_date": close_date,
        "stock_buyhold_return": round(stock_bh_return, 6),
        "trader_return": round(trader_return, 6),
        "theme_return": round(theme_return, 6) if theme_return is not None else None,
        "selection_alpha": round(selection_alpha, 6) if selection_alpha is not None else None,
        "timing_alpha": round(timing_alpha, 6),
    }


# ------------------------------------------------------------------
# Aggregate theme attribution
# ------------------------------------------------------------------

def aggregate_theme_attribution(closed_trades: List[dict], config: dict) -> List[dict]:
    """Aggregate closed trades by theme with attribution."""
    theme_stats: Dict[str, dict] = defaultdict(lambda: {
        "realized_pnl": 0.0,
        "trader_return_sum": 0.0,
        "trader_return_count": 0,
        "stock_bh_return_sum": 0.0,
        "stock_bh_count": 0,
        "theme_return_sum": 0.0,
        "theme_return_count": 0,
        "selection_alpha_sum": 0.0,
        "selection_alpha_count": 0,
        "timing_alpha_sum": 0.0,
        "timing_alpha_count": 0,
        "trade_count": 0,
        "opening_rebased_count": 0,
        "actual_trade_count": 0,
        "total_notional": 0.0,
        "symbols": set(),
    })

    for ct in closed_trades:
        symbol = ct["symbol"]
        theme, tier, benchmark = classify_symbol(symbol, config)
        stats = theme_stats[theme]

        stats["realized_pnl"] += ct.get("realized_pnl", 0)
        stats["trade_count"] += 1
        if ct.get("entry_source") == "opening_rebased":
            stats["opening_rebased_count"] += 1
        else:
            stats["actual_trade_count"] += 1
        stats["symbols"].add(symbol)
        notional = ct.get("quantity", 0) * ct.get("avg_entry", 0)
        stats["total_notional"] += notional

        # Attribution
        attr = compute_trade_attribution(
            symbol,
            ct["open_date"],
            ct["close_date"],
            ct["avg_entry"],
            ct["avg_exit"],
            benchmark,
        )
        if attr:
            stats["trader_return_sum"] += attr["trader_return"]
            stats["trader_return_count"] += 1
            stats["stock_bh_return_sum"] += attr["stock_buyhold_return"]
            stats["stock_bh_count"] += 1
            stats["timing_alpha_sum"] += attr["timing_alpha"]
            stats["timing_alpha_count"] += 1
            if attr["theme_return"] is not None:
                stats["theme_return_sum"] += attr["theme_return"]
                stats["theme_return_count"] += 1
            if attr["selection_alpha"] is not None:
                stats["selection_alpha_sum"] += attr["selection_alpha"]
                stats["selection_alpha_count"] += 1

    results = []
    for theme, stats in sorted(theme_stats.items(), key=lambda x: x[1]["realized_pnl"], reverse=True):
        def avg(sum_key, count_key):
            return stats[sum_key] / stats[count_key] if stats[count_key] > 0 else None

        tr = avg("trader_return_sum", "trader_return_count")
        bh = avg("stock_bh_return_sum", "stock_bh_count")
        th = avg("theme_return_sum", "theme_return_count")
        sel = avg("selection_alpha_sum", "selection_alpha_count")
        tim = avg("timing_alpha_sum", "timing_alpha_count")

        # Determine verdict
        verdict_parts = []
        if th is not None:
            if th > 0.05:
                verdict_parts.append("Theme thesis was correct")
            elif th < -0.05:
                verdict_parts.append("Theme thesis was incorrect")
            else:
                verdict_parts.append("Theme direction was neutral")
        if sel is not None:
            if sel > 0.03:
                verdict_parts.append("Strong stock selection")
            elif sel < -0.03:
                verdict_parts.append("Stock selection lagged ETF")
        if tim is not None:
            if tim > 0.02:
                verdict_parts.append("Positive timing contribution")
            elif tim < -0.02:
                verdict_parts.append("Negative timing contribution")

        _, tier, _ = classify_symbol(list(stats["symbols"])[0] if stats["symbols"] else "", config)

        results.append({
            "theme": theme,
            "tier": tier,
            "trade_count": stats["trade_count"],
            "opening_rebased_count": stats["opening_rebased_count"],
            "actual_trade_count": stats["actual_trade_count"],
            "realized_pnl": round(stats["realized_pnl"], 2),
            "total_notional": round(stats["total_notional"], 2),
            "symbols": sorted(list(stats["symbols"])),
            "avg_trader_return_pct": round(tr * 100, 2) if tr is not None else None,
            "avg_stock_buyhold_return_pct": round(bh * 100, 2) if bh is not None else None,
            "avg_theme_return_pct": round(th * 100, 2) if th is not None else None,
            "avg_selection_alpha_pct": round(sel * 100, 2) if sel is not None else None,
            "avg_timing_alpha_pct": round(tim * 100, 2) if tim is not None else None,
            "verdict": ", ".join(verdict_parts) if verdict_parts else "Insufficient data",
        })

    return results


def run_theme_attribution() -> dict:
    """Main entry: classify themes, compute attribution, save outputs."""
    config = load_themes_config()
    closed_trades = read_json(CLOSED_TRADES_FILE, [])
    if not closed_trades:
        return {"error": "No closed trades found. Run position matcher first."}

    attribution = aggregate_theme_attribution(closed_trades, config)
    write_json(THEME_ATTRIBUTION_FILE, attribution)

    total_pnl = sum(a["realized_pnl"] for a in attribution)
    return {
        "theme_count": len(attribution),
        "total_realized_pnl": round(total_pnl, 2),
        "themes": [a["theme"] for a in attribution],
    }


if __name__ == "__main__":
    result = run_theme_attribution()
    print(json.dumps(result, ensure_ascii=False, indent=2))
