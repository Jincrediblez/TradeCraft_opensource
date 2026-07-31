#!/usr/bin/env python3
"""Phase 4: shared FOMO scoring (app.fomo)."""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.fomo import compute_fomo_score, fomo_level


def _rows(closes, vols=None, start=date(2026, 1, 1)):
    out = []
    for i, c in enumerate(closes):
        d = (start + timedelta(days=i)).isoformat()
        out.append({"time": d, "close": c, "high": c, "low": c,
                    "turnover": (vols[i] if vols else 1_000_000)})
    return out


def test_returns_none_without_enough_history():
    closes = [100.0] * 10  # < 20 days before entry
    rows = _rows(closes)
    assert compute_fomo_score(rows, rows[-1]["time"], 100.0) is None


def test_returns_none_when_entry_date_absent():
    rows = _rows([100.0] * 30)
    assert compute_fomo_score(rows, "2099-01-01", 100.0) is None


def test_high_score_on_parabolic_entry():
    closes = [round(100 * (1.03 ** i), 2) for i in range(30)]
    vols = [1_000_000] * 30
    vols[-1] = 5_000_000  # volume spike on entry day
    rows = _rows(closes, vols)
    result = compute_fomo_score(rows, rows[-1]["time"], closes[-1])
    assert result is not None
    assert result["score"] >= 60
    assert any("涨幅" in r for r in result["reasons"])


def test_low_score_on_flat_entry():
    # Flat price still sits AT its 20-day high (+10), but nothing else fires.
    closes = [100.0] * 30
    rows = _rows(closes)
    result = compute_fomo_score(rows, rows[-1]["time"], 100.0)
    assert result is not None
    assert result["score"] == 10
    assert fomo_level(result["score"]) == "low"
    assert result["reasons"] == ["接近20日高点"]


def test_date_format_flexibility():
    closes = [round(100 * (1.02 ** i), 2) for i in range(30)]
    rows = _rows(closes)
    iso = rows[-1]["time"]
    compact = iso.replace("-", "")
    assert compute_fomo_score(rows, iso, closes[-1])["score"] == \
        compute_fomo_score(rows, compact, closes[-1])["score"]


def test_fomo_level_buckets():
    assert fomo_level(0) == "low"
    assert fomo_level(30) == "low"
    assert fomo_level(45) == "moderate"
    assert fomo_level(70) == "high"
    assert fomo_level(95) == "extreme"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
