#!/usr/bin/env python3
"""Shared FOMO-entry scoring.

The 0–100 FOMO score for a single entry was implemented twice (scoring_engine
for BUY trades, setup_classifier for closed-trade classification) with
identical rules but divergent copies — a bug-fix-in-two-places hazard. This is
the single source of truth. Callers pass in the K-lines (so this module stays
free of any cache/disk dependency) and pick whatever fields they need.
"""

from typing import Dict, List, Optional


def _by_date(rows: List[dict]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for row in rows:
        day = str(row.get("time", ""))[:10].replace("-", "")
        out[day] = row
    return out


def compute_fomo_score(rows: List[dict], entry_date: str, entry_price: float) -> Optional[dict]:
    """Score how "FOMO-like" an entry was. Returns None when data is insufficient.

    ``entry_date`` may be ``YYYYMMDD`` or ``YYYY-MM-DD``. The result dict carries
    ``score`` (0-100), English ``reasons``, and ``ret5``/``ret10``/``ma20``.
    """
    if not rows:
        return None

    by_date = _by_date(rows)
    dates = sorted(by_date.keys())
    target = str(entry_date).replace("-", "")
    try:
        entry_idx = dates.index(target)
    except ValueError:
        return None

    if entry_idx < 20:
        return None

    closes = []
    for d in dates[:entry_idx + 1]:
        try:
            closes.append(float(by_date[d]["close"]))
        except Exception:
            closes.append(0)

    if len(closes) < 20:
        return None

    score = 0
    reasons: List[str] = []

    # 1. 5-day return before buy
    ret5 = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 and closes[-6] > 0 else 0
    if ret5 > 0.15:
        score += 20
        reasons.append(f"5-day gain {ret5:.1%}")
    elif ret5 > 0.10:
        score += 10
        reasons.append(f"5-day gain {ret5:.1%}")

    # 2. Price above MA20
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else closes[-1]
    if ma20 > 0 and entry_price > ma20 * 1.08:
        score += 15
        reasons.append(f"Above MA20 by {(entry_price / ma20 - 1):.1%}")
    elif ma20 > 0 and entry_price > ma20 * 1.04:
        score += 8
        reasons.append(f"Above MA20 by {(entry_price / ma20 - 1):.1%}")

    # 3. 10-day return before buy
    ret10 = (closes[-1] - closes[-11]) / closes[-11] if len(closes) >= 11 and closes[-11] > 0 else 0
    if ret10 > 0.25:
        score += 10
        reasons.append(f"10-day gain {ret10:.1%}")

    # 4. Entry day gap vs prior close
    prior_close = closes[-2] if len(closes) >= 2 else closes[-1]
    if prior_close > 0:
        day_gap = (entry_price - prior_close) / prior_close
        if day_gap > 0.05:
            score += 10
            reasons.append(f"Same-day gap {day_gap:.1%}")
        elif day_gap > 0.03:
            score += 5
            reasons.append(f"Same-day gain {day_gap:.1%}")

    # 5. Near 20-day high
    high20 = max(closes[-20:]) if len(closes) >= 20 else closes[-1]
    if high20 > 0 and entry_price >= high20 * 0.97:
        score += 10
        reasons.append("Near the 20-day high")

    # 6. Volume spike (if turnover available)
    try:
        entry_row = by_date[dates[entry_idx]]
        vol = float(entry_row.get("turnover", 0))
        avg_vol = sum(float(by_date[d].get("turnover", 0)) for d in dates[-20:entry_idx + 1]) / 20
        if avg_vol > 0 and vol > avg_vol * 2.5:
            score += 10
            reasons.append(f"Volume {(vol / avg_vol):.1f}x the 20-day average")
        elif avg_vol > 0 and vol > avg_vol * 1.5:
            score += 5
            reasons.append(f"Volume expansion {(vol / avg_vol):.1f}x")
    except Exception:
        pass

    return {
        "score": min(score, 100),
        "reasons": reasons,
        "ret5": ret5,
        "ret10": ret10,
        "ma20": ma20,
    }


def fomo_level(score: int) -> str:
    if score <= 30:
        return "low"
    if score <= 60:
        return "moderate"
    if score <= 80:
        return "high"
    return "extreme"
