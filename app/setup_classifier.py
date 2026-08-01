#!/usr/bin/env python3
"""Setup classifier: rule-based automatic setup type detection for closed trades.

Rules (priority order, first match wins):
  1. FOMO chase       : fomo_score >= 60
  2. Breakout         : close >= 20d_high * 0.98, volume > 1.5x 20d_avg_vol,
                    prior 5d range < 8% (convergence)
  3. Pullback         : price within MA20 +/-4%, MA20 trending up,
                    prior 20d gain > 10%
  4. Oversold rebound : prior 20d loss > 15%, long lower shadow or volume contraction
  5. Theme rotation   : theme benchmark 20d gain > 5%, no more specific setup matched
  6. Unclassified     : fallback

Confidence levels:
  high   : clear multi-factor match
  medium : partial or single-factor match
  low    : fallback / insufficient data
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.setup_types import CANONICAL_SETUP_TYPES


try:  # normal package import (server, cli, pytest)
    from app.state_store import read_json as _store_read_json, atomic_write_text as _atomic_write
    from app.kline_cache import get_kline as load_kline
    from app.fomo import compute_fomo_score
except ImportError:  # standalone script execution puts app/ on sys.path
    from state_store import read_json as _store_read_json, atomic_write_text as _atomic_write
    from kline_cache import get_kline as load_kline
    from fomo import compute_fomo_score

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "state"
CLOSED_TRADES_FILE = STATE_DIR / "closed_trades.json"
TRADE_SETUPS_FILE = STATE_DIR / "trade_setups.json"

SETUP_TYPES = list(CANONICAL_SETUP_TYPES)

def build_date_index(rows: List[dict]) -> Dict[str, dict]:
    result = {}
    for row in rows:
        day = str(row.get("time", ""))[:10].replace("-", "")
        result[day] = row
    return result


def _parse_float(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def fomo_score_for_trade(symbol: str, entry_date: str, entry_price: float) -> Optional[int]:
    """Compute FOMO score for a single entry. Thin wrapper over app.fomo."""
    result = compute_fomo_score(load_kline(symbol), entry_date, entry_price)
    return result["score"] if result else None


def classify_setup(closed_trade: dict) -> Tuple[str, str, List[str]]:
    """Classify a single closed trade into setup type.

    Returns: (setup_type, confidence, reasons)
    """
    symbol = closed_trade.get("symbol", "")
    entry_date = closed_trade.get("open_date", "")
    entry_price = _parse_float(closed_trade.get("avg_entry", 0))

    rows = load_kline(symbol)
    if not rows:
        return "Unclassified", "low", ["No candle data"]

    by_date = build_date_index(rows)
    dates = sorted(by_date.keys())
    target = entry_date.replace("-", "")

    if target not in by_date:
        return "Unclassified", "low", ["No candle data on the entry date"]

    entry_idx = dates.index(target)
    if entry_idx < 25:
        return "Unclassified", "low", ["Fewer than 25 days of candle history"]

    closes = []
    highs = []
    lows = []
    vols = []
    for d in dates[:entry_idx + 1]:
        row = by_date[d]
        try:
            closes.append(float(row["close"]))
            highs.append(float(row.get("high", row["close"])))
            lows.append(float(row.get("low", row["close"])))
            vols.append(float(row.get("turnover", 0)))
        except Exception:
            closes.append(0)
            highs.append(0)
            lows.append(0)
            vols.append(0)

    if len(closes) < 25:
        return "Unclassified", "low", ["Insufficient data"]

    # Rule 1: FOMO chase / impulsive breakout.
    fomo = fomo_score_for_trade(symbol, entry_date, entry_price)
    if fomo is not None and fomo >= 60:
        return "FOMO chase", "high", [f"FOMO score {fomo}"]

    # Pre-compute common metrics
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else 0
    high20 = max(closes[-20:]) if len(closes) >= 20 else 0

    prior_5d_closes = closes[-6:-1]
    prior_5d_range = (
        (max(prior_5d_closes) - min(prior_5d_closes)) / min(prior_5d_closes)
        if len(prior_5d_closes) >= 2 and min(prior_5d_closes) > 0
        else 0
    )

    prior_20d_return = (
        (closes[-1] - closes[-21]) / closes[-21]
        if len(closes) >= 21 and closes[-21] > 0
        else 0
    )

    avg_vol_20 = sum(vols[-20:]) / 20 if vols else 0
    entry_vol = vols[-1] if vols else 0

    # MA20 slope over 10 days
    ma20_10d_ago = sum(closes[-30:-10]) / 20 if len(closes) >= 30 else 0
    ma20_slope = (ma20 - ma20_10d_ago) / ma20_10d_ago if ma20_10d_ago > 0 else 0

    reasons: List[str] = []

    # Rule 2: planned or impulsive trend breakout.
    if high20 > 0 and closes[-1] >= high20 * 0.95:
        if avg_vol_20 > 0 and entry_vol > avg_vol_20 * 1.3:
            if prior_5d_range < 0.10:
                reasons.append(f"Close near 20-day high ({closes[-1]:.2f}/{high20:.2f})")
                reasons.append(f"Volume {entry_vol / avg_vol_20:.1f}x the 20-day average")
                reasons.append(f"Prior 5-day range {prior_5d_range:.1%}")
                if fomo is not None and fomo >= 50:
                    return "Impulsive breakout", "medium", reasons + [f"FOMO score {fomo}"]
                return "Planned breakout", "high", reasons

    # --- Rule 3: Pullback entry ---
    if ma20 > 0 and abs(entry_price - ma20) / ma20 <= 0.04:
        if ma20_slope > 0.03:
            if prior_20d_return > 0.10:
                reasons.append(f"Price within 4% of MA20 ({entry_price:.2f}/MA20={ma20:.2f})")
                reasons.append(f"Upward MA20 slope {ma20_slope:.1%}")
                reasons.append(f"Prior 20-day gain {prior_20d_return:.1%}")
                return "Pullback entry", "high", reasons

    # --- Rule 4: Oversold rebound ---
    if prior_20d_return < -0.15:
        entry_high = highs[-1]
        entry_low = lows[-1]
        shadow_ratio = (
            (closes[-1] - entry_low) / (entry_high - entry_low)
            if entry_high > entry_low else 0
        )
        vol_contraction = avg_vol_20 > 0 and entry_vol < avg_vol_20 * 0.7
        if shadow_ratio > 0.6 or vol_contraction:
            reasons.append(f"Prior 20-day decline {prior_20d_return:.1%}")
            if shadow_ratio > 0.6:
                reasons.append(f"Long lower shadow (ratio {shadow_ratio:.2f})")
            if vol_contraction:
                reasons.append("Volume contraction with price stabilization")
            return "Oversold rebound", "medium", reasons

    # --- Rule 5: Theme rotation ---
    try:
        from app.theme_classifier import classify_symbol, load_themes_config
        config = load_themes_config()
        theme, tier, benchmark = classify_symbol(symbol, config)
        if benchmark:
            bench_rows = load_kline(benchmark)
            if bench_rows:
                bench_by_date = build_date_index(bench_rows)
                bench_dates = sorted(bench_by_date.keys())
                if target in bench_by_date:
                    b_idx = bench_dates.index(target)
                    if b_idx >= 20:
                        try:
                            b_closes = [
                                float(bench_by_date[d]["close"])
                                for d in bench_dates[:b_idx + 1]
                            ]
                            bench_20d_ret = (
                                (b_closes[-1] - b_closes[-21]) / b_closes[-21]
                                if len(b_closes) >= 21 and b_closes[-21] > 0
                                else 0
                            )
                            if bench_20d_ret > 0.10:
                                reasons.append(f"Theme benchmark 20-day gain {bench_20d_ret:.1%}")
                                return "Theme rotation", "medium", reasons
                        except Exception:
                            pass
    except Exception:
        pass

    # --- Fallback ---
    return "Unclassified", "low", ["No rule matched"]


def auto_classify_all(force: bool = False) -> dict:
    """Auto classify all closed trades and save trade_setups.json."""
    if not CLOSED_TRADES_FILE.exists():
        return {"error": "No closed trades found. Run 'python app/cli.py match' first."}

    closed_trades = _store_read_json(CLOSED_TRADES_FILE, [])

    existing = {}
    if not force:
        existing = _store_read_json(TRADE_SETUPS_FILE, {})

    setups = dict(existing)
    classified = 0
    skipped = 0

    for trade in closed_trades:
        symbol = trade.get("symbol", "")
        open_date = trade.get("open_date", "")
        key = f"{symbol}_{open_date}"

        # Skip if already manually set
        if key in setups and setups[key].get("source") == "manual":
            skipped += 1
            continue

        setup_type, confidence, reasons = classify_setup(trade)

        setups[key] = {
            "symbol": symbol,
            "openDate": open_date,
            "setupType": setup_type,
            "confidence": confidence,
            "source": "auto",
            "reasons": reasons,
            "note": setups.get(key, {}).get("note", ""),
            "updatedAt": "",
        }
        classified += 1

    _atomic_write(TRADE_SETUPS_FILE, json.dumps(setups, ensure_ascii=False, indent=2))

    distribution = {}
    for s in setups.values():
        st = s.get("setupType", "Unclassified")
        distribution[st] = distribution.get(st, 0) + 1

    return {
        "classified": classified,
        "skipped": skipped,
        "total": len(closed_trades),
        "unique_keys": len(setups),
        "distribution": distribution,
    }


if __name__ == "__main__":
    result = auto_classify_all(force=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
