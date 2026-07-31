#!/usr/bin/env python3
"""Seven-dimension trading quality scoring engine.

Scores are 0-100, higher = more problematic (critique-oriented).
"""

import json
import math
import os
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml


try:  # normal package import (server, cli, pytest)
    from app.state_store import read_json as _store_read_json, atomic_write_text as _atomic_write
    from app.kline_cache import get_kline, get_kline as load_kline
    from app.fomo import compute_fomo_score, fomo_level
except ImportError:  # standalone: `python app/scoring_engine.py` puts app/ on sys.path
    from state_store import read_json as _store_read_json, atomic_write_text as _atomic_write
    from kline_cache import get_kline, get_kline as load_kline
    from fomo import compute_fomo_score, fomo_level

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "state"
CONFIG_DIR = ROOT / "config"

TRADES_FILE = STATE_DIR / "trades.json"
CLOSED_TRADES_FILE = STATE_DIR / "closed_trades.json"
OPEN_POSITIONS_FILE = STATE_DIR / "open_positions.json"
THEME_ATTRIBUTION_FILE = STATE_DIR / "theme_attribution.json"
AUDIT_SCORES_FILE = STATE_DIR / "audit_scores.json"
AUDIT_SUMMARY_FILE = STATE_DIR / "audit_summary.json"
AUDIT_COVERAGE_FILE = STATE_DIR / "audit_coverage.json"
MTM_FILE = STATE_DIR / "mtm.json"
STATEMENT_SNAPSHOT_FILE = STATE_DIR / "statement_snapshot.json"

CONFIG_PATH = CONFIG_DIR / "audit.yaml"
THEMES_CONFIG_PATH = CONFIG_DIR / "themes.yaml"

STYLE_DRIFT_WEIGHTS = {
    "theme_judgment": 8 / 88,
    "churn_friction": 18 / 88,
    "stock_selection": 18 / 88,
    "narrative_hype": 8 / 88,
    "entry_quality": 14 / 88,
    "asymmetric_exit": 8 / 88,
    "tail_risk": 14 / 88,
}


def weighted_style_drift(values: Dict[str, float]) -> int:
    return round(sum(float(values.get(key, 0) or 0) * weight for key, weight in STYLE_DRIFT_WEIGHTS.items()))


def read_json(path: Path, default):
    return _store_read_json(path, default)


