#!/usr/bin/env python3
"""Trader Memory Engine: aggregates closed trades by memory tag (theme + setup).

Each memory tag tracks win rate, expectancy, hold time, position size,
and trend vs recent history.
"""

import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from app.theme_classifier import classify_symbol, load_themes_config


try:  # normal package import (server, cli, pytest)
    from app.state_store import read_json as _store_read_json, atomic_write_text as _atomic_write
except ImportError:  # standalone script execution puts app/ on sys.path
    from state_store import read_json as _store_read_json, atomic_write_text as _atomic_write

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "state"
CLOSED_TRADES_FILE = STATE_DIR / "closed_trades.json"
THEME_ATTRIBUTION_FILE = STATE_DIR / "theme_attribution.json"
TRADER_MEMORY_FILE = STATE_DIR / "trader_memory.json"


def _read_json(path: Path, default):
    return _store_read_json(path, default)


def _write_json(path: Path, payload) -> None:
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _median(values: List[float]) -> float:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


def _avg(values: List[float]) -> float:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def build_memory() -> dict:
    closed_trades = _read_json(CLOSED_TRADES_FILE, [])
    if not closed_trades:
        return {"error": "No closed trades found"}

    config = load_themes_config()

    # Determine recent cutoff: last 30 trading days
    all_dates = sorted(set(t.get("close_date", "") for t in closed_trades if t.get("close_date")))
    recent_cutoff = all_dates[-30] if len(all_dates) >= 30 else (all_dates[0] if all_dates else "")

    # Group trades by memory tag
    tag_trades: Dict[str, List[dict]] = defaultdict(list)
    for trade in closed_trades:
        symbol = trade.get("symbol", "")
        theme, tier, _ = classify_symbol(symbol, config)
        setup = trade.get("setup_type", "").strip() or "Unclassified"
        tag = f"{theme} + {setup}"
        tag_trades[tag].append({
            **trade,
            "_theme": theme,
            "_tier": tier,
            "_setup": setup,
        })

    memories = []
    for tag, trades in tag_trades.items():
        theme = trades[0]["_theme"]
        tier = trades[0]["_tier"]
        setup = trades[0]["_setup"]

        # Basic stats
        pnl_values = [t.get("realized_pnl", 0) for t in trades]
        win_count = sum(1 for p in pnl_values if p > 0)
        loss_count = sum(1 for p in pnl_values if p < 0)
        trade_count = len(trades)
        win_rate = (win_count / trade_count * 100) if trade_count > 0 else 0

        # PnL
        total_pnl = sum(pnl_values)
        avg_pnl = _avg(pnl_values)
        wins = [p for p in pnl_values if p > 0]
        losses = [p for p in pnl_values if p < 0]
        avg_win = _avg(wins) if wins else 0
        avg_loss = _avg([abs(p) for p in losses]) if losses else 0

        # Expectancy (as % of avg position size)
        position_sizes = [
            t.get("quantity", 0) * t.get("avg_entry", 0)
            for t in trades
        ]
        avg_position_size = _avg(position_sizes) if position_sizes else 0

        if avg_position_size > 0 and trade_count > 0:
            expectancy = (
                (win_count / trade_count) * avg_win
                - (loss_count / trade_count) * avg_loss
            ) / avg_position_size
        else:
            expectancy = 0.0

        # Hold time
        hold_days = [t.get("holding_days", 0) for t in trades if t.get("holding_days", 0) > 0]
        avg_hold = _avg(hold_days)
        median_hold = _median(hold_days)

        # R-multiple
        r_values = [
            float(t.get("r_multiple"))
            for t in trades
            if t.get("r_multiple") is not None
        ]
        avg_r = _avg(r_values) if r_values else None

        # Max drawdown (worst realized PnL as % of entry cost)
        drawdowns = []
        for t in trades:
            entry = t.get("avg_entry", 0)
            pnl = t.get("realized_pnl", 0)
            qty = t.get("quantity", 0)
            if entry > 0 and qty > 0:
                dd = pnl / (entry * qty)
                drawdowns.append(dd)
        max_dd = min(drawdowns) if drawdowns else 0

        # Commission
        commissions = [t.get("commissions", 0) for t in trades]
        total_commission = sum(commissions)

        # Trend: recent 30d vs historical
        recent_trades = [t for t in trades if t.get("close_date", "") >= recent_cutoff]
        recent_count = len(recent_trades)
        recent_pnl = [t.get("realized_pnl", 0) for t in recent_trades]
        recent_win_count = sum(1 for p in recent_pnl if p > 0)
        recent_win_rate = (recent_win_count / recent_count * 100) if recent_count > 0 else 0

        freq_change = ""
        if trade_count > 0:
            historical_daily = trade_count / max(len(all_dates), 1)
            recent_daily = recent_count / 30 if recent_count > 0 else 0
            if historical_daily > 0:
                change = (recent_daily / historical_daily - 1) * 100
                freq_change = f"{change:+.0f}%"

        # Confidence
        if trade_count >= 15:
            confidence = "high"
        elif trade_count >= 8:
            confidence = "medium"
        else:
            confidence = "low"

        memories.append({
            "memoryTag": tag,
            "theme": theme,
            "setup": setup,
            "tier": tier,
            "stats": {
                "tradeCount": trade_count,
                "winCount": win_count,
                "lossCount": loss_count,
                "winRate": round(win_rate, 1),
                "totalRealizedPnl": round(total_pnl, 2),
                "avgRealizedPnl": round(avg_pnl, 2),
                "avgWin": round(avg_win, 2),
                "avgLoss": round(avg_loss, 2),
                "expectancy": round(expectancy, 4),
                "avgHoldDays": round(avg_hold, 1),
                "medianHoldDays": round(median_hold, 1),
                "avgPositionSize": round(avg_position_size, 2),
                "maxDrawdownPct": round(max_dd * 100, 2),
                "avgRMultiple": round(avg_r, 2) if avg_r is not None else None,
                "bestR": round(max(r_values), 2) if r_values else None,
                "worstR": round(min(r_values), 2) if r_values else None,
                "totalCommission": round(total_commission, 2),
            },
            "trend": {
                "recentTradeCount": recent_count,
                "recentWinRate": round(recent_win_rate, 1),
                "frequencyChange": freq_change,
            },
            "confidence": confidence,
        })

    # Sort by total realized PnL desc
    memories.sort(key=lambda m: m["stats"]["totalRealizedPnl"], reverse=True)

    payload = {
        "generatedAt": "",
        "period": "ytd",
        "tradeCount": len(closed_trades),
        "tagCount": len(memories),
        "memories": memories,
    }

    _write_json(TRADER_MEMORY_FILE, payload)
    return payload


if __name__ == "__main__":
    import sys
    ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(ROOT))
    result = build_memory()
    if "error" in result:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Memory tags: {result['tagCount']}")
        for m in result["memories"][:5]:
            s = m["stats"]
            print(f"  {m['memoryTag']}: {s['tradeCount']} trades, win rate {s['winRate']}%, "
                  f"PnL=${s['totalRealizedPnl']:,.0f}, Exp={s['expectancy']:.2f}, "
                  f"conf={m['confidence']}")
