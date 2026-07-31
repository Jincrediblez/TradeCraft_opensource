#!/usr/bin/env python3
"""Style Drift Engine: detects behavioral drift across five dimensions.

Dimensions:
  1. Drift Score (composite)
  2. Theme Drift
  3. Setup Drift
  4. Position Size Drift
  5. Holding Period Drift
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
TRADES_FILE = STATE_DIR / "trades.json"
THEME_ATTRIBUTION_FILE = STATE_DIR / "theme_attribution.json"
STYLE_DRIFT_FILE = STATE_DIR / "style_drift.json"


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


def build_style_drift() -> dict:
    closed_trades = _read_json(CLOSED_TRADES_FILE, [])
    trades = _read_json(TRADES_FILE, [])
    theme_attribution = _read_json(THEME_ATTRIBUTION_FILE, [])

    if not closed_trades:
        return {"error": "No closed trades found"}

    config = load_themes_config()

    # Determine date ranges
    all_dates = sorted(set(t.get("close_date", "") for t in closed_trades if t.get("close_date")))
    recent_cutoff = all_dates[-30] if len(all_dates) >= 30 else (all_dates[0] if all_dates else "")

    historical_trades = closed_trades
    recent_trades = [t for t in closed_trades if t.get("close_date", "") >= recent_cutoff]

    # --- 1. Drift Score (composite from existing scoring engine) ---
    # We import the existing composite calculation
    try:
        from app.scoring_engine import (
            compute_behavior_radar,
            compute_period_metrics,
            score_asymmetric_exit,
            score_churn_friction,
            score_entry_quality,
            score_narrative_hype,
            score_stock_selection,
            score_tail_risk,
            score_theme_judgment,
            weighted_style_drift,
        )

        metrics = compute_period_metrics(trades, closed_trades)
        s1, _, _ = score_theme_judgment(theme_attribution, {})
        s2, _, _ = score_churn_friction(trades, closed_trades, metrics, {})
        s3, _, _ = score_stock_selection(theme_attribution, {})
        s4, _, _ = score_narrative_hype(trades, closed_trades, theme_attribution, {})
        s5, _, _ = score_entry_quality(trades, closed_trades, theme_attribution, {})
        s6, _, _ = score_asymmetric_exit(closed_trades, trades, {})
        s7, _, _ = score_tail_risk([], trades, {})

        drift_score = weighted_style_drift({
            "theme_judgment": s1,
            "churn_friction": s2,
            "stock_selection": s3,
            "narrative_hype": s4,
            "entry_quality": s5,
            "asymmetric_exit": s6,
            "tail_risk": s7,
        })
        drift_level = "high" if drift_score >= 60 else "medium" if drift_score >= 40 else "low"
    except Exception:
        drift_score = 0
        drift_level = "low"

    # --- 2. Theme Drift ---
    def _theme_stats(trade_list):
        stats = defaultdict(lambda: {"count": 0, "pnl": 0.0, "win_count": 0})
        for t in trade_list:
            symbol = t.get("symbol", "")
            theme, _, _ = classify_symbol(symbol, config)
            s = stats[theme]
            s["count"] += 1
            pnl = t.get("realized_pnl", 0)
            s["pnl"] += pnl
            if pnl > 0:
                s["win_count"] += 1
        return stats

    hist_theme = _theme_stats(historical_trades)
    recent_theme = _theme_stats(recent_trades)

    total_hist = len(historical_trades)
    total_recent = len(recent_trades)
    total_hist_pnl = sum(t.get("realized_pnl", 0) for t in historical_trades) or 1
    total_recent_pnl = sum(t.get("realized_pnl", 0) for t in recent_trades) or 1

    theme_drift = []
    all_themes = set(hist_theme.keys()) | set(recent_theme.keys())
    for theme in sorted(all_themes):
        h = hist_theme[theme]
        r = recent_theme[theme]

        hist_weight = (h["count"] / total_hist * 100) if total_hist > 0 else 0
        recent_weight = (r["count"] / total_recent * 100) if total_recent > 0 else 0
        weight_change = recent_weight - hist_weight

        hist_pnl_pct = (h["pnl"] / total_hist_pnl * 100)
        recent_pnl_pct = (r["pnl"] / total_recent_pnl * 100) if total_recent_pnl != 0 else 0
        pnl_change = recent_pnl_pct - hist_pnl_pct

        hist_wr = (h["win_count"] / h["count"] * 100) if h["count"] > 0 else 0
        recent_wr = (r["win_count"] / r["count"] * 100) if r["count"] > 0 else 0

        # Determine drift type
        if abs(weight_change) >= 15:
            drift_type = "concentration_up" if weight_change > 0 else "concentration_down"
        elif recent_wr < hist_wr - 15:
            drift_type = "quality_down"
        elif recent_wr > hist_wr + 15:
            drift_type = "quality_up"
        else:
            drift_type = "stable"

        theme_drift.append({
            "theme": theme,
            "recentWeightPct": round(recent_weight, 1),
            "historicalWeightPct": round(hist_weight, 1),
            "weightChangePct": round(weight_change, 1),
            "recentPnlContributionPct": round(recent_pnl_pct, 1),
            "historicalPnlContributionPct": round(hist_pnl_pct, 1),
            "pnlChangePct": round(pnl_change, 1),
            "recentWinRate": round(recent_wr, 1),
            "historicalWinRate": round(hist_wr, 1),
            "drift": drift_type,
        })

    theme_drift.sort(key=lambda x: abs(x["weightChangePct"]), reverse=True)

    # --- 3. Setup Drift ---
    def _setup_stats(trade_list):
        stats = defaultdict(lambda: {"count": 0, "pnl": 0.0, "win_count": 0})
        for t in trade_list:
            setup = t.get("setup_type", "").strip() or "未分类"
            s = stats[setup]
            s["count"] += 1
            pnl = t.get("realized_pnl", 0)
            s["pnl"] += pnl
            if pnl > 0:
                s["win_count"] += 1
        return stats

    hist_setup = _setup_stats(historical_trades)
    recent_setup = _setup_stats(recent_trades)

    setup_drift = []
    all_setups = set(hist_setup.keys()) | set(recent_setup.keys())
    for setup in sorted(all_setups):
        h = hist_setup[setup]
        r = recent_setup[setup]

        hist_freq = (h["count"] / total_hist * 100) if total_hist > 0 else 0
        recent_freq = (r["count"] / total_recent * 100) if total_recent > 0 else 0
        freq_change = recent_freq - hist_freq

        hist_wr = (h["win_count"] / h["count"] * 100) if h["count"] > 0 else 0
        recent_wr = (r["win_count"] / r["count"] * 100) if r["count"] > 0 else 0

        if abs(freq_change) >= 10 and recent_wr < hist_wr - 10:
            drift_type = "frequency_up_quality_down"
        elif abs(freq_change) >= 10:
            drift_type = "frequency_up" if freq_change > 0 else "frequency_down"
        elif recent_wr < hist_wr - 10:
            drift_type = "quality_down"
        elif recent_wr > hist_wr + 10:
            drift_type = "quality_up"
        else:
            drift_type = "stable"

        setup_drift.append({
            "setup": setup,
            "recentFreqPct": round(recent_freq, 1),
            "historicalFreqPct": round(hist_freq, 1),
            "freqChangePct": round(freq_change, 1),
            "recentWinRate": round(recent_wr, 1),
            "historicalWinRate": round(hist_wr, 1),
            "drift": drift_type,
        })

    setup_drift.sort(key=lambda x: abs(x["freqChangePct"]), reverse=True)

    # --- 4. Position Size Drift ---
    def _position_sizes(trade_list):
        return [
            t.get("quantity", 0) * t.get("avg_entry", 0)
            for t in trade_list
        ]

    hist_sizes = _position_sizes(historical_trades)
    recent_sizes = _position_sizes(recent_trades)

    hist_avg_size = _avg(hist_sizes)
    recent_avg_size = _avg(recent_sizes)
    size_change = (
        (recent_avg_size / hist_avg_size - 1) * 100
        if hist_avg_size > 0 else 0
    )

    # Max single position concentration (from open positions, not available here)
    max_single_pct = 0

    pos_drift = {
        "recentAvgSize": round(recent_avg_size, 2),
        "historicalAvgSize": round(hist_avg_size, 2),
        "changePct": round(size_change, 1),
        "maxSinglePositionPct": round(max_single_pct, 1),
        "drift": (
            "size_up" if size_change > 30
            else "size_down" if size_change < -30
            else "stable"
        ),
    }

    # --- 5. Holding Period Drift ---
    hist_holds = [t.get("holding_days", 0) for t in historical_trades if t.get("holding_days", 0) > 0]
    recent_holds = [t.get("holding_days", 0) for t in recent_trades if t.get("holding_days", 0) > 0]

    hist_median = _median(hist_holds)
    recent_median = _median(recent_holds)
    hold_change = (
        (recent_median / hist_median - 1) * 100
        if hist_median > 0 else 0
    )

    hist_win_holds = [t.get("holding_days", 0) for t in historical_trades if t.get("realized_pnl", 0) > 0 and t.get("holding_days", 0) > 0]
    hist_loss_holds = [t.get("holding_days", 0) for t in historical_trades if t.get("realized_pnl", 0) < 0 and t.get("holding_days", 0) > 0]
    winner_loser_ratio = (
        _avg(hist_loss_holds) / _avg(hist_win_holds)
        if _avg(hist_win_holds) > 0 else 0
    )

    hold_drift = {
        "recentMedianDays": round(recent_median, 1),
        "historicalMedianDays": round(hist_median, 1),
        "changePct": round(hold_change, 1),
        "winnerAvgDays": round(_avg(hist_win_holds), 1),
        "loserAvgDays": round(_avg(hist_loss_holds), 1),
        "winnerLoserRatio": round(winner_loser_ratio, 2),
        "drift": (
            "compression" if hold_change < -30
            else "extension" if hold_change > 30
            else "stable"
        ),
    }

    # --- Drift Alerts ---
    alerts = []

    for td in theme_drift:
        if td["drift"] == "concentration_up" and abs(td["weightChangePct"]) >= 20:
            alerts.append({
                "severity": "medium",
                "dimension": "theme",
                "text": f"{td['theme']} 集中度从 {td['historicalWeightPct']:.0f}% 升至 {td['recentWeightPct']:.0f}%（+{td['weightChangePct']:.0f}pp）",
            })
        elif td["drift"] == "quality_down":
            alerts.append({
                "severity": "high",
                "dimension": "theme",
                "text": f"{td['theme']} 胜率从 {td['historicalWinRate']:.0f}% 降至 {td['recentWinRate']:.0f}%",
            })

    for sd in setup_drift:
        if sd["drift"] == "frequency_up_quality_down":
            alerts.append({
                "severity": "high",
                "dimension": "setup",
                "text": f"{sd['setup']} 使用频率上升 {sd['freqChangePct']:+.0f}pp，但胜率从 {sd['historicalWinRate']:.0f}% 降至 {sd['recentWinRate']:.0f}%",
            })
        elif sd["drift"] == "frequency_up" and abs(sd["freqChangePct"]) >= 15:
            alerts.append({
                "severity": "medium",
                "dimension": "setup",
                "text": f"{sd['setup']} 使用频率从 {sd['historicalFreqPct']:.0f}% 升至 {sd['recentFreqPct']:.0f}%",
            })

    if pos_drift["drift"] == "size_up":
        alerts.append({
            "severity": "medium",
            "dimension": "position_size",
            "text": f"平均仓位从 ${pos_drift['historicalAvgSize']:,.0f} 增至 ${pos_drift['recentAvgSize']:,.0f}（+{pos_drift['changePct']:.0f}%）",
        })

    if hold_drift["drift"] == "compression":
        alerts.append({
            "severity": "medium",
            "dimension": "holding_period",
            "text": f"持仓周期中位数从 {hold_drift['historicalMedianDays']:.0f} 天压缩至 {hold_drift['recentMedianDays']:.0f} 天",
        })
    elif hold_drift["winnerLoserRatio"] > 1.5:
        alerts.append({
            "severity": "medium",
            "dimension": "holding_period",
            "text": f"亏损单平均持有 {hold_drift['loserAvgDays']:.0f} 天，盈利单仅 {hold_drift['winnerAvgDays']:.0f} 天",
        })

    alerts.sort(key=lambda a: {"high": 0, "medium": 1, "low": 2}.get(a["severity"], 3))

    payload = {
        "generatedAt": "",
        "period": "ytd",
        "driftScore": {"score": drift_score, "level": drift_level},
        "themeDrift": theme_drift,
        "setupDrift": setup_drift,
        "positionSizeDrift": pos_drift,
        "holdingPeriodDrift": hold_drift,
        "driftAlerts": alerts,
    }

    _write_json(STYLE_DRIFT_FILE, payload)
    return payload


if __name__ == "__main__":
    import sys
    ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(ROOT))
    result = build_style_drift()
    if "error" in result:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Drift Score: {result['driftScore']['score']}/100 ({result['driftScore']['level']})")
        print(f"Alerts: {len(result['driftAlerts'])}")
        for a in result["driftAlerts"][:5]:
            print(f"  [{a['severity']}] {a['text']}")
        print(f"\nTheme drift count: {len(result['themeDrift'])}")
        print(f"Setup drift count: {len(result['setupDrift'])}")