def write_json(path: Path, payload) -> None:
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _load_yaml(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError as exc:
        raise RuntimeError(f"缺少配置文件 {path.name}，请确认 config/ 目录完整。") from exc
    except yaml.YAMLError as exc:
        raise RuntimeError(f"配置文件 {path.name} 解析失败：{exc}") from exc


def load_config() -> dict:
    return _load_yaml(CONFIG_PATH)


def load_themes_config() -> dict:
    return _load_yaml(THEMES_CONFIG_PATH)


def parse_date(date_str: str) -> datetime:
    if len(date_str) == 8:
        return datetime.strptime(date_str, "%Y%m%d")
    return datetime.strptime(date_str, "%Y-%m-%d")


# ------------------------------------------------------------------
# Raw metric helpers
# ------------------------------------------------------------------

def compute_daily_trade_counts(trades: List[dict]) -> Dict[str, int]:
    counts = defaultdict(int)
    for t in trades:
        counts[t["trade_date"]] += 1
    return dict(counts)


def compute_period_metrics(trades: List[dict], closed_trades: List[dict], days: int = 20):
    """Compute metrics for recent N trading days vs YTD baseline."""
    if not trades:
        return {
            "r_multiple_count": 0,
            "avg_r_multiple": None,
            "best_r_multiple": None,
            "worst_r_multiple": None,
            "risk_missing_count": 0,
        }

    trade_dates = sorted(set(t["trade_date"] for t in trades))
    all_days = len(trade_dates)
    recent_cutoff = trade_dates[-days] if len(trade_dates) >= days else trade_dates[0]

    recent_trades = [t for t in trades if t["trade_date"] >= recent_cutoff]
    recent_closed = [c for c in closed_trades if c["close_date"] >= recent_cutoff]

    ytd_daily_avg = len(trades) / max(all_days, 1)
    recent_daily_avg = len(recent_trades) / max(days, 1)

    # Holding period
    ytd_holding_days = [c["holding_days"] for c in closed_trades if c["holding_days"] > 0]
    recent_holding_days = [c["holding_days"] for c in recent_closed if c["holding_days"] > 0]
    ytd_median_hold = (sorted(ytd_holding_days)[len(ytd_holding_days) // 2]) if ytd_holding_days else 0
    recent_median_hold = (sorted(recent_holding_days)[len(recent_holding_days) // 2]) if recent_holding_days else 0

    # Round trips
    symbol_buy_dates = defaultdict(list)
    symbol_sell_dates = defaultdict(list)
    for t in recent_trades:
        if t["side"] == "BUY":
            symbol_buy_dates[t["symbol_lb"]].append(parse_date(t["trade_date"]))
        else:
            symbol_sell_dates[t["symbol_lb"]].append(parse_date(t["trade_date"]))

    round_trips_3d = 0
    for symbol, sells in symbol_sell_dates.items():
        buys = symbol_buy_dates.get(symbol, [])
        for sell in sells:
            for buy in buys:
                delta = (sell - buy).days
                if 0 <= delta <= 3:
                    round_trips_3d += 1

    # Friction cost estimate: sum commissions / abs(realized PnL)
    ytd_commissions = sum(t.get("commission", 0) for t in trades)
    ytd_realized = sum(c["realized_pnl"] for c in closed_trades)
    friction_ratio = (ytd_commissions / abs(ytd_realized)) if ytd_realized != 0 else 0

    r_values = [
        float(c.get("r_multiple"))
        for c in closed_trades
        if c.get("r_multiple") is not None
    ]
    risk_missing_count = sum(
        1 for c in closed_trades
        if c.get("risk_status") in {None, "", "risk_missing", "invalid_risk"}
    )

    return {
        "ytd_trade_count": len(trades),
        "ytd_days": all_days,
        "ytd_daily_avg": round(ytd_daily_avg, 2),
        "recent_trade_count": len(recent_trades),
        "recent_daily_avg": round(recent_daily_avg, 2),
        "ytd_median_hold_days": ytd_median_hold,
        "recent_median_hold_days": recent_median_hold,
        "round_trips_3d": round_trips_3d,
        "ytd_commissions": round(ytd_commissions, 2),
        "friction_ratio_pct": round(friction_ratio * 100, 2),
        "r_multiple_count": len(r_values),
        "avg_r_multiple": round(sum(r_values) / len(r_values), 2) if r_values else None,
        "best_r_multiple": round(max(r_values), 2) if r_values else None,
        "worst_r_multiple": round(min(r_values), 2) if r_values else None,
        "risk_missing_count": risk_missing_count,
    }


# ------------------------------------------------------------------
# K-line helpers for FOMO
# ------------------------------------------------------------------

def price_on_or_before(symbol_lb: str, date_str: str) -> Optional[float]:
    target = date_str.replace("-", "")
    price = None
    for row in get_kline(symbol_lb):
        day = str(row.get("time", ""))[:10].replace("-", "")
        if day <= target:
            try:
                price = float(row.get("close", 0))
            except Exception:
                pass
        else:
            break
    return price if price and price > 0 else None


def fomo_score_for_buy(trade: dict) -> Optional[dict]:
    """Compute FOMO score for a single BUY trade. Returns dict or None."""
    symbol = trade["symbol_lb"]
    entry_date = trade["trade_date"]
    entry_price = trade["price"]

    result = compute_fomo_score(get_kline(symbol), entry_date, entry_price)
    if result is None:
        return None

    score = result["score"]
    ma20 = result["ma20"]
    return {
        "symbol": symbol,
        "date": entry_date,
        "score": score,
        "level": fomo_level(score),
        "reasons": result["reasons"],
        "ret5": round(result["ret5"], 4),
        "ret10": round(result["ret10"], 4),
        "entry_price": entry_price,
        "ma20": round(ma20, 4) if ma20 else None,
    }


# ------------------------------------------------------------------
# Six dimension scores
# ------------------------------------------------------------------

def score_theme_judgment(theme_attribution: List[dict], config: dict) -> tuple:
    """Theme Judgment Score: 0-100."""
    score = 0
    evidence = []
    cfg = config.get("thresholds", {}).get("theme_judgment", {})

    # Find worst performing themes
    for attr in theme_attribution:
        theme_return = attr.get("avg_theme_return_pct")
        user_return = attr.get("avg_trader_return_pct")
        pnl = attr.get("realized_pnl", 0)

        if theme_return is not None and user_return is not None:
            if user_return < theme_return * cfg.get("theme_vs_benchmark_ratio", 0.5):
                score += 20
                evidence.append(f"{attr['theme']}: 收益 {user_return:.1f}% 远低于主题基准 {theme_return:.1f}%")
            if user_return < 0 and theme_return > 0:
                score += 15
                evidence.append(f"{attr['theme']}: 主题涨 {theme_return:.1f}% 但你亏损")

    # Unclassified loss contribution
    unclass = next((a for a in theme_attribution if a["theme"] == "unclassified"), None)
    if unclass:
        total_loss = sum(min(a["realized_pnl"], 0) for a in theme_attribution)
        if total_loss < 0:
            unclass_loss = min(unclass["realized_pnl"], 0)
            pct = abs(unclass_loss) / abs(total_loss) * 100
            threshold = cfg.get("unclassified_loss_contribution_pct", 30)
            if pct > threshold:
                score += 20
                evidence.append(f"未分类标的亏损占总亏损 {pct:.1f}%")

    score = min(score, 100)
    level = "high" if score >= 60 else "medium" if score >= 40 else "low"
    return score, level, evidence


def score_churn_friction(trades: List[dict], closed_trades: List[dict], metrics: dict, config: dict) -> tuple:
    """Churn Friction Score: 0-100."""
    score = 0
    evidence = []
    cfg = config.get("thresholds", {}).get("overtrading", {})

    ytd_avg = metrics.get("ytd_daily_avg", 0)
    recent_avg = metrics.get("recent_daily_avg", 0)

    # Frequency spike
    if ytd_avg > 0 and recent_avg > ytd_avg * 2:
        score += 25
        evidence.append(f"近20日日均 {recent_avg:.1f} 次，年初均值 {ytd_avg:.1f} 次")
    elif ytd_avg > 0 and recent_avg > ytd_avg * 1.5:
        score += 15
        evidence.append(f"交易频率上升 {((recent_avg/ytd_avg - 1) * 100):.0f}%")

    # Friction cost
    friction = metrics.get("friction_ratio_pct", 0)
    if friction > cfg.get("friction_cost_pct", 15):
        score += 25
        evidence.append(f"摩擦成本占实现盈亏 {friction:.1f}%")
    elif friction > 8:
        score += 12
        evidence.append(f"摩擦成本占实现盈亏 {friction:.1f}%")

    # Holding period compression
    ytd_hold = metrics.get("ytd_median_hold_days", 0)
    recent_hold = metrics.get("recent_median_hold_days", 0)
    if ytd_hold > 0 and recent_hold < ytd_hold * (1 - cfg.get("holding_days_compression_pct", 50) / 100):
        score += 20
        evidence.append(f"持仓周期中位数从 {ytd_hold} 天压缩至 {recent_hold} 天")

    # Round trips
    rt = metrics.get("round_trips_3d", 0)
    if rt > cfg.get("round_trip_3d_threshold", 5):
        score += 15
        evidence.append(f"3日内反复交易 {rt} 次")

    score = min(score, 100)
    level = "high" if score >= 60 else "medium" if score >= 40 else "low"
    return score, level, evidence


def score_narrative_hype(trades: List[dict], closed_trades: List[dict], theme_attribution: List[dict], config: dict) -> tuple:
    """Narrative Hype Score: 0-100."""
    score = 0
    evidence = []
    cfg = config.get("thresholds", {}).get("optionality", {})

    themes_cfg = load_themes_config()

    # Optionality tier stats
    optionality_attrs = [a for a in theme_attribution if a.get("tier") == "optionality"]
    total_notional = sum(a.get("total_notional", 0) for a in theme_attribution) or 1
    total_loss = sum(min(a.get("realized_pnl", 0), 0) for a in theme_attribution)

    opt_notional = sum(a.get("total_notional", 0) for a in optionality_attrs)
    opt_loss = sum(min(a.get("realized_pnl", 0), 0) for a in optionality_attrs)

    opt_notional_pct = opt_notional / total_notional * 100
    if opt_notional_pct > cfg.get("max_optionality_notional_pct", 25):
        score += 20
        evidence.append(f"Optionality tier 交易金额占比 {opt_notional_pct:.1f}%")

    if total_loss < 0:
        opt_loss_pct = abs(opt_loss) / abs(total_loss) * 100
        if opt_loss_pct > cfg.get("max_optionality_loss_contribution_pct", 40):
            score += 30
            evidence.append(f"Optionality tier 亏损占总亏损 {opt_loss_pct:.1f}%")

    # Optionality win rate
    for attr in optionality_attrs:
        if attr.get("trade_count", 0) > 3:
            # Approximate win rate from realized_pnl sign vs trade count
            # We don't have per-trade PnL in attribution, so use rough heuristic
            avg_pnl = attr.get("realized_pnl", 0) / max(attr.get("trade_count", 1), 1)
            if avg_pnl < 0:
                score += 15
                evidence.append(f"{attr['theme']}: 平均每笔亏损 ${abs(avg_pnl):.0f}")

    score = min(score, 100)
    level = "high" if score >= 60 else "medium" if score >= 40 else "low"
    return score, level, evidence


def score_stock_selection(theme_attribution: List[dict], config: dict) -> tuple:
    """Stock Selection Score: 0-100. Higher = stock picking weaker than benchmark/theme."""
    score = 0
    evidence = []
    cfg = config.get("thresholds", {}).get("stock_selection", {})

    alpha_threshold_pct = cfg.get("negative_selection_alpha_pct", -3.0)
    loss_contrib_threshold_pct = cfg.get("selection_loss_contribution_pct", 40.0)
    weak_theme_threshold = cfg.get("weak_theme_trade_count", 3)

    total_loss = sum(min(a.get("realized_pnl", 0), 0) for a in theme_attribution)

    for attr in theme_attribution:
        sel = attr.get("avg_selection_alpha_pct")
        pnl = attr.get("realized_pnl", 0)
        tc = attr.get("trade_count", 0)

        if sel is None:
            continue

        # Negative selection alpha on a theme where you lost money
        if sel < alpha_threshold_pct and pnl < 0:
            score += 20
            evidence.append(
                f"{attr['theme']}: 选股α {sel:.1f}%，实现盈亏 {pnl:,.0f}"
            )
        elif sel < alpha_threshold_pct and tc >= weak_theme_threshold:
            score += 12
            evidence.append(f"{attr['theme']}: 选股α {sel:.1f}%（{tc} 笔）")

        # Large loss contribution from poor stock selection
        if total_loss < 0 and pnl < 0 and sel < 0:
            loss_contrib = abs(pnl) / abs(total_loss) * 100
            if loss_contrib > loss_contrib_threshold_pct:
                score += 20
                evidence.append(
                    f"{attr['theme']}: 选股劣势导致亏损占总亏损 {loss_contrib:.1f}%"
                )

    # Unclassified / lack of clear theme hurts selection discipline
    unclass = next((a for a in theme_attribution if a["theme"] == "unclassified"), None)
    if unclass:
        unclass_pnl = unclass.get("realized_pnl", 0)
        unclass_sel = unclass.get("avg_selection_alpha_pct")
        if unclass_sel is not None and unclass_sel < alpha_threshold_pct:
            score += 15
            evidence.append(f"未分类标的选股α {unclass_sel:.1f}%")
        elif unclass_pnl < 0 and unclass.get("trade_count", 0) >= weak_theme_threshold:
            score += 10
            evidence.append(f"未分类标的亏损 {unclass_pnl:,.0f}（{unclass['trade_count']} 笔）")

    score = min(score, 100)
    level = "high" if score >= 60 else "medium" if score >= 40 else "low"
    return score, level, evidence


def score_entry_quality(trades: List[dict], closed_trades: List[dict], theme_attribution: List[dict], config: dict) -> tuple:
    """Entry Quality Score: 0-100."""
    score = 0
    evidence = []
    cfg = config.get("thresholds", {}).get("fomo", {})

    buy_trades = [t for t in trades if t["side"] == "BUY"]
    fomo_results = []
    for t in buy_trades:
        fomo = fomo_score_for_buy(t)
        if fomo:
            fomo_results.append(fomo)

    if fomo_results:
        high_fomo = [f for f in fomo_results if f["score"] > cfg.get("high_fomo_threshold", 70)]
        high_fomo_pct = len(high_fomo) / len(fomo_results) * 100
        if high_fomo_pct > cfg.get("high_fomo_trade_pct", 20):
            score += 25
            evidence.append(f"高FOMO买入占比 {high_fomo_pct:.1f}%")
        elif high_fomo_pct > 10:
            score += 12
            evidence.append(f"高FOMO买入占比 {high_fomo_pct:.1f}%")

        # Top FOMO trades
        top_fomo = sorted(fomo_results, key=lambda x: x["score"], reverse=True)[:5]
        for tf in top_fomo:
            if tf["score"] > 60:
                evidence.append(f"{tf['symbol']} {tf['date']}: FOMO {tf['score']}, {', '.join(tf['reasons'])}")

    # Post-entry drawdown analysis on closed trades
    drawdown_trades = []
    for ct in closed_trades:
        if ct.get("realized_pnl", 0) < 0:
            entry = ct["avg_entry"]
            exit_p = ct["avg_exit"]
            if entry > 0:
                dd = (exit_p - entry) / entry
                drawdown_trades.append(dd)

    if drawdown_trades:
        bad_dd_pct = sum(1 for d in drawdown_trades if d < -0.10) / len(drawdown_trades) * 100
        if bad_dd_pct > cfg.get("post_entry_drawdown_trade_pct", 30):
            score += 20
            evidence.append(f"买入后回撤>10%的交易占比 {bad_dd_pct:.1f}%")

    score = min(score, 100)
    level = "high" if score >= 60 else "medium" if score >= 40 else "low"
    return score, level, evidence


def score_asymmetric_exit(closed_trades: List[dict], trades: List[dict], config: dict) -> tuple:
    """Asymmetric Exit Score: 0-100."""
    score = 0
    evidence = []
    cfg = config.get("thresholds", {}).get("sell_fly", {})

    # Sell-fly analysis: for each sell, check post-exit performance
    # We need kline data for the symbol after exit date
    sell_flies = []
    for ct in closed_trades:
        symbol = ct["symbol"]
        exit_date = ct["close_date"]
        rows = get_kline(symbol)
        if not rows:
            continue

        by_date = {str(r.get("time", ""))[:10].replace("-", ""): r for r in rows}
        dates = sorted(by_date.keys())
        if exit_date not in dates:
            continue
        idx = dates.index(exit_date)

        # Look at 20 days after exit
        future_dates = dates[idx + 1:idx + 21]
        if len(future_dates) < 5:
            continue

        try:
            exit_px = float(by_date[exit_date]["close"])
            max_future_px = max(float(by_date[d]["close"]) for d in future_dates)
        except Exception:
            continue

        if exit_px > 0:
            upside = (max_future_px - exit_px) / exit_px
            if upside > 0.10:
                sell_flies.append({
                    "symbol": symbol,
                    "exit_date": exit_date,
                    "missed_upside_pct": round(upside * 100, 2),
                    "missed_pnl": round((max_future_px - exit_px) * ct.get("quantity", 0), 2),
                })

    if closed_trades:
        fly_ratio = len(sell_flies) / len(closed_trades)
        if fly_ratio > cfg.get("sell_fly_ratio_threshold", 0.25):
            score += 25
            evidence.append(f"卖飞交易占比 {fly_ratio:.1%}")
        elif fly_ratio > 0.15:
            score += 12

        total_missed_pnl = sum(f["missed_pnl"] for f in sell_flies)
        total_pnl = sum(ct.get("realized_pnl", 0) for ct in closed_trades)
        threshold = cfg.get("missed_upside_vs_pnl_threshold", 0.20)
        if total_pnl > 0 and total_missed_pnl > abs(total_pnl) * threshold:
            score += 25
            evidence.append(f"估算卖飞少赚 ${total_missed_pnl:,.0f}，超过已实现盈利 {threshold:.0%}")

    # Loser holding vs winner holding asymmetry
    winner_hold = [c["holding_days"] for c in closed_trades if c.get("realized_pnl", 0) > 0]
    loser_hold = [c["holding_days"] for c in closed_trades if c.get("realized_pnl", 0) < 0]
    if winner_hold and loser_hold:
        avg_winner = sum(winner_hold) / len(winner_hold)
        avg_loser = sum(loser_hold) / len(loser_hold)
        if avg_loser > avg_winner * cfg.get("loser_holding_ratio", 1.5):
            score += 20
            evidence.append(f"亏损持仓平均 {avg_loser:.0f} 天，盈利仅 {avg_winner:.0f} 天")

    # Loser add-on detection: buy while current close is below running avg cost.
    lots_by_symbol = defaultdict(list)
    loser_adds = 0
    for t in sorted(trades, key=lambda x: (x["trade_date"], x["trade_time"])):
        sym = t["symbol_lb"]
        if t["side"] == "BUY":
            lots = lots_by_symbol[sym]
            total_qty = sum(l["quantity"] for l in lots)
            if total_qty > 0:
                avg_cost = sum(l["quantity"] * l["price"] for l in lots) / total_qty
                current_price = price_on_or_before(sym, t["trade_date"])
                if current_price is not None and current_price < avg_cost:
                    loser_adds += 1
            lots.append({"quantity": abs(t.get("quantity", 0)), "price": t.get("price", 0)})
        else:
            remaining = abs(t.get("quantity", 0))
            lots = lots_by_symbol[sym]
            while remaining > 1e-9 and lots:
                lot = lots[0]
                matched = min(lot["quantity"], remaining)
                lot["quantity"] -= matched
                remaining -= matched
                if lot["quantity"] <= 1e-9:
                    lots.pop(0)

    if loser_adds > cfg.get("max_loser_add_count", 3):
        score += 15
        evidence.append(f"亏损状态下加仓 {loser_adds} 次")

    score = min(score, 100)
    level = "high" if score >= 60 else "medium" if score >= 40 else "low"
    return score, level, evidence


def score_tail_risk(open_positions: List[dict], trades: List[dict], config: dict) -> tuple:
    """Tail Risk Score: 0-100."""
    score = 0
    evidence = []
    cfg = config.get("thresholds", {}).get("margin", {})

    # Margin detection from cash negative (we don't have exact margin data,
    # but we can infer from Activity Statement if available)
    # For now, use a heuristic: if there are large buy trades that exceed
    # the total sell proceeds significantly
    total_buy = sum(t.get("gross_amount", 0) for t in trades if t["side"] == "BUY")
    total_sell = sum(t.get("gross_amount", 0) for t in trades if t["side"] == "SELL")
    net_invested = total_buy - total_sell

    # Concentration
    if open_positions:
        total_value = sum(p.get("quantity", 0) * p.get("avg_cost", 0) for p in open_positions)
        if total_value > 0:
            sorted_pos = sorted(open_positions, key=lambda p: p.get("quantity", 0) * p.get("avg_cost", 0), reverse=True)
            top3_value = sum(p["quantity"] * p["avg_cost"] for p in sorted_pos[:3])
            top3_pct = top3_value / total_value * 100
            if top3_pct > cfg.get("max_top3_concentration_pct", 70):
                score += 15
                evidence.append(f"前3大持仓集中度 {top3_pct:.1f}%")

            # Single theme concentration (rough: use symbol grouping)
            themes_cfg = load_themes_config()
            from app.theme_classifier import classify_symbol
            theme_values = defaultdict(float)
            for p in open_positions:
                theme, _, _ = classify_symbol(p["symbol"], themes_cfg)
                theme_values[theme] += p["quantity"] * p["avg_cost"]
            if theme_values:
                max_theme_pct = max(v / total_value * 100 for v in theme_values.values())
                if max_theme_pct > cfg.get("max_single_theme_pct", 50):
                    score += 20
                    evidence.append(f"单一主题集中度 {max_theme_pct:.1f}%")

    # Estimated drawdown impact
    # Rough estimate: assume all positions drop 30% (2022-style tech drawdown)
    est_loss = sum(p.get("quantity", 0) * p.get("avg_cost", 0) * 0.3 for p in open_positions)
    # We don't have total NAV, so skip NAV-based threshold for now

    # Margin usage flag (placeholder - needs Activity Statement cash data)
    mtm = read_json(STATE_DIR / "mtm.json", {})
    cash_negative = False
    if isinstance(mtm, dict):
        # Check if there's cash data we can use
        other_pnl = mtm.get("otherPnl", [])
        for item in other_pnl:
            if "cash" in (item.get("displayName") or "").lower() or "CASH" in (item.get("symbol") or ""):
                if item.get("mtmTotal", 0) < 0:
                    cash_negative = True

    if cash_negative:
        score += 20
        evidence.append("现金余额为负（使用 margin）")

    score = min(score, 100)
    level = "high" if score >= 60 else "medium" if score >= 40 else "low"
    return score, level, evidence


# ------------------------------------------------------------------
# Behavior finance radar
# ------------------------------------------------------------------

def _median(values: List[float]) -> float:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


def _avg(values: List[float]) -> float:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _risk_level(score: int) -> str:
    return "high" if score >= 60 else "medium" if score >= 35 else "low"


def _level_text(level: str) -> str:
    return {"high": "高", "medium": "中", "low": "低"}.get(level, "低")


def compute_behavior_radar(trades: List[dict], closed_trades: List[dict], metrics: dict, period: str = "ytd") -> dict:
    """Build a compact behavior-finance radar for the audit UI and prompt."""
    period_norm = (period or "").upper()
    all_mode = period_norm == "ALL"
    trade_dates = sorted(set(t.get("trade_date", "") for t in trades if t.get("trade_date")))
    recent_cutoff = trade_dates[-30] if len(trade_dates) >= 30 else (trade_dates[0] if trade_dates else "")
    recent_trades = [t for t in trades if recent_cutoff and t.get("trade_date", "") >= recent_cutoff]
    recent_closed = [c for c in closed_trades if recent_cutoff and c.get("close_date", "") >= recent_cutoff]

    ytd_daily = float(metrics.get("ytd_daily_avg", 0) or 0)
    recent_daily = round(len(recent_trades) / max(min(30, len(trade_dates)), 1), 2) if recent_trades else 0.0
    frequency_change_pct = round((recent_daily / ytd_daily - 1) * 100, 1) if ytd_daily else 0.0

    all_holds = [c.get("holding_days", 0) for c in closed_trades if c.get("holding_days", 0) > 0]
    recent_holds = [c.get("holding_days", 0) for c in recent_closed if c.get("holding_days", 0) > 0]
    median_hold = round(_median(all_holds), 1)
    recent_median_hold = round(_median(recent_holds), 1)
    hold_change_pct = round((recent_median_hold / median_hold - 1) * 100, 1) if median_hold else 0.0

    winners = [c.get("holding_days", 0) for c in closed_trades if c.get("realized_pnl", 0) > 0 and c.get("holding_days", 0) > 0]
    losers = [c.get("holding_days", 0) for c in closed_trades if c.get("realized_pnl", 0) < 0 and c.get("holding_days", 0) > 0]
    winner_avg = round(_avg(winners), 1)
    loser_avg = round(_avg(losers), 1)
    winner_median = round(_median(winners), 1)
    loser_median = round(_median(losers), 1)

    buy_trades = [t for t in trades if t.get("side") == "BUY"]
    fomo_results = []
    for t in buy_trades:
        fomo = fomo_score_for_buy(t)
        if fomo:
            fomo_results.append(fomo)
    high_fomo = [f for f in fomo_results if f.get("score", 0) >= 70]
    high_fomo_pct = round(len(high_fomo) / len(fomo_results) * 100, 1) if fomo_results else 0.0
    top_fomo = sorted(high_fomo, key=lambda f: f.get("score", 0), reverse=True)[:3]

    round_trips_3d = int(metrics.get("round_trips_3d", 0) or 0)
    score = 0
    alerts = []

    if not all_mode and ytd_daily and frequency_change_pct >= 70:
        score += 25
        alerts.append({"severity": "high", "text": f"最近30个交易日交易频率上升 {frequency_change_pct:.0f}%"})
    elif not all_mode and ytd_daily and frequency_change_pct >= 35:
        score += 14
        alerts.append({"severity": "medium", "text": f"最近30个交易日交易频率上升 {frequency_change_pct:.0f}%"})

    if not all_mode and median_hold and recent_median_hold and recent_median_hold <= median_hold * 0.5:
        score += 22
        alerts.append({"severity": "high", "text": f"持仓周期从 {median_hold:g} 天压缩到 {recent_median_hold:g} 天"})
    elif not all_mode and median_hold and recent_median_hold and recent_median_hold <= median_hold * 0.7:
        score += 12
        alerts.append({"severity": "medium", "text": f"持仓周期从 {median_hold:g} 天压缩到 {recent_median_hold:g} 天"})

    if winner_avg and loser_avg and loser_avg > winner_avg * 1.5:
        score += 18
        alerts.append({"severity": "medium", "text": f"亏损单平均持有 {loser_avg:g} 天，盈利单 {winner_avg:g} 天"})
    elif winner_avg and loser_avg and winner_avg > loser_avg * 2:
        score += 12
        alerts.append({"severity": "medium", "text": f"盈利单平均持有 {winner_avg:g} 天，亏损单 {loser_avg:g} 天，需核查是否过早砍亏或追涨失败"})

    if high_fomo_pct >= 25:
        score += 24
        alerts.append({"severity": "high", "text": f"高 FOMO 买入占比 {high_fomo_pct:.1f}%"})
    elif high_fomo_pct >= 12:
        score += 12
        alerts.append({"severity": "medium", "text": f"高 FOMO 买入占比 {high_fomo_pct:.1f}%"})

    if round_trips_3d >= 8:
        score += 16
        alerts.append({"severity": "high", "text": f"3日内反复交易 {round_trips_3d} 次"})
    elif round_trips_3d >= 4:
        score += 8
        alerts.append({"severity": "medium", "text": f"3日内反复交易 {round_trips_3d} 次"})

    score = min(score, 100)
    level = _risk_level(score)
    if all_mode:
        verdict = "全部区间适合看长期行为轮廓，不适合判断最近30天行为漂移。"
    elif level == "high":
        verdict = "近期行为风险高：交易正在变短、变密或更追涨。"
    elif level == "medium":
        verdict = "近期行为风险中等：有短线化或追涨苗头，需要收紧规则。"
    else:
        verdict = "近期行为风险低：没有明显行为漂移信号。"

    cards = [
        {
            "key": "frequency",
            "title": "交易频率变化",
            "value": "长期总览" if all_mode else f"{frequency_change_pct:+.0f}%",
            "detail": f"近期日均 {recent_daily:g} 次 / 全区间 {ytd_daily:g} 次" if not all_mode else f"全区间日均 {ytd_daily:g} 次，共 {len(trades)} 笔",
            "level": "low" if all_mode else _risk_level(25 if frequency_change_pct >= 70 else 40 if frequency_change_pct >= 35 else 0),
        },
        {
            "key": "holding_period",
            "title": "持仓周期变化",
            "value": "不适用" if all_mode else f"{median_hold:g}→{recent_median_hold:g}天",
            "detail": "全部区间不判断近期压缩" if all_mode else f"变化 {hold_change_pct:+.0f}%",
            "level": "low" if all_mode else _risk_level(65 if median_hold and recent_median_hold and recent_median_hold <= median_hold * 0.5 else 40 if median_hold and recent_median_hold and recent_median_hold <= median_hold * 0.7 else 0),
        },
        {
            "key": "winner_loser_hold",
            "title": "盈亏单持仓差异",
            "value": f"盈{winner_avg:g}天 / 亏{loser_avg:g}天" if winner_avg or loser_avg else "样本不足",
            "detail": f"中位数：盈{winner_median:g}天 / 亏{loser_median:g}天" if winner_median or loser_median else "缺少已平仓样本",
            "level": _risk_level(45 if winner_avg and loser_avg and (loser_avg > winner_avg * 1.5 or winner_avg > loser_avg * 2) else 0),
        },
        {
            "key": "fomo",
            "title": "追涨倾向",
            "value": f"{high_fomo_pct:.1f}%",
            "detail": f"高FOMO买入 {len(high_fomo)} / {len(fomo_results)} 笔" if fomo_results else "无可计算买入样本",
            "level": _risk_level(70 if high_fomo_pct >= 25 else 40 if high_fomo_pct >= 12 else 0),
        },
        {
            "key": "round_trips",
            "title": "短线反复交易",
            "value": f"{round_trips_3d} 次",
            "detail": "3日内同标的买卖往返",
            "level": _risk_level(65 if round_trips_3d >= 8 else 35 if round_trips_3d >= 4 else 0),
        },
        {
            "key": "overall",
            "title": "行为总评",
            "value": f"{_level_text(level)}风险",
            "detail": verdict,
            "level": level,
        },
    ]

    return {
        "period": period,
        "windowTradeDays": 0 if all_mode else min(30, len(trade_dates)),
        "recentCutoff": "" if all_mode else recent_cutoff,
        "score": score,
        "level": level,
        "verdict": verdict,
        "cards": cards,
        "alerts": alerts[:3],
        "topFomoTrades": top_fomo,
    }


def compute_setup_performance(closed_trades: List[dict]) -> List[dict]:
    """Aggregate closed-trade performance by planned setup type."""
    groups = defaultdict(list)
    for trade in closed_trades:
        setup = (trade.get("setup_type") or "").strip() or "未计划"
        groups[setup].append(trade)

    rows = []
    for setup, items in groups.items():
        pnl_values = [float(t.get("realized_pnl", 0) or 0) for t in items]
        r_values = [
            float(t.get("r_multiple"))
            for t in items
            if t.get("r_multiple") is not None
        ]
        win_count = sum(1 for value in pnl_values if value > 0)
        risk_missing_count = sum(
            1 for t in items
            if t.get("risk_status") in {None, "", "risk_missing", "invalid_risk"}
        )
        rows.append({
            "setupType": setup,
            "closedTradeCount": len(items),
            "winRate": round(win_count / len(items) * 100, 1) if items else 0,
            "realizedPnl": round(sum(pnl_values), 2),
            "avgR": round(sum(r_values) / len(r_values), 2) if r_values else None,
            "bestR": round(max(r_values), 2) if r_values else None,
            "worstR": round(min(r_values), 2) if r_values else None,
            "riskMissingCount": risk_missing_count,
        })
    rows.sort(key=lambda row: (row["realizedPnl"], row["closedTradeCount"]), reverse=True)
    return rows


def compute_exit_setup_performance(closed_trades: List[dict]) -> List[dict]:
    """Aggregate closed-trade performance by exit setup type (SELL tag)."""
    groups = defaultdict(list)
    for trade in closed_trades:
        setup = (trade.get("exit_setup_type") or "").strip() or "未标注"
        groups[setup].append(trade)

    rows = []
    for setup, items in groups.items():
        pnl_values = [float(t.get("realized_pnl", 0) or 0) for t in items]
        r_values = [
            float(t.get("r_multiple"))
            for t in items
            if t.get("r_multiple") is not None
        ]
        win_count = sum(1 for value in pnl_values if value > 0)
        risk_missing_count = sum(
            1 for t in items
            if t.get("risk_status") in {None, "", "risk_missing", "invalid_risk"}
        )
        rows.append({
            "setupType": setup,
            "closedTradeCount": len(items),
            "winRate": round(win_count / len(items) * 100, 1) if items else 0,
            "realizedPnl": round(sum(pnl_values), 2),
            "avgR": round(sum(r_values) / len(r_values), 2) if r_values else None,
            "bestR": round(max(r_values), 2) if r_values else None,
            "worstR": round(min(r_values), 2) if r_values else None,
            "riskMissingCount": risk_missing_count,
        })
    rows.sort(key=lambda row: (row["realizedPnl"], row["closedTradeCount"]), reverse=True)
    return rows


def _close_series_after_exit(symbol: str, exit_date: str) -> Optional[dict]:
    rows = get_kline(symbol)
    if not rows:
        return None
    by_date = {str(row.get("time", ""))[:10].replace("-", ""): row for row in rows}
    dates = sorted(by_date.keys())
    if exit_date not in dates:
        return None
    idx = dates.index(exit_date)
    try:
        exit_close = float(by_date[exit_date]["close"])
    except Exception:
        return None
    closes = []
    for offset in (5, 10, 20):
        target_idx = idx + offset
        if target_idx < len(dates):
            try:
                closes.append((offset, dates[target_idx], float(by_date[dates[target_idx]]["close"])))
            except Exception:
                pass
    if not closes:
        return None
    ma20 = None
    if idx >= 19:
        try:
            ma20 = sum(float(by_date[d]["close"]) for d in dates[idx - 19:idx + 1]) / 20
        except Exception:
            ma20 = None
    result = {
        "exitClose": exit_close,
        "exitAboveMA20": bool(ma20 and exit_close >= ma20),
    }
    for offset, day, close in closes:
        result[f"close{offset}d"] = close
        result[f"day{offset}d"] = day
        result[f"return{offset}dPct"] = round((close - exit_close) / exit_close * 100, 2) if exit_close else None
    return result


def compute_exit_quality(closed_trades: List[dict]) -> dict:
    """Evaluate post-exit 5/10/20 trading-day performance."""
    samples = []
    for trade in closed_trades:
        series = _close_series_after_exit(trade.get("symbol", ""), trade.get("close_date", ""))
        if not series:
            continue
        quantity = float(trade.get("quantity", 0) or 0)
        item = {
            "symbol": trade.get("symbol", ""),
            "closeDate": trade.get("close_date", ""),
            "quantity": quantity,
            "realizedPnl": round(float(trade.get("realized_pnl", 0) or 0), 2),
            "entrySource": trade.get("entry_source", ""),
            "setupType": trade.get("setup_type") or "未计划",
            **series,
        }
        ret20 = item.get("return20dPct")
        close20 = item.get("close20d")
        exit_close = item.get("exitClose")
        item["missedPnl20d"] = round((close20 - exit_close) * quantity, 2) if close20 and exit_close else None
        item["trendUnbrokenExit"] = bool(item.get("exitAboveMA20") and ret20 is not None and ret20 > 5)
        samples.append(item)

    def avg_key(key: str) -> Optional[float]:
        values = [float(s[key]) for s in samples if s.get(key) is not None]
        return round(sum(values) / len(values), 2) if values else None

    sell_too_early = [
        s for s in samples
        if s.get("return20dPct") is not None and s.get("return20dPct") > 10
    ]
    trend_unbroken = [s for s in samples if s.get("trendUnbrokenExit")]
    top_missed = sorted(
        [s for s in samples if s.get("missedPnl20d") is not None],
        key=lambda s: s.get("missedPnl20d", 0),
        reverse=True,
    )[:8]
    return {
        "evaluatedCount": len(samples),
        "avgPostExit5dPct": avg_key("return5dPct"),
        "avgPostExit10dPct": avg_key("return10dPct"),
        "avgPostExit20dPct": avg_key("return20dPct"),
        "sellTooEarlyCount20d": len(sell_too_early),
        "trendUnbrokenExitCount": len(trend_unbroken),
        "topMissed20d": top_missed,
    }


def compute_discipline_summary(metrics: dict, behavior_radar: dict, setup_performance: List[dict], exit_quality: dict) -> dict:
    """Create a compact discipline checklist for AI and UI."""
    issues = []

    risk_missing = int(metrics.get("risk_missing_count", 0) or 0)
    if risk_missing:
        issues.append({
            "severity": "high",
            "title": "初始风险缺失",
            "evidence": f"{risk_missing} 笔已平仓交易缺少无效价 / 初始风险，R 倍数无法计算。",
            "rule": "没有无效价和初始风险的交易，不计入有效逻辑。"
        })

    for alert in (behavior_radar.get("alerts") or [])[:2]:
        issues.append({
            "severity": alert.get("severity", "medium"),
            "title": "行为漂移",
            "evidence": alert.get("text", ""),
            "rule": "触发行为漂移时，停止新增同类交易，先复盘最近一笔。"
        })

    unplanned = next((row for row in setup_performance if row.get("setupType") == "未计划"), None)
    if unplanned and unplanned.get("closedTradeCount", 0):
        issues.append({
            "severity": "medium",
            "title": "缺少事前计划",
            "evidence": f"{unplanned.get('closedTradeCount', 0)} 笔已平仓交易没有买卖逻辑标签，盈亏 {unplanned.get('realizedPnl', 0)}。",
            "rule": "下单前必须填写买卖逻辑、无效价、目标持有期和退出条件。"
        })

    sell_too_early = int(exit_quality.get("sellTooEarlyCount20d", 0) or 0)
    trend_unbroken = int(exit_quality.get("trendUnbrokenExitCount", 0) or 0)
    if sell_too_early or trend_unbroken:
        issues.append({
            "severity": "medium",
            "title": "退出纪律需要复核",
            "evidence": f"20日后继续涨超10%的卖出 {sell_too_early} 笔；趋势未破退出 {trend_unbroken} 笔。",
            "rule": "趋势未破时，减仓要写明触发条件，禁止因为短期波动随手卖。"
        })

    deduped = []
    seen = set()
    for issue in issues:
        key = issue["title"]
        if key not in seen:
            deduped.append(issue)
            seen.add(key)

    hard_rules = [
        "没有买卖逻辑、无效价、初始风险的交易，不计入有效交易。",
        "FOMO 或交易频率报警后，下一笔同类交易必须延后一个交易日。",
        "卖出后 20 日表现显著强于卖出价时，复盘退出触发条件，而不是归因于运气。",
        "亏损状态下加仓必须证明原计划未失效，否则视为纪律违约。",
        "每周只复盘规则执行，不把复盘写成市场预测。",
    ]
    return {
        "topIssues": deduped[:3],
        "hardRules": hard_rules,
    }


# ------------------------------------------------------------------
# Aggregate + alerts
# ------------------------------------------------------------------

def build_alerts(scores: dict, evidence_map: dict) -> List[dict]:
    alerts = []
    severity_map = {
        "theme_judgment": {"high": "high", "medium": "medium", "low": "low"},
        "churn_friction": {"high": "high", "medium": "medium", "low": "low"},
        "stock_selection": {"high": "high", "medium": "medium", "low": "low"},
        "narrative_hype": {"high": "high", "medium": "medium", "low": "low"},
        "entry_quality": {"high": "high", "medium": "medium", "low": "low"},
        "asymmetric_exit": {"high": "medium", "medium": "low", "low": "low"},
        "tail_risk": {"high": "high", "medium": "high", "low": "medium"},
    }
    for name, data in scores.items():
        level = data.get("level", "low")
        sev = severity_map.get(name, {}).get(level, "low")
        evs = evidence_map.get(name, [])
        if evs:
            alerts.append({
                "type": name,
                "severity": sev,
                "score": data["score"],
                "detail": evs[0] if evs else "",
                "all_evidence": evs,
            })
    # Sort by severity
    severity_order = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda a: severity_order.get(a["severity"], 3))
    return alerts


def compute_position_rules_check(trades: List[dict], closed_trades: List[dict], open_positions: List[dict], theme_attribution: List[dict]) -> dict:
    """Check hard position-sizing and exploration rules.

    Rules:
      1. Core (beta tier) >= 80% of position value; exploration (optionality) <= 20%.
      2. Any single exploration symbol loss > 1% of account NAV -> halt trading for 7 days.
      3. > 5 trades on same symbol in a calendar week -> emotional trading.
    """
    alerts = []

    # Load account snapshot for NAV
    snapshot = read_json(STATEMENT_SNAPSHOT_FILE, {})
    account = snapshot.get("accountSnapshot", {})
    nav = account.get("endingValue") or account.get("startingValue") or 0.0

    # Symbol -> tier mapping
    sym_to_tier: Dict[str, str] = {}
    for t in theme_attribution:
        tier = t.get("tier", "optionality")
        for s in t.get("symbols", []):
            sym_to_tier[s] = tier

    # Rule 1: core vs exploration sizing
    total_pos_value = 0.0
    optionality_value = 0.0
    for pos in open_positions:
        sym = pos.get("symbol", "")
        qty = pos.get("quantity", 0) or 0
        avg_cost = pos.get("avg_cost", 0) or 0
        unrealized = pos.get("unrealized_pnl") or 0
        value = qty * avg_cost + unrealized
        total_pos_value += value
        tier = sym_to_tier.get(sym, "optionality")
        if tier == "optionality":
            optionality_value += value

    optionality_pct = (optionality_value / total_pos_value) if total_pos_value > 0 else 0.0
    if total_pos_value > 0:
        if optionality_pct > 0.20:
            alerts.append({
                "rule": "仓位分配",
                "severity": "high",
                "title": "探索仓位超限",
                "evidence": (
                    f"探索仓（optionality）占持仓 {optionality_pct * 100:.1f}%，"
                    f"超过20%上限。主线资产 {(1 - optionality_pct) * 100:.1f}%。"
                ),
            })
        elif optionality_pct > 0.18:
            alerts.append({
                "rule": "仓位分配",
                "severity": "medium",
                "title": "探索仓位接近上限",
                "evidence": f"探索仓占持仓 {optionality_pct * 100:.1f}%，接近20%上限。",
            })

    # Rule 2: single exploration symbol loss > 1% NAV -> halt 7 days
    if nav > 0:
        symbol_realized_loss: Dict[str, float] = defaultdict(float)
        for ct in closed_trades:
            pnl = ct.get("realized_pnl", 0) or 0
            if pnl < 0:
                symbol_realized_loss[ct.get("symbol", "")] += abs(pnl)

        symbol_unrealized_loss: Dict[str, float] = {}
        for pos in open_positions:
            sym = pos.get("symbol", "")
            unrealized = pos.get("unrealized_pnl")
            if unrealized is not None and unrealized < 0:
                symbol_unrealized_loss[sym] = abs(unrealized)

        checked_syms = set()
        for sym in set(list(symbol_realized_loss.keys()) + list(symbol_unrealized_loss.keys())):
            if sym in checked_syms:
                continue
            checked_syms.add(sym)
            if sym_to_tier.get(sym, "optionality") != "optionality":
                continue
            loss = symbol_realized_loss.get(sym, 0.0) + symbol_unrealized_loss.get(sym, 0.0)
            if loss / nav > 0.01:
                # Find most recent trade date for this symbol
                last_date = None
                for t in trades:
                    if t.get("symbol_lb") == sym or t.get("symbol") == sym:
                        d = t.get("trade_date", "")
                        if d and (last_date is None or d > last_date):
                            last_date = d
                if last_date:
                    try:
                        last_dt = parse_date(last_date)
                        days_since = (datetime.now() - last_dt).days
                        if days_since < 7:
                            alerts.append({
                                "rule": "探索标的止损",
                                "severity": "high",
                                "title": f"{sym} 亏损超1%且仍在交易",
                                "evidence": (
                                    f"{sym} 累计亏损 {loss:,.0f}（占账户 {loss / nav * 100:.2f}%），"
                                    f"规则要求强制停止交易7天。最近交易在 {days_since} 天前。"
                                ),
                            })
                        else:
                            alerts.append({
                                "rule": "探索标的止损",
                                "severity": "medium",
                                "title": f"{sym} 探索标的亏损超1%",
                                "evidence": (
                                    f"{sym} 累计亏损 {loss:,.0f}（占账户 {loss / nav * 100:.2f}%），"
                                    f"最近交易在 {days_since} 天前（已超7天停赛期）。"
                                ),
                            })
                    except Exception:
                        alerts.append({
                            "rule": "探索标的止损",
                            "severity": "high",
                            "title": f"{sym} 探索标的亏损超1%",
                            "evidence": (
                                f"{sym} 累计亏损 {loss:,.0f}（占账户 {loss / nav * 100:.2f}%），"
                                f"规则要求强制停止交易7天。"
                            ),
                        })
                else:
                    alerts.append({
                        "rule": "探索标的止损",
                        "severity": "high",
                        "title": f"{sym} 探索标的亏损超1%",
                        "evidence": (
                            f"{sym} 累计亏损 {loss:,.0f}（占账户 {loss / nav * 100:.2f}%），"
                            f"规则要求强制停止交易7天。"
                        ),
                    })

    # Rule 3: > 5 trades on same symbol in a calendar week -> emotional trading
    symbol_week_trades: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for t in trades:
        sym = t.get("symbol_lb") or t.get("symbol", "")
        d = t.get("trade_date", "")
        if not d:
            continue
        try:
            dt = parse_date(d)
            week_key = f"{dt.year}-W{dt.isocalendar()[1]:02d}"
            symbol_week_trades[(sym, week_key)].append(d)
        except Exception:
            continue

    emotion_entries = []
    for (sym, week_key), dates in symbol_week_trades.items():
        if len(dates) > 5:
            emotion_entries.append((sym, week_key, len(dates)))
            alerts.append({
                "rule": "情绪交易",
                "severity": "high",
                "title": f"{sym} {week_key} 情绪交易",
                "evidence": f"{sym} 在 {week_key} 交易 {len(dates)} 笔，超过5笔/周上限，自动标记为情绪交易。",
            })

    return {
        "alerts": alerts,
        "optionality_pct": round(optionality_pct, 4),
        "emotion_symbols": [{"symbol": s, "week": w, "count": c} for s, w, c in emotion_entries],
    }


def run_scoring() -> dict:
    """Main entry: compute all six scores and save outputs."""
    config = load_config()
    trades = read_json(TRADES_FILE, [])
    closed_trades = read_json(CLOSED_TRADES_FILE, [])
    open_positions = read_json(OPEN_POSITIONS_FILE, [])
    theme_attribution = read_json(THEME_ATTRIBUTION_FILE, [])
    audit_coverage = read_json(AUDIT_COVERAGE_FILE, {})
    mtm_state = read_json(MTM_FILE, {})

    if not closed_trades:
        return {"error": "No closed trades. Run 'python app/cli.py match' first."}

    period = "ytd"
    metrics = compute_period_metrics(trades, closed_trades)

    s1, l1, e1 = score_theme_judgment(theme_attribution, config)
    s2, l2, e2 = score_churn_friction(trades, closed_trades, metrics, config)
    s3, l3, e3 = score_stock_selection(theme_attribution, config)
    s4, l4, e4 = score_narrative_hype(trades, closed_trades, theme_attribution, config)
    s5, l5, e5 = score_entry_quality(trades, closed_trades, theme_attribution, config)
    s6, l6, e6 = score_asymmetric_exit(closed_trades, trades, config)
    s7, l7, e7 = score_tail_risk(open_positions, trades, config)

    scores = {
        "theme_judgment": {"score": s1, "level": l1},
        "churn_friction": {"score": s2, "level": l2},
        "stock_selection": {"score": s3, "level": l3},
        "narrative_hype": {"score": s4, "level": l4},
        "entry_quality": {"score": s5, "level": l5},
        "asymmetric_exit": {"score": s6, "level": l6},
        "tail_risk": {"score": s7, "level": l7},
    }

    evidence_map = {
        "theme_judgment": e1,
        "churn_friction": e2,
        "stock_selection": e3,
        "narrative_hype": e4,
        "entry_quality": e5,
        "asymmetric_exit": e6,
        "tail_risk": e7,
    }

    alerts = build_alerts(scores, evidence_map)
    behavior_radar = compute_behavior_radar(trades, closed_trades, metrics, period)
    setup_performance = compute_setup_performance(closed_trades)
    exit_setup_performance = compute_exit_setup_performance(closed_trades)
    exit_quality = compute_exit_quality(closed_trades)
    discipline_summary = compute_discipline_summary(metrics, behavior_radar, setup_performance, exit_quality)

    # The seven public dimensions retain their original relative proportions;
    # the normalized weights sum to 100%.
    style_drift = weighted_style_drift({
        "theme_judgment": s1,
        "churn_friction": s2,
        "stock_selection": s3,
        "narrative_hype": s4,
        "entry_quality": s5,
        "asymmetric_exit": s6,
        "tail_risk": s7,
    })

    # Build enriched MTM rankings (winners / losers) with theme & behavior tags
    mtm_rankings = _build_mtm_rankings(mtm_state, theme_attribution, evidence_map)

    # Hard position-sizing and exploration rules
    position_rules = compute_position_rules_check(trades, closed_trades, open_positions, theme_attribution)

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": "ytd",
        "scores": scores,
        "style_drift": {"score": style_drift, "level": "high" if style_drift >= 60 else "medium" if style_drift >= 40 else "low"},
        "metrics": metrics,
        "behavior_radar": behavior_radar,
        "setup_performance": setup_performance,
        "exit_setup_performance": exit_setup_performance,
        "exit_quality": exit_quality,
        "discipline_summary": discipline_summary,
        "alerts": alerts,
        "evidence": evidence_map,
        "theme_attribution": theme_attribution,
        "audit_coverage": audit_coverage,
        "ytd_mtm_contribution": mtm_state.get("stocks", {}) if isinstance(mtm_state, dict) else {},
        "mtm_rankings": mtm_rankings,
        "position_rules": position_rules,
    }

    write_json(AUDIT_SCORES_FILE, scores)
    write_json(AUDIT_SUMMARY_FILE, summary)

    return summary


def _build_mtm_rankings(mtm_state: dict, theme_attribution: List[dict], evidence_map: dict) -> dict:
    """Build top winners/losers with theme, % of total, and behavior factor tags."""
    import re

    stocks = mtm_state.get("stocks", {}) if isinstance(mtm_state, dict) else {}
    total_abs_mtm = sum(
        abs(v.get("mtmTotal", 0)) if isinstance(v, dict) else abs(float(v or 0))
        for v in stocks.values()
    )

    # Symbol -> theme / tier mapping
    sym_to_theme: Dict[str, str] = {}
    sym_to_tier: Dict[str, str] = {}
    theme_alpha: Dict[str, dict] = {}
    for t in theme_attribution:
        theme_name = t.get("theme", "unclassified")
        tier = t.get("tier", "optionality")
        theme_alpha[theme_name] = {
            "trader": t.get("avg_trader_return_pct"),
            "theme": t.get("avg_theme_return_pct"),
            "selection": t.get("avg_selection_alpha_pct"),
            "timing": t.get("avg_timing_alpha_pct"),
            "verdict": t.get("verdict", ""),
        }
        for s in t.get("symbols", []):
            sym_to_theme[s] = theme_name
            sym_to_tier[s] = tier

    # Extract symbol-level behavior tags from evidence
    behavior_map: Dict[str, List[str]] = {}
    # Entry quality: lines like "SYMBOL DATE: FOMO ..."
    for ev in evidence_map.get("entry_quality", []):
        m = re.match(r'^([A-Z][A-Z0-9]*\.[A-Z][A-Z0-9]*)\s', ev)
        if m:
            sym = m.group(1)
            tags = behavior_map.setdefault(sym, [])
            if "FOMO" in ev and "追高风险" not in tags:
                tags.append("追高风险")
            if "回撤" in ev and "入场回撤" not in tags:
                tags.append("入场回撤")

    # Asymmetric exit: sell-fly symbol-level detail (we don't have it in evidence strings,
    # but we can infer from theme verdict for now)
    # Theme judgment: add theme-level tags
    for ev in evidence_map.get("theme_judgment", []):
        if "选股优秀" in ev:
            # Try to find which theme — evidence format is like "theme_name: ..."
            m = re.match(r'^([a-z_]+):', ev)
            if m:
                th = m.group(1)
                for sym, theme in sym_to_theme.items():
                    if theme == th and "选股优秀" not in behavior_map.get(sym, []):
                        behavior_map.setdefault(sym, []).append("选股优秀")

    # Build items
    items = []
    for k, v in stocks.items():
        if not isinstance(v, dict):
            continue
        mtm = v.get("mtmTotal", 0)
        if abs(mtm) < 0.01:
            continue
        sym = v.get("symbol", k)
        pct = (mtm / total_abs_mtm * 100) if total_abs_mtm != 0 else 0
        theme = sym_to_theme.get(sym, "unclassified")
        tier = sym_to_tier.get(sym, "optionality")
        tags = behavior_map.get(sym, [])

        # Derive tags from theme alpha when no direct evidence
        alpha = theme_alpha.get(theme, {})
        if mtm < 0:
            sel = alpha.get("selection")
            tim = alpha.get("timing")
            if sel is not None and sel < -0.03 and "选股差于ETF" not in tags:
                tags.append("选股差于ETF")
            if tim is not None and tim < -0.02 and "择时负贡献" not in tags:
                tags.append("择时负贡献")
        else:
            sel = alpha.get("selection")
            tim = alpha.get("timing")
            if sel is not None and sel > 0.03 and "选股优秀" not in tags:
                tags.append("选股优秀")
            if tim is not None and tim > 0.02 and "择时正贡献" not in tags:
                tags.append("择时正贡献")

        items.append({
            "symbol": sym,
            "displayName": v.get("displayName", sym),
            "mtmTotal": round(mtm, 2),
            "pct_of_total": round(pct, 2),
            "theme": theme,
            "tier": tier,
            "behavior_factors": tags,
            "theme_alpha": alpha,
        })

    winners = sorted([i for i in items if i["mtmTotal"] > 0], key=lambda x: x["mtmTotal"], reverse=True)[:10]
    losers = sorted([i for i in items if i["mtmTotal"] < 0], key=lambda x: x["mtmTotal"])[:10]
    return {"winners": winners, "losers": losers}


if __name__ == "__main__":
    result = run_scoring()
    print(json.dumps(result, ensure_ascii=False, indent=2))
